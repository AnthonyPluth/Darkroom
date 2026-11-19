import json
import random
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from darkroom.domain import dedupe, media
from darkroom.domain.ingest import metadata, pairing, sidecar
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
from tests.dedupe_world import World
from tests.fixtures import media as fixtures
from tests.takeout_fixtures import standard_export

TAKEN = 1562243415


def parsed(lat=None, lon=None, taken=TAKEN):
    geo = {"latitude": lat, "longitude": lon} if lat is not None else {"latitude": 0.0, "longitude": 0.0}
    return sidecar.parse(json.dumps({"photoTakenTime": {"timestamp": str(taken)}, "geoData": geo, "geoDataExif": geo, "description": "d"}))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def build(self, path, side, **kwargs):
        item = TakeoutItem(path=path, sha1="", copies=(path,), sidecar=None)
        tags = exiftool.read([path])[path]
        return metadata.candidate(item, tags, side, 1, "x/y.jpg", 50.0, **kwargs)

    def test_a_takeout_photo_gets_both_perceptual_hashes(self):
        path = fixtures.save_jpeg(fixtures.scene(1), self.root / "a.jpg")
        found = self.build(path, parsed())
        hashes = media.perceptual_hashes(path)
        self.assertEqual((found.phash, found.dhash), (hashes.phash, hashes.dhash))
        self.assertIsNotNone(found.dhash)

    def test_a_different_sidecar_gps_beats_exif_gps(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=(40.7, -74.0))
        found = self.build(path, parsed(10.0, 20.0))
        self.assertEqual((found.lat, found.lon, found.gps_source), (10.0, 20.0, "google"))

    def test_a_sidecar_gps_equal_to_exif_gps_stays_exif(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=(40.7, -74.0))
        found = self.build(path, parsed(40.7, -74.0))
        self.assertEqual((found.lat, found.lon, found.gps_source), (40.7, -74.0, "exif"))

    def test_without_exif_gps_the_sidecar_location_is_used(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00")
        found = self.build(path, parsed(10.0, 20.0))
        self.assertEqual((found.lat, found.lon, found.gps_source), (10.0, 20.0, "google"))

    def test_no_location_anywhere_stays_none(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        found = self.build(path, parsed())
        self.assertEqual((found.lat, found.lon, found.gps_source), (None, None, "none"))

    def test_the_sidecar_time_fills_a_missing_exif_time_with_the_offset_from_gps(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        found = self.build(path, parsed(40.7, -74.0))
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes), (float(TAKEN), "sidecar", -240))
        self.assertEqual(found.captured_local, "2019-07-04T08:30:15")

    def test_without_gps_the_offset_stays_unknown(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        found = self.build(path, parsed())
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes, found.captured_local), (float(TAKEN), "sidecar", None, None))

    def test_a_sidecar_time_over_14_hours_from_the_exif_instant_beats_it(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00")
        found = self.build(path, parsed(taken=TAKEN + 99999))
        self.assertEqual((found.time_source, found.offset_minutes, found.captured_local), ("sidecar", None, None))
        self.assertEqual(found.captured_utc, float(TAKEN + 99999))

    def test_a_sidecar_time_within_14_hours_of_the_exif_instant_does_not(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00")
        found = self.build(path, parsed(taken=TAKEN + 50400))
        self.assertEqual((found.time_source, found.offset_minutes, found.captured_utc), ("exif_offset", -300, 1562261415.0))

    def test_an_exif_local_time_without_offset_that_agrees_with_the_sidecar_takes_its_utc_and_the_offset_between_them(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15")
        found = self.build(path, parsed())
        self.assertEqual((found.captured_local, found.captured_utc, found.time_source, found.offset_minutes), ("2019-07-04T12:30:15", float(TAKEN), "sidecar_offset", 0))

    def test_no_sidecar_leaves_exif_alone_and_marks_the_source(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=(40.7, -74.0), make="Apple")
        found = self.build(path, None, export_id=7)
        self.assertEqual((found.source, found.make, found.sidecar_json, found.export_id, found.path, found.person_id), ("takeout", "Apple", None, 7, "x/y.jpg", 1))
        self.assertEqual((found.sha1, found.bytes), (fixtures_sha(path), path.stat().st_size))

    def test_an_out_of_range_sidecar_time_is_ignored_not_fatal(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        side = sidecar.Sidecar(1e300, None, None, None, None, "none", "{}")
        found = self.build(path, side)
        self.assertEqual((found.captured_utc, found.time_source), (None, "unknown"))

    def test_a_pre_1970_sidecar_time_fills_the_gap(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        found = self.build(path, parsed(40.7, -74.0, taken=-86400))
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes), (-86400.0, "sidecar", -300))

    def test_a_video_quicktime_date_stays_its_utc_instant_and_takes_the_offset_of_the_sidecar_position(self):
        path = fixtures.make_video(self.root / "v.mp4", quicktime_utc="2019:07:04 12:30:15")
        found = self.build(path, parsed(40.7, -74.0, taken=TAKEN + 3600))
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes, found.captured_local), (float(TAKEN), "quicktime_utc", -240, "2019-07-04T08:30:15"))
        self.assertEqual((found.lat, found.lon, found.gps_source), (40.7, -74.0, "google"))

    def test_a_video_quicktime_date_without_any_position_has_no_offset_and_no_wall_clock(self):
        path = fixtures.make_video(self.root / "v.mp4", quicktime_utc="2019:07:04 12:30:15")
        found = self.build(path, parsed())
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes, found.captured_local), (float(TAKEN), "quicktime_utc", None, None))

    def test_a_video_with_its_own_position_gives_the_takeout_and_phone_sides_the_same_time(self):
        path = fixtures.make_video(self.root / "v.mp4", quicktime_utc="2019:07:04 12:30:15", gps=(40.7, -74.0))
        found = self.build(path, parsed(10.0, 20.0, taken=TAKEN + 3600))
        phone = media.fingerprint(path, exiftool.read([path])[path])
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes, found.captured_local), (float(TAKEN), "quicktime_utc", -240, "2019-07-04T08:30:15"))
        self.assertEqual((phone.captured_utc, phone.time_source, phone.offset_minutes, phone.captured_local), (found.captured_utc, found.time_source, found.offset_minutes, found.captured_local))

    def test_an_apple_video_keeps_its_own_offset(self):
        path = fixtures.make_video(self.root / "v.mov", creation="2019:07:04 08:30:15-04:00")
        found = self.build(path, parsed(10.0, 20.0, taken=TAKEN + 3600))
        self.assertEqual((found.captured_utc, found.time_source, found.offset_minutes, found.captured_local), (float(TAKEN), "exif_offset", -240, "2019-07-04T08:30:15"))

    def test_the_raw_sidecar_is_kept(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        side = parsed(40.7, -74.0)
        self.assertEqual(self.build(path, side).sidecar_json, side.raw)

    def test_nothing_is_written_to_disk(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15")
        before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*"))
        self.build(path, parsed(40.7, -74.0))
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")), before)


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def taken_at(text, lat=None, lon=None):
    return parsed(lat, lon, taken=int(utc(text)))


def exif(**tags):
    return {f"ExifIFD:{name}": value for name, value in tags.items()}


class CaptureAgreementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = fixtures.make_jpeg(self.root / "IMG_0001.jpg")

    def build(self, tags, side):
        item = TakeoutItem(path=self.path, sha1="", copies=(self.path,), sidecar=None)
        return metadata.candidate(item, tags, side, 1, "x/y.jpg", 50.0)

    def times(self, found):
        return found.captured_local, found.captured_utc, found.offset_minutes, found.time_source

    def assertOneCaptureTime(self, found):
        if found.captured_utc is None:
            self.assertIsNone(found.offset_minutes)
            return
        if found.captured_local is not None:
            self.assertIsNotNone(found.offset_minutes)
            wall = datetime.fromisoformat(found.captured_local)
            moment = datetime.fromtimestamp(found.captured_utc, UTC).replace(tzinfo=None, microsecond=0)
            self.assertEqual(wall - moment, timedelta(minutes=found.offset_minutes))

    def test_a_wall_clock_four_hours_behind_the_sidecar_instant_gets_that_offset(self):
        found = self.build(exif(DateTimeOriginal="2019:07:04 19:30:00"), taken_at("2019-07-04 23:30:00"))
        self.assertEqual(self.times(found), ("2019-07-04T19:30:00", utc("2019-07-04 23:30:00"), -240, "sidecar_offset"))

    def test_a_scan_dated_decades_apart_from_the_sidecar_takes_the_sidecar_instant_and_never_the_exif_wall_clock(self):
        side = taken_at("1979-06-15 10:00:00", 40.7, -74.0)
        found = self.build(exif(DateTimeOriginal="2020:02:09 12:47:20"), side)
        self.assertEqual(self.times(found), ("1979-06-15T06:00:00", utc("1979-06-15 10:00:00"), -240, "sidecar"))
        self.assertEqual(found.sidecar_json, side.raw)
        found = self.build(exif(DateTimeOriginal="2020:02:09 12:47:20"), taken_at("1979-06-15 10:00:00"))
        self.assertEqual(self.times(found), (None, utc("1979-06-15 10:00:00"), None, "sidecar"))

    def test_no_sidecar_leaves_a_wall_clock_without_offset_unknown(self):
        found = self.build(exif(DateTimeOriginal="2019:07:04 19:30:00"), None)
        self.assertEqual(self.times(found), ("2019-07-04T19:30:00", None, None, "unknown"))

    def test_an_exif_offset_is_kept_while_the_sidecar_is_within_14_hours_of_it(self):
        for sidecar_time in ("2019-07-04 23:30:00", "2019-07-05 09:00:00", "2019-07-05 14:30:00"):
            with self.subTest(sidecar_time=sidecar_time):
                found = self.build(exif(DateTimeOriginal="2019:07:04 19:30:00", OffsetTimeOriginal="-05:00"), taken_at(sidecar_time))
                self.assertEqual(self.times(found), ("2019-07-04T19:30:00", utc("2019-07-05 00:30:00"), -300, "exif_offset"))

    def test_an_exif_offset_yields_to_a_sidecar_time_over_14_hours_away(self):
        for sidecar_time in ("2019-07-05 14:30:01", "2019-07-04 10:29:59", "1979-06-15 10:00:00"):
            with self.subTest(sidecar_time=sidecar_time):
                found = self.build(exif(DateTimeOriginal="2019:07:04 19:30:00", OffsetTimeOriginal="-05:00"), taken_at(sidecar_time, 40.7, -74.0))
                self.assertEqual(found.time_source, "sidecar")
                self.assertEqual(found.captured_utc, utc(sidecar_time))
                self.assertOneCaptureTime(found)

    def test_an_exif_position_with_an_agreeing_sidecar_keeps_its_looked_up_offset(self):
        tags = {**exif(DateTimeOriginal="2019:07:04 19:30:00"), "Composite:GPSLatitude": 40.7, "Composite:GPSLongitude": -74.0}
        found = self.build(tags, taken_at("2019-07-04 23:30:00", 40.7, -74.0))
        self.assertEqual(self.times(found), ("2019-07-04T19:30:00", utc("2019-07-04 23:30:00"), -240, "gps_lookup"))

    def test_half_and_quarter_hour_zones_are_offsets(self):
        cases = (("2019:07:05 05:00:00", 330), ("2019:07:05 05:15:00", 345), ("2019:07:04 20:00:00", -210), ("2019:07:05 12:15:00", 765))
        for wall, minutes in cases:
            with self.subTest(wall=wall):
                found = self.build(exif(DateTimeOriginal=wall), taken_at("2019-07-04 23:30:00"))
                self.assertEqual((found.offset_minutes, found.captured_utc, found.time_source), (minutes, utc("2019-07-04 23:30:00"), "sidecar_offset"))

    def test_the_widest_offsets_are_accepted_and_anything_beyond_them_is_not(self):
        cases = (("2019:07:05 13:30:00", 840), ("2019:07:04 11:30:00", -720), ("2019:07:04 11:15:00", None))
        for wall, minutes in cases:
            with self.subTest(wall=wall):
                found = self.build(exif(DateTimeOriginal=wall), taken_at("2019-07-04 23:30:00"))
                self.assertEqual(found.offset_minutes, minutes)
                self.assertEqual(found.captured_utc, None if minutes is None else utc("2019-07-04 23:30:00"))

    def test_a_looked_up_offset_follows_the_sidecar_place_that_replaced_the_files_own(self):
        tags = {**exif(DateTimeOriginal="2019:07:04 12:30:15"), "Composite:GPSLatitude": 40.7, "Composite:GPSLongitude": -74.0}
        found = self.build(tags, taken_at("2019-07-04 03:30:15", 35.68, 139.69))
        self.assertEqual(self.times(found), ("2019-07-04T12:30:15", utc("2019-07-04 03:30:15"), 540, "gps_lookup"))
        self.assertEqual((found.lat, found.lon, found.gps_source), (35.68, 139.69, "google"))
        self.assertOneCaptureTime(found)

    def test_a_wall_clock_over_14_hours_from_the_sidecar_instant_is_a_correction_and_the_sidecar_wins(self):
        for wall in ("2019:07:05 14:30:01", "2019:07:04 09:29:59"):
            with self.subTest(wall=wall):
                found = self.build(exif(DateTimeOriginal=wall), taken_at("2019-07-04 23:30:00"))
                self.assertEqual(self.times(found), (None, utc("2019-07-04 23:30:00"), None, "sidecar"))
        found = self.build(exif(DateTimeOriginal="2019:07:05 14:30:01"), taken_at("2019-07-04 23:30:00", 40.7, -74.0))
        self.assertEqual(self.times(found), ("2019-07-04T19:30:00", utc("2019-07-04 23:30:00"), -240, "sidecar"))

    def test_a_difference_that_is_no_whole_quarter_hour_is_not_an_offset(self):
        for wall in ("2019:07:04 19:37:00", "2019:07:04 19:30:03", "2019:07:04 19:29:57", "2019:07:04 23:38:00"):
            with self.subTest(wall=wall):
                found = self.build(exif(DateTimeOriginal=wall), taken_at("2019-07-04 23:30:00"))
                self.assertEqual(self.times(found)[1:], (None, None, "unknown"))

    def test_a_second_or_two_of_rounding_still_agrees_and_the_instant_follows_the_wall_clock(self):
        for wall, instant in (("2019:07:04 19:30:01", "2019-07-04 23:30:01"), ("2019:07:04 19:29:58", "2019-07-04 23:29:58")):
            with self.subTest(wall=wall):
                found = self.build(exif(DateTimeOriginal=wall), taken_at("2019-07-04 23:30:00"))
                self.assertEqual(self.times(found)[1:], (utc(instant), -240, "sidecar_offset"))
                self.assertOneCaptureTime(found)

    def test_subseconds_from_exif_are_kept_on_the_derived_instant(self):
        found = self.build(exif(DateTimeOriginal="2019:07:04 19:30:00", SubSecTimeOriginal="25"), taken_at("2019-07-04 23:30:00"))
        self.assertEqual(found.captured_utc, utc("2019-07-04 23:30:00") + 0.25)

    def test_the_wall_clock_difference_wins_over_the_sidecar_position(self):
        found = self.build(exif(DateTimeOriginal="2019:07:04 19:30:00"), taken_at("2019-07-04 23:30:00", 35.68, 139.69))
        self.assertEqual(self.times(found), ("2019-07-04T19:30:00", utc("2019-07-04 23:30:00"), -240, "sidecar_offset"))

    def test_both_sides_of_a_daylight_saving_change_get_the_offset_their_instant_shows(self):
        cases = (
            ("2019:11:03 01:30:00", "2019-11-03 05:30:00", -240), ("2019:11:03 01:30:00", "2019-11-03 06:30:00", -300),
            ("2019:03:10 01:59:59", "2019-03-10 06:59:59", -300), ("2019:03:10 03:00:00", "2019-03-10 07:00:00", -240),
        )
        for wall, instant, minutes in cases:
            with self.subTest(wall=wall, instant=instant):
                found = self.build(exif(DateTimeOriginal=wall), taken_at(instant, 40.7, -74.0))
                self.assertEqual(self.times(found)[1:], (utc(instant), minutes, "sidecar_offset"))

    def test_old_dates_before_1970_agree_the_same_way(self):
        found = self.build(exif(DateTimeOriginal="1964:03:01 10:00:00"), taken_at("1964-03-01 15:00:00"))
        self.assertEqual(self.times(found), ("1964-03-01T10:00:00", utc("1964-03-01 15:00:00"), -300, "sidecar_offset"))
        found = self.build(exif(DateTimeOriginal="1964:03:01 10:00:00"), taken_at("2001-03-01 15:00:00"))
        self.assertEqual(self.times(found), (None, utc("2001-03-01 15:00:00"), None, "sidecar"))

    def test_a_still_whose_only_date_is_create_date_takes_the_sidecar_time(self):
        found = self.build(exif(CreateDate="2020:02:09 12:47:20"), taken_at("1979-06-15 14:00:00", 40.7, -74.0))
        self.assertEqual(self.times(found), ("1979-06-15T10:00:00", utc("1979-06-15 14:00:00"), -240, "sidecar"))
        found = self.build(exif(CreateDate="2020:02:09 12:47:20"), taken_at("1979-06-15 14:00:00"))
        self.assertEqual(self.times(found), (None, utc("1979-06-15 14:00:00"), None, "sidecar"))

    def test_a_create_date_still_counts_without_a_sidecar_time_or_next_to_a_date_time_original(self):
        found = self.build(exif(CreateDate="2020:02:09 12:47:20"), None)
        self.assertEqual(self.times(found), ("2020-02-09T12:47:20", None, None, "unknown"))
        found = self.build(exif(DateTimeOriginal="2020:02:09 12:47:20", CreateDate="2021:01:01 00:00:00"), taken_at("2020-02-09 17:47:20"))
        self.assertEqual(self.times(found), ("2020-02-09T12:47:20", utc("2020-02-09 17:47:20"), -300, "sidecar_offset"))

    def test_a_wall_clock_and_an_instant_from_different_sources_are_never_stored_as_one_capture_time(self):
        rng = random.Random(437)
        for _ in range(300):
            wall = datetime(1900, 1, 1) + timedelta(seconds=rng.randrange(0, 150 * 365 * 86400))
            apart = timedelta(minutes=rng.choice((rng.randrange(-900, 900), rng.randrange(-60, 61) * 15)), seconds=rng.choice((0, 0, 1, -2, 7)))
            found = self.build(exif(DateTimeOriginal=wall.strftime("%Y:%m:%d %H:%M:%S")), taken_at((wall - apart).isoformat(sep=" ")))
            self.assertOneCaptureTime(found)
            if found.time_source == "sidecar":
                self.assertIsNone(found.captured_local)
                self.assertGreater(abs(apart), timedelta(hours=14))
            else:
                self.assertEqual(found.captured_local, wall.isoformat(timespec="seconds"))

    def test_a_scan_with_a_disagreeing_sidecar_within_14_hours_still_matches_its_phone_copy_by_wall_clock(self):
        world = World(self)
        jo = world.person("jo")
        account = world.account("jo@x.test", jo)
        tags = {**exif(DateTimeOriginal="2023:11:14 22:13:20"), "IFD0:Make": "Apple", "IFD0:Model": "iPhone 15 Pro", "File:ImageWidth": 64, "File:ImageHeight": 48}
        found = self.build(tags, taken_at("2023-11-15 03:13:25"))
        world.item(jo, captured_utc=utc("2023-11-14 22:13:20"), captured_local="2023-11-14T22:13:20", width=64, height=48, suffix=".jpg")
        candidate = dedupe.candidate_of(TakeoutItem(path=self.path, sha1="", copies=(self.path,), sidecar=None), found, self.root)
        decision = dedupe.decide(world.index(account), candidate, dedupe.DedupeSettings())
        self.assertEqual((decision.tier, decision.action), (2, "skip"))


def fixtures_sha(path):
    from darkroom.domain import media

    return media.sha1_of(path)[0]


class CountsTests(unittest.TestCase):
    def test_counts_for_the_standard_export(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = standard_export(Path(folder) / "in")
            staging = Path(folder) / "staging"
            for part in manifest.parts:
                with zipfile.ZipFile(part) as archive:
                    archive.extractall(staging)
            counts = metadata.summarize(pairing.pair(staging))
        self.assertEqual(counts.unpaired, 2)
        self.assertEqual(counts.orphans, 1)
        self.assertEqual(counts.edited, 1)
        self.assertEqual(counts.live_pairs, 2)
        self.assertEqual(counts.album_only, 0)
        self.assertGreater(counts.paired, 8)


if __name__ == "__main__":
    unittest.main()
