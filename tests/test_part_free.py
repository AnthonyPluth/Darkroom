import contextlib
import hashlib
import os
import unittest
import zipfile
from unittest import mock

from sqlalchemy import select

from darkroom.domain import exports
from darkroom.domain.ingest import part_free, part_ingest, run
from darkroom.storage import paths
from darkroom.storage.models import (
    DedupeDecision, GoogleAccount, Person, Event, Export, ExportPart, ImportRun, ManagedImportIntent, MediaItem, ReviewItem, TakeoutEntry, TakeoutSidecar,
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

    def test_a_zip_that_cannot_be_read_pins_it(self):
        with mock.patch.object(part_free.paths, "open_inbox_file", side_effect=OSError("made-up read error")):
            self.assert_kept()

    def test_a_waiting_entry_without_its_held_copy_is_never_proven_for_the_delete(self):
        export = self.world.opened.get(Export, self.export.id)
        with self.assertRaises(part_free.Refused) as refused:
            part_free.prove_part(self.world.opened, self.world.photos, run.owner_person(self.world.opened, export), export, self.world.part(self.export, 1),
                                 NOW + TICK, hashing=False, require_held=True)
        self.assertEqual(refused.exception.reason, "held_missing")
        self.world.opened.rollback()
        self.assertEqual(self.world.states(), [(1, None), (2, None)])

    def test_a_zip_gone_before_its_proof_is_not_recorded_freed(self):
        export = self.world.opened.get(Export, self.export.id)
        self.world.zip_of(1).unlink()
        with self.assertRaises(part_free.Refused) as refused:
            part_free.listing(self.world.photos, run.owner_person(self.world.opened, export), export, self.world.part(self.export, 1))
        self.assertEqual(refused.exception.reason, "zip_missing")
        self.world.free()
        self.assertEqual(dict(self.world.states())[1], None)

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


class ProofDetailTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()

    def kept_for(self, reason, number=1):
        zipped = self.world.zip_of(number)
        before = zipped.read_bytes()
        self.world.opened.commit()
        try:
            part_free.prove_part(self.world.opened, self.world.photos, self.owner(), self.world.opened.get(Export, self.export.id),
                                 self.world.part(self.export, number), NOW + TICK, hashing=True, require_held=False)
        except part_free.Refused as refused:
            self.assertEqual(refused.reason, reason)
        else:
            self.fail(f"the proof held, but {reason} was expected")
        finally:
            self.world.opened.rollback()
        self.assertEqual((zipped.read_bytes(), dict(self.world.states())[number]), (before, None))

    def owner(self):
        return run.owner_person(self.world.opened, self.world.opened.get(Export, self.export.id))

    def test_a_ledger_entry_the_zip_does_not_hold_differs_from_the_listing(self):
        self.world.opened.add(TakeoutEntry(export_id=self.export.id, part_number=1, name="Takeout/empty-folder/", kind="other", bytes=0, crc32=0,
                                           outcome="recorded", listed_at=NOW))
        self.world.opened.commit()
        self.kept_for("listing_differs")

    def test_a_skipped_entry_without_a_matched_item_or_its_pending_sidecar_text_is_not_proven(self):
        exact = self.world.entry(EXACT)
        self.world.change(exact, metadata_pending=True, sidecar_entry_id=None)
        self.kept_for("metadata_unrecorded")
        self.world.change(exact, metadata_pending=False)
        self.world.change(self.world.opened.get(DedupeDecision, exact.decision_id), matched_item_id=None)
        self.kept_for("decision_unsafe")

    def test_a_review_whose_recorded_hash_differs_or_whose_hold_is_gone_is_not_proven(self):
        review = self.world.opened.execute(select(ReviewItem).where(ReviewItem.decision_id == self.world.entry(REVIEWED).decision_id)).scalar_one()
        hold_sha1 = review.hold_sha1
        self.world.change(review, hold_sha1="0" * 40)
        self.kept_for("review_differs", number=2)
        self.world.change(review, hold_sha1=hold_sha1)
        self.world.hold_of().unlink()
        self.kept_for("file_missing", number=2)

    def test_a_placed_entry_whose_item_differs_is_not_proven(self):
        item = self.world.opened.get(MediaItem, self.world.entry(PLACED).item_id)
        self.world.change(item, sha1="0" * 40)
        self.kept_for("placed_differs")
        self.world.change(self.world.entry(PLACED), item_id=self.world.entry("Photos from 2019/IMG_5002.JPG").item_id)
        self.kept_for("decision_unsafe")

    def test_a_held_path_outside_the_held_store_or_an_undecided_entry_is_not_proven(self):
        waiting = self.world.entry(TIER_2)
        self.world.change(waiting, held_path=f"_inbox/jo/review/{EXPORT}/{waiting.id}.jpg")
        self.kept_for("held_differs")
        self.world.change(waiting, held_path=None, outcome="listed")
        self.kept_for("undecided_entry")

    def test_a_copy_is_proven_by_its_item_and_a_copy_of_nothing_is_not(self):
        root, other = self.world.entry(PLACED), self.world.entry("Photos from 2019/IMG_5002.JPG")
        self.world.change(other, outcome="copy", copy_of_id=None)
        self.kept_for("undecided_entry", number=2)
        self.world.change(other, copy_of_id=root.id)
        self.kept_for("undecided_entry", number=2)
        self.world.change(other, sha1=root.sha1)
        self.world.free()
        self.assertEqual(dict(self.world.states())[2], "freed")

    def test_a_held_copy_already_there_with_other_bytes_is_never_replaced(self):
        waiting = self.world.entry(TIER_2)
        target = self.world.held_copy()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"other held bytes")
        self.world.free()
        self.assertEqual((dict(self.world.states())[1], target.read_bytes(), self.world.entry(TIER_2).held_path), (None, b"other held bytes", None))
        self.assertIn("part 1: held_differs", self.world.kind_of_events("export_part_kept"))
        self.assertEqual(waiting.outcome, "waiting_export")

    def test_an_export_whose_import_is_not_done_or_that_failed_is_busy(self):
        export = self.world.opened.get(Export, self.export.id)
        self.world.change(export, status="imported")
        self.world.opened.add(ImportRun(export_id=export.id, started=NOW, status="failed"))
        self.world.opened.commit()
        self.kept_for("export_busy")
        self.world.change(export, status="failed")
        self.kept_for("export_busy")

    def test_an_export_without_an_owner_fails_the_attempt_and_frees_nothing(self):
        zips = self.world.zips()
        with mock.patch.object(part_free.run, "owner_person", side_effect=run.StageFailed("the export's account has no default person")):
            report = self.world.free()
        self.assertEqual((report.failed, report.parts, self.world.zips()), (1, 0, zips))
        self.assertEqual(self.world.kind_of_events("export_free_failed"), ["StageFailed"])

    def test_a_missing_exports_folder_has_no_zip_to_free(self):
        folder = self.world.folder()
        folder.rename(folder.with_name("away"))
        export = self.world.opened.get(Export, self.export.id)
        self.assertFalse(part_free.zip_present(self.world.photos, self.owner(), export, self.world.part(self.export, 1)))
        self.assertEqual(self.world.free().parts, 0)

    def test_a_second_pass_that_differs_from_the_first_refuses(self):
        path = self.world.placed_file()
        identity = paths.file_identity(path.lstat())
        found = part_free.Checked(path, "0" * 40, identity, in_the_inbox=False)
        cases = [(part_free.Proof(None, {}), part_free.Proof(None, {})),
                 (part_free.Proof(identity, {path: found}), part_free.Proof(identity, {})),
                 (part_free.Proof(identity, {path: found}), part_free.Proof(identity, {path: part_free.Checked(path, "1" * 40, identity, False)}))]
        for first, second in cases:
            with self.subTest(first=first), self.assertRaises(part_free.Refused) as refused:
                part_free.confirm(self.world.photos, first, second)
            self.assertEqual(refused.exception.reason, "changed")
        with self.assertRaises(part_free.Refused) as refused:
            part_free.hashed(self.world.photos, part_free.Checked(path.with_name("absent.jpg"), "0" * 40, identity, False))
        self.assertEqual(refused.exception.reason, "file_missing")
        with self.assertRaises(part_free.Refused) as refused:
            part_free.hashed(self.world.photos, found)
        self.assertEqual(refused.exception.reason, "hash_differs")


class ClauseTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()

    def found(self):
        export = self.world.opened.get(Export, self.export.id)
        return export, run.owner_person(self.world.opened, export)

    def refused(self, reason, number=1, require_held=False):
        before = self.world.zips()
        export, owner = self.found()
        try:
            part_free.prove_part(self.world.opened, self.world.photos, owner, export, self.world.part(self.export, number), NOW + TICK, hashing=True,
                                 require_held=require_held)
        except part_free.Refused as refused:
            found = refused.reason
        else:
            found = None
        finally:
            self.world.opened.rollback()
        self.assertEqual((found, self.world.zips(), dict(self.world.states())[number]), (reason, before, None))

    @contextlib.contextmanager
    def changed(self, row, **values):
        old = {key: getattr(row, key) for key in values}
        self.world.change(row, **values)
        try:
            yield
        finally:
            self.world.change(row, **old)

    def decision(self, name):
        return self.world.opened.get(DedupeDecision, self.world.entry(name).decision_id)

    def test_each_clause_of_a_placed_entrys_proof_refuses_on_its_own(self):
        placed, decision = self.world.entry(PLACED), self.decision(PLACED)
        item = self.world.opened.get(MediaItem, placed.item_id)
        cases = [
            (placed, {"decision_id": None}, "decision_unsafe"),
            (decision, {"reversed_at": NOW}, "decision_unsafe"),
            (decision, {"decided_by": "owner"}, "decision_unsafe"),
            (decision, {"tier": 4}, "decision_unsafe"),
            (decision, {"takeout_item_id": None}, "decision_unsafe"),
            (item, {"source": "phone"}, "placed_differs"),
            (item, {"sha1": "0" * 40}, "placed_differs"),
            (placed, {"sha1": None}, "placed_differs"),
            (item, {"person_id": self.world.ids["sam"]}, "placed_differs"),
            (item, {"path": item.path.replace("google-only", "originals", 1)}, "file_missing"),
        ]
        for row, values, reason in cases:
            with self.subTest(values=values), self.changed(row, **values):
                self.refused(reason)

    def test_a_placed_file_in_another_persons_folder_refuses(self):
        item = self.world.opened.get(MediaItem, self.world.entry(PLACED).item_id)
        sam = self.world.opened.get(Person, self.world.ids["sam"])
        moved = item.path.replace("jo/", f"{sam.slug}/", 1)
        (self.world.root / moved).parent.mkdir(parents=True, exist_ok=True)
        os.link(self.world.root / item.path, self.world.root / moved)
        (self.world.root / item.path).unlink()
        with self.changed(item, path=moved):
            self.refused("placed_differs")

    def test_a_placed_file_the_identity_check_cannot_reach_refuses(self):
        with mock.patch.object(part_free.paths, "library_identity", side_effect=paths.LinkRefused("made-up link")):
            self.refused("changed")
        with mock.patch.object(part_free.paths, "plain_fingerprint", side_effect=paths.LinkRefused("made-up link")):
            self.refused("changed")

    def test_each_clause_of_a_review_entrys_proof_refuses_on_its_own(self):
        reviewed = self.world.entry(REVIEWED)
        review = self.world.opened.execute(select(ReviewItem).where(ReviewItem.decision_id == reviewed.decision_id)).scalar_one()
        for row, values, reason in [(reviewed, {"decision_id": None}, "review_differs"), (review, {"hold_sha1": None}, "review_differs"),
                                    (review, {"hold_sha1": "0" * 40}, "review_differs"), (review, {"hold_path": "_inbox/jo/held/x/1.jpg"}, "file_missing")]:
            with self.subTest(values=values), self.changed(row, **values):
                self.refused(reason, number=2)

    def test_each_clause_of_a_skipped_entrys_proof_refuses_on_its_own(self):
        exact, decision = self.world.entry(EXACT), self.decision(EXACT)
        cases = [
            (exact, {"decision_id": None}, "decision_unsafe"),
            (decision, {"decided_by": "owner"}, "decision_unsafe"),
            (decision, {"reversed_at": NOW}, "decision_unsafe"),
            (decision, {"tier": 4}, "decision_unsafe"),
            (decision, {"tier": 5}, "decision_unsafe"),
            (decision, {"matched_item_id": None}, "decision_unsafe"),
            (exact, {"metadata_pending": True, "sidecar_entry_id": None}, "metadata_unrecorded"),
        ]
        for row, values, reason in cases:
            with self.subTest(values=values), self.changed(row, **values):
                self.refused(reason)

    def test_a_skipped_entry_whose_matched_item_was_deleted_refuses(self):
        decision = self.decision(EXACT)
        self.world.opened.delete(self.world.opened.get(MediaItem, decision.matched_item_id))
        self.world.opened.commit()
        self.refused("decision_unsafe")

    def test_each_clause_of_a_held_entrys_proof_refuses_on_its_own(self):
        waiting = self.world.entry(TIER_2)
        self.refused("held_missing", require_held=True)
        with self.changed(waiting, held_path=f"_inbox/jo/held/{EXPORT}/{waiting.id}.jpg", sha1=None):
            self.refused("held_missing", require_held=True)
        with self.changed(waiting, held_path=f"_inbox/jo/held/export-9/{waiting.id}.jpg"):
            self.refused("held_differs")
        with self.changed(waiting, held_path=f"_inbox/jo/held/{EXPORT}/{waiting.id}.jpg"):
            self.refused("file_missing")

    def test_each_clause_of_a_copys_proof_refuses_on_its_own(self):
        root, copy = self.world.entry(PLACED), self.world.entry("Photos from 2019/IMG_5002.JPG")
        sidecar = self.world.opened.get(TakeoutEntry, root.sidecar_entry_id)
        failed = self.world.entry(EXACT)
        other = Export(account_id=self.world.account, kind="incremental", google_id="google-other", created_at=NOW - 3 * DAY, status="imported")
        self.world.opened.add(other)
        self.world.opened.flush()
        self.world.opened.add(ExportPart(export_id=other.id, number=1, filename="other.zip", status="done"))
        self.world.opened.flush()
        elsewhere = TakeoutEntry(export_id=other.id, part_number=1, name="Takeout/elsewhere.jpg", kind="media", bytes=1, crc32=1, outcome="placed", sha1=root.sha1,
                                 listed_at=NOW)
        self.world.opened.add(elsewhere)
        self.world.opened.commit()
        cases = [
            ({"copy_of_id": None}, "undecided_entry"),
            ({"copy_of_id": elsewhere.id, "sha1": root.sha1}, "undecided_entry"),
            ({"copy_of_id": root.id, "sha1": "0" * 40}, "undecided_entry"),
            ({"copy_of_id": copy.id, "sha1": copy.sha1}, "undecided_entry"),
        ]
        for values, reason in cases:
            with self.subTest(values=values), self.changed(copy, outcome="copy", **values):
                self.refused(reason, number=2)
        with self.changed(sidecar, sha1=root.sha1), self.changed(copy, outcome="copy", copy_of_id=sidecar.id, sha1=root.sha1):
            self.refused("undecided_entry", number=2)
        with self.changed(failed, outcome="failed"), self.changed(copy, outcome="copy", copy_of_id=failed.id, sha1=failed.sha1):
            self.refused("failed_entry", number=2)
        with self.changed(root, sha1=None), self.changed(copy, outcome="copy", copy_of_id=root.id, sha1=None):
            self.refused("undecided_entry", number=2)

    def test_each_clause_about_the_parts_other_entries_refuses_on_its_own(self):
        sidecar = self.world.opened.get(TakeoutEntry, self.world.entry(PLACED).sidecar_entry_id)
        page = self.world.entry_named(ARCHIVE_PAGE)
        for row, values, reason in [(sidecar, {"outcome": "listed"}, "undecided_entry"), (page, {"outcome": "listed"}, "undecided_entry"),
                                    (page, {"name": "Takeout/notes/"}, "other_kept"), (self.world.entry(EXACT), {"captured_utc": None}, "measure_pending"),
                                    (self.world.entry(TIER_2), {"outcome": "listed"}, "undecided_entry")]:
            with self.subTest(values=values), self.changed(row, **values):
                self.refused(reason)
        stored = self.world.opened.execute(select(TakeoutSidecar).where(TakeoutSidecar.entry_id == sidecar.id)).scalar_one()
        with self.changed(stored, text=stored.text.replace("photoTakenTime", "photoTakenTimE")):
            self.refused("sidecar_unrecorded")
        self.world.opened.delete(stored)
        self.world.opened.commit()
        self.refused("sidecar_unrecorded")

    def test_each_clause_about_the_part_and_its_export_refuses_on_its_own(self):
        part, export = self.world.part(self.export, 1), self.world.opened.get(Export, self.export.id)
        for row, values, reason in [(part, {"ingested_at": None}, "not_settled"), (part, {"status": "failed"}, "not_settled"),
                                    (part, {"ingest_state": "refused"}, "not_settled"), (part, {"filename": "notes.txt"}, "not_export_folder"),
                                    (export, {"stage": "deduped"}, "export_busy"), (export, {"status": "imported"}, "export_busy"),
                                    (export, {"status": "expired"}, "export_busy")]:
            with self.subTest(values=values), self.changed(row, **values):
                self.refused(reason)

    def test_each_clause_about_the_zips_listing_refuses_on_its_own(self):
        zipped = self.world.zip_of(1)
        data = zipped.read_bytes()
        zipped.write_bytes(b"\0" * len(data))
        self.refused("listing_differs")
        zipped.write_bytes(data)
        with mock.patch.object(part_free.part_reader, "entry_listing", side_effect=part_free.part_reader.PartRefused("duplicate_name", "made-up")):
            self.refused("listing_differs")
        page = self.world.entry_named(ARCHIVE_PAGE)
        with self.changed(page, crc32=(page.crc32 + 1) % 2**32):
            self.refused("unknown_entry")

    def test_the_held_stores_own_clauses_refuse_on_their_own(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        waiting = self.world.entry(TIER_2)
        export = self.world.opened.get(Export, self.export.id)
        with self.changed(export, status="downloading"), self.assertRaises(part_free.Refused) as refused:
            part_free.prove_held(self.world.opened, self.world.photos, export, [waiting], hashing=True)
        self.assertEqual(refused.exception.reason, "export_busy")
        self.world.opened.rollback()
        found = part_free.prove_held(self.world.opened, self.world.photos, export, [self.world.entry(TIER_2)], hashing=True)
        self.world.opened.rollback()
        self.assertEqual((len(found.ready), found.kept), (1, {}))
        with self.changed(self.world.entry(TIER_2), metadata_pending=True):
            found = part_free.prove_held(self.world.opened, self.world.photos, export, [self.world.entry(TIER_2)], hashing=True)
            self.world.opened.rollback()
            self.assertEqual((found.ready, found.kept), ([], {"metadata_unrecorded": 1}))
        marker = self.world.placed_file().with_name(f"{paths.PLACING}0123456789abcdef.json")
        marker.write_bytes(b"{}")
        self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), (None, held))
        marker.unlink()
        with mock.patch.object(part_free.paths, "remove_held_copy", side_effect=paths.NotAHeldCopy("made-up change")):
            self.world.free(at=NOW + 4 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), (None, held))
        self.assertIn("held copies: changed", self.world.kind_of_events("export_held_kept"))
        self.world.free(at=NOW + 5 * TICK)
        self.assertEqual((self.world.entry(TIER_2).held_state, self.world.held_store()), ("freed", {}))

    def test_a_held_copy_with_no_decision_of_its_own_is_kept_and_counted_and_the_others_still_go(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        with self.changed(self.world.entry(TIER_2), decision_id=None):
            report = self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((report.held, report.kept, self.world.held_store()), (0, 1, held))
        self.assertEqual(self.world.kind_of_events("export_held_kept"), ["1 held copies: decision_unsafe"])

    def test_the_held_store_writers_own_refusals(self):
        waiting = self.world.entry(TIER_2)
        rows = part_free.part_rows(self.world.opened, self.world.opened.get(Export, self.export.id), self.world.part(self.export, 2))
        with self.assertRaises(part_free.Refused) as refused:
            part_free.source_of(waiting, rows, 2)
        self.assertEqual(refused.exception.reason, "held_missing")
        with self.assertRaises(part_free.Refused) as refused:
            list(part_free.entry_chunks(None, {}, waiting, 1))
        self.assertEqual(refused.exception.reason, "listing_differs")
        self.world.zip_of(1).unlink()
        export, owner = self.found()
        with self.assertRaises(part_free.Refused) as refused:
            part_free.hold_waiting(self.world.opened, self.world.photos, owner, export, self.world.part(self.export, 1))
        self.assertEqual(refused.exception.reason, "zip_missing")


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
        part = self.world.opened.execute(select(ExportPart).where(ExportPart.number == 1)).scalar_one()
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

    def test_a_part_left_freeing_behind_a_linked_folder_fails_only_its_own_export_and_other_accounts_still_ingest(self):
        with mock.patch.object(part_free.paths, "remove_part_zip", side_effect=Crash), self.assertRaises(Crash):
            self.world.free()
        self.world.reopen()
        other = GoogleAccount(email="other@x.test", default_person_id=self.world.ids["sam"], created=1.0)
        self.world.opened.add(other)
        self.world.opened.commit()
        later = self.world.create(lambda builder: builder.photo("IMG_7001.JPG", part=0), parts=1, google_id="export-2")
        self.world.change(self.world.opened.get(Export, later.id), account_id=other.id)
        folder = self.world.folder()
        away = folder.with_name("away")
        folder.rename(away)
        folder.symlink_to(away, target_is_directory=True)
        with no_leaks(self, "IMG_7001", "IMG_5001"):
            report = self.world.free(at=NOW + 2 * TICK)
        self.assertEqual((report.failed, self.world.states()[0]), (1, (1, "freeing")))
        self.assertTrue(all(path.exists() for path in away.glob("*.zip")))
        ran = part_ingest.run_queued(self.world.opened, self.world.settings, NOW + 2 * TICK)
        self.assertEqual(ran, [(later.id, 1)])
        self.assertEqual(self.world.states()[0], (1, "freeing"))

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
