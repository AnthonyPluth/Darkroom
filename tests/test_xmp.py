import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom.domain import xmp
from darkroom.providers import exiftool
from darkroom.storage import paths
from tests.fixtures import media as fixtures

TAKEN = 1562243415
GOOGLE = xmp.GoogleMetadata(lat=45.4340, lon=12.3388, taken_utc=float(TAKEN), description="A made-up square")


def sha1(path):
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()


def tags_of(path):
    return exiftool.read([path])[path]


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()

    def listing(self):
        return {str(p.relative_to(self.folder)): sha1(p) for p in sorted(self.folder.rglob("*")) if p.is_file()}


class GoogleMetadataTests(unittest.TestCase):
    def test_reads_location_time_and_description_from_the_takeout_json(self):
        raw = json.dumps({"photoTakenTime": {"timestamp": str(TAKEN)}, "geoData": {"latitude": 45.434, "longitude": 12.3388}, "description": " d "})
        self.assertEqual(xmp.GoogleMetadata.of(raw), xmp.GoogleMetadata(45.434, 12.3388, float(TAKEN), "d"))

    def test_no_json_or_bad_json_gives_nothing(self):
        for raw in (None, "not json", "[]"):
            with self.subTest(raw=raw):
                self.assertEqual(xmp.GoogleMetadata.of(raw), xmp.GoogleMetadata(None, None, None, None))


class GapsTests(unittest.TestCase):
    def test_every_missing_field_is_filled(self):
        fields = xmp.gaps({}, {}, GOOGLE, with_date=True)
        self.assertEqual(fields, {
            xmp.LATITUDE: "45.4340000", xmp.LONGITUDE: "12.3388000",
            xmp.CREATED: "2019:07:04 14:30:15+02:00", xmp.DESCRIPTION: "A made-up square",
        })

    def test_fields_the_file_has_are_left_alone(self):
        own = {"GPS:GPSLatitude": 1.0, "GPS:GPSLongitude": 2.0, "ExifIFD:DateTimeOriginal": "2019:07:04 12:30:15", "IFD0:ImageDescription": "mine"}
        self.assertEqual(xmp.gaps(own, {}, GOOGLE, with_date=True), {})

    def test_fields_an_existing_sidecar_has_are_left_alone(self):
        existing = {"XMP-exif:GPSLatitude": 9.0, "XMP-exif:GPSLongitude": 9.0, "XMP-photoshop:DateCreated": "2001:01:01 00:00:00", "XMP-dc:Description": "kept"}
        self.assertEqual(xmp.gaps({}, existing, GOOGLE, with_date=True), {})

    def test_a_blank_description_in_the_file_counts_as_missing(self):
        self.assertIn(xmp.DESCRIPTION, xmp.gaps({"IFD0:ImageDescription": "   "}, {}, GOOGLE, with_date=False))

    def test_without_the_date_target_no_date_is_written(self):
        self.assertNotIn(xmp.CREATED, xmp.gaps({}, {}, GOOGLE, with_date=False))

    def test_an_unknown_offset_is_written_as_the_utc_instant(self):
        google = xmp.GoogleMetadata(None, None, float(TAKEN), None)
        self.assertEqual(xmp.gaps({}, {}, google, with_date=True), {xmp.CREATED: "2019:07:04 12:30:15+00:00"})

    def test_the_offset_comes_from_the_files_own_location_when_google_has_none(self):
        google = xmp.GoogleMetadata(None, None, float(TAKEN), None)
        own = {"GPS:GPSLatitude": 40.7, "GPS:GPSLongitude": -74.0, "GPS:GPSLongitudeRef": "W"}
        self.assertEqual(xmp.gaps(own, {}, google, with_date=True), {xmp.CREATED: "2019:07:04 08:30:15-04:00"})

    def test_a_time_out_of_range_writes_no_date(self):
        self.assertIsNone(xmp.created_text(1e20, None, None))

    def test_offsets_with_minutes(self):
        self.assertEqual(xmp.offset_text(-570), "-09:30")
        self.assertEqual(xmp.offset_text(345), "+05:45")

    def test_nothing_from_google_writes_nothing(self):
        self.assertEqual(xmp.gaps({}, {}, xmp.GoogleMetadata(None, None, None, None), with_date=True), {})


class FillTests(Folder):
    def test_a_sidecar_carries_the_missing_fields_and_reads_back(self):
        photo = fixtures.make_jpeg(self.folder / "IMG_0001.JPG")
        before = sha1(photo)
        self.assertTrue(xmp.fill(photo, GOOGLE, with_date=True))
        sidecar = self.folder / "IMG_0001.JPG.xmp"
        tags = tags_of(sidecar)
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 45.4340, places=5)
        self.assertAlmostEqual(tags["XMP-exif:GPSLongitude"], 12.3388, places=5)
        self.assertEqual(tags["XMP-photoshop:DateCreated"], "2019:07:04 14:30:15+02:00")
        self.assertEqual(tags["XMP-dc:Description"], "A made-up square")
        self.assertEqual(sha1(photo), before)
        self.assertNotIn("XMP-dc:Description", tags_of(photo))

    def test_southern_and_western_positions_keep_their_sign(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg")
        xmp.fill(photo, xmp.GoogleMetadata(-33.8568, -70.6483, None, None), with_date=True)
        tags = tags_of(self.folder / "a.jpg.xmp")
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], -33.8568, places=5)
        self.assertAlmostEqual(tags["XMP-exif:GPSLongitude"], -70.6483, places=5)

    def test_a_second_fill_writes_no_second_sidecar_and_changes_nothing(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg")
        xmp.fill(photo, GOOGLE, with_date=True)
        before = self.listing()
        self.assertFalse(xmp.fill(photo, GOOGLE, with_date=True))
        self.assertEqual(self.listing(), before)

    def test_an_existing_sidecar_is_merged_not_clobbered(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg")
        sidecar = self.folder / "a.jpg.xmp"
        exiftool.write_xmp(sidecar, {"XMP-dc:Description": "Written by hand", "XMP-dc:Title": "Kept title"})
        self.assertTrue(xmp.fill(photo, GOOGLE, with_date=True))
        tags = tags_of(sidecar)
        self.assertEqual(tags["XMP-dc:Description"], "Written by hand")
        self.assertEqual(tags["XMP-dc:Title"], "Kept title")
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 45.4340, places=5)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a.jpg", "a.jpg.xmp"])

    def test_a_file_with_everything_gets_no_sidecar(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", gps=(1.0, 2.0))
        fixtures.set_tags(photo, {"ImageDescription": "mine"})
        self.assertFalse(xmp.fill(photo, GOOGLE, with_date=True))
        self.assertFalse((self.folder / "a.jpg.xmp").exists())

    def test_a_file_exiftool_cannot_read_gets_no_sidecar(self):
        photo = self.folder / "a.jpg"
        photo.write_bytes(b"x")
        self.assertFalse(xmp.fill(photo, GOOGLE, with_date=True, read=lambda files: {}))
        self.assertFalse(xmp.fill(photo, GOOGLE, with_date=True, read=lambda files: {photo: {"Error": "bad"}}))
        self.assertFalse((self.folder / "a.jpg.xmp").exists())

    def test_an_unreadable_existing_sidecar_is_not_overwritten(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg")
        sidecar = self.folder / "a.jpg.xmp"
        sidecar.write_bytes(b"kept")
        write = mock.Mock()
        self.assertFalse(xmp.fill(photo, GOOGLE, with_date=True, read=lambda files: {photo: {}}, write=write))
        write.assert_not_called()
        self.assertEqual(sidecar.read_bytes(), b"kept")

    def test_a_symlink_where_the_sidecar_goes_is_refused(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg")
        other = fixtures.make_jpeg(self.folder / "b.jpg")
        before = sha1(other)
        (self.folder / "a.jpg.xmp").symlink_to(other)
        with self.assertRaises(paths.NotASidecar):
            xmp.fill(photo, GOOGLE, with_date=True)
        self.assertEqual(sha1(other), before)


class WriteXmpTests(Folder):
    def test_refuses_any_target_not_ending_in_xmp(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg")
        before = self.listing()
        for target in (photo, self.folder / "a.jpg.XMP", self.folder / "a.xmp.jpg", self.folder / ".xmp", self.folder / "a"):
            with self.subTest(target=target.name), self.assertRaises(paths.NotASidecar):
                exiftool.write_xmp(target, {"XMP-dc:Description": "x"})
        self.assertEqual(self.listing(), before)

    def test_refuses_tags_outside_xmp_and_empty_values(self):
        for fields in ({"EXIF:ImageDescription": "x"}, {"XMP-dc:Description": ""}, {"-overwrite_original": "x"}, {"XMP-dc:Description": "a\x00b"}):
            with self.subTest(fields=fields), self.assertRaises(exiftool.WriteFailed):
                exiftool.write_xmp(self.folder / "a.xmp", fields)
        self.assertEqual(self.listing(), {})

    def test_no_fields_writes_nothing(self):
        with mock.patch("subprocess.run") as run:
            self.assertFalse(exiftool.write_xmp(self.folder / "a.xmp", {}))
        run.assert_not_called()
        self.assertEqual(self.listing(), {})

    def test_a_failed_write_leaves_no_file_behind(self):
        done = mock.Mock(returncode=1, stderr="Error: private/path.jpg")
        with mock.patch("subprocess.run", return_value=done), self.assertRaises(exiftool.WriteFailed) as raised:
            exiftool.write_xmp(self.folder / "a.xmp", {"XMP-dc:Description": "x"})
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_value_starting_with_a_dash_stays_a_value(self):
        target = self.folder / "a.xmp"
        exiftool.write_xmp(target, {"XMP-dc:Description": "-overwrite_original"})
        self.assertEqual(tags_of(target)["XMP-dc:Description"], "-overwrite_original")

    def test_never_passes_overwrite_original(self):
        calls = []
        real = exiftool.subprocess.run

        def spy(args, **kwargs):
            calls.append(list(args))
            return real(args, **kwargs)

        with mock.patch.object(exiftool.subprocess, "run", side_effect=spy):
            exiftool.write_xmp(self.folder / "a.xmp", {"XMP-dc:Description": "x"})
            exiftool.write_xmp(self.folder / "a.xmp", {"XMP-dc:Title": "y"})
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertFalse([a for a in args if a.startswith("-overwrite_original")])
            output = Path(args[args.index("-o") + 1])
            self.assertNotEqual(output, self.folder / "a.xmp")
            self.assertTrue(output.name.endswith(".xmp"))


class WriteSidecarTests(Folder):
    def render(self, text):
        def write(temp, source):
            temp.write_text(text + ("" if source is None else source.read_text()))
        return write

    def test_creates_then_replaces_and_cleans_its_temporary_folder(self):
        target = self.folder / "a.jpg.xmp"
        paths.write_sidecar(target, self.render("one"))
        paths.write_sidecar(target, self.render("two+"))
        self.assertEqual(target.read_text(), "two+one")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["a.jpg.xmp"])

    def test_a_render_that_writes_nothing_is_refused(self):
        with self.assertRaises(paths.NotASidecar):
            paths.write_sidecar(self.folder / "a.xmp", lambda temp, source: None)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_folder_where_the_sidecar_goes_is_refused(self):
        (self.folder / "a.xmp").mkdir()
        with self.assertRaises(paths.NotASidecar):
            paths.write_sidecar(self.folder / "a.xmp", self.render("x"))

    def test_a_sidecar_created_meanwhile_is_not_clobbered(self):
        target = self.folder / "a.xmp"

        def racing(temp, source):
            temp.write_text("mine")
            target.write_text("theirs")

        with self.assertRaises(paths.AlreadyExists):
            paths.write_sidecar(target, racing)
        self.assertEqual(target.read_text(), "theirs")

    def test_without_hard_links_the_sidecar_is_copied(self):
        target = self.folder / "a.xmp"
        with mock.patch.object(paths.os, "link", side_effect=OSError("no links")):
            paths.write_sidecar(target, self.render("copied"))
        self.assertEqual(target.read_text(), "copied")

    def test_sidecar_of_keeps_the_whole_name(self):
        self.assertEqual(paths.sidecar_of(Path("/p/IMG_0001.HEIC")), Path("/p/IMG_0001.HEIC.xmp"))

    def test_original_must_be_inside_that_persons_originals(self):
        photos = paths.Photos(self.folder)
        self.assertEqual(photos.original("jo", "jo/originals/a.jpg"), self.folder / "jo/originals/a.jpg")
        for relative in ("jo/google-only/a.jpg", "sam/originals/a.jpg", "jo/originals", "jo/originals/../../x", "/etc/passwd"):
            with self.subTest(relative=relative), self.assertRaises(paths.OutsideRoot):
                photos.original("jo", relative)

if __name__ == "__main__":
    unittest.main()
