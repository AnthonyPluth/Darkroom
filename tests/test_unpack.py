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
        (staging.parent / unpack.UNPACKED_MARKER_FOLDER / "0002").unlink()
        before = {p: p.stat().st_mtime_ns for p in staging.rglob("*") if p.is_file()}
        ((_, again),) = self.run_inbox(now=20.0)
        self.assertEqual((again.parts_unpacked, again.parts_skipped), (1, 1))
        self.assertEqual(again.files, 0)
        for path, stamp in before.items():
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

    def test_an_archive_cannot_forge_a_resume_marker(self):
        first = self.inbox / "takeout-20260101T000000Z-001.zip"
        second = self.inbox / "takeout-20260101T000000Z-002.zip"
        with zipfile.ZipFile(first, "w") as archive:
            archive.writestr(f"{unpack.UNPACKED_MARKER_FOLDER}/0002", b"done")
            archive.writestr("Takeout/a.txt", b"a")
        with zipfile.ZipFile(second, "w") as archive:
            archive.writestr("Takeout/b.txt", b"b")
        ((_, report),) = self.run_inbox()
        self.assertEqual(report.parts_unpacked, 2)
        self.assertTrue((self.staging() / "Takeout/b.txt").is_file())

    def test_events_never_carry_archive_file_names(self):
        (self.inbox / "private-holiday-name-001.zip").write_bytes(b"junk")
        ((_, report),) = self.run_inbox()
        self.assertIn("private-holiday-name", report.problems[0])
        with db.session(self.engine) as opened:
            details = [e.detail for e in opened.query(Event)]
        self.assertTrue(details)
        self.assertFalse(any("private" in (d or "") for d in details))

    def test_a_disk_error_fails_the_export_and_records_an_event(self):
        from unittest import mock

        standard_export(self.inbox)
        with mock.patch("shutil.copyfileobj", side_effect=OSError(28, "No space left on device")):
            ((_, report),) = self.run_inbox()
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertEqual(self.events(), ["unpack_failed"])
        self.assertTrue(report.problems)

    def test_several_accounts_need_an_explicit_choice(self):
        with db.session(self.engine) as opened:
            person = self.person(opened)
            opened.add(GoogleAccount(email="jo2@example.com", default_person_id=person.id, created=2.0))
        standard_export(self.inbox)
        with db.session(self.engine) as opened:
            person = self.person(opened)
            with self.assertRaises(unpack.UnpackFailed):
                unpack.inspect_manual_inbox(opened, self.photos, person, 1.0)
            with self.assertRaises(unpack.UnpackFailed):
                unpack.inspect_manual_inbox(opened, self.photos, person, 1.0, "nobody@example.com")
            (export,) = unpack.inspect_manual_inbox(opened, self.photos, person, 1.0, "jo2@example.com")
            self.assertEqual(opened.get(GoogleAccount, export.account_id).email, "jo2@example.com")

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

    def registered(self):
        with db.session(self.engine) as opened:
            (export,) = unpack.inspect_manual_inbox(opened, self.photos, self.person(opened), 1.0)
            return self.photos.root / "_inbox/jo" / unpack.staging_name(export)

    def unpack_again(self, now=2.0):
        with db.session(self.engine) as opened:
            return unpack.unpack(opened, self.photos, self.person(opened), opened.query(Export).one(), now)

    def originals(self):
        folder = self.photos.root / "jo/originals"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def originals_tree(self):
        return {str(p.relative_to(self.originals())): p.read_bytes() for p in self.originals().rglob("*") if p.is_file()}

    def one_zip(self, entries):
        with zipfile.ZipFile(self.inbox / "takeout-20260101T000000Z-001.zip", "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)

    def assert_refused_and_originals_kept(self, report, before):
        self.assertEqual(report.problems, [unpack.LINK_IN_THE_INBOX])
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertEqual(self.events(), ["unpack_failed"])
        self.assertEqual(self.originals_tree(), before)

    def test_a_linked_staging_folder_is_refused_and_nothing_lands_in_originals(self):
        standard_export(self.inbox)
        export = self.registered()
        export.mkdir()
        (export / "staging").symlink_to(self.originals(), target_is_directory=True)
        self.assert_refused_and_originals_kept(self.unpack_again(), {})
        self.assertFalse((self.photos.root / "jo" / unpack.UNPACKED_MARKER_FOLDER).exists())

    def test_a_linked_export_folder_is_refused(self):
        standard_export(self.inbox)
        export = self.registered()
        (self.photos.root / "jo/staging").mkdir(parents=True)
        export.symlink_to(self.photos.root / "jo", target_is_directory=True)
        self.assert_refused_and_originals_kept(self.unpack_again(), {})
        self.assertEqual(list((self.photos.root / "jo/staging").iterdir()), [])

    def test_a_linked_folder_inside_staging_is_never_written_through(self):
        self.one_zip([("Takeout/a.txt", b"a")])
        staging = self.registered() / "staging"
        staging.mkdir(parents=True)
        (staging / "Takeout").symlink_to(self.originals(), target_is_directory=True)
        self.assert_refused_and_originals_kept(self.unpack_again(), {})

    def planted(self, plant):
        kept = self.originals() / "keep.jpg"
        kept.write_bytes(b"an original")
        before = self.originals_tree()
        self.one_zip([("Takeout/a.txt", b"longer than the original")])
        staging = self.registered() / "staging"
        (staging / "Takeout").mkdir(parents=True)
        plant(kept, staging / "Takeout/a.txt")
        self.assert_refused_and_originals_kept(self.unpack_again(), before)

    def test_a_linked_file_inside_staging_is_never_written_through(self):
        self.planted(lambda kept, place: place.symlink_to(kept))

    def test_a_hard_linked_file_inside_staging_is_never_written_through(self):
        self.planted(lambda kept, place: os.link(kept, place))

    def test_a_folder_swapped_for_a_link_between_check_and_use_is_never_written_through(self):
        from unittest import mock

        self.one_zip([("Takeout/a.txt", b"a"), ("Takeout/b.txt", b"b")])
        staging = self.registered() / "staging"
        real = unpack.safe_target

        def swapping(folder, name):
            checked = real(folder, name)
            if name == "Takeout/b.txt":
                (staging / "Takeout/a.txt").unlink()
                (staging / "Takeout").rmdir()
                (staging / "Takeout").symlink_to(self.originals(), target_is_directory=True)
            return checked

        with mock.patch.object(unpack, "safe_target", swapping):
            report = self.unpack_again()
        self.assert_refused_and_originals_kept(report, {})

    def test_a_part_swapped_for_a_link_is_refused(self):
        standard_export(self.inbox)
        self.registered()
        part = self.inbox / "takeout-20260101T000000Z-002.zip"
        moved = self.root / "elsewhere.zip"
        part.rename(moved)
        part.symlink_to(moved)
        report = self.unpack_again()
        self.assertEqual(report.public, ["part 2 is a link or not a plain file"])
        self.assertEqual(self.export_state(), [("failed", "unpack")])
        self.assertFalse(self.staging().exists())

    def test_a_linked_zip_in_the_manual_inbox_is_not_registered(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        manifest = standard_export(elsewhere)
        for part in manifest.parts:
            (self.inbox / part.name).symlink_to(part)
        (self.inbox / "linked").symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(unpack.group_manual_zips(self.inbox), {})
        self.assertEqual(self.run_inbox(), [])

    def test_a_linked_manual_inbox_is_refused(self):
        self.inbox.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        standard_export(elsewhere)
        self.inbox.symlink_to(elsewhere, target_is_directory=True)
        with db.session(self.engine) as opened, self.assertRaises(unpack.UnpackFailed):
            unpack.inspect_manual_inbox(opened, self.photos, self.person(opened), 1.0)
        self.assertEqual(self.export_state(), [])

    def test_a_folder_outside_the_inbox_still_unpacks(self):
        folder = self.root / "data/drop"
        folder.mkdir(parents=True)
        manifest = standard_export(folder)
        with db.session(self.engine) as opened:
            person = self.person(opened)
            (export,) = unpack.register_zips(opened, person, folder, 1.0)
            report = unpack.unpack(opened, self.photos, person, export, 2.0)
        self.assertEqual((report.parts_unpacked, report.problems), (2, []))
        for name in manifest.media:
            self.assertTrue((self.staging() / name).is_file(), name)

    def test_a_staging_folder_outside_the_inbox_is_refused(self):
        from unittest import mock

        standard_export(self.inbox)
        self.registered()
        with mock.patch.object(unpack, "staging_dir", return_value=self.originals() / "staging"):
            report = self.unpack_again()
        self.assertEqual(report.problems, [unpack.OUTSIDE_THE_INBOX])
        self.assertEqual(self.originals_tree(), {})

    def test_a_hard_linked_zip_in_the_manual_inbox_is_not_registered(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for part in standard_export(elsewhere).parts:
            os.link(part, self.inbox / part.name)
        before = self.originals_tree()
        self.assertEqual(unpack.group_manual_zips(self.inbox), {})
        self.assertEqual(self.run_inbox(), [])
        self.assertEqual(self.originals_tree(), before)

    def test_a_part_swapped_for_a_hard_link_is_refused(self):
        standard_export(self.inbox)
        self.registered()
        part = self.inbox / "takeout-20260101T000000Z-001.zip"
        moved = self.root / "elsewhere.zip"
        part.rename(moved)
        os.link(moved, part)
        report = self.unpack_again()
        self.assertEqual(report.public, ["part 1 is a link or not a plain file"])
        self.assertFalse(self.staging().exists())
        self.assertEqual(self.originals_tree(), {})

    def in_folder(self, folder):
        before = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, before)

    def registered_from(self, folder):
        with db.session(self.engine) as opened:
            person = self.person(opened)
            (export,) = unpack.register_zips(opened, person, folder, 1.0)
            return unpack.unpack(opened, self.photos, person, export, 2.0)

    def test_a_relative_folder_registers_and_unpacks_its_zips(self):
        for spelled in ("data/drop", "data/elsewhere/../drop"):
            with self.subTest(spelled=spelled):
                with db.session(self.engine) as opened:
                    opened.query(ExportPart).delete()
                    opened.query(Export).delete()
                (self.root / "data/elsewhere").mkdir(parents=True, exist_ok=True)
                (self.root / "data/drop").mkdir(parents=True, exist_ok=True)
                standard_export(self.root / "data/drop")
                self.in_folder(self.root)
                self.assertEqual(len(next(iter(unpack.group_manual_zips(Path(spelled)).values()))), 2)
                report = self.registered_from(Path(spelled))
                self.assertEqual((report.parts_unpacked + report.parts_skipped, report.problems), (2, []))

    def test_a_relative_folder_inside_the_inbox_still_unpacks(self):
        standard_export(self.inbox)
        self.in_folder(self.photos.root)
        report = self.registered_from(Path("_inbox/jo/manual"))
        self.assertEqual((report.parts_unpacked, report.problems), (2, []))

    def test_a_folder_reached_through_a_link_is_registered_by_its_real_path(self):
        real = self.root / "data/drop"
        real.mkdir(parents=True)
        standard_export(real)
        (self.root / "drop-link").symlink_to(real, target_is_directory=True)
        report = self.registered_from(self.root / "drop-link")
        self.assertEqual((report.parts_unpacked, report.problems), (2, []))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(Export).one().inbox_dir, str(real))

    def test_a_registered_folder_swapped_for_a_link_is_refused(self):
        drop = self.root / "data/drop"
        drop.mkdir(parents=True)
        standard_export(drop)
        other = self.root / "data/other"
        other.mkdir()
        standard_export(other, seed=7)
        with db.session(self.engine) as opened:
            unpack.register_zips(opened, self.person(opened), drop, 1.0)
        drop.rename(self.root / "data/old")
        drop.symlink_to(other, target_is_directory=True)
        report = self.unpack_again()
        self.assertEqual(report.public, ["part 1 is a link or not a plain file"])
        self.assertFalse(self.staging().exists())
        self.assertEqual(self.originals_tree(), {})

    def test_an_ancestor_swapped_for_a_link_is_refused(self):
        drop = self.root / "data/drop"
        drop.mkdir(parents=True)
        standard_export(drop)
        with db.session(self.engine) as opened:
            unpack.register_zips(opened, self.person(opened), drop, 1.0)
        (self.root / "data").rename(self.root / "moved")
        (self.root / "data").symlink_to(self.root / "moved", target_is_directory=True)
        report = self.unpack_again()
        self.assertEqual(report.public, ["part 1 is a link or not a plain file"])
        self.assertFalse(self.staging().exists())

    def test_skipped_zips_are_counted(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        first, second = standard_export(elsewhere).parts
        (self.inbox / first.name).symlink_to(first)
        os.link(second, self.inbox / second.name)
        self.assertEqual(unpack.skipped_zips(self.inbox), 2)
        self.assertEqual(unpack.skipped_zips(self.root / "missing"), 0)

    def test_a_manual_inbox_swapped_for_a_link_after_it_was_checked_registers_nothing(self):
        from unittest import mock

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        standard_export(elsewhere)
        before = self.originals_tree()
        real = self.photos.manual_inbox

        def checked_then_swapped(slug):
            folder = real(slug)
            folder.rmdir()
            folder.symlink_to(elsewhere, target_is_directory=True)
            return folder

        with mock.patch.object(self.photos, "manual_inbox", side_effect=checked_then_swapped), \
                db.session(self.engine) as opened, self.assertRaises(unpack.UnpackFailed):
            unpack.inspect_manual_inbox(opened, self.photos, self.person(opened), 1.0)
        self.assertEqual(self.export_state(), [])
        self.assertEqual(self.originals_tree(), before)

    def test_zips_in_a_folder_inside_the_manual_inbox_are_registered_where_they_are(self):
        folder = self.inbox / "sub"
        standard_export(folder)
        ((_, report),) = self.run_inbox()
        self.assertEqual((report.parts_unpacked, report.problems), (2, []))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(Export).one().inbox_dir, str(folder))

    def test_a_link_recorded_on_the_way_to_the_manual_inbox_is_refused(self):
        (self.inbox / "hop").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(unpack.UnpackFailed):
            unpack.recorded_folder(self.photos, self.inbox / "hop")
        self.assertEqual(unpack.recorded_folder(self.photos, self.inbox), str(self.inbox))

    def test_person_without_an_account_cannot_register(self):
        with db.session(self.engine) as opened:
            add_person(opened, "al")
            other = opened.query(Person).filter(Person.slug == "al").one()
            with self.assertRaises(unpack.UnpackFailed):
                unpack.register_zips(opened, other, self.inbox, 1.0)
