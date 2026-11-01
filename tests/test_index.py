import os
import tempfile
import unittest
from pathlib import Path

from darkroom.domain import events, index
from darkroom.providers import exiftool
from darkroom.storage import db
from darkroom.storage.models import MediaItem, Person
from darkroom.storage.paths import Photos
from tests.fixtures import media as fixtures
from tests.shared import add_person, own_database


def listing(root):
    return sorted((str(p), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in Path(root).rglob("*"))


class IndexTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = Photos(self.root)
        self.engine = own_database(self)
        with db.session(self.engine) as opened:
            add_person(opened, "jo")
        self.originals = self.photos.ensure_person_tree("jo")[0]

    def run_index(self, now=100.0, read=exiftool.read):
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            return index.index_person(opened, self.photos, person, now, read)

    def items(self):
        with db.session(self.engine) as opened:
            return {i.path: i for i in opened.query(MediaItem)}

    def test_indexes_photos_and_videos_with_their_fields(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=(40.7, -74.0), make="Apple", model="iPhone")
        fixtures.make_video(self.originals / "sub" / "b.mov")
        report = self.run_index()
        self.assertEqual((report.new, report.unchanged, report.changed, report.failed), (2, 0, 0, 0))
        items = self.items()
        self.assertEqual(sorted(items), ["jo/originals/a.jpg", "jo/originals/sub/b.mov"])
        photo = items["jo/originals/a.jpg"]
        self.assertEqual((photo.source, photo.make, photo.time_source, photo.gps_source), ("phone", "Apple", "exif_offset", "exif"))
        self.assertFalse(photo.confirmed_in_google)

    def test_a_second_run_indexes_nothing_new(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15")
        self.run_index()
        report = self.run_index(now=200.0)
        self.assertEqual((report.new, report.unchanged, report.changed, report.failed), (0, 1, 0, 0))
        self.assertEqual(self.items()["jo/originals/a.jpg"].indexed_at, 100.0)

    def test_a_file_replaced_at_the_same_path_is_indexed_again(self):
        path = fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15", offset="+00:00")
        self.run_index()
        before = self.items()["jo/originals/a.jpg"].sha1
        fixtures.make_jpeg(path, taken="2020:01:01 00:00:00", offset="+00:00", colour=(1, 2, 3), size=(80, 60))
        os.utime(path, (1_900_000_000, 1_900_000_000))
        report = self.run_index(now=300.0)
        self.assertEqual((report.new, report.changed), (0, 1))
        after = self.items()["jo/originals/a.jpg"]
        self.assertNotEqual(after.sha1, before)
        self.assertEqual((after.width, after.indexed_at), (80, 300.0))

    def test_skipped_files_and_folders(self):
        fixtures.make_jpeg(self.originals / "keep.jpg")
        fixtures.make_jpeg(self.originals / ".hidden.jpg")
        fixtures.make_jpeg(self.originals / ".stfolder" / "x.jpg")
        fixtures.make_jpeg(self.originals / ".stversions" / "y.jpg")
        fixtures.make_jpeg(self.originals / ".uploads_tmp" / "z.jpg")
        (self.originals / "keep.jpg.xmp").write_text("<xmp/>")
        self.run_index()
        self.assertEqual(sorted(self.items()), ["jo/originals/keep.jpg"])

    def test_an_unreadable_file_is_reported_and_the_run_completes(self):
        fixtures.make_jpeg(self.originals / "good.jpg", taken="2019:07:04 12:30:15")
        (self.originals / "bad.jpg").write_bytes(b"not a picture")
        report = self.run_index()
        self.assertEqual((report.new, report.failed), (1, 1))
        self.assertEqual(sorted(self.items()), ["jo/originals/good.jpg"])
        with db.session(self.engine) as opened:
            self.assertEqual([e.type for e in events.recent(opened, type="index_failed")], ["index_failed"])

    def test_an_exiftool_error_entry_is_a_failure(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        report = self.run_index(read=lambda paths: {p: {"SourceFile": str(p), "Error": "broken"} for p in paths})
        self.assertEqual((report.new, report.failed), (0, 1))

    def test_exiftool_dying_fails_the_whole_run_but_not_the_service(self):
        fixtures.make_jpeg(self.originals / "a.jpg")

        def dies(paths):
            raise exiftool.Unreadable("exiftool crashed")

        report = self.run_index(read=dies)
        self.assertEqual((report.new, report.failed), (0, 1))

    def test_exiftool_failing_counts_only_the_files_it_was_given(self):
        fixtures.make_jpeg(self.originals / "a.jpg")
        self.run_index()

        fixtures.make_jpeg(self.originals / "b.jpg")

        def dies(paths):
            raise exiftool.Unreadable("exiftool crashed")

        report = self.run_index(now=200.0, read=dies)
        self.assertEqual((report.unchanged, report.failed), (1, 1))

    def test_a_file_that_vanishes_before_stat_is_a_failure_not_a_crash(self):
        from unittest import mock

        photo = fixtures.make_jpeg(self.originals / "a.jpg")
        real = Path.stat

        def stat(self, *args, **kwargs):
            if self.name == photo.name:
                raise FileNotFoundError(self.name)
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            report = self.run_index()
        self.assertEqual((report.new, report.failed), (0, 1))

    def test_skipped_folders_are_pruned_not_walked(self):
        from unittest import mock

        fixtures.make_jpeg(self.originals / "keep" / "a.jpg")
        fixtures.make_jpeg(self.originals / ".stversions" / "old" / "b.jpg")
        visited = []
        real = os.walk

        def spy(top, *args, **kwargs):
            for folder, names, files in real(top, *args, **kwargs):
                visited.append(Path(folder).name)
                yield folder, names, files

        with mock.patch("os.walk", spy):
            self.assertEqual([p.name for p in index.walk(self.originals)], ["a.jpg"])
        self.assertNotIn("old", visited)

    def test_the_indexer_never_writes_under_originals(self):
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15", gps=(40.7, -74.0))
        fixtures.make_video(self.originals / "b.mov")
        (self.originals / "bad.jpg").write_bytes(b"nope")
        before = listing(self.root / "jo")
        self.run_index()
        self.run_index(now=200.0)
        self.assertEqual(listing(self.root / "jo"), before)

    def test_index_file_handles_one_file_and_ignores_strangers(self):
        photo = fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 12:30:15")
        outside = fixtures.make_jpeg(self.root / "elsewhere.jpg")
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            self.assertEqual(index.index_file(opened, self.photos, person, photo, 1.0).new, 1)
            self.assertEqual(index.index_file(opened, self.photos, person, outside, 1.0).new, 0)
            self.assertEqual(index.index_file(opened, self.photos, person, self.originals / "missing.jpg", 1.0).new, 0)
        self.assertEqual(sorted(self.items()), ["jo/originals/a.jpg"])

    def test_an_empty_or_missing_folder_is_fine(self):
        self.assertEqual(self.run_index().new, 0)
        self.assertEqual(index.walk(self.root / "nowhere"), [])

    def test_people_are_kept_apart(self):
        with db.session(self.engine) as opened:
            add_person(opened, "sam")
        sam = self.photos.ensure_person_tree("sam")[0]
        fixtures.make_jpeg(self.originals / "a.jpg")
        fixtures.make_jpeg(sam / "a.jpg", colour=(5, 5, 5))
        with db.session(self.engine) as opened:
            for person in opened.query(Person):
                index.index_person(opened, self.photos, person, 1.0)
        with db.session(self.engine) as opened:
            self.assertEqual(sorted((i.person_id, i.path) for i in opened.query(MediaItem)), [(1, "jo/originals/a.jpg"), (2, "sam/originals/a.jpg")])


if __name__ == "__main__":
    unittest.main()
