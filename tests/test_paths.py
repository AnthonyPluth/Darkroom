import contextlib
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

    def test_writing_a_copy_leaves_only_the_copy_and_never_replaces_one(self):
        paths.write_once(self.destination, [b"photo"])
        self.assertEqual([path.name for path in self.folder.iterdir()], ["a.jpg"])
        with self.assertRaises(paths.AlreadyExists):
            paths.write_once(self.destination, [b"other"])
        with mock.patch("os.link", side_effect=OSError("cross-device")):
            paths.write_once(self.folder / "b.jpg", [b"photo"])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["a.jpg", "b.jpg"])
        self.assertEqual(self.destination.read_bytes(), b"photo")

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


class RemoveLosingCopyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        self.keeper = self.file("jo/originals/immich/IMG_0001-aaaa.jpg", b"phone")
        self.loser = self.file("jo/google-only/2021/03/IMG_0001.jpg", b"takeout")
        self.file("jo/google-only/2021/03/IMG_0001.jpg.xmp", b"<xmp/>")

    def file(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def remove(self, losing="jo/google-only/2021/03/IMG_0001.jpg", losing_sha1=None, keeping="jo/originals/immich/IMG_0001-aaaa.jpg", keeping_sha1=None, person="jo"):
        return paths.remove_losing_copy(self.photos, person, losing, losing_sha1 or paths.file_sha1(self.loser), keeping, keeping_sha1 or paths.file_sha1(self.keeper))

    def test_the_losing_takeout_copy_and_its_sidecar_go_and_the_keeper_stays_byte_for_byte(self):
        self.assertTrue(self.remove())
        self.assertFalse(self.loser.exists())
        self.assertFalse((self.root / "jo/google-only/2021/03/IMG_0001.jpg.xmp").exists())
        self.assertEqual(self.keeper.read_bytes(), b"phone")
        (self.root / "jo/google-only/2021/03/IMG_0001.jpg.xmp").write_bytes(b"<xmp/>")
        self.assertFalse(self.remove(losing_sha1="b" * 40))
        self.assertFalse((self.root / "jo/google-only/2021/03/IMG_0001.jpg.xmp").exists())

    def test_a_losing_immich_original_goes_when_the_takeout_copy_is_kept(self):
        self.assertTrue(self.remove(losing="jo/originals/immich/IMG_0001-aaaa.jpg", losing_sha1=paths.file_sha1(self.keeper),
                                    keeping="jo/google-only/2021/03/IMG_0001.jpg", keeping_sha1=paths.file_sha1(self.loser)))
        self.assertFalse(self.keeper.exists())
        self.assertEqual(self.loser.read_bytes(), b"takeout")


    def assertRefused(self, **arguments):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}
        with self.assertRaises((paths.NotALosingCopy, paths.OutsideRoot)):
            self.remove(**arguments)
        self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}, before)

    def test_a_keeper_or_loser_that_is_not_the_file_darkroom_recorded_or_a_missing_keeper_is_refused(self):
        self.assertRefused(keeping_sha1="a" * 40)
        self.assertRefused(losing_sha1="a" * 40)
        recorded = paths.file_sha1(self.keeper)
        self.keeper.unlink()
        self.assertRefused(keeping_sha1=recorded)

    def test_paths_outside_the_persons_library_are_refused(self):
        self.file("sam/google-only/2021/03/IMG_0001.jpg", b"takeout")
        self.file("_inbox/jo/review/1/IMG_0001.jpg", b"takeout")
        self.file("jo/originals/IMG_0001.jpg", b"takeout")
        for losing in ("sam/google-only/2021/03/IMG_0001.jpg", "_inbox/jo/review/1/IMG_0001.jpg", "jo/originals/IMG_0001.jpg",
                       "jo/google-only/../originals/IMG_0001.jpg", "jo/google-only", "jo/originals/immich", "/etc/passwd"):
            with self.subTest(losing=losing):
                self.assertRefused(losing=losing)
        self.file("sam/originals/immich/a.jpg", b"phone")
        self.assertRefused(keeping="sam/originals/immich/a.jpg")
        self.assertRefused(keeping="_inbox/jo/review/1/IMG_0001.jpg")
        self.assertRefused(person="sam")

    def test_links_and_folders_are_never_followed_or_removed(self):
        (self.root / "jo/google-only/2021/04/IMG_0002.jpg").mkdir(parents=True)
        self.assertRefused(losing="jo/google-only/2021/04/IMG_0002.jpg")
        elsewhere = self.file("outside/a.jpg", b"takeout")
        link = self.root / "jo/google-only/2021/03/link.jpg"
        link.symlink_to(elsewhere)
        self.assertRefused(losing="jo/google-only/2021/03/link.jpg", losing_sha1=paths.file_sha1(elsewhere))
        self.assertTrue(link.is_symlink() and elsewhere.exists())
        (self.root / "jo/google-only/2020").symlink_to(self.root / "outside")
        self.assertRefused(losing="jo/google-only/2020/a.jpg", losing_sha1=paths.file_sha1(elsewhere))
        kept_link = self.root / "jo/originals/immich/kept.jpg"
        kept_link.symlink_to(self.keeper)
        self.assertRefused(keeping="jo/originals/immich/kept.jpg")

    def test_the_keeper_and_the_loser_are_never_the_same_file(self):
        self.assertRefused(losing="jo/originals/immich/IMG_0001-aaaa.jpg", losing_sha1=paths.file_sha1(self.keeper))
        os.link(self.keeper, self.root / "jo/google-only/2021/03/hard.jpg")
        self.assertRefused(losing="jo/google-only/2021/03/hard.jpg", losing_sha1=paths.file_sha1(self.keeper))

    def contents(self):
        return sorted(p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink())

    def test_a_file_swapped_in_at_any_step_is_never_removed_and_a_copy_of_the_photo_always_stays(self):
        steps = [("fd_sha1", 1), ("fd_sha1", 2), ("link", 1), ("rename_no_replace", 1), ("still_there", 1), ("unlink", 1), ("still_there", 2), ("rename_no_replace", 2)]
        for (step, nth), target in [(step, target) for step in steps for target in ("loser", "keeper")]:
            with self.subTest(step=step, nth=nth, target=target):
                self.setUp()
                self.file("jo/google-only/2021/03/bystander.jpg", b"bystander")
                calls, real = {}, {"fd_sha1": paths.fd_sha1, "link": os.link, "rename": os.rename, "unlink": os.unlink, "still_there": paths.Keeper.still_there,
                                   "rename_no_replace": paths.rename_no_replace}

                def hooked(*args, step=step, nth=nth, target=target, calls=calls, real=real, name="", **kwargs):
                    calls[name] = calls.get(name, 0) + 1
                    if name == step and calls[name] == nth:
                        swapped = self.loser if target == "loser" else self.keeper
                        swapped.with_name("incoming").write_bytes(b"intruder")
                        real["rename"](swapped.with_name("incoming"), swapped)
                    return real[name](*args, **kwargs)

                patches = [mock.patch.object(paths, "fd_sha1", lambda *a: hooked(*a, name="fd_sha1")),
                           mock.patch.object(paths, "rename_no_replace", lambda *a: hooked(*a, name="rename_no_replace")),
                           mock.patch.object(paths.Keeper, "still_there", lambda keeper: hooked(keeper, name="still_there"))]
                patches += [mock.patch.object(paths.os, call, lambda *a, call=call, **k: hooked(*a, name=call, **k)) for call in ("link", "unlink")]
                with contextlib.ExitStack() as stack, contextlib.suppress(paths.NotALosingCopy, OSError):
                    for patch in patches:
                        stack.enter_context(patch)
                    self.remove(losing_sha1=paths.file_sha1(self.loser), keeping_sha1=paths.file_sha1(self.keeper))
                left = self.contents()
                self.assertTrue({b"bystander", b"intruder"} <= set(left), left)
                self.assertTrue(b"phone" in left or b"takeout" in left if target == "keeper" else b"phone" in left, left)

    def crash_at_the_unlink(self):
        real = os.unlink

        def crashing(name, *args, **kwargs):
            if name == paths.CLAIMED:
                raise KeyboardInterrupt
            return real(name, *args, **kwargs)

        with mock.patch.object(paths.os, "unlink", crashing), self.assertRaises(KeyboardInterrupt):
            self.remove()
        (private,) = [p for p in self.loser.parent.iterdir() if p.name.startswith(paths.LOSING)]
        claim = private / paths.CLAIMED
        self.assertTrue(claim.exists() and not self.loser.exists())
        self.assertEqual(private.stat().st_mode & 0o777, paths.PRIVATE_MODE)
        return claim

    def test_a_crash_between_the_claim_and_the_unlink_is_finished_by_the_next_call(self):
        claim, sha1 = self.crash_at_the_unlink(), paths.file_sha1(self.keeper)
        self.assertTrue(paths.remove_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", paths.file_sha1(claim), "jo/originals/immich/IMG_0001-aaaa.jpg", sha1))
        self.assertEqual(sorted(p.name for p in self.loser.parent.iterdir()), [])

    def test_a_claim_left_by_a_crash_goes_back_when_the_pair_no_longer_holds(self):
        claim = self.crash_at_the_unlink()
        sha1 = paths.file_sha1(claim)
        self.keeper.write_bytes(b"changed")
        with self.assertRaises(paths.NotALosingCopy):
            paths.remove_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", sha1, "jo/originals/immich/IMG_0001-aaaa.jpg", "a" * 40)
        self.assertEqual((self.loser.read_bytes(), claim.exists()), (b"takeout", False))
        self.crash_at_the_unlink()
        self.assertTrue(paths.restore_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", sha1))
        self.assertEqual(self.loser.read_bytes(), b"takeout")
        self.assertFalse(paths.restore_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", sha1))

    def test_a_stale_claim_never_takes_the_sidecar_of_a_photo_now_at_the_name(self):
        claim = self.crash_at_the_unlink()
        self.file("jo/google-only/2021/03/IMG_0001.jpg", b"a new photo")
        self.file("jo/google-only/2021/03/IMG_0001.jpg.xmp", b"its own sidecar")
        paths.remove_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", paths.file_sha1(claim), "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper))
        self.assertEqual((self.loser.read_bytes(), self.loser.with_name("IMG_0001.jpg.xmp").read_bytes()), (b"a new photo", b"its own sidecar"))

    def test_a_file_put_at_the_claim_name_after_the_check_is_never_replaced(self):
        real = paths.entry_at

        def racing(name, folder):
            found = real(name, folder)
            if name == paths.CLAIMED and found is None:
                (private,) = [p for p in self.loser.parent.iterdir() if p.name.startswith(paths.LOSING)]
                (private / name).write_bytes(b"someone else's file")
            return found

        with mock.patch.object(paths, "entry_at", racing), self.assertRaises(paths.NotALosingCopy):
            self.remove()
        self.assertEqual(sorted(self.contents()), sorted([b"phone", b"takeout", b"<xmp/>", b"someone else's file"]))

    def test_a_sidecar_swapped_in_after_it_was_inspected_is_never_removed(self):
        real = paths.rename_no_replace

        def swapping(source, target, folder, into=None):
            if source.endswith(".xmp") and target == paths.CLAIMED_SIDECAR:
                self.file("jo/google-only/2021/03/incoming", b"a sidecar swapped in")
                os.replace(self.loser.with_name("incoming"), self.loser.with_name("IMG_0001.jpg.xmp"))
            return real(source, target, folder, into)

        with mock.patch.object(paths, "rename_no_replace", swapping):
            self.remove()
        self.assertFalse(self.loser.exists())
        self.assertIn(b"a sidecar swapped in", self.contents())

    def test_without_renameat2_the_claim_falls_back_to_a_checked_link(self):
        with mock.patch.object(paths, "LIBC", object()):
            self.assertTrue(self.remove())
        self.assertEqual((self.loser.exists(), self.keeper.read_bytes()), (False, b"phone"))

    def test_a_swap_during_the_link_fallback_keeps_the_file_swapped_in(self):
        real, swapped = paths.entry_at, []

        def swapping(name, folder):
            if name == self.loser.name and not swapped:
                swapped.append(name)
                self.file("jo/google-only/2021/03/incoming", b"intruder")
                os.replace(self.loser.with_name("incoming"), self.loser)
            return real(name, folder)

        with mock.patch.object(paths, "LIBC", object()), mock.patch.object(paths, "entry_at", swapping), contextlib.suppress(paths.NotALosingCopy):
            self.remove()
        self.assertIn(b"intruder", self.contents())
        self.assertIn(b"takeout", self.contents())

    def test_a_crash_at_the_sidecar_claim_is_finished_by_the_next_call(self):
        real = os.unlink

        def crashing(name, *args, **kwargs):
            if name == paths.CLAIMED_SIDECAR:
                raise KeyboardInterrupt
            return real(name, *args, **kwargs)

        sha1 = paths.file_sha1(self.loser)
        with mock.patch.object(paths.os, "unlink", crashing), self.assertRaises(KeyboardInterrupt):
            self.remove()
        self.assertEqual([p.name for p in self.loser.parent.glob(f"{paths.LOSING}*/*")], [paths.CLAIMED_SIDECAR])
        self.assertFalse(self.remove(losing_sha1=sha1))
        self.assertEqual(list(self.loser.parent.iterdir()), [])

    def test_a_keeper_link_left_by_a_hard_crash_is_recovered(self):
        held = self.keeper.with_name(f"{paths.KEEPING}0123456789abcdef")
        os.link(self.keeper, held)
        self.assertTrue(self.remove())
        self.assertEqual((held.exists(), self.keeper.read_bytes(), self.keeper.stat().st_nlink), (False, b"phone", 1))

    def test_a_link_fallback_cut_short_between_link_and_unlink_is_finished(self):
        private = self.loser.parent / paths.claim_name(paths.file_sha1(self.loser), self.loser.name)
        private.mkdir(mode=paths.PRIVATE_MODE)
        os.link(self.loser, private / paths.CLAIMED)
        self.assertTrue(self.remove())
        self.assertEqual((self.loser.exists(), private.exists()), (False, False))

    def test_a_file_swapped_into_the_private_claim_folder_before_the_unlink_survives(self):
        real = paths.Keeper.still_there

        def swapping(keeper):
            (private,) = [p for p in self.loser.parent.iterdir() if p.name.startswith(paths.LOSING)]
            if (private / paths.CLAIMED).exists():
                (private / "incoming").write_bytes(b"intruder")
                os.replace(private / "incoming", private / paths.CLAIMED)
            return real(keeper)

        with mock.patch.object(paths.Keeper, "still_there", swapping), contextlib.suppress(paths.NotALosingCopy):
            self.remove()
        self.assertIn(b"intruder", self.contents())

    def test_a_claim_folder_that_is_not_darkroom_private_is_refused(self):
        for kind in ("open", "link"):
            with self.subTest(kind=kind):
                self.setUp()
                private = self.loser.parent / paths.claim_name(paths.file_sha1(self.loser), self.loser.name)
                if kind == "open":
                    private.mkdir()
                    os.chmod(private, 0o755)
                else:
                    (self.root / "outside").mkdir()
                    private.symlink_to(self.root / "outside", target_is_directory=True)
                self.assertRefused()

    def test_a_newer_sidecar_written_while_the_keepers_was_staged_is_never_discarded(self):
        sidecar = self.keeper.with_name(f"{self.keeper.name}.xmp")
        sidecar.write_bytes(b"first")
        real = paths.exchange

        def racing(first, second, folder):
            self.file("jo/originals/immich/incoming", b"newer, from another writer")
            os.replace(self.keeper.with_name("incoming"), sidecar)
            return real(first, second, folder)

        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)) as keeper:
            with mock.patch.object(paths, "exchange", racing), self.assertRaises(paths.NotALosingCopy):
                keeper.write_sidecar(b"ours", b"first")
        self.assertEqual(sidecar.read_bytes(), b"newer, from another writer")

    def test_a_keeper_link_held_by_a_removal_in_progress_is_never_taken_for_crash_debris(self):
        held = self.keeper.with_name(f"{paths.KEEPING}0123456789abcdef")
        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)):
            os.link(self.keeper, held)
            self.assertRefused()
            self.assertTrue(held.exists())

    def test_a_sidecar_rollback_never_discards_a_newer_sidecar(self):
        sidecar = self.keeper.with_name(f"{self.keeper.name}.xmp")
        sidecar.write_bytes(b"first")
        real, calls = paths.exchange, []

        def racing(first, second, folder):
            calls.append(first)
            if len(calls) <= 3:
                self.file("jo/originals/immich/incoming", b"other %d" % len(calls))
                os.replace(self.keeper.with_name("incoming"), sidecar)
            return real(first, second, folder)

        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)) as keeper:
            with mock.patch.object(paths, "exchange", racing), self.assertRaises(paths.NotALosingCopy):
                keeper.write_sidecar(b"ours", b"first")
        self.assertTrue({b"other 1", b"other 2"} <= set(self.contents()))

    def test_a_short_write_never_publishes_a_truncated_sidecar(self):
        real = os.write
        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)) as keeper, \
                mock.patch.object(paths.os, "write", lambda fd, data: real(fd, bytes(data[:2]))):
            keeper.write_sidecar(b"<x:xmpmeta/>", None)
        self.assertEqual(self.keeper.with_name(f"{self.keeper.name}.xmp").read_bytes(), b"<x:xmpmeta/>")

    def test_a_new_photo_and_sidecar_at_the_name_keep_their_sidecar(self):
        real, arrived = paths.entry_at, []

        def arriving(name, folder):
            if name == "IMG_0001.jpg.xmp" and not arrived:
                arrived.append(name)
                self.file("jo/google-only/2021/03/IMG_0001.jpg", b"a new photo")
                self.file("jo/google-only/2021/03/incoming", b"its sidecar")
                os.replace(self.loser.with_name("incoming"), self.loser.with_name("IMG_0001.jpg.xmp"))
            return real(name, folder)

        with mock.patch.object(paths, "entry_at", arriving):
            self.remove()
        self.assertEqual((self.loser.read_bytes(), self.loser.with_name("IMG_0001.jpg.xmp").read_bytes()), (b"a new photo", b"its sidecar"))

    def test_a_keeper_whose_name_is_taken_after_the_loser_went_is_kept_and_reported(self):
        real = os.unlink

        def taking(name, *args, **kwargs):
            if name == paths.CLAIMED:
                self.file("jo/originals/immich/incoming", b"intruder")
                os.replace(self.keeper.with_name("incoming"), self.keeper)
            return real(name, *args, **kwargs)

        with mock.patch.object(paths.os, "unlink", taking), self.assertRaisesRegex(paths.NotALosingCopy, "stays as"):
            self.remove()
        self.assertEqual(sorted(self.contents()), sorted([b"intruder", b"phone"]))
        (held,) = [p for p in self.keeper.parent.iterdir() if p.name.startswith(paths.KEEPING)]
        self.assertEqual(held.read_bytes(), b"phone")

    def test_without_an_atomic_exchange_an_existing_sidecar_is_never_replaced(self):
        sidecar = self.keeper.with_name(f"{self.keeper.name}.xmp")
        sidecar.write_bytes(b"first")
        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)) as keeper, \
                mock.patch.object(paths, "LIBC", object()), self.assertRaises(paths.NotALosingCopy):
            keeper.write_sidecar(b"ours", b"first")
        self.assertEqual((sidecar.read_bytes(), [p.name for p in self.keeper.parent.iterdir() if p.name.startswith(".xmp-")]), (b"first", []))

    def test_only_a_generated_keeper_hold_is_ever_taken_for_crash_debris(self):
        legit = self.keeper.with_name(".keeping-the-owners-own-link")
        os.link(self.keeper, legit)
        self.assertRefused()
        self.assertTrue(legit.exists())

    def test_a_restore_puts_back_only_the_recorded_copy_and_never_a_live_claim(self):
        sha1 = paths.file_sha1(self.loser)
        private = self.loser.parent / paths.claim_name(sha1, self.loser.name)
        private.mkdir(mode=paths.PRIVATE_MODE)
        self.loser.rename(private / paths.CLAIMED)
        for wrong in ("other bytes", "link"):
            with self.subTest(wrong=wrong):
                (private / paths.CLAIMED).rename(private / "kept-aside")
                if wrong == "link":
                    (private / paths.CLAIMED).symlink_to(private / "kept-aside")
                else:
                    (private / paths.CLAIMED).write_bytes(b"someone else's")
                self.assertFalse(paths.restore_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", sha1))
                self.assertFalse(self.loser.exists() or self.loser.is_symlink())
                (private / paths.CLAIMED).unlink()
                (private / "kept-aside").rename(private / paths.CLAIMED)
        fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        paths.fcntl.flock(fd, paths.fcntl.LOCK_EX)
        self.assertFalse(paths.restore_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", sha1))
        paths.fcntl.flock(fd, paths.fcntl.LOCK_UN)
        self.assertTrue(paths.restore_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", sha1))
        self.assertEqual(self.loser.read_bytes(), b"takeout")

    def test_a_removal_refused_because_the_keeper_is_busy_leaves_the_other_removals_claim(self):
        sha1 = paths.file_sha1(self.loser)
        private = self.loser.parent / paths.claim_name(sha1, self.loser.name)
        private.mkdir(mode=paths.PRIVATE_MODE)
        self.loser.rename(private / paths.CLAIMED)
        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)), self.assertRaises(paths.KeeperBusy):
            self.remove(losing_sha1=sha1)
        self.assertTrue((private / paths.CLAIMED).exists())

    def test_a_keeper_sidecar_changed_after_the_carry_keeps_the_loser(self):
        sidecar = self.keeper.with_name(f"{self.keeper.name}.xmp")
        real = paths.Keeper.still_there

        def changing(keeper):
            if sidecar.exists():
                sidecar.write_bytes(b"replaced by someone else")
            return real(keeper)

        with mock.patch.object(paths.Keeper, "still_there", changing), self.assertRaises(paths.NotALosingCopy):
            paths.remove_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", paths.file_sha1(self.loser), "jo/originals/immich/IMG_0001-aaaa.jpg",
                                     paths.file_sha1(self.keeper), lambda keeper: keeper.write_sidecar(b"carried", None) or b"carried")
        self.assertEqual(self.loser.read_bytes(), b"takeout")

    def test_a_sidecar_replaced_right_after_the_carry_published_it_keeps_the_loser(self):
        sidecar = self.keeper.with_name(f"{self.keeper.name}.xmp")

        def carry(keeper):
            keeper.write_sidecar(b"carried with Google's metadata", None)
            self.file("jo/originals/immich/incoming", b"another writer's sidecar without it")
            os.replace(self.keeper.with_name("incoming"), sidecar)
            return b"carried with Google's metadata"

        with self.assertRaises(paths.NotALosingCopy):
            paths.remove_losing_copy(self.photos, "jo", "jo/google-only/2021/03/IMG_0001.jpg", paths.file_sha1(self.loser), "jo/originals/immich/IMG_0001-aaaa.jpg",
                                     paths.file_sha1(self.keeper), carry)
        self.assertEqual(self.loser.read_bytes(), b"takeout")

    def test_a_250_byte_name_is_claimed_and_removed(self):
        long = "a" * 246 + ".jpg"
        self.loser.rename(self.loser.with_name(long))
        self.loser.with_name("IMG_0001.jpg.xmp").unlink()
        self.assertTrue(self.remove(losing=f"jo/google-only/2021/03/{long}", losing_sha1=paths.file_sha1(self.loser.with_name(long))))
        self.assertEqual(list(self.loser.parent.iterdir()), [])

    def test_the_keepers_sidecar_is_read_and_written_only_through_its_pinned_folder(self):
        sidecar = self.keeper.with_name(f"{self.keeper.name}.xmp")
        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)) as keeper:
            self.assertIsNone(keeper.sidecar())
            keeper.write_sidecar(b"first", None)
            self.assertEqual((keeper.sidecar(), sidecar.read_bytes()), (b"first", b"first"))
            keeper.write_sidecar(b"second", b"first")
            with self.assertRaises(paths.NotALosingCopy):
                keeper.write_sidecar(b"third", b"first")
        self.assertEqual(sidecar.read_bytes(), b"second")
        self.assertEqual([p.name for p in self.keeper.parent.iterdir() if p.name.startswith(".xmp-")], [])
        sidecar.unlink()
        sidecar.symlink_to(self.loser)
        with paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", paths.file_sha1(self.keeper)) as keeper, self.assertRaises(paths.NotALosingCopy):
            keeper.write_sidecar(b"through a link", None)
        self.assertEqual(self.loser.read_bytes(), b"takeout")
        with self.assertRaises(paths.NotALosingCopy), paths.checked_keeper(self.photos, "jo", "jo/originals/immich/IMG_0001-aaaa.jpg", "a" * 40):
            self.fail("a keeper that is not the recorded file is never yielded")


class InboxLinkTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        self.photos.ensure_person_tree("jo")
        self.originals = self.root / "jo/originals"
        self.kept = self.originals / "IMG_1.JPG"
        self.kept.write_bytes(b"an original")
        self.export = self.root / "_inbox/jo/export-1"
        self.export.mkdir()

    def originals_tree(self):
        return {str(p.relative_to(self.originals)): p.read_bytes() for p in self.originals.rglob("*") if p.is_file()}

    def test_a_link_on_the_way_to_an_inbox_folder_is_refused(self):
        (self.export / "staging").symlink_to(self.originals, target_is_directory=True)
        (self.root / "_inbox/jo/review").symlink_to(self.originals, target_is_directory=True)
        (self.root / "_inbox/jo/.thumbs").symlink_to(self.originals, target_is_directory=True)
        for call in (lambda: self.photos.staging("jo", "export-1"), lambda: self.photos.review_hold("jo", "export-1"),
                     lambda: self.photos.thumbs("jo")):
            with self.subTest(call=call), self.assertRaises(paths.LinkRefused):
                call()
        self.assertTrue(issubclass(paths.LinkRefused, paths.OutsideRoot))

    def test_a_linked_export_or_manual_folder_is_refused(self):
        (self.root / "_inbox/jo/export-2").symlink_to(self.root / "jo", target_is_directory=True)
        (self.root / "jo/staging").mkdir()
        with self.assertRaises(paths.LinkRefused):
            self.photos.staging("jo", "export-2")
        (self.root / "_inbox/jo/manual").rmdir()
        (self.root / "_inbox/jo/manual").symlink_to(self.originals, target_is_directory=True)
        with self.assertRaises(paths.LinkRefused):
            self.photos.manual_inbox("jo")

    def test_a_folder_without_links_is_its_own_path(self):
        self.assertEqual(self.photos.staging("jo", "export-1"), self.export / "staging")
        self.assertEqual(self.photos.in_the_inbox("jo", "manual"), self.root / "_inbox/jo/manual")

    def test_only_places_inside_the_inbox_are_opened(self):
        for path in (Path("_inbox/jo/a"), self.originals / "IMG_1.JPG", self.root / "_inbox/../jo/originals/IMG_1.JPG", self.root.parent / "x"):
            with self.subTest(path=path), self.assertRaises(paths.OutsideRoot):
                paths.open_inbox_file(self.photos, path)

    def test_an_inbox_file_is_read_without_following_a_link(self):
        plain = self.export / "a.txt"
        plain.write_bytes(b"plain")
        with paths.open_inbox_file(self.photos, plain) as handle:
            self.assertEqual(handle.read(), b"plain")
        self.assertEqual(b"".join(paths.inbox_chunks(self.photos, plain)), b"plain")
        (self.export / "link.txt").symlink_to(self.kept)
        (self.export / "hop").symlink_to(self.originals, target_is_directory=True)
        (self.export / "folder").mkdir()
        os.mkfifo(self.export / "pipe")
        for path in (self.export / "link.txt", self.export / "hop/IMG_1.JPG", self.export / "folder", self.export / "pipe"):
            with self.subTest(path=path), self.assertRaises(paths.LinkRefused):
                paths.inbox_sha1(self.photos, path)

    def test_a_hard_linked_file_is_never_read(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        outside = Path(other.name).resolve() / "part.zip"
        outside.write_bytes(b"zip")
        os.link(self.kept, self.export / "hard.jpg")
        os.link(outside, Path(other.name).resolve() / "twin.zip")
        for read in (lambda: paths.inbox_sha1(self.photos, self.export / "hard.jpg"), lambda: paths.open_import_part(self.photos, self.export / "hard.jpg"),
                     lambda: paths.plain_sha1(outside)):
            with self.subTest(read=read), self.assertRaises(paths.LinkRefused):
                read()
        self.assertEqual(paths.plain_entries(str(self.export), ["hard.jpg"]), [])
        self.assertEqual(self.originals_tree(), {"IMG_1.JPG": b"an original"})

    def test_a_file_outside_the_inbox_is_never_read_through_a_linked_folder(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        real = Path(other.name).resolve() / "real"
        real.mkdir()
        (real / "part.zip").write_bytes(b"zip")
        (Path(other.name).resolve() / "linked").symlink_to(real, target_is_directory=True)
        with paths.open_plain_file(real / "part.zip") as handle:
            self.assertEqual(handle.read(), b"zip")
        with self.assertRaises(paths.LinkRefused):
            paths.open_plain_file(Path(other.name).resolve() / "linked/part.zip")

    def test_an_import_part_outside_the_inbox_is_read_but_never_through_a_link(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        elsewhere = Path(other.name).resolve()
        (elsewhere / "part.zip").write_bytes(b"zip")
        with paths.open_import_part(self.photos, elsewhere / "part.zip") as handle:
            self.assertEqual(handle.read(), b"zip")
        (elsewhere / "linked.zip").symlink_to(elsewhere / "part.zip")
        (elsewhere / "into").symlink_to(self.export, target_is_directory=True)
        (self.export / "part.zip").write_bytes(b"zip")
        for path in (elsewhere / "linked.zip", elsewhere / "into/part.zip"):
            with self.subTest(path=path), self.assertRaises(paths.LinkRefused):
                paths.open_import_part(self.photos, path)
        with paths.open_import_part(self.photos, self.export / "part.zip") as handle:
            self.assertEqual(handle.read(), b"zip")
        self.assertEqual(paths.plain_sha1(elsewhere / "part.zip"), paths.inbox_sha1(self.photos, self.export / "part.zip"))

    def test_a_listing_keeps_only_plain_files(self):
        (self.export / "a.jpg").write_bytes(b"a")
        (self.export / "b.jpg").symlink_to(self.kept)
        (self.export / "c").mkdir()
        self.assertEqual(paths.plain_entries(str(self.export), ["b.jpg", "a.jpg", "c", "gone.jpg"]), ["a.jpg"])

    def test_an_entry_is_written_in_a_pinned_folder_and_never_through_a_link(self):
        with paths.inbox_folder(self.photos, self.export / "staging", create=True) as staged:
            with paths.inbox_subfolder(staged, ["Takeout", "Photos"], create=True) as folder:
                out = paths.open_inbox_entry("a.jpg", folder, 3)
                assert out is not None
                with out:
                    out.write(b"abc")
                self.assertIsNone(paths.open_inbox_entry("a.jpg", folder, 3))
                out = paths.open_inbox_entry("a.jpg", folder, 2)
                assert out is not None
                with out:
                    out.write(b"xy")
                os.symlink(self.kept, "link.jpg", dir_fd=folder)
                os.link(self.kept, self.export / "staging/Takeout/Photos/hard.jpg")
                for name in ("link.jpg", "hard.jpg"):
                    with self.subTest(name=name), self.assertRaises(paths.LinkRefused):
                        paths.open_inbox_entry(name, folder, 1)
            (self.export / "staging/Takeout/hop").symlink_to(self.originals, target_is_directory=True)
            with self.assertRaises(paths.LinkRefused), paths.inbox_subfolder(staged, ["Takeout", "hop"], create=True):
                pass
            (self.export / "staging/Takeout/file").write_bytes(b"")
            with self.assertRaises(paths.LinkRefused), paths.inbox_subfolder(staged, ["Takeout", "file"], create=False):
                pass
        self.assertEqual((self.export / "staging/Takeout/Photos/a.jpg").read_bytes(), b"xy")
        self.assertEqual(self.originals_tree(), {"IMG_1.JPG": b"an original"})

    def test_a_folder_swapped_for_a_link_after_the_check_is_never_entered(self):
        staging = self.photos.staging("jo", "export-1")
        staging.symlink_to(self.originals, target_is_directory=True)
        with self.assertRaises(paths.LinkRefused), paths.inbox_folder(self.photos, staging, create=True):
            pass
        with self.assertRaises(paths.LinkRefused):
            paths.write_inbox_once(self.photos, staging / "IMG_2.JPG", [b"new"])
        self.assertEqual(self.originals_tree(), {"IMG_1.JPG": b"an original"})

    def test_a_marker_is_set_once_and_a_linked_marker_is_refused(self):
        with paths.inbox_folder(self.photos, self.export / ".markers", create=True) as markers:
            self.assertFalse(paths.inbox_marker_set("0001", markers))
            paths.set_inbox_marker("0001", markers)
            self.assertTrue(paths.inbox_marker_set("0001", markers))
            os.symlink(self.kept, "0002", dir_fd=markers)
            with self.assertRaises(paths.LinkRefused):
                paths.inbox_marker_set("0002", markers)
            with self.assertRaises(FileExistsError):
                paths.set_inbox_marker("0002", markers)
        self.assertEqual(self.originals_tree(), {"IMG_1.JPG": b"an original"})

    def test_write_inbox_once_writes_a_new_file_and_never_replaces_one(self):
        hold = self.root / "_inbox/jo/review/export-1/a.jpg"
        digest = paths.write_inbox_once(self.photos, hold, [b"he", b"ld"])
        self.assertEqual((hold.read_bytes(), digest), (b"held", paths.file_sha1(hold)))
        with self.assertRaises(paths.AlreadyExists):
            paths.write_inbox_once(self.photos, hold, [b"other"])
        self.assertEqual(hold.read_bytes(), b"held")
        self.assertEqual([p.name for p in hold.parent.iterdir()], ["a.jpg"])

    def test_write_inbox_once_refuses_a_file_that_changed_while_it_was_copied(self):
        hold = self.root / "_inbox/jo/review/export-1/a.jpg"
        with self.assertRaises(paths.ChangedWhileCopied):
            paths.write_inbox_once(self.photos, hold, [b"swapped"], "0" * 40)
        self.assertEqual(list(hold.parent.iterdir()), [])

    def test_write_inbox_once_copies_where_hard_links_are_not_possible(self):
        hold = self.root / "_inbox/jo/review/export-1/a.jpg"
        with mock.patch.object(paths.os, "link", side_effect=PermissionError("no hard links")):
            paths.write_inbox_once(self.photos, hold, [b"held"])
        self.assertEqual(hold.read_bytes(), b"held")
        self.assertEqual([p.name for p in hold.parent.iterdir()], ["a.jpg"])
        with mock.patch.object(paths.os, "link", side_effect=PermissionError("no hard links")), \
                mock.patch.object(paths, "copy_fd", side_effect=OSError("disk full")), self.assertRaises(OSError):
            paths.write_inbox_once(self.photos, hold.with_name("b.jpg"), [b"held"])
        self.assertEqual([p.name for p in hold.parent.iterdir()], ["a.jpg"])
        (hold.parent / "c.jpg").symlink_to(self.kept)
        with paths.inbox_folder(self.photos, hold.parent, create=False) as folder, self.assertRaises(paths.AlreadyExists):
            paths.copy_entry("a.jpg", "c.jpg", folder)
        self.assertEqual(self.kept.read_bytes(), b"an original")


if __name__ == "__main__":
    unittest.main()
