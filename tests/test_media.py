import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

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
        self.assertIsNotNone(found.dhash)
        self.assertEqual(found.captured_utc, utc(2020, 1, 2, 2, 4, 5))

    def test_a_photo_gets_a_sixteen_digit_phash_and_dhash_that_differ(self):
        found = self.prints(fixtures.save_jpeg(fixtures.scene(1), self.folder / "a.jpg"))
        self.assertRegex(found.phash, r"^[0-9a-f]{16}$")
        self.assertRegex(found.dhash, r"^[0-9a-f]{16}$")
        self.assertNotEqual(found.phash, found.dhash)

    def test_a_photo_and_its_recompressed_copy_have_close_hashes_and_another_scene_far_ones(self):
        original = media.perceptual_hashes(fixtures.save_jpeg(fixtures.scene(1), self.folder / "a.jpg"))
        copy = media.perceptual_hashes(fixtures.save_jpeg(fixtures.recompressed(fixtures.scene(1)), self.folder / "b.jpg"))
        other = media.perceptual_hashes(fixtures.save_jpeg(fixtures.scene(2), self.folder / "c.jpg"))
        close = lambda mine, theirs: (int(mine, 16) ^ int(theirs, 16)).bit_count()
        self.assertLessEqual(close(original.phash, copy.phash), 2)
        self.assertLessEqual(close(original.dhash, copy.dhash), 2)
        self.assertGreater(close(original.phash, other.phash), 16)
        self.assertGreater(close(original.dhash, other.dhash), 16)

    def test_a_file_that_is_not_a_picture_has_no_hashes(self):
        broken = self.folder / "broken.jpg"
        broken.write_bytes(b"not a picture")
        self.assertEqual(media.perceptual_hashes(broken), media.PerceptualHashes(None, None))
        self.assertEqual(media.perceptual_hashes(self.folder / "missing.jpg"), media.PerceptualHashes(None, None))

    def test_an_image_with_too_many_pixels_is_never_decoded_and_has_no_hashes(self):
        bomb = fixtures.make_pixel_bomb(self.folder / "bomb.png")
        self.assertEqual(media.perceptual_hashes(bomb), media.PerceptualHashes(None, None))
        large = fixtures.make_pixel_bomb(self.folder / "large.png", width=12_000, height=11_000)
        with mock.patch("imagehash.phash", side_effect=AssertionError("an oversized image was decoded")):
            self.assertEqual(media.perceptual_hashes(large), media.PerceptualHashes(None, None))
        small = fixtures.save_jpeg(fixtures.scene(1), self.folder / "small.jpg")
        with mock.patch.object(media, "MAX_PIXELS", 320 * 240 - 1):
            self.assertEqual(media.perceptual_hashes(small), media.PerceptualHashes(None, None))
        self.assertIsNotNone(media.perceptual_hashes(small).phash)

    def test_numbers_that_are_not_finite_or_do_not_fit_are_left_out(self):
        tags = {"File:MIMEType": "image/jpeg", "IFD0:ImageWidth": float("inf"), "IFD0:ImageHeight": 2**40, "QuickTime:Duration": 1e308,
                "Composite:GPSLatitude": float("nan"), "Composite:GPSLongitude": 10.0}
        found = media.fingerprint(fixtures.make_jpeg(self.folder / "a.jpg"), tags)
        self.assertEqual((found.width, found.height, found.duration_ms, found.lat, found.lon, found.gps_source), (None, None, None, None, None, "none"))
        self.assertIsNone(media.number(float("-inf")))
        self.assertIsNone(media.number(10**400))
        self.assertEqual(media.stored_int(1234.0), 1234)

    def test_a_capture_time_whose_instant_is_out_of_range_is_unknown(self):
        for taken, offset in (("0001:01:01 00:00:00", "+05:00"), ("9999:12:31 23:00:00", "-05:00"), ("2020:01:01 00:00:00", "+99:00")):
            with self.subTest(taken=taken, offset=offset):
                self.assertEqual(media.capture({"DateTimeOriginal": taken, "OffsetTimeOriginal": offset}, None, None)[::3], (None, "unknown"))
        self.assertEqual(media.capture({"DateTimeOriginal": "2020:01:01 00:00:00", "OffsetTimeOriginal": "+01:00"}, None, None)[0], utc(2019, 12, 31, 23))

    def test_a_video_has_a_duration_and_no_perceptual_hash(self):
        found = self.prints(fixtures.make_video(self.folder / "v.mov"))
        self.assertEqual((found.phash, found.dhash), (None, None))
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
