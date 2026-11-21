import hashlib
import os
import unittest
import zipfile
from unittest import mock

from sqlalchemy import select

from darkroom.domain import exports
from darkroom.domain.ingest import part_free, run
from darkroom.storage import paths
from darkroom.storage.models import (
    DedupeDecision, Event, Export, ExportPart, ImportRun, ManagedImportIntent, MediaItem, ReviewItem, TakeoutEntry, TakeoutSidecar,
)
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_finish_export import FinishWorld
from tests.test_part_ingest import Crash

TICK = 60.0
EXPORT = "export-1"
MOMENT = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
NEAR = {"offset": "+02:00", "make": "Apple", "model": "iPhone 11", "colour": (90, 90, 200)}
PLACED = "Photos from 2019/IMG_5001.JPG"
EXACT = "Photos from 2019/IMG_1001.JPG"
TIER_2 = "Photos from 2019/IMG_1003.JPG"
REVIEWED = "Photos from 2019/IMG_1005.JPG"
ARCHIVE_PAGE = "Takeout/archive_browser.html"


class FreeWorld(FinishWorld):
    def scenario(self, builder):
        builder.photo("IMG_5001.JPG", part=0, description="a made-up pier")
        exact = builder.image_bytes(".JPG")
        self.original("jo", "IMG_1001.JPG", exact)
        builder.photo("IMG_1001.JPG", data=exact, gps=None, part=0)
        self.original("jo", "IMG_1003.JPG", self.scratch_jpeg("t2a.jpg", colour=(10, 120, 30), **MOMENT))
        builder.photo("IMG_1003.JPG", data=self.scratch_jpeg("t2b.jpg", colour=(12, 118, 33), **MOMENT), gps=None, taken=1562221815, part=0)
        builder.non_media(part=0)
        builder.photo("IMG_5002.JPG", part=1)
        self.original("jo", "IMG_1005.JPG", self.scratch_jpeg("t4a.jpg", taken="2019:08:01 10:00:00", **NEAR))
        builder.photo("IMG_1005.JPG", data=self.scratch_jpeg("t4b.jpg", taken="2019:08:01 10:00:01", **NEAR), gps=None, part=1)

    def made(self):
        export = self.create(self.scenario, parts=2, google_id=EXPORT)
        self.ingest_all(export, (1, 2))
        return export

    def folder(self):
        return self.root / "_inbox/jo" / EXPORT

    def zip_of(self, number):
        self.opened.expire_all()
        return self.folder() / self.opened.execute(select(ExportPart.filename).where(ExportPart.number == number)).scalar_one()

    def zips(self):
        return {path.name: path.read_bytes() for path in sorted(self.folder().glob("*.zip"))}

    def held_store(self):
        store = self.root / "_inbox/jo/held"
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(store.rglob("*")) if path.is_file()}

    def free(self, at=NOW + TICK):
        self.opened.expire_all()
        return part_free.free_ready(self.opened, self.settings, at)

    def states(self):
        self.opened.expire_all()
        return [(part.number, part.free_state) for part in self.opened.execute(select(ExportPart).order_by(ExportPart.number)).scalars()]

    def placed_file(self, name=PLACED):
        item = self.opened.get(MediaItem, self.entry(name).item_id)
        return self.root / item.path

    def hold_of(self, name=REVIEWED):
        review = self.opened.execute(select(ReviewItem).where(ReviewItem.decision_id == self.entry(name).decision_id)).scalar_one()
        return self.root / review.hold_path

    def change(self, row, **values):
        for key, value in values.items():
            setattr(row, key, value)
        self.opened.commit()

    def entry_named(self, name):
        self.opened.expire_all()
        return self.opened.execute(select(TakeoutEntry).where(TakeoutEntry.name == name)).scalar_one()

    def held_copy(self, name=TIER_2):
        return self.root / "_inbox/jo/held" / EXPORT / f"{self.entry(name).id}.jpg"

    def kind_of_events(self, kind):
        self.opened.expire_all()
        return [row.detail for row in self.opened.execute(select(Event).where(Event.type == kind).order_by(Event.id)).scalars()]


def swap(path, data=None):
    twin = path.with_name(f"{path.name}.swap")
    twin.write_bytes(path.read_bytes() if data is None else data)
    os.replace(twin, path)


class FreeingTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()

    def test_a_part_is_not_freed_in_the_tick_it_was_ingested(self):
        zips = self.world.zips()
        self.assertEqual(self.world.free(at=NOW).parts, 0)
        self.assertEqual(self.world.zips(), zips)
        self.assertEqual(self.world.states(), [(1, None), (2, None)])
        self.assertEqual(self.world.held_store(), {})

    def test_each_part_is_freed_once_its_proof_holds_and_the_held_store_once_the_export_is_imported(self):
        sizes = {name: len(data) for name, data in self.world.zips().items()}
        with no_leaks(self, "IMG_5001", "IMG_1003", "made-up pier"):
            report = self.world.free()
        self.assertEqual((report.parts, report.kept, report.failed, report.bytes), (2, 0, 0, sum(sizes.values())))
        self.assertEqual(self.world.states(), [(1, "freed"), (2, "freed")])
        self.assertEqual(self.world.zips(), {})
        held = self.world.held_store()
        waiting = self.world.entry(TIER_2)
        self.assertEqual((waiting.outcome, list(held)), ("waiting_export", [waiting.held_path]))
        self.assertEqual((waiting.held_path, hashlib.sha1(held[waiting.held_path], usedforsecurity=False).hexdigest()),
                         (str(self.world.held_copy().relative_to(self.world.root)), waiting.sha1))
        self.assertEqual(self.world.kind_of_events("export_part_freed"), [f"part {number}: {size} bytes" for number, size in enumerate(sizes.values(), start=1)])
        self.assertEqual(exports.as_dict(self.world.opened, self.world.opened.get(Export, self.export.id), NOW)["bytes_freed"], sum(sizes.values()))
        self.assertEqual(self.world.free(at=NOW + 2 * TICK).parts, 0)
        self.world.finish(self.export, at=NOW + 3 * TICK)
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "imported")
        decided = self.world.opened.get(DedupeDecision, self.world.entry(TIER_2).decision_id)
        self.assertEqual((self.world.entry(TIER_2).outcome, decided.tier), ("skipped", 2))
        self.assertEqual(self.world.held_store(), held)
        report = self.world.free(at=NOW + 4 * TICK)
        self.assertEqual((report.held, report.kept), (1, 0))
        self.assertEqual(self.world.held_store(), {})
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.kind_of_events("export_held_freed")), ("freed", ["1 held copies"]))
        said = " ".join(self.world.kind_of_events("export_part_freed") + self.world.kind_of_events("export_held_freed"))
        self.assertNotIn("IMG_", said)

    def test_a_freed_export_finishes_with_the_decisions_and_files_of_whole_export_ingest(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((self.world.zips(), self.world.held_store()), ({}, {}))
        twin = FreeWorld(self)
        found = twin.create(twin.scenario, parts=2, status="downloaded", queue=False)
        twin.run_import(found)
        self.assertEqual(self.world.comparable(), twin.comparable())

    def test_a_managed_import_never_frees_a_zip_or_a_held_copy(self):
        self.world.opened.add(ManagedImportIntent(person_id=self.world.ids["jo"], export_id=self.export.id, owner_id="u-jo", resource_manifest="{}", created_at=NOW))
        self.world.opened.commit()
        zips = self.world.zips()
        self.assertEqual(self.world.free().parts, 0)
        self.world.finish(self.export, at=NOW + 2 * TICK)
        self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((self.world.zips(), self.world.held_store(), self.world.states()), (zips, {}, [(1, None), (2, None)]))


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()

    def assert_kept(self, number=1, freed=None):
        zipped = self.world.zip_of(number)
        before, held = zipped.read_bytes(), self.world.held_store()
        report = self.world.free()
        self.assertEqual(zipped.read_bytes(), before)
        self.assertEqual(dict(self.world.states())[number], freed)
        after = self.world.held_store()
        self.assertEqual({key: after.get(key) for key in held}, held)
        if number == 1:
            self.assertEqual(after, held)
        return report

    def test_an_entry_the_ledger_does_not_know_pins_the_zip(self):
        self.world.opened.delete(self.world.entry_named(ARCHIVE_PAGE))
        self.world.opened.commit()
        self.assert_kept()
        self.assertIn("part 1: unknown_entry", self.world.kind_of_events("export_part_kept"))

    def test_a_failed_entry_pins_the_zip(self):
        self.world.change(self.world.entry(PLACED), outcome="failed")
        self.assert_kept()

    def test_a_placed_file_with_the_wrong_hash_pins_the_zip(self):
        self.world.placed_file().write_bytes(b"not the photo that was placed")
        self.assert_kept()
        self.assertIn("part 1: hash_differs", self.world.kind_of_events("export_part_kept"))

    def test_a_placed_file_that_is_missing_or_a_link_pins_the_zip(self):
        placed = self.world.placed_file()
        moved = placed.with_name("moved.jpg")
        placed.rename(moved)
        self.assert_kept()
        placed.symlink_to(moved)
        self.assert_kept()

    def test_a_held_copy_that_is_missing_pins_the_zip(self):
        waiting = self.world.entry(TIER_2)
        self.world.change(waiting, held_path=f"_inbox/jo/held/{EXPORT}/{waiting.id}.jpg")
        self.assert_kept()

    def test_a_review_hold_whose_hash_differs_pins_its_zip(self):
        self.world.hold_of().write_bytes(b"not the held photo")
        self.assert_kept(number=2)

    def test_a_sidecar_whose_stored_text_is_short_pins_the_zip(self):
        stored = self.world.opened.execute(select(TakeoutSidecar).where(TakeoutSidecar.entry_id == self.world.entry(PLACED).sidecar_entry_id)).scalar_one()
        self.world.change(stored, text=stored.text[:-1])
        self.assert_kept()

    def test_a_kind_darkroom_might_read_pins_the_zip(self):
        self.world.change(self.world.entry_named(ARCHIVE_PAGE), name="Takeout/notes-made-up.txt")
        self.assert_kept()

    def test_a_link_at_the_exports_folder_or_at_the_zip_is_never_followed(self):
        folder = self.world.folder()
        elsewhere = folder.with_name("elsewhere")
        folder.rename(elsewhere)
        folder.symlink_to(elsewhere, target_is_directory=True)
        zips = {path.name: path.read_bytes() for path in elsewhere.glob("*.zip")}
        self.assertEqual(self.world.free().parts, 0)
        self.assertEqual({path.name: path.read_bytes() for path in elsewhere.glob("*.zip")}, zips)
        folder.unlink()
        elsewhere.rename(folder)
        zipped = self.world.zip_of(1)
        real = folder.with_name("real.zip")
        zipped.rename(real)
        zipped.symlink_to(real)
        self.world.free()
        self.assertEqual((dict(self.world.states())[1], real.read_bytes(), zipped.is_symlink()), (None, zips[zipped.name], True))

    def test_a_zip_outside_the_exports_own_folder_is_never_freed(self):
        other = self.world.root / "_inbox/jo/manual"
        other.mkdir(parents=True, exist_ok=True)
        for path in self.world.folder().glob("*.zip"):
            path.rename(other / path.name)
        self.world.change(self.world.opened.get(Export, self.export.id), inbox_dir=str(other))
        zips = {path.name: path.read_bytes() for path in other.glob("*.zip")}
        self.assertEqual(self.world.free().parts, 0)
        self.assertEqual({path.name: path.read_bytes() for path in other.glob("*.zip")}, zips)

    def test_a_part_still_being_ingested_is_never_freed(self):
        with run.account_lock(self.world.settings, self.world.account):
            report = self.world.free()
        self.assertEqual((report.parts, self.world.states()), (0, [(1, None), (2, None)]))
        self.world.change(self.world.part(self.export, 1), ingest_state="ingesting")
        self.assert_kept()

    def test_an_export_being_finished_is_never_freed(self):
        self.world.opened.add(ImportRun(export_id=self.export.id, started=NOW, status="running"))
        self.world.opened.commit()
        self.assert_kept()
        self.assertEqual(self.world.states(), [(1, None), (2, None)])

    def test_an_earlier_export_of_the_account_waiting_pins_the_zip(self):
        self.world.opened.add(Export(account_id=self.world.account, kind="incremental", google_id="google-earlier", created_at=NOW - 2 * DAY, status="downloading"))
        self.world.opened.commit()
        self.assert_kept()
        self.assertEqual(self.world.states(), [(1, None), (2, None)])

    def test_a_re_downloaded_part_whose_listing_differs_is_kept(self):
        zipped = self.world.zip_of(1)
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("Takeout/Google Photos/Photos from 2019/IMG_5001.JPG", b"other bytes")
        self.assert_kept()
        self.assertIn("part 1: listing_differs", self.world.kind_of_events("export_part_kept"))

    def test_a_reversed_or_unmeasured_decision_pins_the_zip(self):
        decision = self.world.opened.get(DedupeDecision, self.world.entry(EXACT).decision_id)
        self.world.change(decision, reversed_at=NOW)
        self.assert_kept()
        self.world.change(decision, reversed_at=None)
        self.world.change(self.world.entry(EXACT), captured_utc=None)
        self.assert_kept()

    def test_a_review_undo_or_reassignment_in_progress_pins_every_zip(self):
        marker = self.world.placed_file().with_name(f"{paths.DROPPING}0123456789abcdef-IMG_5001.JPG")
        marker.write_bytes(b"set aside")
        self.assert_kept()
        self.assertEqual(self.world.states(), [(1, None), (2, None)])


class SwappedFileTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()
        self.real_hold = part_free.hold_waiting
        self.real_remove = paths.remove_part_zip

    def after_holding(self, change):
        def holding(*args):
            done = self.real_hold(*args)
            change()
            return done
        return mock.patch.object(part_free, "hold_waiting", side_effect=holding)

    def kept_with(self, patched, reason, number=1):
        zipped = self.world.zip_of(number)
        before = zipped.read_bytes()
        with patched:
            self.world.free()
        self.assertEqual((dict(self.world.states())[number], zipped.read_bytes()), (None, before))
        self.assertIn(f"part {number}: {reason}", self.world.kind_of_events("export_part_kept"))

    def test_a_placed_file_swapped_after_it_was_hashed_pins_the_zip(self):
        placed = self.world.placed_file()
        self.kept_with(self.after_holding(lambda: swap(placed)), "changed")

    def test_a_held_copy_swapped_after_it_was_written_pins_the_zip(self):
        held = self.world.held_copy()
        self.kept_with(self.after_holding(lambda: swap(held, b"not the held photo")), "hash_differs")

    def test_a_zip_swapped_after_its_first_check_pins_it(self):
        zipped = self.world.zip_of(1)
        self.kept_with(self.after_holding(lambda: swap(zipped)), "changed")

    def test_a_review_hold_swapped_after_it_was_hashed_pins_its_zip(self):
        hold = self.world.hold_of()
        self.kept_with(self.after_holding(lambda: swap(hold)), "changed", number=2)

    def test_a_zip_swapped_just_before_the_delete_is_left_alone(self):
        zipped = self.world.zip_of(1)

        def removing(photos, person, export, filename, identity):
            if filename == zipped.name:
                swap(zipped)
            return self.real_remove(photos, person, export, filename, identity)

        before = zipped.read_bytes()
        with mock.patch.object(part_free.paths, "remove_part_zip", side_effect=removing):
            self.world.free()
        self.assertEqual((dict(self.world.states())[1], zipped.read_bytes()), (None, before))
        self.assertIn("part 1: zip_changed", self.world.kind_of_events("export_part_kept"))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()

    def test_a_crash_after_freeing_and_before_the_delete_finishes_on_the_next_run(self):
        zipped = self.world.zip_of(1)
        with mock.patch.object(part_free.paths, "remove_part_zip", side_effect=Crash), self.assertRaises(Crash):
            self.world.free()
        self.world.reopen()
        self.assertEqual((self.world.states(), zipped.exists()), ([(1, "freeing"), (2, None)], True))
        self.world.free(at=NOW + 2 * TICK)
        self.assertEqual((self.world.states(), zipped.exists()), ([(1, "freed"), (2, "freed")], False))

    def test_a_crash_after_freeing_whose_proof_then_fails_is_refused_and_the_zip_kept(self):
        zipped = self.world.zip_of(1)
        before = zipped.read_bytes()
        with mock.patch.object(part_free.paths, "remove_part_zip", side_effect=Crash), self.assertRaises(Crash):
            self.world.free()
        self.world.reopen()
        self.world.change(self.world.entry(PLACED), outcome="failed")
        self.world.free(at=NOW + 2 * TICK)
        self.assertEqual((dict(self.world.states())[1], zipped.read_bytes()), (None, before))

    def test_a_crash_after_the_delete_and_before_freed_is_recorded_on_the_next_run(self):
        zipped = self.world.zip_of(1)
        size = zipped.stat().st_size
        with mock.patch.object(part_free, "record_freed", side_effect=Crash), self.assertRaises(Crash):
            self.world.free()
        self.world.reopen()
        self.assertEqual((dict(self.world.states())[1], zipped.exists()), ("freeing", False))
        self.world.free(at=NOW + 2 * TICK)
        part = self.world.part(self.export, 1)
        self.assertEqual((part.free_state, part.bytes_freed, part.freed_at), ("freed", size, NOW + 2 * TICK))

    def test_a_part_left_freeing_is_never_recorded_freed_while_its_folder_cannot_be_read(self):
        with mock.patch.object(part_free.paths, "remove_part_zip", side_effect=Crash), self.assertRaises(Crash):
            self.world.free()
        self.world.reopen()
        folder = self.world.folder()
        away = folder.with_name("away")
        folder.rename(away)
        report = self.world.free(at=NOW + 2 * TICK)
        self.assertEqual((report.failed, dict(self.world.states())[1]), (1, "freeing"))
        away.rename(folder)
        self.world.free(at=NOW + 3 * TICK)
        self.assertEqual(self.world.states(), [(1, "freed"), (2, "freed")])

    def test_a_part_left_freeing_whose_zip_became_a_link_a_hard_link_or_a_folder_is_not_recorded_freed(self):
        zipped = self.world.zip_of(1)
        data = zipped.read_bytes()
        real = self.world.root / "_inbox/jo/real.zip"
        changes = {
            "link": lambda: (zipped.rename(real), zipped.symlink_to(real)),
            "hard link": lambda: os.link(zipped, real),
            "folder": lambda: (zipped.rename(real), zipped.mkdir()),
        }
        for kind, change in changes.items():
            with self.subTest(kind=kind):
                self.world.change(self.world.part(self.export, 1), free_state="freeing", free_started_at=NOW)
                change()
                self.world.free(at=NOW + 2 * TICK)
                part = self.world.part(self.export, 1)
                self.assertEqual((part.free_state, part.bytes_freed, os.path.lexists(zipped)), (None, None, True))
                self.assertEqual(real.read_bytes(), data)
                if zipped.is_symlink():
                    zipped.unlink()
                elif zipped.is_dir():
                    zipped.rmdir()
                if not zipped.exists():
                    real.rename(zipped)
                else:
                    real.unlink()
        self.assertEqual(zipped.read_bytes(), data)

    def test_a_held_store_that_cannot_be_opened_is_never_recorded_freed(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        store = self.world.root / "_inbox/jo/held" / EXPORT
        away = store.with_name("away")
        store.rename(away)
        report = self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((report.failed, report.held, self.world.entry(TIER_2).held_state), (1, 0, None))
        away.rename(store)
        self.assertEqual(self.world.held_store(), held)
        self.world.free(at=NOW + 4 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), ("freed", {}))

    def test_a_held_store_left_freeing_that_cannot_be_opened_stays_freeing_until_it_can(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        with mock.patch.object(part_free.paths, "remove_held_copy", side_effect=Crash), self.assertRaises(Crash):
            self.world.free(at=NOW + 3 * TICK)
        self.world.reopen()
        store = self.world.root / "_inbox/jo/held" / EXPORT
        away = store.with_name("away")
        store.rename(away)
        self.world.free(at=NOW + 4 * TICK)
        self.assertEqual(self.world.entry(TIER_2).held_state, "freeing")
        away.rename(store)
        self.assertEqual(self.world.held_store(), held)
        self.world.free(at=NOW + 5 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), ("freed", {}))

    def test_a_held_copy_replaced_by_a_link_or_a_folder_is_not_recorded_freed(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        copy = self.world.held_copy()
        data = copy.read_bytes()
        real = self.world.root / "_inbox/jo/real.jpg"
        copy.rename(real)
        copy.symlink_to(real)
        self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, real.read_bytes(), copy.is_symlink()), (None, data, True))
        copy.unlink()
        copy.mkdir()
        self.world.free(at=NOW + 4 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, copy.is_dir()), (None, True))

    def test_a_crash_while_freeing_the_held_store_finishes_on_the_next_run(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        with mock.patch.object(part_free.paths, "remove_held_copy", side_effect=Crash), self.assertRaises(Crash):
            self.world.free(at=NOW + 3 * TICK)
        self.world.reopen()
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), ("freeing", held))
        self.world.free(at=NOW + 4 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), ("freed", {}))

    def test_the_held_store_is_kept_when_a_held_item_is_no_longer_safely_decided(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        decision = self.world.opened.get(DedupeDecision, self.world.entry(TIER_2).decision_id)
        self.world.change(decision, reversed_at=NOW)
        self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), (None, held))
        self.world.change(decision, reversed_at=None)
        self.world.change(self.world.entry(PLACED), outcome="failed")
        self.world.free(at=NOW + 4 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), (None, held))


if __name__ == "__main__":
    unittest.main()
