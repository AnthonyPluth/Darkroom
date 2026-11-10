import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from darkroom.domain import thumbs
from darkroom.providers import exiftool
from darkroom.storage.models import DedupeDecision, MediaItem
from darkroom.storage.paths import Photos
from tests.fixtures import media as fixtures
from tests.review_world import ReviewCase


def jpeg_bytes(size=(320, 200), colour=(10, 200, 90)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG")
    return out.getvalue()


def decoded(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def no_preview(path):
    raise AssertionError("only a video asks for a preview")


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve() / "photos"
        self.root.mkdir()
        self.photos = Photos(self.root)
        self.photos.ensure_person_tree("jo")
        self.originals = self.root / "jo" / "originals"

    def test_a_jpeg_is_scaled_down_and_cached_under_the_persons_thumbs(self):
        source = fixtures.make_jpeg(self.originals / "big.jpg", size=(2000, 1000))
        data = thumbs.thumbnail(self.photos, "jo", source, no_preview)
        image = decoded(data)
        self.assertEqual((image.format, image.size), ("JPEG", (640, 320)))
        cached = list((self.root / "_inbox" / "jo" / ".thumbs").iterdir())
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].read_bytes(), data)
        with mock.patch.object(thumbs, "render", side_effect=AssertionError("the cache should answer")):
            self.assertEqual(thumbs.thumbnail(self.photos, "jo", source, no_preview), data)

    def test_a_changed_file_gets_a_new_thumbnail(self):
        source = fixtures.make_jpeg(self.originals / "a.jpg", size=(100, 50))
        first = thumbs.thumbnail(self.photos, "jo", source, no_preview)
        fixtures.make_jpeg(source, size=(50, 100))
        self.assertNotEqual(decoded(thumbs.thumbnail(self.photos, "jo", source, no_preview)).size, decoded(first).size)

    def test_a_heic_is_converted_to_jpeg(self):
        source = fixtures.make_heic(self.originals / "IMG_0001.HEIC", size=(64, 48))
        image = decoded(thumbs.thumbnail(self.photos, "jo", source, no_preview))
        self.assertEqual((image.format, image.size), ("JPEG", (64, 48)))

    def test_a_video_uses_its_embedded_preview(self):
        source = fixtures.make_video(self.originals / "clip.mov")
        image = decoded(thumbs.thumbnail(self.photos, "jo", source, lambda path: jpeg_bytes()))
        self.assertEqual(image.size, (320, 200))

    def test_a_video_without_a_preview_gets_the_placeholder(self):
        source = fixtures.make_video(self.originals / "clip.mp4")
        image = decoded(thumbs.thumbnail(self.photos, "jo", source, lambda path: None))
        self.assertEqual(image.size, thumbs.PLACEHOLDER_SIZE)
        self.assertEqual(decoded(thumbs.thumbnail(self.photos, "jo", fixtures.make_video(self.originals / "b.mov"), exiftool.embedded_preview)).size,
                         thumbs.PLACEHOLDER_SIZE)

    def test_a_video_whose_preview_cannot_be_read_gets_the_placeholder(self):
        source = fixtures.make_video(self.originals / "clip.mov")
        for preview in (lambda path: b"not an image", mock.Mock(side_effect=exiftool.Missing("no exiftool"))):
            with self.subTest(preview=preview):
                self.assertEqual(decoded(thumbs.render(source, preview)).size, thumbs.PLACEHOLDER_SIZE)

    def test_an_unreadable_photo_gets_the_placeholder(self):
        source = self.originals / "broken.jpg"
        source.write_bytes(b"not a jpeg at all")
        self.assertEqual(decoded(thumbs.thumbnail(self.photos, "jo", source, no_preview)).size, thumbs.PLACEHOLDER_SIZE)

    def test_a_cache_that_cannot_be_written_still_answers(self):
        source = fixtures.make_jpeg(self.originals / "a.jpg")
        with mock.patch.object(thumbs.paths, "write_inbox_once", side_effect=PermissionError("read-only")):
            self.assertEqual(decoded(thumbs.thumbnail(self.photos, "jo", source, no_preview)).size, (64, 48))

    def test_a_linked_thumbs_folder_is_never_read_or_written_through(self):
        source = fixtures.make_jpeg(self.originals / "a.jpg")
        thumbs.thumbnail(self.photos, "jo", source, no_preview)
        folder = self.root / "_inbox" / "jo" / ".thumbs"
        (name,) = [p.name for p in folder.iterdir()]
        planted = self.originals / name
        planted.write_bytes(b"not a thumbnail")
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()
        folder.symlink_to(self.originals, target_is_directory=True)
        before = sorted((p.name, p.read_bytes()) for p in self.originals.iterdir())
        self.assertEqual(decoded(thumbs.thumbnail(self.photos, "jo", source, no_preview)).size, (64, 48))
        self.assertEqual(sorted((p.name, p.read_bytes()) for p in self.originals.iterdir()), before)

    def test_a_cache_swapped_for_a_link_is_not_read(self):
        source = fixtures.make_jpeg(self.originals / "a.jpg")
        thumbs.thumbnail(self.photos, "jo", source, no_preview)
        (cached,) = (self.root / "_inbox" / "jo" / ".thumbs").iterdir()
        secret = self.root / "secret.jpg"
        secret.write_bytes(b"not for the viewer")
        cached.unlink()
        cached.symlink_to(secret)
        self.assertNotEqual(thumbs.thumbnail(self.photos, "jo", source, no_preview), b"not for the viewer")

    def test_files_outside_the_photo_root_or_links_are_refused(self):
        outside = fixtures.make_jpeg(self.root.parent / "outside.jpg")
        link = self.originals / "link.jpg"
        link.symlink_to(outside)
        for source in (outside, link, self.originals / "missing.jpg", self.originals):
            with self.subTest(source=source), self.assertRaises(thumbs.NotAPhoto):
                thumbs.thumbnail(self.photos, "jo", source, no_preview)
        self.assertFalse((self.root / "_inbox" / "jo" / ".thumbs").exists())


class EmbeddedPreviewTests(unittest.TestCase):
    def test_the_first_tag_holding_an_image_is_returned(self):
        image = jpeg_bytes()
        answers = [subprocess.CompletedProcess([], 0, b"", b""), subprocess.CompletedProcess([], 0, image, b"")]
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch.object(exiftool, "run_binary", side_effect=answers) as run:
            self.assertEqual(exiftool.embedded_preview(Path("/x/clip.mov")), image)
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["-PreviewImage", "-ThumbnailImage"])

    def test_a_preview_that_takes_too_long_gives_the_placeholder(self):
        with tempfile.TemporaryDirectory() as folder:
            source = fixtures.make_video(Path(folder) / "slow.mov")
            with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), \
                    mock.patch.object(exiftool, "run_binary", side_effect=subprocess.TimeoutExpired(["exiftool"], exiftool.PREVIEW_TIMEOUT)) as run:
                self.assertEqual(decoded(thumbs.render(source, exiftool.embedded_preview)).size, thumbs.PLACEHOLDER_SIZE)
            self.assertEqual(run.call_count, 1)

    def test_no_image_in_any_tag_is_none(self):
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), \
                mock.patch.object(exiftool, "run_binary", return_value=subprocess.CompletedProcess([], 1, b"junk", b"")):
            self.assertIsNone(exiftool.embedded_preview(Path("/x/clip.mov")))


class ThumbRouteTests(ReviewCase):
    def test_both_sides_of_a_pair_answer_a_jpeg(self):
        review_id = self.pending().id
        for side in ("library", "export"):
            with self.subTest(side=side):
                status, data = self.call("GET", f"/api/review/{review_id}/thumb/{side}")
                self.assertEqual(status, 200)
                self.assertEqual(decoded(data).format, "JPEG")

    def test_a_library_path_outside_the_root_is_404(self):
        row = self.pending()
        opened = self.opened
        matched = opened.get(MediaItem, self.decision_of("IMG_1005.JPG").matched_item_id)
        fixtures.make_jpeg(self.world.base / "secret.jpg")
        for path in ("../secret.jpg", "jo/originals/../../../secret.jpg", "sam/originals/IMG_1004.JPG"):
            with self.subTest(path=path):
                matched.path = path
                opened.commit()
                self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/library"), (404, {"error": "no such picture"}))

    def test_a_linked_original_is_404(self):
        row = self.pending()
        secret = fixtures.make_jpeg(self.world.base / "secret.jpg")
        original = self.world.root / "jo" / "originals" / "IMG_1005.JPG"
        original.unlink()
        original.symlink_to(secret)
        self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/library"), (404, {"error": "no such picture"}))

    def test_a_link_to_another_file_in_the_persons_folder_is_404(self):
        row = self.pending()
        original = self.world.root / "jo" / "originals" / "IMG_1005.JPG"
        other = self.world.root / "jo" / "originals" / "IMG_1001.JPG"
        self.assertTrue(other.is_file())
        original.unlink()
        original.symlink_to(other)
        self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/library"), (404, {"error": "no such picture"}))
        folder = self.world.root / "jo" / "originals" / "linked"
        folder.symlink_to(self.world.root / "jo" / "originals")
        opened = self.opened
        opened.get(MediaItem, self.decision_of("IMG_1005.JPG").matched_item_id).path = "jo/originals/linked/IMG_1001.JPG"
        opened.commit()
        self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/library"), (404, {"error": "no such picture"}))

    def test_a_missing_hold_or_side_is_404(self):
        row = self.pending()
        self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/middle"), (404, {"error": "no such picture"}))
        self.hold().unlink()
        self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/export"), (404, {"error": "no such picture"}))

    def test_a_pair_without_a_library_item_is_404(self):
        row = self.pending()
        opened = self.opened
        opened.get(DedupeDecision, self.decision_of("IMG_1005.JPG").id).matched_item_id = None
        opened.commit()
        self.assertEqual(self.call("GET", f"/api/review/{row.id}/thumb/library"), (404, {"error": "no such picture"}))
        detail = self.call("GET", f"/api/review/{row.id}")[1]
        self.assertEqual((detail["library"], detail["phash_distance"], detail["review"]["library_name"]), (None, None, None))


if __name__ == "__main__":
    unittest.main()
