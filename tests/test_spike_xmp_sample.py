import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "spikes" / "make_xmp_sample.py"
spec = importlib.util.spec_from_file_location("make_xmp_sample", SCRIPT)
make_xmp_sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_xmp_sample)


def read_tags(path):
    out = subprocess.run(
        ["exiftool", "-j", "-n", "-a", "-G1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)[0]


class XmpSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.samples = Path(cls.folder.name)
        make_xmp_sample.build(cls.samples)

    def test_writes_exactly_the_five_files(self):
        self.assertEqual(
            sorted(p.name for p in self.samples.iterdir()),
            ["sample-a.jpg", "sample-a.jpg.xmp", "sample-b.jpg", "sample-b.xmp", "sample-c.jpg"],
        )

    def test_embedded_values_are_the_wrong_ones(self):
        for name in ("sample-a.jpg", "sample-b.jpg", "sample-c.jpg"):
            with self.subTest(name=name):
                tags = read_tags(self.samples / name)
                self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2001:01:01 01:01:01")
                self.assertEqual(tags["IFD0:ImageDescription"], "WRONG embedded description")
                self.assertAlmostEqual(tags["GPS:GPSLatitude"], 10.0)
                self.assertAlmostEqual(tags["GPS:GPSLongitude"], 20.0)

    def test_sidecar_values_are_the_right_ones(self):
        for name in ("sample-a.jpg.xmp", "sample-b.xmp"):
            with self.subTest(name=name):
                tags = read_tags(self.samples / name)
                self.assertEqual(tags["XMP-exif:DateTimeOriginal"], "2024:06:15 12:00:00")
                self.assertEqual(tags["XMP-dc:Description"], "RIGHT sidecar description")
                self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 45.4340, places=4)
                self.assertAlmostEqual(tags["XMP-exif:GPSLongitude"], 12.3388, places=4)

    def test_control_has_no_sidecar(self):
        self.assertFalse((self.samples / "sample-c.jpg.xmp").exists())
        self.assertFalse((self.samples / "sample-c.xmp").exists())

    def test_embedded_jpeg_has_no_xmp_values(self):
        tags = read_tags(self.samples / "sample-a.jpg")
        self.assertNotIn("XMP-dc:Description", tags)
        self.assertNotIn("XMP-exif:DateTimeOriginal", tags)

    def test_rerun_overwrites_existing_output(self):
        make_xmp_sample.build(self.samples)
        self.assertEqual(read_tags(self.samples / "sample-a.jpg.xmp")["XMP-dc:Description"], "RIGHT sidecar description")


if __name__ == "__main__":
    unittest.main()
