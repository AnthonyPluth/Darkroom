import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from darkroom.domain.ingest import unpack
from darkroom.storage import db
from darkroom.storage.models import Event, Export, ExportPart, GoogleAccount, Person
from darkroom.storage.paths import Photos
from tests.shared import add_person, own_database
from tests.takeout_fixtures import TakeoutBuilder, standard_export


class UnpackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = Photos(self.root / "photos")
        self.engine = own_database(self)
        with db.session(self.engine) as opened:
            person_id = add_person(opened, "jo")
            opened.add(GoogleAccount(email="jo@example.com", default_person_id=person_id, created=1.0))
        self.inbox = self.photos.manual_inbox("jo")
        self.inbox.mkdir(parents=True)

    def person(self, opened):
        return opened.query(Person).one()

    def run_inbox(self, now=10.0):
        with db.session(self.engine) as opened:
            person = self.person(opened)
            exports = unpack.inspect_manual_inbox(opened, self.photos, person, now)
            return [(e.id, unpack.unpack(opened, self.photos, person, e, now)) for e in exports]

    def export_state(self):
        with db.session(self.engine) as opened:
            return [(e.status, e.stage) for e in opened.query(Export)]

    def events(self):
        with db.session(self.engine) as opened:
            return [e.type for e in opened.query(Event).order_by(Event.id)]

    def staging(self):
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            return unpack.staging_dir(self.photos, self.person(opened), export)

    def test_merges_split_parts_into_one_tree(self):
        manifest = standard_export(self.inbox)
        ((_, report),) = self.run_inbox()
        self.assertEqual(report.parts_unpacked, 2)
        self.assertEqual(report.problems, [])
        staging = self.staging()
        for name in manifest.media:
            self.assertTrue((staging / name).is_file(), name)
        for sidecar in manifest.pairings.values():
            if sidecar:
                self.assertTrue((staging / sidecar).is_file(), sidecar)
        self.assertEqual(self.export_state(), [("unpacking", "unpacked")])
        self.assertEqual(self.events(), ["unpacked"])

    def test_rerun_skips_finished_parts(self):
        standard_export(self.inbox)
        self.run_inbox()
        ((_, again),) = self.run_inbox(now=20.0)
        self.assertEqual((again.parts_unpacked, again.parts_skipped, again.files), (0, 2, 0))

    def test_interrupted_run_resumes_without_rewriting_finished_files(self):
        manifest = standard_export(self.inbox)
        self.run_inbox()
        staging = self.staging()
        (staging / unpack.UNPACKED_MARKER_FOLDER / "0002").unlink()
        before = {p: p.stat().st_mtime_ns for p in staging.rglob("*") if p.is_file()}
        ((_, again),) = self.run_inbox(now=20.0)
        self.assertEqual((again.parts_unpacked, again.parts_skipped), (1, 1))
        self.assertEqual(again.files, 0)
        for path, stamp in before.items():
            if path.parent.name != unpack.UNPACKED_MARKER_FOLDER:
                self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(len(manifest.parts), 2)

    def test_empty_part_fails_the_export_and_stages_nothing(self):
        builder = TakeoutBuilder(self.inbox, parts=2)
        builder.photo("IMG_1.JPG")
        builder.photo("IMG_2.JPG", part=1)
        builder.zero_byte_part = 1
        builder.build()
        ((_, report),) = self.run_inbox()
        self.assertEqual(len(report.problems), 1)
        self.assertIn("empty", report.problems[0])
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertEqual(self.events(), ["unpack_failed"])
        self.assertFalse(self.staging().exists())
        with db.session(self.engine) as opened:
            self.assertEqual([p.status for p in opened.query(ExportPart).order_by(ExportPart.number)], ["done", "corrupt"])

    def test_corrupt_part_fails_the_export(self):
        builder = TakeoutBuilder(self.inbox)
        builder.photo("IMG_1.JPG")
        builder.corrupt_part = 0
        builder.build()
        ((_, report),) = self.run_inbox()
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertTrue(report.problems)
        self.assertFalse(self.staging().exists())

    def test_not_a_zip_fails_the_export(self):
        (self.inbox / "takeout-20260101T000000Z-001.zip").write_bytes(b"not a zip at all")
        ((_, report),) = self.run_inbox()
        self.assertIn("not a valid zip", report.problems[0])

    def test_missing_part_fails_the_export(self):
        builder = TakeoutBuilder(self.inbox, parts=3)
        builder.photo("IMG_1.JPG")
        builder.build()
        (self.inbox / "takeout-20260101T000000Z-002.zip").unlink()
        ((_, report),) = self.run_inbox()
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertEqual(report.problems, ["part 2 of 3 is missing"])

    def test_part_removed_after_registration_is_reported_missing(self):
        standard_export(self.inbox)
        with db.session(self.engine) as opened:
            person = self.person(opened)
            (export,) = unpack.inspect_manual_inbox(opened, self.photos, person, 1.0)
        (self.inbox / "takeout-20260101T000000Z-002.zip").unlink()
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            report = unpack.unpack(opened, self.photos, self.person(opened), export, 2.0)
        self.assertIn("not in the inbox", report.problems[0])

    def test_gap_in_part_numbers_names_the_missing_part(self):
        standard_export(self.inbox, parts=2)
        with db.session(self.engine) as opened:
            person = self.person(opened)
            (export,) = unpack.inspect_manual_inbox(opened, self.photos, person, 1.0)
            export.part_count = 3
            report = unpack.unpack(opened, self.photos, person, export, 2.0)
        self.assertIn("part 3 of 3 is missing", report.problems[0])

    def test_traversal_entry_is_rejected(self):
        path = self.inbox / "takeout-20260101T000000Z-001.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Takeout/ok.txt", b"fine")
            archive.writestr("../escape.txt", b"bad")
        ((_, report),) = self.run_inbox()
        self.assertIn("outside the staging folder", report.problems[0])
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertFalse((self.inbox.parent / "escape.txt").exists())
        self.assertFalse(list(self.root.rglob("escape.txt")))

    def test_absolute_entry_is_rejected(self):
        path = self.inbox / "takeout-20260101T000000Z-001.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("/tmp/darkroom-absolute.txt", b"bad")
        ((_, report),) = self.run_inbox()
        self.assertIn("outside the staging folder", report.problems[0])
        self.assertFalse(Path("/tmp/darkroom-absolute.txt").exists())

    def test_symlink_entry_is_rejected(self):
        path = self.inbox / "takeout-20260101T000000Z-001.zip"
        with zipfile.ZipFile(path, "w") as archive:
            link = zipfile.ZipInfo("Takeout/link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "/etc/passwd")
        ((_, report),) = self.run_inbox()
        self.assertIn("symbolic link", report.problems[0])

    def test_safe_target_accepts_ordinary_names(self):
        staging = self.root / "stage"
        staging.mkdir()
        target = unpack.safe_target(staging, "Takeout/Google Photos/a.jpg")
        self.assertEqual(target, staging.resolve() / "Takeout/Google Photos/a.jpg")
        with self.assertRaises(unpack.UnpackFailed):
            unpack.safe_target(staging, "")
        with self.assertRaises(unpack.UnpackFailed):
            unpack.safe_target(staging, "a\\..\\..\\b")

    def test_safe_target_rejects_a_path_through_a_symlink(self):
        staging = self.root / "stage"
        staging.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, staging / "hop")
        with self.assertRaises(unpack.UnpackFailed):
            unpack.safe_target(staging, "hop/file.txt")

    def test_directory_entries_are_created(self):
        path = self.inbox / "takeout-20260101T000000Z-001.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Takeout/empty/", b"")
            archive.writestr("Takeout/a.txt", b"a")
        self.run_inbox()
        self.assertTrue((self.staging() / "Takeout/empty").is_dir())

    def test_registering_twice_does_not_duplicate(self):
        standard_export(self.inbox)
        self.run_inbox()
        self.run_inbox(now=20.0)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(Export).count(), 1)
            self.assertEqual(opened.query(ExportPart).count(), 2)

    def test_single_zip_without_a_part_number_is_one_part(self):
        TakeoutBuilder(self.inbox).photo("IMG_1.JPG")
        folder = self.inbox / "sub"
        folder.mkdir()
        with zipfile.ZipFile(folder / "photos.zip", "w") as archive:
            archive.writestr("Takeout/a.txt", b"a")
        groups = unpack.group_manual_zips(self.inbox)
        self.assertEqual(groups[(folder, "photos")], {1: folder / "photos.zip"})
        self.assertEqual(unpack.group_manual_zips(self.root / "missing"), {})

    def test_person_without_an_account_cannot_register(self):
        with db.session(self.engine) as opened:
            add_person(opened, "al")
            other = opened.query(Person).filter(Person.slug == "al").one()
            with self.assertRaises(unpack.UnpackFailed):
                unpack.register_zips(opened, other, self.inbox, 1.0)
