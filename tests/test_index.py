import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import delete

from darkroom.domain import accounts, dedupe, events, index, media, pixel_folder, review, takeout_copies
from darkroom.domain.ingest import run
from darkroom.providers import exiftool
from darkroom.storage import db
from darkroom.storage.models import DedupeDecision, Export, GoogleAccount, ImmichAsset, IndexSkip, MediaItem, Person
from darkroom.storage.paths import Photos
from tests.fixtures import media as fixtures
from tests.shared import add_person, own_database


def listing(root):
    return sorted((str(p), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in Path(root).rglob("*"))


class IndexTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = Photos(self.root)
        self.engine = own_database(self)
        with db.session(self.engine) as opened:
            add_person(opened, "jo")
        self.originals = self.photos.ensure_person_tree("jo")[0]

    def run_index(self, now=100.0, read=exiftool.read):
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            return index.index_person(opened, self.photos, person, now, read)

    def items(self):
        with db.session(self.engine) as opened:
            return {i.path: i for i in opened.query(MediaItem)}

    def test_indexes_photos_and_videos_with_their_fields(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=(40.7, -74.0), make="Apple", model="iPhone")
        fixtures.make_video(self.originals / "sub" / "b.mov")
        report = self.run_index()
        self.assertEqual((report.new, report.unchanged, report.changed, report.failed), (2, 0, 0, 0))
        items = self.items()
        self.assertEqual(sorted(items), ["jo/originals/a.jpg", "jo/originals/sub/b.mov"])
        photo = items["jo/originals/a.jpg"]
        self.assertEqual((photo.source, photo.make, photo.time_source, photo.gps_source), ("phone", "Apple", "exif_offset", "exif"))
        self.assertFalse(photo.confirmed_in_google)

    def test_a_photo_is_indexed_with_both_perceptual_hashes_and_a_video_with_neither(self):
        fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        fixtures.make_video(self.originals / "b.mov")
        self.run_index()
        photo, video = self.items()["jo/originals/a.jpg"], self.items()["jo/originals/b.mov"]
        self.assertEqual((len(photo.phash), len(photo.dhash)), (16, 16))
        self.assertEqual((video.phash, video.dhash), (None, None))

    def test_an_item_indexed_before_dhash_existed_gets_one_without_being_read_again(self):
        path = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        with db.session(self.engine) as opened:
            item = opened.query(MediaItem).one()
            expected, item.dhash = item.dhash, None
            kept = (item.sha1, item.phash, item.indexed_at)
        report = self.run_index(now=200.0, read=lambda paths: self.fail("exiftool was run for a file that did not change"))
        self.assertEqual((report.new, report.unchanged, report.changed, report.failed), (0, 1, 0, 0))
        found = self.items()["jo/originals/a.jpg"]
        self.assertEqual((found.dhash, (found.sha1, found.phash, found.indexed_at)), (expected, kept))
        self.assertTrue(path.exists())

    def test_the_backfill_skips_what_cannot_be_hashed_and_still_fills_the_rest(self):
        fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        fixtures.save_jpeg(fixtures.scene(2), self.originals / "b.jpg")
        fixtures.save_jpeg(fixtures.scene(3), self.originals / "c.jpg")
        self.run_index()
        with db.session(self.engine) as opened:
            for item in opened.query(MediaItem):
                item.dhash = None
            opened.add(MediaItem(person_id=1, source="phone", path="../outside.jpg", sha1="x", bytes=1, phash="0" * 16, indexed_at=1.0))
            opened.add(MediaItem(person_id=1, source="phone", path="jo/originals/gone.jpg", sha1="y", bytes=1, phash="0" * 16, indexed_at=1.0))
        (self.originals / "b.jpg").write_bytes(b"no longer a picture")
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            self.assertEqual(index.backfill_dhash(opened, self.photos, person), 2)
        found = self.items()
        self.assertEqual({path: item.dhash is not None for path, item in found.items()},
                         {"jo/originals/a.jpg": True, "jo/originals/b.jpg": False, "jo/originals/c.jpg": True, "../outside.jpg": False, "jo/originals/gone.jpg": False})

    def test_the_backfill_leaves_other_people_and_takeout_items_alone(self):
        fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        with db.session(self.engine) as opened:
            add_person(opened, "sam")
            opened.query(MediaItem).one().dhash = None
            opened.add(MediaItem(person_id=1, source="takeout", path="jo/originals/a.jpg-copy", sha1="z", bytes=1, phash="0" * 16, indexed_at=1.0))
            sam = opened.query(Person).filter_by(slug="sam").one()
            self.assertEqual(index.backfill_dhash(opened, self.photos, sam), 0)
            self.assertEqual(sorted((i.source, i.dhash is None) for i in opened.query(MediaItem)), [("phone", True), ("takeout", True)])

    def test_a_second_run_indexes_nothing_new(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15")
        self.run_index()
        report = self.run_index(now=200.0)
        self.assertEqual((report.new, report.unchanged, report.changed, report.failed), (0, 1, 0, 0))
        self.assertEqual(self.items()["jo/originals/a.jpg"].indexed_at, 100.0)

    def test_a_file_replaced_at_the_same_path_is_indexed_again(self):
        path = fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15", offset="+00:00")
        self.run_index()
        before = self.items()["jo/originals/a.jpg"].sha1
        fixtures.make_jpeg(path, taken="2020:01:01 00:00:00", offset="+00:00", colour=(1, 2, 3), size=(80, 60))
        os.utime(path, (1_900_000_000, 1_900_000_000))
        report = self.run_index(now=300.0)
        self.assertEqual((report.new, report.changed), (0, 1))
        after = self.items()["jo/originals/a.jpg"]
        self.assertNotEqual(after.sha1, before)
        self.assertEqual((after.width, after.indexed_at), (80, 300.0))

    def test_skipped_files_and_folders(self):
        fixtures.make_jpeg(self.originals / "keep.jpg")
        fixtures.make_jpeg(self.originals / ".hidden.jpg")
        fixtures.make_jpeg(self.originals / ".stfolder" / "x.jpg")
        fixtures.make_jpeg(self.originals / ".stversions" / "y.jpg")
        fixtures.make_jpeg(self.originals / ".uploads_tmp" / "z.jpg")
        (self.originals / "keep.jpg.xmp").write_text("<xmp/>")
        self.run_index()
        self.assertEqual(sorted(self.items()), ["jo/originals/keep.jpg"])

    def test_an_unreadable_file_is_reported_and_the_run_completes(self):
        fixtures.make_jpeg(self.originals / "good.jpg", taken="2019:07:04 12:30:15")
        (self.originals / "bad.jpg").write_bytes(b"not a picture")
        report = self.run_index()
        self.assertEqual((report.new, report.failed), (1, 1))
        self.assertEqual(sorted(self.items()), ["jo/originals/good.jpg"])
        with db.session(self.engine) as opened:
            self.assertEqual([e.type for e in events.recent(opened, type="index_failed")], ["index_failed"])

    def test_an_exiftool_error_entry_is_a_failure(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        report = self.run_index(read=lambda paths: {p: {"SourceFile": str(p), "Error": "broken"} for p in paths})
        self.assertEqual((report.new, report.failed), (0, 1))

    def test_exiftool_dying_fails_the_whole_run_but_not_the_service(self):
        fixtures.make_jpeg(self.originals / "a.jpg")

        def dies(paths):
            raise exiftool.Unreadable("exiftool crashed")

        report = self.run_index(read=dies)
        self.assertEqual((report.new, report.failed), (0, 1))

    def test_exiftool_failing_counts_only_the_files_it_was_given(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        self.run_index()

        fixtures.make_jpeg(self.originals / "b.jpg")

        def dies(paths):
            raise exiftool.Unreadable("exiftool crashed")

        report = self.run_index(now=200.0, read=dies)
        self.assertEqual((report.unchanged, report.failed), (1, 1))

    def test_a_file_that_vanishes_before_stat_is_a_failure_not_a_crash(self):
        from unittest import mock

        photo = fixtures.make_jpeg(self.originals / "a.jpg")
        real = Path.stat

        def stat(self, *args, **kwargs):
            if self.name == photo.name:
                raise FileNotFoundError(self.name)
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            report = self.run_index()
        self.assertEqual((report.new, report.failed), (0, 1))

    def test_skipped_folders_are_pruned_not_walked(self):
        from unittest import mock

        fixtures.make_jpeg(self.originals / "keep" / "a.jpg")
        fixtures.make_jpeg(self.originals / ".stversions" / "old" / "b.jpg")
        visited = []
        real = os.walk

        def spy(top, *args, **kwargs):
            for folder, names, files in real(top, *args, **kwargs):
                visited.append(Path(folder).name)
                yield folder, names, files

        with mock.patch("os.walk", spy):
            self.assertEqual([p.name for p in index.walk(self.originals)], ["a.jpg"])
        self.assertNotIn("old", visited)

    def test_the_indexer_never_writes_under_originals(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15", gps=(40.7, -74.0))
        fixtures.make_video(self.originals / "b.mov")
        (self.originals / "bad.jpg").write_bytes(b"nope")
        before = listing(self.root / "jo")
        self.run_index()
        self.run_index(now=200.0)
        self.assertEqual(listing(self.root / "jo"), before)

    def test_index_file_handles_one_file_and_ignores_strangers(self):
        photo = fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15")
        outside = fixtures.make_jpeg(self.root / "elsewhere.jpg")
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            self.assertEqual(index.index_file(opened, self.photos, person, photo, 1.0).new, 1)
            self.assertEqual(index.index_file(opened, self.photos, person, outside, 1.0).new, 0)
            self.assertEqual(index.index_file(opened, self.photos, person, self.originals / "missing.jpg", 1.0).new, 0)
        self.assertEqual(sorted(self.items()), ["jo/originals/a.jpg"])

    def test_an_empty_or_missing_folder_is_fine(self):
        self.assertEqual(self.run_index().new, 0)
        self.assertEqual(index.walk(self.root / "nowhere"), [])

    def test_people_are_kept_apart(self):
        with db.session(self.engine) as opened:
            add_person(opened, "sam")
        sam = self.photos.ensure_person_tree("sam")[0]
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_jpeg(sam / "a.jpg", colour=(5, 5, 5))
        with db.session(self.engine) as opened:
            for person in opened.query(Person):
                index.index_person(opened, self.photos, person, 1.0)
        with db.session(self.engine) as opened:
            self.assertEqual(sorted((i.person_id, i.path) for i in opened.query(MediaItem)), [(1, "jo/originals/a.jpg"), (2, "sam/originals/a.jpg")])



def described(paths, extra=None):
    return {path: {"SourceFile": str(path), "File:MIMEType": "image/jpeg", **(extra or {}).get(path.name, {})} for path in paths}


class PoisonFileTests(unittest.TestCase):
    setUp = IndexTests.setUp
    run_index = IndexTests.run_index
    items = IndexTests.items

    def skips(self):
        with db.session(self.engine) as opened:
            return {row.path: row.reason for row in opened.query(IndexSkip)}

    def failure_details(self):
        with db.session(self.engine) as opened:
            return [event.detail for event in events.recent(opened, type="index_failed")]

    def test_a_header_claiming_billions_of_pixels_is_indexed_without_decoding_and_the_rest_still_are(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15")
        fixtures.make_pixel_bomb(self.originals / "bomb.png")
        fixtures.make_jpeg(self.originals / "z.jpg", colour=(9, 9, 9))
        report = self.run_index()
        self.assertEqual((report.new, report.failed), (3, 0))
        items = self.items()
        self.assertEqual((items["jo/originals/bomb.png"].phash, items["jo/originals/bomb.png"].dhash), (None, None))
        self.assertEqual(len(items["jo/originals/a.jpg"].phash), 16)
        self.assertEqual(len(items["jo/originals/z.jpg"].phash), 16)

    def test_metadata_numbers_that_overflow_do_not_stop_the_run(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_jpeg(self.originals / "poison.jpg", colour=(9, 9, 9))
        fixtures.make_jpeg(self.originals / "z.jpg", colour=(5, 50, 5))
        poison = {"poison.jpg": {"IFD0:ImageWidth": float("inf"), "IFD0:ImageHeight": 1e300, "QuickTime:Duration": 1e308,
                                 "ExifIFD:DateTimeOriginal": "0001:01:01 00:00:00", "ExifIFD:OffsetTimeOriginal": "+05:00",
                                 "Composite:GPSLatitude": float("nan"), "Composite:GPSLongitude": 10.0},
                  "z.jpg": {"ExifIFD:DateTimeOriginal": "9999:12:31 23:00:00", "ExifIFD:OffsetTimeOriginal": "-05:00", "IFD0:ImageWidth": -4}}
        report = self.run_index(read=lambda paths: described(paths, poison))
        self.assertEqual((report.new, report.failed), (3, 0))
        bad, late = self.items()["jo/originals/poison.jpg"], self.items()["jo/originals/z.jpg"]
        self.assertEqual((bad.width, bad.height, bad.duration_ms, bad.captured_utc, bad.time_source, bad.lat, bad.gps_source),
                         (None, None, None, None, "unknown", None, "none"))
        self.assertEqual(bad.captured_local, "0001-01-01T00:00:00")
        self.assertEqual((late.captured_utc, late.time_source, late.width), (None, "unknown", None))

    def test_a_file_that_makes_exiftool_fail_is_set_aside_and_the_others_are_indexed(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        poison = fixtures.make_jpeg(self.originals / "poison.jpg", colour=(9, 9, 9))
        fixtures.make_jpeg(self.originals / "z.jpg", colour=(5, 50, 5))
        calls = []

        def chokes(paths):
            calls.append([path.name for path in paths])
            if poison in paths:
                raise exiftool.Unreadable("exiftool crashed on this file")
            return described(paths)

        report = self.run_index(read=chokes)
        self.assertEqual((report.new, report.failed, report.skipped), (2, 1, 0))
        self.assertEqual(sorted(self.items()), ["jo/originals/a.jpg", "jo/originals/z.jpg"])
        self.assertEqual(self.skips(), {"jo/originals/poison.jpg": index.UNREADABLE})
        self.assertEqual(self.failure_details(), [f"{index.UNREADABLE}, {index.SKIP_NOTE}"])
        self.assertEqual(calls, [["a.jpg", "poison.jpg", "z.jpg"], ["a.jpg"], ["poison.jpg"], ["z.jpg"]])

        fixtures.make_jpeg(self.originals / "b.jpg", colour=(1, 1, 90))
        calls.clear()
        report = self.run_index(now=200.0, read=chokes)
        self.assertEqual((report.new, report.unchanged, report.failed, report.skipped), (1, 2, 0, 1))
        self.assertEqual(calls, [["b.jpg"]])
        self.assertEqual(len(self.failure_details()), 1)

        os.utime(poison, (1_900_000_000, 1_900_000_000))
        report = self.run_index(now=300.0, read=described)
        self.assertEqual((report.new, report.skipped), (1, 0))
        self.assertIn("jo/originals/poison.jpg", self.items())
        self.assertEqual(self.skips(), {})

    def test_a_file_exiftool_times_out_on_is_not_set_aside_and_is_indexed_next_run_unchanged(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        slow = fixtures.make_jpeg(self.originals / "slow.jpg", colour=(9, 9, 9))
        fixtures.make_jpeg(self.originals / "z.jpg", colour=(5, 50, 5))

        def times_out(paths):
            if slow in paths:
                raise exiftool.TimedOut("exiftool took too long to read the files")
            return described(paths)

        report = self.run_index(read=times_out)
        self.assertEqual((report.new, report.failed, report.set_aside), (2, 1, 0))
        self.assertEqual(self.skips(), {})
        report = self.run_index(now=200.0, read=described)
        self.assertEqual((report.new, report.unchanged, report.skipped), (1, 2, 0))
        self.assertIn("jo/originals/slow.jpg", self.items())

    def test_several_slow_files_share_one_time_budget_and_none_is_set_aside(self):
        fixtures.make_jpeg(self.originals / "good.jpg")
        for n in range(6):
            fixtures.make_jpeg(self.originals / f"slow{n}.jpg", colour=(n, 9, 9))
        clock = [1000.0]
        calls = []

        def slow_reader(paths):
            calls.append([path.name for path in paths])
            if len(paths) > 1:
                clock[0] += exiftool.TIMEOUT
                raise exiftool.TimedOut("exiftool took too long to read the files")
            if paths[0].name.startswith("slow"):
                clock[0] += exiftool.FILE_TIMEOUT
                raise exiftool.TimedOut("exiftool took too long to read the files")
            clock[0] += 1
            return described(paths)

        with mock.patch.object(index, "monotonic", lambda: clock[0]):
            report = self.run_index(read=slow_reader)
        self.assertLessEqual(clock[0] - 1000.0, exiftool.TIMEOUT + index.ISOLATION_SECONDS + exiftool.FILE_TIMEOUT)
        self.assertEqual(len(calls), 1 + 1 + 5)
        self.assertEqual((report.new, report.failed, report.set_aside), (1, 6, 0))
        self.assertEqual(self.skips(), {})
        self.assertEqual(self.run_index(now=200.0, read=described).new, 6)

    def test_only_the_batch_that_failed_is_read_again_file_by_file(self):
        for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "poison.jpg"):
            fixtures.make_jpeg(self.originals / name, colour=(len(name), ord(name[0]) % 200, 9))
        calls = []

        def chokes(paths):
            calls.append([path.name for path in paths])
            if any(path.name == "poison.jpg" for path in paths):
                raise exiftool.Unreadable("exiftool crashed on this file")
            return described(paths)

        with mock.patch.object(exiftool, "BATCH", 3):
            report = self.run_index(read=chokes)
        self.assertEqual(calls, [["a.jpg", "b.jpg", "c.jpg"], ["d.jpg", "e.jpg", "poison.jpg"], ["d.jpg"], ["e.jpg"], ["poison.jpg"]])
        self.assertEqual((report.new, report.set_aside), (5, 1))

    def test_exiftool_and_hashing_run_without_the_write_lock_which_is_taken_before_the_deciding_reads(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        order = []
        real_items, real_skips, real_fingerprint = index.phone_items, index.skips_of, media.fingerprint

        def items(opened, person):
            order.append("items")
            return real_items(opened, person)

        def skips(opened, person):
            order.append("skips")
            return real_skips(opened, person)

        def reader(paths):
            order.append("exiftool")
            return described(paths)

        def fingerprint(path, tags):
            order.append("fingerprint")
            return real_fingerprint(path, tags)

        with mock.patch.object(index, "phone_items", items), mock.patch.object(index, "skips_of", skips), \
                mock.patch.object(media, "fingerprint", fingerprint), mock.patch.object(index.db, "write_lock", lambda opened: order.append("lock")):
            self.assertEqual(self.run_index(read=reader).new, 1)
            self.assertEqual(order[:7], ["items", "skips", "exiftool", "fingerprint", "lock", "items", "skips"])
            order.clear()
            self.run_index(now=200.0, read=reader)
            self.assertNotIn("lock", order)

    def test_what_another_run_recorded_before_the_lock_decides_what_this_run_writes(self):
        photo = fixtures.make_jpeg(self.originals / "a.jpg")
        real_lock = index.db.write_lock

        def another_run_finishes_first(opened):
            with db.session(self.engine) as other:
                other.add(IndexSkip(person_id=1, path="jo/originals/a.jpg", bytes=photo.stat().st_size, mtime=photo.stat().st_mtime,
                                    reason=index.UNREADABLE, skipped_at=100.0))
            real_lock(opened)

        with mock.patch.object(index.db, "write_lock", another_run_finishes_first):
            report = self.run_index(read=described)
        self.assertEqual((report.new, report.skipped, report.failed), (0, 1, 0))
        self.assertEqual(self.items(), {})
        self.assertEqual(self.skips(), {"jo/originals/a.jpg": index.UNREADABLE})

    def test_a_path_set_aside_again_while_the_cleanup_waits_for_the_lock_keeps_its_new_skip(self):
        notes = self.originals / "notes.jpg"
        notes.write_bytes(b"not a picture")
        self.run_index()
        notes.unlink()
        real_lock = index.db.write_lock

        def the_reconcile_sets_it_aside_first(opened):
            notes.write_bytes(b"not a picture either")
            with db.session(self.engine) as other:
                other.execute(delete(IndexSkip))
                other.add(IndexSkip(person_id=1, path="jo/originals/notes.jpg", bytes=notes.stat().st_size, mtime=notes.stat().st_mtime,
                                    reason=index.NOT_MEDIA, skipped_at=150.0))
            real_lock(opened)

        with mock.patch.object(index.db, "write_lock", the_reconcile_sets_it_aside_first):
            self.run_index(now=200.0, read=lambda paths: self.fail("the walk ran before the file came back"))
        self.assertEqual(self.skips(), {"jo/originals/notes.jpg": index.NOT_MEDIA})
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(IndexSkip).one().skipped_at, 150.0)

    def test_nothing_to_forget_takes_no_lock(self):
        (self.originals / "notes.jpg").write_bytes(b"not a picture")
        self.run_index()
        with mock.patch.object(index.db, "write_lock", side_effect=AssertionError("locked with nothing to write")):
            self.assertEqual(self.run_index(now=200.0, read=lambda paths: self.fail("read again")).skipped, 1)

    def test_the_dhash_backfill_writes_nothing_for_an_item_reindexed_while_it_hashed(self):
        fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        with db.session(self.engine) as opened:
            item = opened.query(MediaItem).one()
            item.dhash = None
            item_id = item.id
        real_lock = index.db.write_lock

        def reindexed_meanwhile(opened):
            with db.session(self.engine) as other:
                other.get(MediaItem, item_id).sha1 = "f" * 40
            real_lock(opened)

        with mock.patch.object(index.db, "write_lock", reindexed_meanwhile), db.session(self.engine) as opened:
            self.assertEqual(index.backfill_dhash(opened, self.photos, opened.query(Person).one()), 0)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(MediaItem, item_id).dhash)

    def test_running_out_of_memory_on_a_file_is_not_set_aside(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        with mock.patch.object(media, "fingerprint", side_effect=MemoryError()):
            report = self.run_index(read=described)
        self.assertEqual((report.failed, report.set_aside), (1, 0))
        self.assertEqual(self.skips(), {})
        self.assertEqual(self.run_index(now=200.0, read=described).new, 1)

    def test_a_set_aside_file_is_tried_again_after_a_day_even_unchanged(self):
        (self.originals / "notes.jpg").write_bytes(b"not a picture")
        self.run_index()
        self.assertEqual(self.run_index(now=100.0 + index.RETRY_SET_ASIDE_SECONDS - 1, read=lambda paths: self.fail("read again too soon")).skipped, 1)
        report = self.run_index(now=100.0 + index.RETRY_SET_ASIDE_SECONDS, read=described)
        self.assertEqual((report.new, report.skipped), (1, 0))
        self.assertEqual(self.skips(), {})

    def test_an_original_replaced_by_unreadable_bytes_leaves_no_stale_fingerprint_for_dedupe_or_status(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            account = GoogleAccount(email="jo.photos@example.test", default_person_id=person.id, created=1.0)
            opened.add(account)
            opened.flush()
            account_id, old = account.id, opened.query(MediaItem).one()
            old_id, old_sha1, old_phash = old.id, old.sha1, old.phash
            self.assertIn(old_sha1, dedupe.PersonIndex.load(opened, account_id).hashes)
        photo.write_bytes(b"not a picture any more")
        report = self.run_index(now=200.0)
        self.assertEqual((report.changed, report.set_aside), (0, 1))
        with db.session(self.engine) as opened:
            loaded = dedupe.PersonIndex.load(opened, account_id)
            self.assertNotIn(old_sha1, loaded.hashes)
            self.assertNotIn(old_id, loaded.by_id)
            self.assertNotIn(old_phash, [shot.phash for shot in loaded.phone])
            item = opened.get(MediaItem, old_id)
            self.assertEqual((item.sha1, item.phash, item.dhash, item.captured_utc, item.mtime), (index.UNKNOWN_SHA1, None, None, None, None))
            self.assertEqual(index.counts_by_person(opened), [("jo", person.name, 0)])
        fixtures.save_jpeg(fixtures.scene(2), photo)
        report = self.run_index(now=300.0)
        self.assertEqual((report.changed, report.skipped), (1, 0))
        self.assertEqual(self.skips(), {})
        with db.session(self.engine) as opened:
            self.assertIn(old_id, dedupe.PersonIndex.load(opened, account_id).by_id)

    def test_exiftool_failing_on_every_file_sets_nothing_aside(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_jpeg(self.originals / "b.jpg", colour=(9, 9, 9))

        def dies(paths):
            raise exiftool.Unreadable("exiftool crashed")

        report = self.run_index(read=dies)
        self.assertEqual((report.new, report.failed, report.skipped), (0, 2, 0))
        self.assertEqual(self.skips(), {})
        self.assertEqual(self.run_index(now=200.0, read=described).new, 2)

    def test_a_file_whose_decoding_raises_anything_is_set_aside_and_the_others_are_indexed(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_jpeg(self.originals / "poison.jpg", colour=(9, 9, 9))
        fixtures.make_jpeg(self.originals / "z.jpg", colour=(5, 50, 5))
        real = media.fingerprint

        def fingerprint(path, tags):
            if path.name == "poison.jpg":
                raise RuntimeError("the decoder gave up")
            return real(path, tags)

        with mock.patch.object(media, "fingerprint", fingerprint):
            report = self.run_index(read=described)
        self.assertEqual((report.new, report.failed), (2, 1))
        self.assertEqual(self.skips(), {"jo/originals/poison.jpg": index.UNUSABLE})
        with mock.patch.object(media, "fingerprint", side_effect=AssertionError("a set-aside file was read again")):
            self.assertEqual(self.run_index(now=200.0, read=described).skipped, 1)

    def test_a_file_that_is_not_media_is_set_aside_until_it_changes(self):
        (self.originals / "notes.jpg").write_bytes(b"not a picture")
        report = self.run_index()
        self.assertEqual((report.new, report.failed), (0, 1))
        self.assertEqual(self.skips(), {"jo/originals/notes.jpg": index.NOT_MEDIA})
        report = self.run_index(now=200.0, read=lambda paths: self.fail("exiftool was run for a set-aside file that did not change"))
        self.assertEqual((report.failed, report.skipped), (0, 1))

    def test_a_file_exiftool_says_nothing_about_is_set_aside_and_the_others_are_indexed(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_jpeg(self.originals / "silent.jpg", colour=(9, 9, 9))
        report = self.run_index(read=lambda paths: described([path for path in paths if path.name != "silent.jpg"]))
        self.assertEqual((report.new, report.failed), (1, 1))
        self.assertEqual(self.skips(), {"jo/originals/silent.jpg": index.NO_METADATA})

    def test_a_set_aside_file_that_is_gone_is_forgotten(self):
        notes = self.originals / "notes.jpg"
        notes.write_bytes(b"not a picture")
        self.run_index()
        notes.unlink()
        self.run_index(now=200.0)
        self.assertEqual(self.skips(), {})

    def test_a_file_that_vanishes_while_it_is_read_is_retried_next_time_not_set_aside(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        with mock.patch.object(media, "sha1_of", side_effect=FileNotFoundError("gone")):
            report = self.run_index(read=described)
        self.assertEqual((report.new, report.failed), (0, 1))
        self.assertEqual(self.skips(), {})
        self.assertEqual(self.run_index(now=200.0, read=described).new, 1)

    def test_setting_files_aside_never_writes_under_originals(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_pixel_bomb(self.originals / "bomb.png")
        (self.originals / "notes.jpg").write_bytes(b"not a picture")
        before = listing(self.root / "jo")
        self.run_index()
        self.run_index(now=200.0)
        self.assertEqual(listing(self.root / "jo"), before)

    def test_index_file_sets_a_poison_file_aside_too(self):
        poison = fixtures.make_jpeg(self.originals / "poison.jpg")
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            with mock.patch.object(media, "fingerprint", side_effect=OverflowError("too big")):
                report = index.index_file(opened, self.photos, person, poison, 1.0, described)
        self.assertEqual((report.new, report.failed), (0, 1))
        self.assertEqual(self.skips(), {"jo/originals/poison.jpg": index.UNUSABLE})



class ConfirmationTests(unittest.TestCase):
    setUp = IndexTests.setUp
    run_index = IndexTests.run_index

    def confirm(self, name="IMG_1.JPG"):
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            account = opened.query(GoogleAccount).one_or_none()
            if account is None:
                account = GoogleAccount(email="jo.photos@example.test", default_person_id=person.id, created=1.0)
                opened.add(account)
                opened.flush()
            export = Export(account_id=account.id, kind="manual", created_at=50.0)
            opened.add(export)
            opened.flush()
            item = opened.query(MediaItem).one()
            opened.add(DedupeDecision(export_id=export.id, account_id=account.id, source_path=f"Takeout/{name}", person_id=person.id, matched_item_id=item.id,
                                      tier=1, reason="identical", decided_by="auto", decided_at=60.0))
            opened.flush()
            dedupe.confirm_served_matches(opened, account.id, {person.id})
            return account.id, person.id, item.id

    def state(self, account_id, person_id, item_id):
        with db.session(self.engine) as opened:
            item = opened.get(MediaItem, item_id)
            return (item.confirmed_in_google, accounts.confirmed_people(opened, account_id), pixel_folder.confirmed_for(opened, account_id, item_id, person_id),
                    run.unmatched_count(opened, person_id, 10**9))

    def linked(self, item_id):
        with db.session(self.engine) as opened:
            return opened.query(DedupeDecision).filter(DedupeDecision.matched_item_id == item_id).count()

    def test_a_confirmed_original_replaced_by_unreadable_bytes_is_no_longer_confirmed_or_counted(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        ids = self.confirm()
        self.assertEqual(self.state(*ids), (True, {ids[1]}, True, 0))
        photo.write_bytes(b"not a picture any more")
        self.assertEqual(self.run_index(now=200.0).set_aside, 1)
        self.assertEqual(self.state(*ids), (False, set(), False, 0))
        self.assertEqual(self.linked(ids[2]), 0)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(DedupeDecision).count(), 1)

    def test_a_path_that_recovers_with_different_contents_does_not_inherit_the_confirmation(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        ids = self.confirm()
        photo.write_bytes(b"not a picture any more")
        self.run_index(now=200.0)
        fixtures.save_jpeg(fixtures.scene(2), photo)
        self.assertEqual(self.run_index(now=300.0).changed, 1)
        self.assertEqual(self.state(*ids), (False, set(), False, 1))

    def test_an_original_replaced_by_other_readable_contents_loses_its_confirmation(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        ids = self.confirm()
        fixtures.save_jpeg(fixtures.scene(2), photo)
        os.utime(photo, (1_900_000_000, 1_900_000_000))
        self.assertEqual(self.run_index(now=200.0).changed, 1)
        self.assertEqual(self.state(*ids), (False, set(), False, 1))
        self.assertEqual(self.linked(ids[2]), 0)

    def test_the_same_contents_with_a_new_mtime_keep_the_confirmation(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        ids = self.confirm()
        os.utime(photo, (1_900_000_000, 1_900_000_000))
        self.assertEqual(self.run_index(now=200.0).changed, 1)
        self.assertEqual(self.state(*ids), (True, {ids[1]}, True, 0))

    def test_dedupe_forget_confirmation_detaches_the_matches_and_keeps_the_decisions(self):
        fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        ids = self.confirm()
        with db.session(self.engine) as opened:
            dedupe.forget_confirmation(opened, opened.get(MediaItem, ids[2]))
        self.assertEqual(self.state(*ids)[:3], (False, set(), False))
        self.assertEqual(self.linked(ids[2]), 0)
        with db.session(self.engine) as opened:
            self.assertEqual([(row.tier, row.reversed_at) for row in opened.query(DedupeDecision)], [(1, None)])

    def test_a_set_aside_original_is_not_listed_as_missing_gps_queued_for_a_pixel_or_paired(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "immich" / "a.jpg")
        self.run_index()
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            item = opened.query(MediaItem).one()
            opened.add(ImmichAsset(person_id=person.id, asset_id="asset-a", kind="photo", status="stored", sha1=item.sha1, seen_at=1.0, original_item_id=item.id))
            opened.flush()
            self.assertEqual(review.missing_gps(opened, None, None, None, 0)[1], 1)
            self.assertEqual(set(takeout_copies.immich_originals(opened, person)), {item.id})
            pixel_folder.queue(opened, item, 1.0)
        photo.write_bytes(b"not a picture any more")
        self.assertEqual(self.run_index(now=200.0).set_aside, 1)
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            self.assertEqual(review.missing_gps(opened, None, None, None, 0), ([], 0))
            self.assertEqual(takeout_copies.immich_originals(opened, person), {})
            with self.assertRaises(pixel_folder.NotInOriginals):
                pixel_folder.queue(opened, opened.query(MediaItem).one(), 2.0)

    def test_an_export_time_that_is_not_an_instant_never_counts_as_seen_after_a_pixel_copy_was_placed(self):
        fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        self.run_index()
        ids = self.confirm()
        for created, expected in ((float("inf"), False), (1e300, False), (float("-inf"), False), (150.0, True)):
            with self.subTest(created=created), db.session(self.engine) as opened:
                opened.query(Export).one().created_at = created
                opened.flush()
                self.assertEqual(pixel_folder.confirmed_for(opened, ids[0], ids[2], ids[1], since=100.0), expected)
                self.assertTrue(pixel_folder.confirmed_for(opened, ids[0], ids[2], ids[1]))

    def test_the_same_bytes_coming_back_are_confirmed_again_only_by_a_new_match(self):
        photo = fixtures.save_jpeg(fixtures.scene(1), self.originals / "a.jpg")
        original = photo.read_bytes()
        self.run_index()
        ids = self.confirm()
        photo.write_bytes(b"not a picture any more")
        self.run_index(now=200.0)
        photo.write_bytes(original)
        os.utime(photo, (1_900_000_000, 1_900_000_000))
        self.assertEqual(self.run_index(now=300.0).changed, 1)
        self.assertEqual(self.state(*ids), (False, set(), False, 1))
        self.confirm("IMG_1 (1).JPG")
        self.assertEqual(self.state(*ids), (True, {ids[1]}, True, 0))


if __name__ == "__main__":
    unittest.main()
