import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fixtures import media as fixtures
from tools.spikes import compare_hashes


def run(*argv):
    out = io.StringIO()
    code = compare_hashes.main(list(argv), out)
    return code, out.getvalue()


def sidecar(origin):
    return json.dumps({"title": "x", "photoTakenTime": {"timestamp": "1"}, "googlePhotosOrigin": origin}).encode()


class CompareTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.originals = self.folder / "originals"
        self.originals.mkdir()

    def build_export(self):
        same = fixtures.make_jpeg(self.originals / "same.jpg", taken="2019:07:04 12:30:15", offset="-05:00", colour=(10, 20, 30))
        fixtures.make_jpeg(self.originals / "live.heic.jpg", colour=(70, 80, 90))
        fixtures.make_video(self.originals / "live.heic.mov")
        edited = fixtures.make_jpeg(self.originals / "edited.jpg", taken="2019:07:04 12:30:15", offset="-05:00", make="Aaaa", colour=(40, 50, 60))
        resized = fixtures.make_jpeg(self.originals / "resized.jpg", colour=(5, 5, 5))
        stage = self.folder / "stage"
        year = stage / "Takeout" / "Google Photos" / "Photos from 2019"
        album = stage / "Takeout" / "Google Photos" / "Family trip"
        year.mkdir(parents=True)
        album.mkdir(parents=True)
        (year / "same.jpg").write_bytes(same.read_bytes())
        (year / "live.heic.jpg").write_bytes((self.originals / "live.heic.jpg").read_bytes())
        changed = year / "edited.jpg"
        changed.write_bytes(edited.read_bytes())
        fixtures.set_tags(changed, {"Make": "Zzzz"})
        bigger = year / "resized.jpg"
        bigger.write_bytes(resized.read_bytes())
        fixtures.set_tags(bigger, {"Make": "Added by Google", "Model": "A long model name that changes the size"})
        (year / "same.jpg.json").write_bytes(sidecar({"mobileUpload": {"deviceType": "IOS_PHONE"}}))
        (album / "shared.jpg").write_bytes(fixtures.make_jpeg(self.folder / "shared-src.jpg", colour=(1, 2, 3)).read_bytes())
        (album / "shared.jpg.json").write_bytes(sidecar({"fromSharedAlbum": {}}))
        return stage

    def zipped(self, stage):
        target = self.folder / "export"
        target.mkdir()
        with zipfile.ZipFile(target / "takeout-001.zip", "w") as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
        return target

    def test_takeout_counts_from_a_directory_and_from_zips_agree(self):
        stage = self.build_export()
        _, from_directory = run("takeout", "--export", str(stage), "--originals", str(self.originals))
        _, from_zip = run("takeout", "--export", str(self.zipped(stage)), "--originals", str(self.originals))
        self.assertIn("Media files in the export: 5", from_directory)
        self.assertIn("Byte-identical to an original: 2 (40.0%)", from_directory)
        self.assertIn("Same name and size, different bytes: 1 (20.0%)", from_directory)
        self.assertIn("Same name, different size: 1 (20.0%)", from_directory)
        self.assertIn("No counterpart in the originals: 1 (20.0%)", from_directory)
        self.assertIn("tag that differs in these: IFD0:Make", from_directory)
        self.assertIn("Items found only under album folders: 1 (20.0%)", from_directory)
        self.assertIn("shared.jpg:", from_directory)
        self.assertIn('"fromSharedAlbum"', from_directory)
        self.assertNotIn(str(self.folder), from_directory)
        self.assertEqual(from_directory.split("originals indexed")[1], from_zip.split("originals indexed")[1])

    def test_live_photo_video_missing_from_the_export_is_counted(self):
        export = self.build_export()
        _code, text = run("takeout", "--export", str(export), "--originals", str(self.originals))
        self.assertIn("Live Photos in the originals (a still with a video of the same name): 1; video in the export: 0, missing: 1", text)
        fixtures.make_video(export / "Takeout" / "Google Photos" / "Photos from 2019" / "live.heic.mov")
        _code, text = run("takeout", "--export", str(export), "--originals", str(self.originals))
        self.assertIn("video in the export: 1, missing: 0", text)

    def test_nothing_is_written(self):
        export = self.build_export()
        before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.folder.rglob("*"))
        run("takeout", "--export", str(export), "--originals", str(self.originals))
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.folder.rglob("*")), before)

    def test_phone_mode(self):
        fixtures.make_jpeg(self.originals / "a.jpg", colour=(1, 1, 1))
        fixtures.make_jpeg(self.originals / "b.jpg", colour=(2, 2, 2))
        phone = self.folder / "phone"
        phone.mkdir()
        (phone / "a.jpg").write_bytes((self.originals / "a.jpg").read_bytes())
        (phone / "b.jpg").write_bytes(b"different")
        (phone / "c.jpg").write_bytes(b"new")
        code, text = run("phone", "--phone-files", str(phone), "--originals", str(self.originals))
        self.assertEqual(code, 0)
        self.assertIn("Files exported from the iPhone: 3", text)
        self.assertIn("Byte-identical on Unraid: 1 (33.3%)", text)
        self.assertIn("different bytes: b.jpg", text)
        self.assertIn("not on Unraid: c.jpg", text)

    def test_helpers(self):
        self.assertEqual(compare_hashes.percent(1, 0), "1")
        self.assertEqual(compare_hashes.scrubbed({"a": [1], "b": {"c": "x"}}), {"a": ["int"], "b": {"c": "str"}})
        self.assertEqual(compare_hashes.differing_tags({"A": 1, "System:X": 1}, {"A": 2, "System:X": 2}), ["A"])


if __name__ == "__main__":
    unittest.main()
