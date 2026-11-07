import os
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import events, legacy
from darkroom.storage.models import LegacyFile, MediaItem, Person
from tests.import_world import NOW, World, sha1_of, tree

MOMENT = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}


class Recorder:
    def __init__(self, accept=True):
        self.accept = accept
        self.offered = []

    def offer(self, opened, person, original, now):
        self.offered.append((person.slug, original.path))
        return self.accept


def listing(root):
    return sorted((str(p.relative_to(root)), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size, sha1_of(p)) for p in Path(root).rglob("*") if p.is_file())


class LegacyWorld:
    def __init__(self, test):
        self.test = test
        self.world = World(test)
        self.source = self.world.base / "old-sync" / "jo"
        self.source.mkdir(parents=True)
        self.jo = self.world.opened.get(Person, self.world.ids["jo"])
        self.sam = self.world.opened.get(Person, self.world.ids["sam"])

    def imported(self):
        export = self.world.downloaded()
        self.world.run_import(export)
        return export

    def takeout_bytes(self):
        item = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.person_id == self.jo.id).order_by(MediaItem.id)).scalars().first()
        return (self.world.root / item.path).read_bytes()

    def put(self, name, data):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def configure(self):
        legacy.set_source(self.world.opened, self.world.photos, self.jo, str(self.source), NOW)
        self.world.opened.commit()

    def run(self, queue=None, at=NOW):
        return legacy.import_person(self.world.opened, self.world.settings, self.jo, at, queue=queue)

    def rows(self, slug="jo"):
        person = self.world.ids[slug]
        return {row.path: row for row in self.world.opened.execute(select(LegacyFile).where(LegacyFile.person_id == person)).scalars()}


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.home = LegacyWorld(self)
        self.world = self.home.world
        self.home.imported()
        self.moment_copy = self.world.scratch_jpeg("again.jpg", colour=(11, 119, 31), **MOMENT)
        self.fresh = self.world.scratch_jpeg("fresh.jpg", taken="2015:01:01 10:00:00", offset="+00:00", colour=(1, 2, 3))
        self.unconfirmed = (self.world.root / "jo/originals/IMG_3001.JPG").read_bytes()
        self.confirmed = (self.world.root / "jo/originals/IMG_1001.JPG").read_bytes()
        self.others = (self.world.root / "sam/originals/2019/IMG_1002.JPG").read_bytes()
        self.home.put("google-only-copy.jpg", self.home.takeout_bytes())
        self.home.put("2019/confirmed-copy.jpg", self.confirmed)
        self.home.put("reencoded.jpg", self.moment_copy)
        self.home.put("not-backed-up.jpg", self.unconfirmed)
        self.home.put("fresh.jpg", self.fresh)
        self.home.put("sams-photo.jpg", self.others)
        self.home.put("notes.txt", b"not a photo")
        self.home.configure()

    def test_each_file_is_matched_in_the_persons_takeout_or_not(self):
        report = self.home.run()
        rows = self.home.rows()
        self.assertEqual({path: (row.status, row.tier) for path, row in rows.items()}, {
            "google-only-copy.jpg": ("matched", 1), "2019/confirmed-copy.jpg": ("matched", 1), "reencoded.jpg": ("matched", 2),
            "not-backed-up.jpg": ("unmatched", 5), "fresh.jpg": ("unmatched", 5), "sams-photo.jpg": ("unmatched", 5),
        })
        self.assertEqual((report.new, report.matched, report.unmatched, report.ignored, report.failed), (6, 3, 3, 1, 0))
        self.assertEqual((report.copied, report.already_there), (2, 1))

    def test_unmatched_files_are_copied_into_originals_once_and_matched_ones_are_not(self):
        before = tree(self.world.root / "jo" / "originals")
        self.home.run()
        after = tree(self.world.root / "jo" / "originals")
        added = {path: sha for path, sha in after.items() if path not in before}
        self.assertEqual({sha for sha in added.values()}, {legacy_sha(self.fresh), legacy_sha(self.others)})
        self.assertTrue(all(path.startswith("legacy/") for path in added))
        self.assertEqual({path: sha for path, sha in after.items() if path in before}, before)
        rows = self.home.rows()
        existing = self.world.opened.execute(select(MediaItem).where(MediaItem.path == "jo/originals/IMG_3001.JPG")).scalar_one()
        self.assertEqual(rows["not-backed-up.jpg"].original_item_id, existing.id)
        self.assertIsNone(rows["reencoded.jpg"].original_item_id)

    def test_a_copy_is_indexed_as_an_original_of_that_person(self):
        self.home.run()
        item = self.world.opened.get(MediaItem, self.home.rows()["fresh.jpg"].original_item_id)
        self.assertEqual((item.person_id, item.source, item.sha1, item.confirmed_in_google), (self.home.jo.id, "phone", legacy_sha(self.fresh), False))
        self.assertTrue(item.path.startswith("jo/originals/legacy/"))

    def test_another_persons_takeout_never_counts_and_nothing_lands_in_their_folder(self):
        sam_before = tree(self.world.root / "sam")
        self.home.run()
        self.assertEqual(self.home.rows()["sams-photo.jpg"].status, "unmatched")
        self.assertEqual(tree(self.world.root / "sam"), sam_before)
        self.assertEqual(self.home.rows("sam"), {})

    def test_the_source_folder_is_never_changed(self):
        before = listing(self.home.source)
        self.home.run()
        self.home.run()
        self.assertEqual(listing(self.home.source), before)
        self.assertEqual({p.name for p in self.home.source.iterdir()}, {"2019", "google-only-copy.jpg", "reencoded.jpg", "not-backed-up.jpg", "fresh.jpg", "sams-photo.jpg", "notes.txt"})

    def test_a_second_run_changes_nothing_for_files_already_seen(self):
        self.home.run()
        state = (self.world.state(), listing(self.world.root / "jo"), sorted((r.path, r.status, r.tier, r.seen_at) for r in self.home.rows().values()))
        report = self.home.run(at=NOW + 5)
        self.assertEqual((report.new, report.matched, report.unmatched, report.copied, report.failed, report.already_seen), (0, 0, 0, 0, 0, 6))
        self.assertEqual((self.world.state(), listing(self.world.root / "jo"), sorted((r.path, r.status, r.tier, r.seen_at) for r in self.home.rows().values())), state)

    def test_a_new_file_and_a_changed_one_are_handled_on_a_later_run_and_the_earlier_copy_stays(self):
        self.home.run()
        self.home.put("later.jpg", self.world.scratch_jpeg("later.jpg", taken="2014:02:02 10:00:00", offset="+00:00", colour=(9, 9, 9)))
        self.home.put("fresh.jpg", self.fresh + b"x")
        report = self.home.run(at=NOW + 5)
        self.assertEqual((report.new, report.unmatched, report.copied, report.changed), (2, 2, 2, 1))
        self.assertEqual(self.home.rows()["fresh.jpg"].sha1, legacy_sha(self.fresh + b"x"))
        self.assertEqual(sha1_of(self.world.root / "jo/originals/legacy" / legacy.copy_name(self.home.source / "fresh.jpg", legacy_sha(self.fresh))), legacy_sha(self.fresh))

    def test_a_crash_after_the_copy_is_picked_up_without_a_second_copy(self):
        original = legacy.bring_in
        calls = []

        def crashing(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(result[0].path)
            raise OSError("disk went away")

        legacy.bring_in = crashing
        self.addCleanup(setattr, legacy, "bring_in", original)
        report = self.home.run()
        self.assertEqual(report.failed, 3)
        self.assertEqual({path: row.status for path, row in self.home.rows().items()}, {
            "google-only-copy.jpg": "matched", "2019/confirmed-copy.jpg": "matched", "reencoded.jpg": "matched",
            "not-backed-up.jpg": "failed", "fresh.jpg": "failed", "sams-photo.jpg": "failed",
        })
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.files, found.matched, found.unmatched, found.failed), (6, 3, 0, 3))
        self.assertEqual(legacy.describe(found), "jo: 6 legacy files, 3 matched in Google, 3 not backed up (3 could not be imported yet, 0 sent to the Pixel folder, 0 waiting for it)")
        legacy.bring_in = original
        copies = sorted((self.world.root / "jo/originals/legacy").iterdir())
        done = self.home.run(at=NOW + 5)
        self.assertEqual((done.new, done.failed), (3, 0))
        self.assertEqual({row.status for row in self.home.rows().values()}, {"matched", "unmatched"})
        self.assertEqual(sorted((self.world.root / "jo/originals/legacy").iterdir()), copies)

    def test_unmatched_originals_are_offered_to_the_pixel_queue_and_retried_until_taken(self):
        refusing = Recorder(accept=False)
        report = self.home.run(refusing)
        self.assertEqual((report.queued, len(refusing.offered)), (0, 3))
        self.assertTrue(all(row.queued_at is None for row in self.home.rows().values()))
        taking = Recorder()
        again = self.home.run(taking, at=NOW + 5)
        self.assertEqual((again.new, again.queued, len(taking.offered)), (0, 3, 3))
        self.assertEqual({path for path, row in self.home.rows().items() if row.queued_at is not None}, {"not-backed-up.jpg", "fresh.jpg", "sams-photo.jpg"})
        last = Recorder()
        self.home.run(last, at=NOW + 9)
        self.assertEqual(last.offered, [])

    def test_matched_files_are_never_offered_to_the_pixel_queue(self):
        taking = Recorder()
        self.home.run(taking)
        self.assertEqual({path for _, path in taking.offered if "legacy" not in path and "IMG_3001" not in path}, set())
        self.assertEqual(len(taking.offered), 3)

    def test_the_totals_give_the_unmatched_count_per_person(self):
        self.home.run()
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}
        self.assertEqual((found["jo"].files, found["jo"].matched, found["jo"].unmatched, found["jo"].queued), (6, 3, 3, 0))
        self.assertEqual((found["sam"].configured, found["sam"].files), (False, 0))
        self.assertEqual(legacy.describe(found["jo"]), "jo: 6 legacy files, 3 matched in Google, 3 not backed up (0 could not be imported yet, 0 sent to the Pixel folder, 3 waiting for it)")
        self.assertEqual(legacy.describe(found["sam"]), "sam: no legacy folder")

    def test_a_different_file_at_the_copys_name_is_never_overwritten_and_the_file_counts_as_not_backed_up(self):
        target = self.world.root / "jo/originals/legacy" / legacy.copy_name(self.home.source / "fresh.jpg", legacy_sha(self.fresh))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"someone else's bytes")
        before = tree(self.world.root / "jo" / "originals")
        report = self.home.run()
        self.assertEqual(target.read_bytes(), b"someone else's bytes")
        self.assertEqual(report.failed, 1)
        self.assertEqual(self.home.rows()["fresh.jpg"].status, "failed")
        self.assertEqual({path: sha for path, sha in tree(self.world.root / "jo" / "originals").items() if path in before}, before)
        self.assertEqual(self.world.opened.execute(select(MediaItem).where(MediaItem.path == "jo/originals/legacy/" + target.name)).first(), None)
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.unmatched, found.failed), (2, 1))
        self.assertIn("1 could not be imported yet", legacy.describe(found))

    def test_a_failed_file_is_tried_again_on_the_next_run(self):
        target = self.world.root / "jo/originals/legacy" / legacy.copy_name(self.home.source / "fresh.jpg", legacy_sha(self.fresh))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"someone else's bytes")
        self.home.run()
        target.rename(target.with_name("moved-away"))
        again = self.home.run(at=NOW + 5)
        self.assertEqual((again.new, again.failed, self.home.rows()["fresh.jpg"].status), (1, 0, "unmatched"))

    def test_a_reader_failure_is_recorded_without_the_file_name(self):
        from darkroom.providers import exiftool
        from tests.privacy import no_leaks

        def failing(paths):
            raise exiftool.Unreadable("exiftool said canary-private-name.jpg is bad")

        with no_leaks(self, "canary-private-name", database=self.world.engine):
            report = legacy.import_person(self.world.opened, self.world.settings, self.home.jo, NOW, read=failing)
        self.assertEqual((report.failed, report.new), (6 + 1, 0))
        self.assertEqual({row.status for row in self.home.rows().values()}, {"failed"})

    def test_a_folder_that_cannot_be_listed_counts_as_not_backed_up_until_a_later_run_reads_it(self):
        real = os.walk

        def walk(top, onerror=None, **kwargs):
            yield from real(top, onerror=onerror, **kwargs)
            onerror(PermissionError(13, "denied", str(Path(top) / "locked")))

        with mock.patch("darkroom.domain.legacy.os.walk", walk):
            first = self.home.run()
        self.assertEqual((first.failed, first.new), (1, 6))
        self.assertEqual(self.home.rows()["locked/"].status, "failed")
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.failed, found.unmatched), (1, 3))
        self.assertIn("4 not backed up (1 could not be imported yet", legacy.describe(found))
        with mock.patch("darkroom.domain.legacy.os.walk", walk):
            again = self.home.run(at=NOW + 5)
        self.assertEqual((again.failed, again.new), (1, 0))
        fixed = self.home.run(at=NOW + 9)
        self.assertEqual((fixed.failed, fixed.new), (0, 0))
        self.assertNotIn("locked/", self.home.rows())
        self.assertEqual({entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"].failed, 0)

    def test_a_queue_that_raises_leaves_the_file_counted_as_failed_and_retried(self):
        class Raising:
            def offer(self, opened, person, original, now):
                raise OSError("queue folder is full")

        report = self.home.run(Raising())
        self.assertEqual(report.failed, 3)
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.matched, found.unmatched, found.failed), (3, 0, 3))
        again = self.home.run(Recorder(), at=NOW + 5)
        self.assertEqual((again.failed, again.new, again.queued, again.already_there), (0, 3, 3, 1))

    def test_exports_the_person_owns_still_prove_after_their_account_stops_serving_them(self):
        from darkroom.storage.models import Export, GoogleAccount, PixelPerson

        self.world.opened.query(Export).update({Export.owner_person_id: self.home.jo.id})
        self.world.opened.query(PixelPerson).filter(PixelPerson.person_id == self.home.jo.id).delete()
        self.world.opened.get(GoogleAccount, self.world.account).default_person_id = self.home.sam.id
        self.world.opened.commit()
        self.home.run()
        self.assertEqual({path: row.status for path, row in self.home.rows().items()}, {
            "google-only-copy.jpg": "matched", "2019/confirmed-copy.jpg": "matched", "reencoded.jpg": "matched",
            "not-backed-up.jpg": "unmatched", "fresh.jpg": "unmatched", "sams-photo.jpg": "unmatched",
        })

    def test_a_reversed_takeout_match_is_not_proof(self):
        from darkroom.domain import dedupe
        from darkroom.storage.models import DedupeDecision

        takeout = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.person_id == self.home.jo.id)).scalars().first()
        decision = self.world.opened.execute(select(DedupeDecision).where(DedupeDecision.takeout_item_id == takeout.id)).scalar_one()
        dedupe.reverse(self.world.opened, decision.id, NOW, "owner")
        self.world.opened.commit()
        self.home.run()
        self.assertEqual(self.home.rows()["google-only-copy.jpg"].status, "unmatched")

    def test_an_event_records_counts_and_no_file_names(self):
        self.home.run()
        entry = events.recent(self.world.opened, type="legacy_imported")[0]
        self.assertEqual((entry.subject, entry.detail), ("jo", "6 new, 3 matched, 3 unmatched, 0 failed"))


class LegacyGuardTests(unittest.TestCase):
    def setUp(self):
        self.home = LegacyWorld(self)
        self.world = self.home.world
        self.home.put("a.jpg", self.world.scratch_jpeg("a.jpg", colour=(5, 5, 5)))

    def test_the_legacy_library_waits_for_the_persons_takeout_import(self):
        self.home.configure()
        with self.assertRaises(legacy.NotReady):
            self.home.run()
        self.assertEqual(self.home.rows(), {})
        self.assertFalse((self.world.root / "jo/originals/legacy").exists())

    def test_a_person_without_a_folder_has_nothing_to_import(self):
        with self.assertRaises(legacy.NoSource):
            self.home.run()

    def test_the_folder_must_exist_and_be_absolute(self):
        for folder in ("relative/path", str(self.world.base / "missing")):
            with self.assertRaises(legacy.BadSource):
                legacy.set_source(self.world.opened, self.world.photos, self.home.jo, folder, NOW)
        self.assertIsNone(self.home.jo.legacy_path)

    def test_the_folder_may_not_overlap_the_photo_folder(self):
        for folder in (self.world.root, self.world.root / "jo" / "originals", self.world.base):
            with self.assertRaises(legacy.BadSource):
                legacy.set_source(self.world.opened, self.world.photos, self.home.jo, str(folder), NOW)

    def test_two_people_may_not_share_or_nest_legacy_folders(self):
        self.home.configure()
        nested = self.home.source / "sam"
        nested.mkdir()
        for folder in (self.home.source, nested, self.home.source.parent):
            with self.assertRaises(legacy.BadSource):
                legacy.set_source(self.world.opened, self.world.photos, self.home.sam, str(folder), NOW)
        self.assertIsNone(self.home.sam.legacy_path)

    def test_a_folder_moved_onto_the_photo_tree_is_refused_at_run_time(self):
        self.home.imported()
        self.home.configure()
        self.home.jo.legacy_path = str(self.world.root / "jo" / "originals")
        with self.assertRaises(legacy.BadSource):
            self.home.run()

    def test_a_link_in_the_source_is_not_followed(self):
        self.home.imported()
        outside = self.world.base / "elsewhere.jpg"
        outside.write_bytes(self.world.scratch_jpeg("l.jpg", colour=(7, 7, 7)))
        os.symlink(outside, self.home.source / "link.jpg")
        self.home.configure()
        report = self.home.run()
        self.assertEqual(report.new, 1)
        self.assertNotIn("link.jpg", self.home.rows())

    def test_an_import_already_running_for_the_person_is_refused(self):
        from darkroom.storage import locks

        self.home.imported()
        self.home.configure()
        with locks.exclusive(self.world.settings.data_dir / "locks" / "legacy-import-jo.lock"):
            with self.assertRaises(legacy.Busy):
                self.home.run()
        self.assertEqual(self.home.rows(), {})


def legacy_sha(data):
    import hashlib

    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


