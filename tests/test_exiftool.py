import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from darkroom.providers import exiftool
from tests.fixtures import media as fixtures
from tools import verify


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
        with mock.patch.object(exiftool, "run") as run:
            self.assertEqual(exiftool.read([]), {})
        run.assert_not_called()

    def test_a_failed_run_with_no_output_is_unreadable(self):
        done = mock.Mock(stdout="", stderr="boom\n", returncode=1)
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch.object(exiftool, "run", return_value=done), \
                self.assertRaises(exiftool.Unreadable):
            exiftool.read([self.folder / "x.jpg"])

    def test_exiftool_taking_too_long_is_unreadable_and_the_time_allowed_grows_with_the_batch(self):
        timeouts = []

        def slow(command, timeout=exiftool.TIMEOUT):
            timeouts.append(timeout)
            raise subprocess.TimeoutExpired(command, timeout)

        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch.object(exiftool, "run", slow):
            for count in (1, 3, exiftool.BATCH):
                with self.subTest(count=count), self.assertRaises(exiftool.TimedOut):
                    exiftool.read([self.folder / f"{n}.jpg" for n in range(count)])
        self.assertEqual(timeouts, [exiftool.FILE_TIMEOUT, 3 * exiftool.FILE_TIMEOUT, exiftool.TIMEOUT])

    def test_output_that_is_not_a_list_of_entries_is_unreadable_or_ignored(self):
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"):
            for stdout in ("[{", "{\"SourceFile\": \"x\"}"):
                with self.subTest(stdout=stdout), mock.patch.object(exiftool, "run", return_value=mock.Mock(stdout=stdout, stderr="", returncode=0)), \
                        self.assertRaises(exiftool.Unreadable):
                    exiftool.read([self.folder / "x.jpg"])
            odd = mock.Mock(stdout='[1, {"no": "source"}, {"SourceFile": "/a.jpg", "File:MIMEType": "image/jpeg"}]', stderr="", returncode=0)
            with mock.patch.object(exiftool, "run", return_value=odd):
                self.assertEqual(exiftool.read([Path("/a.jpg")]), {Path("/a.jpg"): {"SourceFile": "/a.jpg", "File:MIMEType": "image/jpeg"}})

    def test_an_empty_answer_with_success_means_nothing_found(self):
        done = mock.Mock(stdout="", stderr="", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch.object(exiftool, "run", return_value=done):
            self.assertEqual(exiftool.read([self.folder / "x.jpg"]), {})

    def test_missing(self):
        with mock.patch("shutil.which", return_value=None), self.assertRaises(exiftool.Missing):
            exiftool.version()

    def test_version(self):
        done = mock.Mock(stdout="13.00\n", returncode=0)
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch.object(exiftool, "run", return_value=done):
            self.assertEqual(exiftool.version(), "13.00")

    def test_version_failure_raises_called_process_error(self):
        done = mock.Mock(stdout="", stderr="bad", returncode=2)
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch.object(exiftool, "run", return_value=done), \
                self.assertRaises(subprocess.CalledProcessError):
            exiftool.version()


class RunTests(unittest.TestCase):
    def test_returns_output_error_and_status(self):
        done = exiftool.run([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"])
        self.assertEqual((done.stdout, done.stderr, done.returncode), ("out\n", "err", 3))

    def test_large_output_does_not_block(self):
        done = exiftool.run([sys.executable, "-c", "print('x' * 1000000)"])
        self.assertEqual(len(done.stdout.strip()), 1000000)

    def test_a_command_that_outlives_the_timeout_is_killed(self):
        for timeout in (0.1, 0.3):
            with self.subTest(timeout=timeout):
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    exiftool.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout)
                self.assertLess(time.monotonic() - started, 10)

    def test_never_sleeps(self):
        with mock.patch("time.sleep", side_effect=OSError(22, "Invalid argument")):
            done = exiftool.run([sys.executable, "-c", "import time; time.sleep(0.2); print('ok')"])
        self.assertEqual(done.stdout, "ok\n")


class FrozenClockTests(unittest.TestCase):
    def test_repeated_runs_under_the_frozen_clock_never_fail(self):
        library = verify.find_faketime()
        if not library:
            self.skipTest(verify.missing_faketime_message())
        env = verify.frozen_env(dict(os.environ), library)
        with mock.patch.dict(os.environ, env):
            for _ in range(20):
                done = exiftool.run([sys.executable, "-c", "print('ok')"])
                self.assertEqual((done.stdout, done.returncode), ("ok\n", 0))


if __name__ == "__main__":
    unittest.main()
