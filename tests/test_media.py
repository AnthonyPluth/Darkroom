import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from darkroom.domain import media
from darkroom.providers import exiftool
from tests.fixtures import media as fixtures

NEW_YORK = (40.7128, -74.0060)


def utc(*parts):
    return datetime(*parts, tzinfo=UTC).timestamp()


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def prints(self, path):
        return media.fingerprint(path, exiftool.read([path])[path])

    def test_a_photo_with_an_exif_offset(self):
        path = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=NEW_YORK, make="Apple", model="iPhone 15", size=(64, 48))
        found = self.prints(path)
        self.assertEqual((found.make, found.model, found.width, found.height), ("Apple", "iPhone 15", 64, 48))
        self.assertEqual(found.time_source, "exif_offset")
        self.assertEqual(found.offset_minutes, -300)
        self.assertEqual(found.captured_local, "2019-07-04T12:30:15")
        self.assertEqual(found.captured_utc, utc(2019, 7, 4, 17, 30, 15))
        self.assertEqual(found.gps_source, "exif")
        self.assertAlmostEqual(found.lat, NEW_YORK[0], places=3)
        self.assertAlmostEqual(found.lon, NEW_YORK[1], places=3)
        self.assertEqual(len(found.sha1), 40)
        self.assertEqual(found.bytes, path.stat().st_size)
        self.assertIsNotNone(found.phash)

    def test_without_an_offset_the_gps_lookup_gives_one(self):
        path = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", gps=NEW_YORK)
        found = self.prints(path)
        self.assertEqual((found.time_source, found.offset_minutes), ("gps_lookup", -240))
        self.assertEqual(found.captured_utc, utc(2019, 7, 4, 16, 30, 15))

    def test_the_exif_offset_wins_over_the_gps_lookup(self):
        path = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", offset="+09:00", gps=NEW_YORK)
        found = self.prints(path)
        self.assertEqual((found.time_source, found.offset_minutes), ("exif_offset", 540))

    def test_a_time_without_offset_or_gps_is_local_only(self):
        found = self.prints(fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15"))
        self.assertEqual((found.time_source, found.offset_minutes, found.captured_utc), ("unknown", None, None))
        self.assertEqual(found.captured_local, "2019-07-04T12:30:15")

    def test_no_time_at_all(self):
        found = self.prints(fixtures.make_jpeg(self.folder / "a.jpg"))
        self.assertEqual((found.time_source, found.captured_local, found.captured_utc), ("unknown", None, None))
        self.assertEqual((found.gps_source, found.lat, found.lon), ("none", None, None))

    def test_gps_at_null_island_is_treated_as_none(self):
        found = self.prints(fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", gps=(0.0, 0.0)))
        self.assertEqual((found.gps_source, found.lat), ("none", None))
        self.assertEqual(found.time_source, "unknown")

    def test_southern_and_western_hemispheres_are_signed(self):
        found = self.prints(fixtures.make_jpeg(self.folder / "a.jpg", gps=(-33.87, -70.65)))
        self.assertLess(found.lat, 0)
        self.assertLess(found.lon, 0)

    def test_a_heic_photo_gets_a_perceptual_hash(self):
        found = self.prints(fixtures.make_heic(self.folder / "a.heic", taken="2020:01:02 03:04:05", offset="+01:00"))
        self.assertIsNotNone(found.phash)
        self.assertEqual(found.captured_utc, utc(2020, 1, 2, 2, 4, 5))

    def test_a_video_has_a_duration_and_no_perceptual_hash(self):
        found = self.prints(fixtures.make_video(self.folder / "v.mov"))
        self.assertIsNone(found.phash)
        self.assertGreater(found.duration_ms, 500)
        self.assertLess(found.duration_ms, 2000)

    def test_a_video_creation_date_with_an_offset(self):
        found = self.prints(fixtures.make_video(self.folder / "v.mov", creation="2021:05:06 07:08:09-04:00"))
        self.assertEqual((found.time_source, found.offset_minutes), ("exif_offset", -240))
        self.assertEqual(found.captured_utc, utc(2021, 5, 6, 11, 8, 9))

    def test_sub_second_time_is_kept(self):
        path = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", offset="+00:00")
        fixtures.set_tags(path, {"SubSecTimeOriginal": "250"})
        self.assertEqual(self.prints(path).captured_utc, utc(2019, 7, 4, 12, 30, 15) + 0.25)

    def test_two_identical_photos_share_a_hash_and_a_different_one_does_not(self):
        a = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15")
        b = self.folder / "b.jpg"
        b.write_bytes(a.read_bytes())
        c = fixtures.make_jpeg(self.folder / "c.jpg", taken="2019:07:04 12:30:15", colour=(10, 200, 10))
        self.assertEqual(self.prints(a).sha1, self.prints(b).sha1)
        self.assertNotEqual(self.prints(a).sha1, self.prints(c).sha1)

    def test_parsers(self):
        self.assertEqual(media.parse_offset("Z"), 0)
        self.assertEqual(media.parse_offset("+0530"), 330)
        self.assertEqual(media.parse_offset("-05:00"), -300)
        self.assertIsNone(media.parse_offset("later"))
        self.assertIsNone(media.parse_local("not a date"))
        self.assertIsNone(media.parse_local(5))
        self.assertIsNone(media.parse_local("2019:13:45 99:99:99"))
        self.assertEqual(media.parse_local("2019:07:04 12:30:15Z")[1], 0)
        self.assertIsNone(media.number(True))
        self.assertIsNone(media.text("  "))


if __name__ == "__main__":
    unittest.main()
