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



class PlacingMarkerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.destination = self.folder / "a.jpg"
        self.marker = paths.placing_marker(self.destination, "0123456789abcdef")

    def test_the_marker_is_the_same_file_as_the_copy_from_the_start(self):
        paths.write_once(self.destination, [b"photo"], self.marker)
        self.assertTrue(os.path.samefile(self.marker, self.destination))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [".placing-0123456789abcdef-a.jpg", "a.jpg"])

    def test_a_copy_that_was_not_made_leaves_no_marker(self):
        self.destination.write_bytes(b"already there")
        with self.assertRaises(paths.AlreadyExists):
            paths.write_once(self.destination, [b"photo"], self.marker)
        with mock.patch("os.link", side_effect=OSError("cross-device")):
            paths.write_once(self.folder / "b.jpg", [b"photo"], paths.placing_marker(self.folder / "b.jpg", "0123456789abcdef"))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["a.jpg", "b.jpg"])

    def test_clearing_keeps_the_markers_of_what_a_failed_attempt_could_not_remove(self):
        paths.write_once(self.destination, [b"photo"], self.marker)
        paths.mark_new_sidecar(self.destination, "0123456789abcdef")
        sidecar_marker = paths.placing_marker(paths.sidecar_of(self.destination), "0123456789abcdef")
        paths.clear_placing(self.destination, "0123456789abcdef", committed=False)
        self.assertTrue(self.marker.is_file())
        self.assertFalse(sidecar_marker.exists())
        self.destination.unlink()
        paths.clear_placing(self.destination, "0123456789abcdef", committed=False)
        self.assertFalse(self.marker.exists())
        paths.write_once(self.destination, [b"photo"], self.marker)
        paths.clear_placing(self.destination, "0123456789abcdef", committed=True)
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

if __name__ == "__main__":
    unittest.main()
