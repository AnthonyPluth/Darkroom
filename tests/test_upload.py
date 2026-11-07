import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom.storage import paths
from tests.device_world import sha1

class UploadPathsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.photos = paths.Photos(self.root)
        self.photos.ensure_person_tree("jo")

    def test_uploads_are_received_only_into_an_upload_folder(self):
        with self.assertRaises(paths.NotAnUpload):
            paths.receive_upload(self.photos.originals("jo"), io.BytesIO(b"abc"), 3)
        temp, digest = paths.receive_upload(self.photos.uploads_tmp("jo"), io.BytesIO(b"abc"), 3)
        self.assertEqual((temp.read_bytes(), digest), (b"abc", sha1(b"abc")))
        with self.assertRaises(paths.NotAnUpload):
            paths.discard_upload(self.photos.originals("jo") / "IMG.JPG")
        with self.assertRaises(paths.NotAnUpload):
            paths.adopt_upload(self.photos.originals("jo") / "IMG.JPG", self.photos.originals("jo") / "x.JPG")
        paths.discard_upload(temp)
        self.assertFalse(temp.exists())

    def test_an_adopted_upload_never_replaces_a_file_and_keeps_its_temp_when_refused(self):
        held = self.photos.originals("jo") / "IMG.JPG"
        held.write_bytes(b"kept")
        temp, _ = paths.receive_upload(self.photos.uploads_tmp("jo"), io.BytesIO(b"new"), 3)
        with self.assertRaises(paths.AlreadyExists):
            paths.adopt_upload(temp, held)
        self.assertEqual((held.read_bytes(), temp.exists()), (b"kept", True))
        with mock.patch("os.link", side_effect=OSError("cross-device")):
            paths.adopt_upload(temp, self.photos.originals("jo") / "copied.JPG")
        self.assertEqual(((self.photos.originals("jo") / "copied.JPG").read_bytes(), temp.exists()), (b"new", False))

    def test_a_short_stream_removes_its_temp(self):
        with self.assertRaises(paths.IncompleteUpload):
            paths.receive_upload(self.photos.uploads_tmp("jo"), io.BytesIO(b"ab"), 3)
        self.assertEqual(list(self.photos.uploads_tmp("jo").iterdir()), [])



if __name__ == "__main__":
    unittest.main()
