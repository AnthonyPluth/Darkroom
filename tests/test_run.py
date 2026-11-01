import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import run
from darkroom.providers import exiftool


class RunTests(unittest.TestCase):
    def test_check_config_reports_problems_and_a_missing_exiftool(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", side_effect=exiftool.Missing("exiftool is not installed")):
                code = run.main(["check-config"])
        self.assertEqual(code, 1)
        self.assertIn("DARKROOM_SECRET_KEY", err.getvalue())
        self.assertIn("exiftool is not installed", err.getvalue())

    def test_check_config_passes_when_complete(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example"}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                self.assertEqual(run.main(["check-config"]), 0)

    def test_serve_starts_a_server_and_stops_on_interrupt(self):
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 1234)
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(run.handler, "make_server", return_value=server), mock.patch.object(run, "migrate", return_value=0) as migrated, \
                mock.patch.object(run.jobs, "build") as built, mock.patch.object(run.db, "make_engine"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.main(["serve"]), 0)
        server.server_close.assert_called_once()
        migrated.assert_called_once()
        built.return_value.stop.assert_called_once()

    def test_migrate_creates_the_database(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()):
            values = {"DARKROOM_DATA_DIR": folder + "/data", "DARKROOM_PHOTOS_ROOT": folder}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(run.main(["migrate"]), 0)
                self.assertEqual(run.main(["migrate"]), 0)
            self.assertTrue(os.path.exists(folder + "/data/darkroom.db"))


class JobsCommandTests(unittest.TestCase):
    def test_runs_one_job_by_name(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()):
            values = {"DARKROOM_DATA_DIR": folder, "DARKROOM_PHOTOS_ROOT": folder, "DATABASE_URL": f"sqlite:///{folder}/j.db"}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(run.jobs.Scheduler, "run_one") as ran:
                self.assertEqual(run.main(["jobs", "alerts.tick"]), 0)
            ran.assert_called_once()

    def test_an_unknown_job_lists_the_known_ones(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err:
            values = {"DARKROOM_DATA_DIR": folder, "DARKROOM_PHOTOS_ROOT": folder, "DATABASE_URL": f"sqlite:///{folder}/j.db"}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(run.main(["jobs", "nope"]), 1)
        self.assertIn("alerts.tick", err.getvalue())

    def test_a_job_name_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run.main(["jobs"])


class ExiftoolTests(unittest.TestCase):
    def test_missing(self):
        with mock.patch("shutil.which", return_value=None), self.assertRaises(exiftool.Missing):
            exiftool.version()

    def test_version(self):
        done = mock.Mock(stdout="13.00\n")
        with mock.patch("shutil.which", return_value="/usr/bin/exiftool"), mock.patch("subprocess.run", return_value=done):
            self.assertEqual(exiftool.version(), "13.00")


if __name__ == "__main__":
    unittest.main()
