import contextlib
import hashlib
import os
import tempfile
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
        return paths.place_pixel_copy(self.photos, pixel_id, person, f"{person}/originals/2024/IMG_0001.HEIC", sha1(self.data if person == "jo" else b"sam's photo"))

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
            paths.place_pixel_copy(self.photos, 1, "jo", "jo/originals/2024/IMG_0001.HEIC", "0" * 40)
        self.assertFalse((self.root / "_pixels/1/jo/2024/IMG_0001.HEIC").exists())
        with self.assertRaises(paths.OutsideRoot):
            paths.place_pixel_copy(self.photos, 1, "jo", "sam/originals/2024/IMG_0001.HEIC", sha1(b"sam's photo"))
        with self.assertRaises(paths.OutsideRoot):
            paths.place_pixel_copy(self.photos, 1, "jo", "jo/google-only/g.jpg", sha1(b"google only"))
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


if __name__ == "__main__":
    unittest.main()
