import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures import media

MOMENT = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", "gps": (45.4, -12.3), "make": "Apple", "model": "iPhone 11"}


class TaggedFixtureTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        saved = dict(media.TAGGED)
        media.TAGGED.clear()
        self.addCleanup(lambda: (media.TAGGED.clear(), media.TAGGED.update(saved)))

    def runs(self):
        return mock.patch.object(media.subprocess, "run", wraps=subprocess.run)

    def test_a_repeated_fixture_is_the_file_exiftool_wrote_without_running_it_again(self):
        with self.runs() as run:
            first = media.make_jpeg(self.root / "a.jpg", **MOMENT).read_bytes()
            again = media.make_jpeg(self.root / "b.jpg", **MOMENT).read_bytes()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(again, first)
        media.TAGGED.clear()
        self.assertEqual(media.make_jpeg(self.root / "c.jpg", **MOMENT).read_bytes(), first)
        self.assertEqual(media.exif_tags(**{key: MOMENT[key] for key in ("taken", "offset", "gps", "make", "model")})["Make"], "Apple")

    def test_other_tags_another_image_or_another_kind_of_file_runs_exiftool(self):
        with self.runs() as run:
            plain = media.make_jpeg(self.root / "a.jpg", **MOMENT).read_bytes()
            other_tags = media.make_jpeg(self.root / "b.jpg", **{**MOMENT, "model": "iPhone 12"}).read_bytes()
            other_image = media.make_jpeg(self.root / "c.jpg", colour=(1, 2, 3), **MOMENT).read_bytes()
            video = media.make_video(self.root / "d.mp4", creation=MOMENT["taken"])
            video_again = media.make_video(self.root / "e.mp4", creation=MOMENT["taken"])
            copied = self.root / "f.mov"
            copied.write_bytes(media.TINY_VIDEO.read_bytes())
            media.set_tags(copied, {"QuickTime:CreateDate": MOMENT["taken"], "Keys:CreationDate": MOMENT["taken"]})
        self.assertEqual(run.call_count, 5)
        self.assertEqual(len({plain, other_tags, other_image}), 3)
        self.assertEqual(video.read_bytes(), video_again.read_bytes())

    def test_a_file_exiftool_could_not_tag_is_not_remembered(self):
        broken = self.root / "broken.jpg"
        broken.write_bytes(b"not a photo")
        for _ in range(2):
            with self.subTest(), self.runs() as run, self.assertRaises(subprocess.CalledProcessError):
                media.set_tags(broken, {"Make": "Apple"})
            self.assertEqual(run.call_count, 1)
        self.assertEqual(media.TAGGED, {})
        self.assertEqual(broken.read_bytes(), b"not a photo")

    def test_no_tags_leaves_the_file_alone(self):
        path = self.root / "a.jpg"
        path.write_bytes(b"as written")
        with self.runs() as run:
            media.set_tags(path, {})
        run.assert_not_called()
        self.assertEqual(path.read_bytes(), b"as written")


if __name__ == "__main__":
    unittest.main()
