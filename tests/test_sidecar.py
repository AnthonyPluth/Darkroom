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
        self.assertEqual((parsed.lat, parsed.lon), (10.0, 20.0))
        self.assertEqual((parsed.description, parsed.title, parsed.origin), ("Beach day", "IMG_1.JPG", "mobile_upload"))

    def test_the_raw_json_is_kept(self):
        raw = build()
        self.assertEqual(sidecar.parse(raw).raw, raw)

    def test_the_place_the_person_set_wins_over_the_one_the_file_had(self):
        parsed = sidecar.parse(build())
        self.assertEqual((parsed.lat, parsed.lon), (10.0, 20.0))

    def test_a_zero_plain_geo_falls_back_to_the_files_own_place(self):
        parsed = sidecar.parse(build(geoData={"latitude": 0.0, "longitude": 0.0}))
        self.assertEqual((parsed.lat, parsed.lon), (40.7, -74.0))

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

    def test_a_byte_order_mark_is_ignored(self):
        raw = build()
        parsed = sidecar.parse("﻿" + raw)
        self.assertEqual((parsed.photo_taken_utc, parsed.lat, parsed.raw), (1562243415.0, 10.0, raw))

    def test_deep_nesting_or_a_huge_number_is_none_and_never_raises(self):
        for raw in ("[" * 200_000, "{\"a\":" * 200_000, '{"photoTakenTime": {"timestamp": ' + "9" * 5000 + "}}"):
            with self.subTest(length=len(raw)):
                self.assertIsNone(sidecar.parse(raw))


class DecodeTests(unittest.TestCase):
    def test_plain_utf8_is_read_as_it_is(self):
        raw = build(description="Café au bord de l'eau")
        self.assertEqual(sidecar.decode(raw.encode()), sidecar.Decoded(raw, repaired=False))

    def test_a_utf8_byte_order_mark_is_dropped(self):
        raw = build()
        self.assertEqual(sidecar.decode(b"\xef\xbb\xbf" + raw.encode()), sidecar.Decoded(raw, repaired=False))

    def test_utf16_with_or_without_a_byte_order_mark_is_read(self):
        raw = build(description="Café")
        for data in (raw.encode("utf-16"), b"\xff\xfe" + raw.encode("utf-16-le"), b"\xfe\xff" + raw.encode("utf-16-be"),
                     raw.encode("utf-16-le"), raw.encode("utf-16-be")):
            with self.subTest(start=data[:4]):
                self.assertEqual(sidecar.decode(data), sidecar.Decoded(raw, repaired=False))

    def test_latin1_bytes_are_replaced_and_the_time_and_place_survive(self):
        data = build(description="CAPTION").encode().replace(b"CAPTION", "Café made-up".encode("latin-1"))
        decoded = sidecar.decode(data)
        self.assertTrue(decoded.repaired)
        parsed = sidecar.parse(decoded.text)
        self.assertEqual((parsed.photo_taken_utc, parsed.lat, parsed.lon, parsed.description), (1562243415.0, 10.0, 20.0, "Caf� made-up"))

    def test_broken_utf16_is_repaired_not_refused(self):
        decoded = sidecar.decode(b"\xff\xfe" + build().encode("utf-16-le")[:-1])
        self.assertTrue(decoded.repaired)

    def test_a_file_up_to_the_limit_is_read_and_a_larger_one_is_not(self):
        raw = build()
        padded = raw + " " * (sidecar.LIMIT - len(raw))
        self.assertEqual(sidecar.parse(sidecar.decode(padded.encode()).text).photo_taken_utc, 1562243415.0)
        self.assertIsNone(sidecar.decode((padded + " ").encode()))

    def test_empty_or_truncated_text_decodes_but_does_not_parse(self):
        for data in (b"", build().encode()[:40]):
            with self.subTest(data=data):
                decoded = sidecar.decode(data)
                self.assertFalse(decoded.repaired)
                self.assertIsNone(sidecar.parse(decoded.text))


if __name__ == "__main__":
    unittest.main()
