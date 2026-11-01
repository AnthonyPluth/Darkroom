import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom.providers import exiftool
from tests.fixtures import media as fixtures


class ReadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_reads_tags_by_path_in_group_form(self):
        photo = fixtures.make_jpeg(self.folder / "a.jpg", taken="2019:07:04 12:30:15", make="Apple", model="iPhone 15")
        found = exiftool.read([photo])
        tags = found[photo]
        self.assertEqual(tags["IFD0:Make"], "Apple")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2019:07:04 12:30:15")

    def test_a_file_that_is_not_a_picture_is_still_described_so_the_caller_can_judge(self):
        broken = self.folder / "bad.jpg"
        broken.write_bytes(b"not a picture")
        self.assertEqual(exiftool.read([broken])[broken]["File:MIMEType"], "text/plain")

    def test_many_files_are_read_in_batches(self):
        files = [fixtures.make_jpeg(self.folder / f"{n}.jpg", size=(8, 8)) for n in range(5)]
        with mock.patch.object(exiftool, "BATCH", 2):
            self.assertEqual(set(exiftool.read(files)), set(files))

    def test_no_files_no_call(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(exiftool.read([]), {})
        run.assert_not_called()

    def test_a_failed_run_with_no_output_is_unreadable(self):
        done = mock.Mock(stdout="", stderr="boom\n", returncode=1)
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch("subprocess.run", return_value=done), \
                self.assertRaises(exiftool.Unreadable):
            exiftool.read([self.folder / "x.jpg"])

    def test_an_empty_answer_with_success_means_nothing_found(self):
        done = mock.Mock(stdout="", stderr="", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch("subprocess.run", return_value=done):
            self.assertEqual(exiftool.read([self.folder / "x.jpg"]), {})

    def test_missing(self):
        with mock.patch("shutil.which", return_value=None), self.assertRaises(exiftool.Missing):
            exiftool.version()

    def test_version(self):
        done = mock.Mock(stdout="13.00\n")
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch("subprocess.run", return_value=done):
            self.assertEqual(exiftool.version(), "13.00")


if __name__ == "__main__":
    unittest.main()
