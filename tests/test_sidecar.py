import json
import unittest

from darkroom.domain.ingest import sidecar


def build(**overrides):
    base = {"title": "IMG_1.JPG", "description": "Beach day", "photoTakenTime": {"timestamp": "1562243415"},
            "geoData": {"latitude": 10.0, "longitude": 20.0}, "geoDataExif": {"latitude": 40.7, "longitude": -74.0},
            "googlePhotosOrigin": {"mobileUpload": {"deviceType": "IOS_PHONE"}}}
    base.update(overrides)
    return json.dumps(base)


class SidecarTests(unittest.TestCase):
    def test_reads_time_place_description_title_and_origin(self):
        parsed = sidecar.parse(build())
        self.assertEqual(parsed.photo_taken_utc, 1562243415.0)
        self.assertEqual((parsed.lat, parsed.lon), (40.7, -74.0))
        self.assertEqual((parsed.description, parsed.title, parsed.origin), ("Beach day", "IMG_1.JPG", "mobile_upload"))

    def test_the_raw_json_is_kept(self):
        raw = build()
        self.assertEqual(sidecar.parse(raw).raw, raw)

    def test_exif_geo_wins_over_plain_geo(self):
        self.assertEqual(sidecar.parse(build()).lat, 40.7)

    def test_zero_exif_geo_falls_back_to_plain_geo(self):
        parsed = sidecar.parse(build(geoDataExif={"latitude": 0.0, "longitude": 0.0}))
        self.assertEqual((parsed.lat, parsed.lon), (10.0, 20.0))

    def test_zero_everywhere_means_no_location(self):
        parsed = sidecar.parse(build(geoDataExif={"latitude": 0.0, "longitude": 0.0}, geoData={"latitude": 0.0, "longitude": 0.0}))
        self.assertEqual((parsed.lat, parsed.lon), (None, None))

    def test_impossible_or_malformed_coordinates_are_ignored(self):
        for bad in ({"latitude": 91.0, "longitude": 0.5}, {"latitude": "1", "longitude": 2}, {"latitude": True, "longitude": 1}, [], None):
            with self.subTest(bad=bad):
                parsed = sidecar.parse(build(geoDataExif=bad, geoData=bad))
                self.assertEqual((parsed.lat, parsed.lon), (None, None))

    def test_missing_or_bad_time_is_none(self):
        for value in ({}, {"timestamp": "abc"}, {"timestamp": "0"}, {"timestamp": "1e999"}, {"timestamp": "nan"}, {"timestamp": "-1e18"}, "x", None):
            with self.subTest(value=value):
                self.assertIsNone(sidecar.parse(build(photoTakenTime=value)).photo_taken_utc)

    def test_a_pre_1970_time_is_kept(self):
        self.assertEqual(sidecar.parse(build(photoTakenTime={"timestamp": "-86400"})).photo_taken_utc, -86400.0)

    def test_origin_kinds(self):
        self.assertEqual(sidecar.parse(build(googlePhotosOrigin={"fromSharedAlbum": {}})).origin, "shared_album")
        self.assertEqual(sidecar.parse(build(googlePhotosOrigin={"webUpload": {}})).origin, "web_upload")
        self.assertEqual(sidecar.parse(build(googlePhotosOrigin={"somethingNew": {}})).origin, "other")
        self.assertEqual(sidecar.parse(build(googlePhotosOrigin=None)).origin, "none")

    def test_blank_text_is_none(self):
        parsed = sidecar.parse(build(description="  ", title=""))
        self.assertEqual((parsed.description, parsed.title), (None, None))

    def test_not_json_or_not_an_object_is_none(self):
        self.assertIsNone(sidecar.parse("nope"))
        self.assertIsNone(sidecar.parse("[1]"))


if __name__ == "__main__":
    unittest.main()
