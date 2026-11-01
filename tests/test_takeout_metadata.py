import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from darkroom.domain.ingest import metadata, pairing, sidecar
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
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

    def test_exif_gps_beats_a_different_sidecar_gps(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00", gps=(40.7, -74.0))
        found = self.build(path, parsed(10.0, 20.0))
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

    def test_exif_time_beats_the_sidecar_time(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15", offset="-05:00")
        found = self.build(path, parsed(taken=TAKEN + 99999))
        self.assertEqual((found.time_source, found.offset_minutes), ("exif_offset", -300))
        self.assertEqual(found.captured_utc, 1562261415.0)

    def test_an_exif_local_time_without_offset_keeps_its_local_text_and_takes_the_sidecar_utc(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15")
        found = self.build(path, parsed())
        self.assertEqual((found.captured_local, found.captured_utc, found.time_source, found.offset_minutes), ("2019-07-04T12:30:15", float(TAKEN), "sidecar", None))

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

    def test_the_raw_sidecar_is_kept(self):
        path = fixtures.make_jpeg(self.root / "a.jpg")
        side = parsed(40.7, -74.0)
        self.assertEqual(self.build(path, side).sidecar_json, side.raw)

    def test_nothing_is_written_to_disk(self):
        path = fixtures.make_jpeg(self.root / "a.jpg", taken="2019:07:04 12:30:15")
        before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*"))
        self.build(path, parsed(40.7, -74.0))
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")), before)


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
