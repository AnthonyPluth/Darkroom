import contextlib
import errno
import hashlib
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from darkroom.storage import paths


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): sha1(path.read_bytes()) for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}


class PixelFolderPathTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        for slug in ("jo", "sam"):
            (self.root / slug / "originals" / "2024").mkdir(parents=True)
            (self.root / slug / "google-only").mkdir(parents=True)
        self.data = b"jo's photo"
        (self.root / "jo/originals/2024/IMG_0001.HEIC").write_bytes(self.data)
        (self.root / "sam/originals/2024/IMG_0001.HEIC").write_bytes(b"sam's photo")
        (self.root / "jo/google-only/g.jpg").write_bytes(b"google only")
        self.kept = {name: digest for name, digest in tree(self.root).items() if "/originals/" in name or "/google-only/" in name}

    def assert_kept_unchanged(self):
        now = {name: digest for name, digest in tree(self.root).items() if "/originals/" in name or "/google-only/" in name}
        self.assertEqual(now, self.kept)

    def place(self, pixel_id: int = 1, person: str = "jo") -> str:
        return paths.place_pixel_copy(self.photos, pixel_id, person, f"{person}/originals/2024/IMG_0001.HEIC", sha1(self.data if person == "jo" else b"sam's photo"), time.time())

    def test_the_folder_of_a_pixel_and_person(self):
        self.assertEqual(self.photos.pixel_folder(3, "jo"), self.root / "_pixels/3/jo")
        self.assertEqual(self.photos.pixel_copy(3, "jo", "2024/a.jpg"), self.root / "_pixels/3/jo/2024/a.jpg")
        for bad in (0, -1, True):
            with self.assertRaises(paths.OutsideRoot):
                self.photos.pixel_folder(bad, "jo")
        for bad in ("..", "Jo", "a/b", ""):
            with self.assertRaises(paths.OutsideRoot):
                self.photos.pixel_folder(1, bad)

    def test_a_copy_is_a_separate_file_with_the_originals_bytes(self):
        name = self.place()
        self.assertEqual(name, "2024/IMG_0001.HEIC")
        copy = self.root / "_pixels/1/jo/2024/IMG_0001.HEIC"
        self.assertEqual(copy.read_bytes(), self.data)
        self.assertNotEqual(copy.stat().st_ino, (self.root / "jo/originals/2024/IMG_0001.HEIC").stat().st_ino)
        self.assertEqual(list(self.photos.pixel_incoming().iterdir()), [])
        self.assertEqual(self.place(), name)
        self.assert_kept_unchanged()

    def test_a_copy_is_refused_when_the_original_changed_or_is_not_in_originals(self):
        with self.assertRaises(paths.NotAPixelCopy):
            paths.place_pixel_copy(self.photos, 1, "jo", "jo/originals/2024/IMG_0001.HEIC", "0" * 40, time.time())
        self.assertFalse((self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").exists())
        with self.assertRaises(paths.OutsideRoot):
            paths.place_pixel_copy(self.photos, 1, "jo", "sam/originals/2024/IMG_0001.HEIC", sha1(b"sam's photo"), time.time())
        with self.assertRaises(paths.OutsideRoot):
            paths.place_pixel_copy(self.photos, 1, "jo", "jo/google-only/g.jpg", sha1(b"google only"), time.time())
        self.assert_kept_unchanged()

    def test_pruning_removes_only_the_copy_and_its_empty_folders(self):
        name = self.place()
        self.assertTrue(paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data)))
        self.assertFalse((self.root / "_pixels/1/jo/2024").exists())
        self.assertTrue((self.root / "_pixels/1/jo").is_dir())
        self.assertFalse(paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data)))
        self.assert_kept_unchanged()

    def test_pruning_never_reaches_originals_google_only_or_another_persons_folder(self):
        self.place(1, "jo")
        self.place(1, "sam")
        for bad in ("../../../jo/originals/2024/IMG_0001.HEIC", "../sam/2024/IMG_0001.HEIC", "../../2/jo/x", "2024/../../sam/2024/IMG_0001.HEIC",
                    ".hidden", "", "/etc/passwd", "a//b"):
            with self.subTest(bad), self.assertRaises(paths.OutsideRoot):
                paths.remove_pixel_copy(self.photos, 1, "jo", bad, sha1(self.data))
        self.assertTrue((self.root / "_pixels/1/sam/2024/IMG_0001.HEIC").is_file())
        self.assert_kept_unchanged()

    def test_a_link_in_the_pixel_folder_is_never_followed(self):
        os.makedirs(self.root / "_pixels/1/jo/2024")
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").symlink_to(self.root / "jo/originals/2024/IMG_0001.HEIC")
        with self.assertRaises(paths.OutsideRoot):
            paths.remove_pixel_copy(self.photos, 1, "jo", "2024/IMG_0001.HEIC", sha1(self.data))
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").unlink()
        (self.root / "_pixels/1/jo/2024").rmdir()
        (self.root / "_pixels/1/jo/2024").symlink_to(self.root / "jo/originals/2024")
        with self.assertRaises(paths.OutsideRoot):
            paths.remove_pixel_copy(self.photos, 1, "jo", "2024/IMG_0001.HEIC", sha1(self.data))
        self.assert_kept_unchanged()

    def test_a_person_folder_that_links_to_originals_is_refused(self):
        (self.root / "_pixels/1").mkdir(parents=True)
        (self.root / "_pixels/1/jo").symlink_to(self.root / "jo/originals")
        with self.assertRaises(paths.OutsideRoot):
            paths.remove_pixel_copy(self.photos, 1, "jo", "2024/IMG_0001.HEIC", sha1(self.data))
        with self.assertRaises(paths.OutsideRoot):
            self.place()
        (self.root / "_pixels/1/jo").unlink()
        (self.root / "_pixels/1").rmdir()
        (self.root / "_pixels").rmdir()
        (self.root / "_pixels").symlink_to(self.root / "jo")
        with self.assertRaises(paths.OutsideRoot):
            paths.remove_pixel_copy(self.photos, 1, "jo", "2024/IMG_0001.HEIC", sha1(self.data))
        self.assert_kept_unchanged()

    def test_a_staging_folder_that_links_into_originals_is_refused(self):
        (self.root / "_pixels").mkdir()
        (self.root / "_pixels/.incoming").symlink_to(self.root / "jo/originals")
        with self.assertRaises(paths.OutsideRoot):
            self.photos.pixel_incoming()
        with self.assertRaises(paths.OutsideRoot):
            self.place()
        self.assertEqual(sorted(path.name for path in (self.root / "jo/originals").iterdir()), ["2024"])
        self.assertFalse((self.root / "_pixels/1").exists())
        (self.root / "_pixels/.incoming").unlink()
        (self.root / "_pixels/.incoming").symlink_to(self.root / "jo/originals/not-there")
        with self.assertRaises(paths.OutsideRoot):
            self.place()
        self.assertFalse((self.root / "jo/originals/not-there").exists())
        self.assert_kept_unchanged()

    def unchecked(self):
        def pixel_copy(photos, pixel_id, person, name):
            return photos.root / "_pixels" / str(pixel_id) / person / name

        def pixel_incoming(photos):
            return photos.root / "_pixels" / ".incoming"

        return mock.patch.multiple(paths.Photos, pixel_copy=pixel_copy, pixel_incoming=pixel_incoming)

    def test_a_staging_folder_swapped_for_a_link_after_the_check_is_never_written_through(self):
        (self.root / "_pixels").mkdir()
        (self.root / "_pixels/.incoming").symlink_to(self.root / "jo/originals")
        with self.unchecked(), self.assertRaises(paths.NotAPixelCopy):
            self.place()
        self.assertEqual(sorted(path.name for path in (self.root / "jo/originals").iterdir()), ["2024"])
        self.assertFalse((self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").exists())
        self.assert_kept_unchanged()

    def test_a_destination_folder_swapped_for_a_link_after_the_check_is_never_written_through(self):
        (self.root / "jo/originals/other").mkdir()
        (self.root / "_pixels/1/jo").mkdir(parents=True)
        (self.root / "_pixels/1/jo/2024").symlink_to(self.root / "jo/originals/other")
        for link_fails in (False, True):
            with self.subTest(link_fails=link_fails), self.unchecked(), self.assertRaises(paths.NotAPixelCopy), \
                    mock.patch("darkroom.storage.paths.os.link", side_effect=OSError("no hard links")) if link_fails else contextlib.nullcontext():
                self.place()
            self.assertEqual(list((self.root / "jo/originals/other").iterdir()), [])
            self.assertFalse((self.root / "_pixels/.incoming").exists() and any((self.root / "_pixels/.incoming").iterdir()))
        self.assert_kept_unchanged()

    def test_a_folder_swapped_for_a_link_into_originals_while_pruning_is_refused(self):
        name = self.place()
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").unlink()
        (self.root / "_pixels/1/jo/2024").rmdir()
        (self.root / "_pixels/1/jo/2024").symlink_to(self.root / "jo/originals/2024")
        with self.unchecked(), self.assertRaises(paths.NotAPixelCopy):
            paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        self.assertEqual((self.root / "jo/originals/2024/IMG_0001.HEIC").read_bytes(), self.data)
        self.assert_kept_unchanged()

    def test_a_copy_replaced_after_it_was_hashed_is_not_pruned(self):
        name = self.place()
        copy = self.root / "_pixels/1/jo" / name
        real = paths.fd_sha1

        def replaced_after_hashing(fd: int) -> str:
            found = real(fd)
            (copy.parent / "other").write_bytes(b"another file")
            os.replace(copy.parent / "other", copy)
            return found

        with mock.patch("darkroom.storage.paths.fd_sha1", replaced_after_hashing), self.assertRaises(paths.NotAPixelCopy):
            paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        self.assertEqual(copy.read_bytes(), b"another file")
        self.assert_kept_unchanged()

    def test_a_copy_is_made_on_a_share_without_hard_links_and_others_can_read_it(self):
        with mock.patch("darkroom.storage.paths.os.link", side_effect=OSError("no hard links here")):
            name = self.place()
        copy = self.root / "_pixels/1/jo" / name
        self.assertEqual(copy.read_bytes(), self.data)
        self.assertEqual(copy.stat().st_mode & 0o044, 0o044)
        self.assertEqual(list(self.photos.pixel_incoming().iterdir()), [])
        self.assert_kept_unchanged()

    def test_a_different_file_already_in_the_copys_place_is_never_replaced(self):
        (self.root / "_pixels/1/jo/2024").mkdir(parents=True)
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").write_bytes(b"something else")
        with self.assertRaises(paths.AlreadyExists):
            self.place()
        self.assertEqual((self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").read_bytes(), b"something else")
        self.assertEqual(list(self.photos.pixel_incoming().iterdir()), [])
        self.assert_kept_unchanged()

    def test_folders_made_for_a_copy_can_be_traversed_by_others_under_any_umask(self):
        (self.root / "_pixels").mkdir()
        os.chmod(self.root / "_pixels", 0o750)
        old = os.umask(0o077)
        try:
            name = self.place()
        finally:
            os.umask(old)
        for folder in ("_pixels/1", "_pixels/1/jo", "_pixels/1/jo/2024"):
            self.assertEqual((self.root / folder).stat().st_mode & 0o777, 0o755, folder)
        self.assertEqual((self.root / "_pixels").stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.root / "_pixels/1/jo" / name).stat().st_mode & 0o777, 0o644)

    def test_a_kept_folder_mounted_inside_the_pixel_folder_is_refused(self):
        name = self.place()
        kept = (self.root / "jo/originals").stat()
        real_fstat = os.fstat

        def mounted(fd: int) -> os.stat_result:
            found = real_fstat(fd)
            if os.path.samestat(found, (self.root / "_pixels/1/jo/2024").stat()):
                return kept
            return found

        with mock.patch("darkroom.storage.paths.os.fstat", mounted), self.assertRaises(paths.NotAPixelCopy):
            paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        self.assertTrue((self.root / "_pixels/1/jo" / name).is_file())
        self.assert_kept_unchanged()

    def test_a_folder_below_originals_bind_mounted_into_the_pixel_folder_is_refused(self):
        name = self.place()
        mountinfo = self.root / "mountinfo"
        point = str(self.root / "_pixels/1/jo/2024").replace(" ", "\\040")
        mountinfo.write_text(f"36 35 98:0 /jo/originals/2024 {point} rw,noatime master:1 - ext4 /dev/root rw\n")
        with mock.patch.object(paths, "MOUNTINFO", mountinfo), mock.patch.object(paths.sys, "platform", "linux"):
            self.assertEqual(paths.mounts_under(self.root / "_pixels"), [self.root / "_pixels/1/jo/2024"])
            with self.assertRaises(paths.NotAPixelCopy):
                paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
            with self.assertRaises(paths.NotAPixelCopy):
                paths.place_pixel_copy(self.photos, 1, "jo", "jo/originals/2024/IMG_0001.HEIC", sha1(self.data), time.time())
        self.assertTrue((self.root / "_pixels/1/jo" / name).is_file())
        self.assert_kept_unchanged()

    def test_a_folder_on_another_device_inside_the_pixel_folder_is_refused(self):
        name = self.place()
        (self.root / "jo/google-only/2024").mkdir()
        below = (self.root / "jo/google-only/2024").stat()
        other_device = os.stat_result((below[0], below[1], below[2] + 1, *below[3:10]))
        real_fstat = os.fstat

        def mounted(fd: int) -> os.stat_result:
            found = real_fstat(fd)
            return other_device if os.path.samestat(found, (self.root / "_pixels/1/jo/2024").stat()) else found

        with mock.patch("darkroom.storage.paths.os.fstat", mounted), self.assertRaises(paths.NotAPixelCopy):
            paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        self.assertTrue((self.root / "_pixels/1/jo" / name).is_file())
        self.assert_kept_unchanged()

    def test_unreadable_mount_information_refuses_placing_and_pruning(self):
        name = self.place()
        with mock.patch.object(paths, "MOUNTINFO", self.root / "missing"), mock.patch.object(paths.sys, "platform", "linux"):
            with self.assertRaises(paths.NotAPixelCopy):
                paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
            with self.assertRaises(paths.NotAPixelCopy):
                paths.place_pixel_copy(self.photos, 2, "jo", "jo/originals/2024/IMG_0001.HEIC", sha1(self.data), time.time())
        self.assertTrue((self.root / "_pixels/1/jo" / name).is_file())
        self.assertFalse((self.root / "_pixels/2/jo/2024/IMG_0001.HEIC").exists())
        self.assert_kept_unchanged()

    def test_the_pixels_folder_itself_mounted_from_elsewhere_is_refused(self):
        name = self.place()
        mountinfo = self.root / "mountinfo"
        mountinfo.write_text(f"36 35 98:0 /jo/originals {self.root / '_pixels'} rw,noatime master:1 - ext4 /dev/root rw\n")
        with mock.patch.object(paths, "MOUNTINFO", mountinfo), mock.patch.object(paths.sys, "platform", "linux"):
            with self.assertRaises(paths.NotAPixelCopy):
                paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        self.assertTrue((self.root / "_pixels/1/jo" / name).is_file())
        self.assert_kept_unchanged()

    def test_mount_information_is_read_only_on_linux(self):
        with mock.patch.object(paths, "MOUNTINFO", self.root / "missing"), mock.patch.object(paths.sys, "platform", "darwin"):
            self.assertEqual(paths.mounts_under(self.root / "_pixels"), [])

    def test_a_file_that_is_not_the_placed_copy_is_not_pruned(self):
        name = self.place()
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").write_bytes(b"changed on the way")
        with self.assertRaises(paths.NotAPixelCopy):
            paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").unlink()
        (self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").mkdir()
        with self.assertRaises(paths.NotAPixelCopy):
            paths.remove_pixel_copy(self.photos, 1, "jo", name, sha1(self.data))
        self.assert_kept_unchanged()


    def no_links(self):
        return mock.patch("darkroom.storage.paths.os.link", side_effect=OSError("no hard links here"))

    def crash_in_a_child(self, crash):
        child = os.fork()
        if child == 0:
            try:
                with self.no_links(), crash:
                    self.place()
            finally:
                os._exit(0)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    def test_a_crash_while_placing_without_hard_links_never_leaves_part_of_a_copy(self):
        def exit_now(*_args, **_kwargs):
            os._exit(0)

        def half_then_exit(source, target):
            os.write(target, self.data[: len(self.data) // 2])
            os._exit(0)

        real_fsync = os.fsync

        def exit_after_publishing(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                os._exit(0)
            real_fsync(fd)

        crashes = {
            "while staging": (mock.patch("darkroom.storage.paths.copy_fd", half_then_exit), None),
            "after claiming the name": (mock.patch("darkroom.storage.paths.replace_claim", exit_now), b""),
            "just before the rename": (mock.patch("darkroom.storage.paths.os.rename", exit_now), b""),
            "after the rename": (mock.patch("darkroom.storage.paths.os.fsync", exit_after_publishing), self.data),
        }
        copy = self.root / "_pixels/1/jo/2024/IMG_0001.HEIC"
        for point, (crash, left) in crashes.items():
            with self.subTest(point):
                with contextlib.suppress(FileNotFoundError):
                    copy.unlink()
                self.crash_in_a_child(crash)
                self.assertEqual(copy.read_bytes() if copy.exists() else None, left)
                with self.no_links():
                    self.place()
                self.assertEqual(copy.read_bytes(), self.data)
                self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o644)
                self.assert_kept_unchanged()

    def test_a_crash_right_after_the_hard_link_leaves_a_copy_the_next_pass_accepts(self):
        def exit_now(*_args, **_kwargs):
            os._exit(0)

        copy = self.root / "_pixels/1/jo/2024/IMG_0001.HEIC"
        child = os.fork()
        if child == 0:
            try:
                with mock.patch("darkroom.storage.paths.os.unlink", exit_now):
                    self.place()
            finally:
                os._exit(0)
        os.waitpid(child, 0)
        self.assertEqual(copy.read_bytes(), self.data)
        self.assertEqual(copy.stat().st_nlink, 2)
        self.assertEqual(self.place(), "2024/IMG_0001.HEIC")
        self.assertEqual(copy.stat().st_nlink, 1)
        self.assertEqual(list(self.photos.pixel_incoming().iterdir()), [])
        self.assert_kept_unchanged()

    def test_a_copy_linked_to_a_staging_name_is_refused_unless_it_matches_with_exactly_two_links(self):
        copy = self.root / "_pixels/1/jo/2024/IMG_0001.HEIC"
        copy.parent.mkdir(parents=True)
        incoming = self.root / "_pixels/.incoming"
        incoming.mkdir()
        staged = incoming / ".copy-0123456789abcdef"
        copy.write_bytes(b"not the photo")
        os.link(copy, staged)
        with self.assertRaises(paths.NotAPixelCopy):
            self.place()
        self.assertTrue(os.path.samefile(copy, staged))
        self.assertEqual(copy.read_bytes(), b"not the photo")
        copy.unlink()
        staged.unlink()
        copy.write_bytes(self.data)
        os.link(copy, staged)
        os.link(copy, self.root / "elsewhere")
        with self.assertRaises(paths.NotAPixelCopy):
            self.place()
        self.assertTrue(os.path.samefile(copy, staged))
        self.assertEqual(copy.stat().st_nlink, 3)
        self.assert_kept_unchanged()

    def test_an_empty_leftover_in_the_copys_place_is_replaced(self):
        copy = self.root / "_pixels/1/jo/2024/IMG_0001.HEIC"
        for link_fails in (True, False):
            with self.subTest(link_fails=link_fails):
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.unlink(missing_ok=True)
                copy.touch()
                with self.no_links() if link_fails else contextlib.nullcontext():
                    self.place()
                self.assertEqual(copy.read_bytes(), self.data)
                self.assertEqual(copy.stat().st_nlink, 1)
                self.assertEqual(list(self.photos.pixel_incoming().iterdir()), [])
        self.assert_kept_unchanged()

    def test_a_staging_folder_on_another_disk_leaves_nothing_at_the_copys_place(self):
        with self.no_links(), mock.patch("darkroom.storage.paths.os.rename", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(paths.NotAPixelCopy):
                self.place()
        self.assertFalse((self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").exists())
        self.assertEqual(list(self.photos.pixel_incoming().iterdir()), [])
        self.assert_kept_unchanged()

    def test_stale_staging_files_are_swept_and_nothing_else_is(self):
        incoming = self.root / "_pixels/.incoming"
        incoming.mkdir(parents=True)
        old = 1_000_000.0
        stale = incoming / ".copy-0123456789abcdef"
        fresh = incoming / ".copy-fedcba9876543210"
        link = incoming / ".copy-1111111111111111"
        folder = incoming / ".copy-2222222222222222"
        unrelated = incoming / "notes"
        stale.write_bytes(b"half a photo")
        fresh.write_bytes(b"being written")
        link.symlink_to(self.root / "jo/originals/2024/IMG_0001.HEIC")
        folder.mkdir()
        unrelated.write_bytes(b"not Darkroom's")
        for each in (stale, link, folder, unrelated):
            os.utime(each, (old, old), follow_symlinks=False)
        now = old + 3601
        os.utime(fresh, (now - 60, now - 60))
        paths.place_pixel_copy(self.photos, 1, "jo", "jo/originals/2024/IMG_0001.HEIC", sha1(self.data), now)
        self.assertFalse(stale.exists())
        self.assertEqual(fresh.read_bytes(), b"being written")
        self.assertTrue(link.is_symlink())
        self.assertTrue(folder.is_dir())
        self.assertEqual(unrelated.read_bytes(), b"not Darkroom's")
        self.assertEqual(sorted(each.name for each in incoming.iterdir()), sorted([fresh.name, link.name, folder.name, unrelated.name]))
        self.assert_kept_unchanged()

    def test_a_copy_already_in_place_is_made_readable_by_others(self):
        name = self.place()
        copy = self.root / "_pixels/1/jo" / name
        copy.chmod(0o600)
        self.assertEqual(self.place(), name)
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o644)
        self.assert_kept_unchanged()

    def test_a_hard_link_in_the_copys_place_is_refused(self):
        original = self.root / "jo/originals/2024/IMG_0001.HEIC"
        original.chmod(0o600)
        copy = self.root / "_pixels/1/jo/2024/IMG_0001.HEIC"
        copy.parent.mkdir(parents=True)
        os.link(original, copy)
        with self.assertRaises(paths.NotAPixelCopy):
            self.place()
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        copy.unlink()
        other = self.root / "elsewhere"
        other.write_bytes(self.data)
        other.chmod(0o600)
        os.link(other, copy)
        with self.assertRaises(paths.NotAPixelCopy):
            self.place()
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o600)
        self.assert_kept_unchanged()

if __name__ == "__main__":
    unittest.main()
