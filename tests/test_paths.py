import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom.storage import paths


class PhotosTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)

    def test_the_folder_tree(self):
        p = self.photos
        self.assertEqual(p.originals("jo"), self.root / "jo/originals")
        self.assertEqual(p.google_only("jo", 2019, 7), self.root / "jo/google-only/2019/07")
        self.assertEqual(p.google_only_undated("jo"), self.root / "jo/google-only/undated")
        self.assertEqual(p.inbox("jo"), self.root / "_inbox/jo")
        self.assertEqual(p.manual_inbox("jo"), self.root / "_inbox/jo/manual")
        self.assertEqual(p.staging("jo", "takeout-1"), self.root / "_inbox/jo/takeout-1/staging")
        self.assertEqual(p.review_hold("jo", "takeout-1"), self.root / "_inbox/jo/review/takeout-1")
        self.assertEqual(p.uploads_tmp("jo"), self.root / "jo/originals/.uploads_tmp")
        self.assertEqual(p.thumbs("jo"), self.root / "_inbox/jo/.thumbs")
        self.assertEqual(p.browser_profile("acct-1"), self.root / "_browser/acct-1/profile")
        self.assertEqual(p.failures("acct-1", "job-9"), self.root / "_browser/acct-1/failures/job-9")

    def test_traversal_and_bad_names_are_refused(self):
        p = self.photos
        for bad in ("..", "../x", "a/b", "/etc", "Jo", ""):
            with self.assertRaises(paths.OutsideRoot):
                p.originals(bad)
        for bad in ("..", ".hidden", "a/b", "../../x", ""):
            with self.assertRaises(paths.OutsideRoot):
                p.staging("jo", bad)
        with self.assertRaises(paths.OutsideRoot):
            p.google_only("jo", 2019, 13)
        with self.assertRaises(paths.OutsideRoot):
            p.inside("..", "x")
        with self.assertRaises(paths.OutsideRoot):
            p.inside()

    def test_a_symlink_out_of_the_root_is_refused(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.root / "jo").mkdir()
        (self.root / "jo" / "originals").symlink_to(outside.name)
        with self.assertRaises(paths.OutsideRoot):
            self.photos.originals("jo")

    def test_ensure_person_tree_creates_and_never_deletes(self):
        folders = self.photos.ensure_person_tree("jo")
        self.assertTrue(all(f.is_dir() for f in folders))
        keep = self.photos.originals("jo") / "a.jpg"
        keep.write_bytes(b"x")
        self.photos.ensure_person_tree("jo")
        self.assertTrue(keep.exists())


class FailedTreeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.photos = paths.Photos(self.root)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None) for p in self.root.rglob("*")}

    def test_a_failure_after_originals_and_google_only_were_made_removes_exactly_those(self):
        (self.root / "_inbox").write_bytes(b"in the way")
        (self.root / "sam" / "originals").mkdir(parents=True)
        (self.root / "sam" / "originals" / "keep.jpg").write_bytes(b"photo")
        before = self.snapshot()
        with self.assertRaises(OSError):
            self.photos.ensure_person_tree("jo")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "jo").exists())

    def test_it_leaves_alone_what_existed_before_even_with_files_in_it(self):
        (self.root / "jo" / "originals").mkdir(parents=True)
        (self.root / "jo" / "originals" / "keep.jpg").write_bytes(b"photo")
        (self.root / "_inbox").write_bytes(b"in the way")
        with self.assertRaises(OSError):
            self.photos.ensure_person_tree("jo")
        self.assertEqual((self.root / "jo" / "originals" / "keep.jpg").read_bytes(), b"photo")
        self.assertFalse((self.root / "jo" / "google-only").exists())

    def test_it_never_removes_a_folder_that_holds_something(self):
        real = Path.mkdir
        calls = []

        def failing(path, *args, **kwargs):
            calls.append(path)
            if path.name == "manual":
                (self.root / "jo" / "google-only" / "late.txt").write_bytes(b"x")
                raise PermissionError("no")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", failing), self.assertRaises(PermissionError):
            self.photos.ensure_person_tree("jo")
        self.assertEqual((self.root / "jo" / "google-only" / "late.txt").read_bytes(), b"x")
        self.assertFalse((self.root / "jo" / "originals").exists())

    def test_a_retry_after_the_blocker_is_gone_works(self):
        (self.root / "_inbox").write_bytes(b"in the way")
        with self.assertRaises(OSError):
            self.photos.ensure_person_tree("jo")
        (self.root / "_inbox").unlink()
        self.assertTrue(all(folder.is_dir() for folder in self.photos.ensure_person_tree("jo")))


class WriteOnceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_writes_once_and_returns_the_sha1(self):
        destination = self.folder / "sub" / "a.bin"
        digest = paths.write_once(destination, [b"hel", b"lo"])
        self.assertEqual(digest, "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d")
        self.assertEqual(destination.read_bytes(), b"hello")
        self.assertEqual([p.name for p in destination.parent.iterdir()], ["a.bin"])

    def test_never_overwrites_and_leaves_no_temp_file(self):
        destination = self.folder / "a.bin"
        paths.write_once(destination, [b"one"])
        with self.assertRaises(paths.AlreadyExists):
            paths.write_once(destination, [b"two"])
        self.assertEqual(destination.read_bytes(), b"one")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["a.bin"])

    def test_a_failing_stream_leaves_nothing(self):
        def broken():
            yield b"part"
            raise OSError("disk went away")

        with self.assertRaises(OSError):
            paths.write_once(self.folder / "a.bin", broken())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_falls_back_to_a_copy_when_hard_links_are_unavailable(self):
        from unittest import mock

        destination = self.folder / "a.bin"
        with mock.patch("os.link", side_effect=OSError("cross-device")):
            paths.write_once(destination, [b"data"])
        self.assertEqual(destination.read_bytes(), b"data")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["a.bin"])

    def test_fallback_never_overwrites_a_file_that_appears_late(self):
        from unittest import mock

        destination = self.folder / "a.bin"
        destination.write_bytes(b"original")
        with mock.patch("os.link", side_effect=OSError("cross-device")), mock.patch.object(Path, "exists", return_value=False):
            with self.assertRaises(paths.AlreadyExists):
                paths.write_once(destination, [b"new"])
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["a.bin"])

    def test_fallback_removes_a_partial_copy_on_failure(self):
        from unittest import mock

        destination = self.folder / "a.bin"
        with mock.patch("os.link", side_effect=OSError("cross-device")), mock.patch("os.fsync", side_effect=[None, OSError("disk")]):
            with self.assertRaises(OSError):
                paths.write_once(destination, [b"data"])
        self.assertEqual(list(self.folder.iterdir()), [])



class PlacingRecordTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.destination = self.folder / "a.jpg"
        self.token = "0123456789abcdef"

    def test_the_record_has_the_same_short_name_whatever_the_photo_is_called(self):
        for name in ("a.jpg", "L" * 251 + ".jpg"):
            with self.subTest(length=len(name)):
                destination = self.folder / name
                placing = paths.Placing(name, "1" * 40, True, True, "2" * 40)
                paths.mark_placing(destination, self.token, placing)
                self.assertEqual([path.name for path in self.folder.iterdir()], [".placing-0123456789abcdef.json"])
                self.assertEqual(paths.read_placing(paths.placing_record(destination, self.token)), placing)
                paths.placing_record(destination, self.token).unlink()

    def test_a_record_that_is_not_one_is_not_read(self):
        record = paths.placing_record(self.destination, self.token)
        good = {"name": "a.jpg", "sha1": "1" * 40, "made": True, "sidecar": True, "sidecar_sha1": None}
        for data in ({**good, "name": "../a.jpg"}, {**good, "name": ".placing-x"}, {**good, "name": ""}, {**good, "sha1": "x"},
                     {**good, "made": 1}, {**good, "sidecar_sha1": "x"}, {**good, "sidecar": False, "sidecar_sha1": "2" * 40}, {key: value for key, value in good.items() if key != "sidecar"}, [good]):
            with self.subTest(data=data):
                record.write_text(json.dumps(data))
                self.assertIsNone(paths.read_placing(record))
        record.write_bytes(b"not json")
        self.assertIsNone(paths.read_placing(record))

    def test_clearing_keeps_the_record_while_anything_it_names_is_still_there(self):
        record = paths.placing_record(self.destination, self.token)
        paths.write_once(self.destination, [b"photo"])
        paths.sidecar_of(self.destination).write_bytes(b"<x/>")
        paths.mark_placing(self.destination, self.token, paths.Placing("a.jpg", "1" * 40, True, True))
        for left in (self.destination, paths.sidecar_of(self.destination)):
            paths.clear_placing(self.destination, self.token, committed=False)
            self.assertTrue(record.is_file())
            left.unlink()
        paths.clear_placing(self.destination, self.token, committed=False)
        self.assertFalse(record.exists())
        paths.write_once(self.destination, [b"photo"])
        paths.mark_placing(self.destination, self.token, paths.Placing("a.jpg", "1" * 40, True, True))
        paths.clear_placing(self.destination, self.token, committed=True)
        self.assertEqual([path.name for path in self.folder.iterdir()], ["a.jpg"])


class DropLibraryCopyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.root = self.base / "photos"
        self.root.mkdir()
        self.photos = paths.Photos(self.root)
        self.photos.ensure_person_tree("jo")
        self.photos.ensure_person_tree("sam")
        self.copy = self.write("jo/google-only/2020/01/a.jpg", b"same bytes")
        self.write("jo/google-only/2020/01/a.jpg.xmp", b"<xmp/>")
        self.kept = self.write("sam/google-only/2020/01/a.jpg", b"same bytes")

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_an_identical_copy_and_its_sidecar_are_removed(self):
        paths.drop_library_copy(self.photos, "jo/google-only/2020/01/a.jpg", self.kept)
        self.assertFalse(self.copy.exists())
        self.assertFalse(self.copy.with_name("a.jpg.xmp").exists())
        self.assertEqual(self.kept.read_bytes(), b"same bytes")

    def test_putting_back_never_replaces_a_file_made_meanwhile(self):
        aside = paths.set_library_copy_aside(self.photos, "jo/google-only/2020/01/a.jpg", self.kept)
        newer = self.write("jo/google-only/2020/01/a.jpg", b"made while it was set aside")
        newer_sidecar = self.write("jo/google-only/2020/01/a.jpg.xmp", b"<new/>")
        staged = sorted(path.name for path in self.copy.parent.iterdir() if path.name.startswith(paths.DROPPING))
        with self.assertRaises(FileExistsError):
            aside.restore()
        self.assertEqual((newer.read_bytes(), newer_sidecar.read_bytes()), (b"made while it was set aside", b"<new/>"))
        self.assertEqual(sorted(path.name for path in self.copy.parent.iterdir() if path.name.startswith(paths.DROPPING)), staged)
        self.assertEqual(len(staged), 2)

    def test_putting_back_restores_one_file_when_the_other_place_is_taken(self):
        aside = paths.set_library_copy_aside(self.photos, "jo/google-only/2020/01/a.jpg", self.kept)
        newer_sidecar = self.write("jo/google-only/2020/01/a.jpg.xmp", b"<new/>")
        with self.assertRaises(FileExistsError):
            aside.restore()
        self.assertEqual(self.copy.read_bytes(), b"same bytes")
        self.assertEqual(newer_sidecar.read_bytes(), b"<new/>")

    def test_a_sidecar_that_cannot_be_set_aside_leaves_no_record(self):
        with mock.patch("os.rename", side_effect=OSError("name too long")), self.assertRaises(OSError):
            paths.set_sidecar_aside(self.photos, "jo/google-only/2020/01/a.jpg", 1, 0)
        self.assertEqual(self.copy.with_name("a.jpg.xmp").read_bytes(), b"<xmp/>")
        self.assertEqual([path.name for path in self.copy.parent.iterdir() if path.name.startswith(paths.DROPPING)], [])

    def test_a_set_aside_sidecar_that_cannot_be_unlinked_keeps_its_record(self):
        aside = paths.set_sidecar_aside(self.photos, "jo/google-only/2020/01/a.jpg", 1, 0)
        ((_, moved),) = aside.staged
        real = Path.unlink

        def failing(path, *args, **kwargs):
            if path == moved:
                raise OSError("busy")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", failing):
            aside.finish()
        self.assertEqual(moved.read_bytes(), b"<xmp/>")
        self.assertTrue(paths.record_of(moved).is_file())
        aside.finish()
        self.assertEqual([path.name for path in self.copy.parent.iterdir() if path.name.startswith(paths.DROPPING)], [])

    def test_putting_back_a_set_aside_sidecar_works_when_nothing_is_in_the_way(self):
        paths.set_sidecar_aside(self.photos, "jo/google-only/2020/01/a.jpg", 1, 0).restore()
        self.assertEqual(self.copy.with_name("a.jpg.xmp").read_bytes(), b"<xmp/>")
        self.assertEqual([path.name for path in self.copy.parent.iterdir() if path.name.startswith(paths.DROPPING)], [])

    def test_nothing_outside_a_google_only_folder_is_removed(self):
        original = self.write("jo/originals/a.jpg", b"same bytes")
        held = self.write("_inbox/jo/review/export-1/a.jpg", b"same bytes")
        for relative in ("jo/originals/a.jpg", "_inbox/jo/review/export-1/a.jpg", "jo/google-only", "jo/google-only/../originals/a.jpg",
                         "../photos/jo/google-only/2020/01/a.jpg", "jo/google-only//a.jpg", "Jo/google-only/2020/01/a.jpg"):
            with self.subTest(relative=relative), self.assertRaises((paths.NotALibraryCopy, paths.OutsideRoot)):
                paths.drop_library_copy(self.photos, relative, self.kept)
        self.assertTrue(original.is_file() and held.is_file() and self.copy.is_file())

    def test_a_copy_is_kept_unless_an_identical_file_stays(self):
        different = self.write("sam/google-only/2020/01/b.jpg", b"other bytes")
        outside = self.base / "outside.jpg"
        outside.write_bytes(b"same bytes")
        link = self.root / "sam" / "google-only" / "link.jpg"
        link.symlink_to(self.kept)
        for kept in (different, self.copy, outside, link, self.root / "missing.jpg"):
            with self.subTest(kept=kept), self.assertRaises(paths.NotALibraryCopy):
                paths.drop_library_copy(self.photos, "jo/google-only/2020/01/a.jpg", kept)
        self.assertEqual(self.copy.read_bytes(), b"same bytes")

    def test_a_sidecar_is_carried_byte_for_byte_and_never_over_another(self):
        target = self.root / "sam" / "google-only" / "2020" / "01" / "c.jpg"
        self.assertTrue(paths.carry_sidecar(self.copy, target))
        self.assertEqual(target.with_name("c.jpg.xmp").read_bytes(), b"<xmp/>")
        self.assertTrue(paths.carry_sidecar(self.copy, target))
        target.with_name("c.jpg.xmp").write_bytes(b"other")
        with self.assertRaises(paths.AlreadyExists):
            paths.carry_sidecar(self.copy, target)
        self.assertEqual(target.with_name("c.jpg.xmp").read_bytes(), b"other")
        self.assertFalse(paths.carry_sidecar(self.kept, target))

    def test_the_sidecar_can_be_left_in_place(self):
        paths.drop_library_copy(self.photos, "jo/google-only/2020/01/a.jpg", self.kept, with_sidecar=False)
        self.assertFalse(self.copy.exists())
        self.assertTrue(self.copy.with_name("a.jpg.xmp").is_file())

    def test_a_failure_part_way_restores_both_files(self):
        real = os.rename

        def rename(source, target):
            if str(source).endswith(".xmp"):
                raise PermissionError("made to fail")
            return real(source, target)

        with mock.patch.object(paths.os, "rename", side_effect=rename), self.assertRaises(PermissionError):
            paths.drop_library_copy(self.photos, "jo/google-only/2020/01/a.jpg", self.kept)
        self.assertEqual(self.copy.read_bytes(), b"same bytes")
        self.assertEqual(self.copy.with_name("a.jpg.xmp").read_bytes(), b"<xmp/>")
        self.assertEqual(sorted(path.name for path in self.copy.parent.iterdir()), ["a.jpg", "a.jpg.xmp"])

    def test_a_staged_file_that_cannot_be_deleted_still_counts_as_gone(self):
        with mock.patch.object(paths.Path, "unlink", side_effect=PermissionError("made to fail")):
            paths.drop_library_copy(self.photos, "jo/google-only/2020/01/a.jpg", self.kept)
        self.assertFalse(self.copy.exists())
        self.assertFalse(self.copy.with_name("a.jpg.xmp").exists())

    def test_a_linked_copy_is_never_followed(self):
        target = self.base / "elsewhere.jpg"
        target.write_bytes(b"same bytes")
        link = self.root / "jo" / "google-only" / "2020" / "01" / "link.jpg"
        link.symlink_to(target)
        with self.assertRaises(paths.NotALibraryCopy):
            paths.drop_library_copy(self.photos, "jo/google-only/2020/01/link.jpg", self.kept)
        self.assertTrue(target.is_file())

SHARED = 0o2770


def mode_of(path):
    return path.lstat().st_mode & 0o7777


class SharedInboxTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = paths.Photos(Path(folder.name))
        self.inbox = self.photos.root / "_inbox"

    def under_umask(self, mask):
        previous = os.umask(mask)
        self.addCleanup(os.umask, previous)

    def test_the_shared_mode_is_setgid_and_group_rwx_with_nothing_for_others(self):
        self.assertEqual(paths.SHARED_FOLDER_MODE, 0o2770)
        self.assertEqual(paths.SHARED_FOLDER_MODE & 0o007, 0)

    def test_the_inbox_and_the_downloads_folder_are_shared_whatever_the_umask(self):
        for mask in (0o077, 0o022, 0o000):
            with self.subTest(umask=oct(mask)):
                self.under_umask(mask)
                self.photos.share_inbox()
                self.assertEqual((mode_of(self.inbox), mode_of(self.inbox / ".downloads")), (SHARED, SHARED))
                os.chmod(self.inbox / ".downloads", 0o755)
                os.chmod(self.inbox, 0o755)

    def test_a_persons_inbox_and_manual_folder_are_shared_and_their_library_folders_are_not(self):
        self.under_umask(0o077)
        self.photos.ensure_person_tree("jo")
        for folder in (self.inbox, self.inbox / "jo", self.inbox / "jo" / "manual"):
            self.assertEqual(mode_of(folder), SHARED, folder)
        for folder in (self.photos.root / "jo" / "originals", self.photos.root / "jo" / "google-only"):
            self.assertEqual(mode_of(folder), 0o700, folder)

    def test_an_export_folder_is_shared_with_its_persons_inbox(self):
        self.under_umask(0o077)
        folder = self.photos.make_export_folder("jo", "export-4")
        self.assertEqual(folder, self.inbox / "jo" / "export-4")
        for each in (self.inbox, self.inbox / "jo", folder):
            self.assertEqual(mode_of(each), SHARED, each)

    def test_existing_inbox_folders_made_before_sharing_are_shared_at_startup(self):
        self.under_umask(0o022)
        (self.inbox / "jo").mkdir(parents=True)
        (self.inbox / "jo" / "photo.jpg").write_bytes(b"x")
        shared = self.photos.share_inbox()
        self.assertEqual(shared, [self.inbox, self.inbox / ".downloads", self.inbox / "jo"])
        self.assertEqual(mode_of(self.inbox / "jo"), SHARED)
        self.assertEqual(mode_of(self.inbox / "jo" / "photo.jpg"), 0o644)

    def test_a_link_instead_of_a_shared_folder_is_refused_and_its_target_is_not_changed(self):
        self.under_umask(0o022)
        elsewhere = self.photos.root / "jo" / "originals"
        elsewhere.mkdir(parents=True)
        self.inbox.mkdir()
        for link, call in ((self.inbox / ".downloads", self.photos.share_inbox),
                           (self.inbox / "jo", lambda: self.photos.make_export_folder("jo", "export-1")),
                           (self.inbox / "sam", lambda: self.photos.make_export_folder("pat", "export-1"))):
            with self.subTest(link=link.name):
                if link.name == "sam":
                    (self.inbox / "pat").mkdir()
                    link = self.inbox / "pat" / "export-1"
                link.symlink_to(elsewhere, target_is_directory=True)
                with self.assertRaises(paths.NotInTheInbox):
                    call()
                self.assertEqual(mode_of(elsewhere), 0o755)
                link.unlink()


if __name__ == "__main__":
    unittest.main()
