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


if __name__ == "__main__":
    unittest.main()
