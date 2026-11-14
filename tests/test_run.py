import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import run
from darkroom.providers import exiftool
from darkroom.storage import db


class RunTests(unittest.TestCase):
    def test_the_demo_stays_on_sqlite_whatever_database_url_says(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@db.example/darkroom"}, clear=True):
                settings = run.demo_settings(folder)
        self.assertEqual(db.database_url(settings), f"sqlite:///{folder}/data/darkroom.db")

    def test_check_config_reports_problems_and_a_missing_exiftool(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", side_effect=exiftool.Missing("exiftool is not installed")):
                code = run.main(["check-config"])
        self.assertEqual(code, 1)
        self.assertIn("DARKROOM_SECRET_KEY", out.getvalue())
        self.assertIn("exiftool is not installed", err.getvalue())

    def test_check_config_passes_when_complete(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example",
                      "OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom"}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"), \
                    mock.patch.object(run.oidc, "discovery", return_value={"scopes_supported": ["openid", "offline_access"]}):
                self.assertEqual(run.main(["check-config"]), 0)

    def test_check_config_reports_a_misused_no_auth_flag(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example",
                      "DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_HOST": "0.0.0.0"}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                self.assertEqual(run.main(["check-config"]), 1)
        self.assertIn("DARKROOM_ALLOW_NO_AUTH=1 only works when DARKROOM_HOST is a local address", err.getvalue())

    def test_check_config_warns_when_sessions_cannot_be_rechecked(self):
        from darkroom import oidc

        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example",
                      "OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom"}
            with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"), \
                    mock.patch.object(oidc, "discovery", return_value={"scopes_supported": ["openid"]}):
                self.assertEqual(run.main(["check-config"]), 1)
        self.assertIn("offline_access", err.getvalue())

    def test_revoke_sessions_ends_sessions_by_email_or_for_everyone(self):
        from darkroom.storage import db
        from darkroom.storage.models import AuthSession, User

        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            values = {"DARKROOM_DATA_DIR": folder, "DARKROOM_PHOTOS_ROOT": folder, "DATABASE_URL": f"sqlite:///{folder}/r.db"}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(run.main(["migrate"]), 0)
                engine = db.make_engine(values["DATABASE_URL"])
                with db.session(engine) as opened:
                    opened.add_all([User(sub="u1", email="jo@example.test"), User(sub="u2", email="sam@example.test")])
                    opened.flush()
                    opened.add_all([AuthSession(token_hash="a", sub="u1", created=1.0), AuthSession(token_hash="b", sub="u1", created=1.0),
                                    AuthSession(token_hash="c", sub="u2", created=1.0)])
                engine.dispose()
                self.assertEqual(run.main(["revoke-sessions"]), 1)
                self.assertEqual(run.main(["revoke-sessions", "--email", "JO@example.test"]), 0)
                self.assertEqual(run.main(["revoke-sessions", "--sub", "nobody"]), 0)
                self.assertEqual(run.main(["revoke-sessions", "--all"]), 0)
                engine = db.make_engine(values["DATABASE_URL"])
                with db.session(engine) as opened:
                    self.assertEqual(opened.query(AuthSession).count(), 0)
                engine.dispose()
        self.assertIn("needs --email, --sub or --all", err.getvalue())
        self.assertEqual(out.getvalue().splitlines()[-3:], ["ended 2 sessions", "ended 0 sessions", "ended 1 session"])

    def test_serve_starts_a_server_and_stops_on_interrupt(self):
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 1234)
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(run.handler, "make_server", return_value=server) as made, mock.patch.object(run, "migrate", return_value=0) as migrated, \
                mock.patch.object(run.jobs, "build") as built, mock.patch.object(run.db, "make_engine") as engine, \
                mock.patch.object(run.secret_key, "ensure", side_effect=lambda settings, engine: settings) as keyed, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.main(["serve"]), 0)
        keyed.assert_called_once()
        server.server_close.assert_called_once()
        migrated.assert_called_once()
        built.return_value.stop.assert_called_once()
        self.assertIs(made.call_args.kwargs["engine"], engine.return_value)

    def test_serve_refuses_to_start_when_no_auth_is_misused(self):
        for values in ({"DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_HOST": "0.0.0.0"},
                       {"DARKROOM_ALLOW_NO_AUTH": "1", "OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "x", "DARKROOM_PUBLIC_URL": "https://d.example"}):
            with self.subTest(values=values), contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.dict(os.environ, values, clear=True), \
                    mock.patch.object(run.handler, "make_server") as made, mock.patch.object(run, "migrate") as migrated:
                self.assertEqual(run.main(["serve"]), 1)
            made.assert_not_called()
            migrated.assert_not_called()
            self.assertIn("DARKROOM_ALLOW_NO_AUTH=1", err.getvalue())

    def test_migrate_creates_the_database(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()):
            values = {"DARKROOM_DATA_DIR": folder + "/data", "DARKROOM_PHOTOS_ROOT": folder}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(run.main(["migrate"]), 0)
                self.assertEqual(run.main(["migrate"]), 0)
            self.assertTrue(os.path.exists(folder + "/data/darkroom.db"))


class IndexCommandTests(unittest.TestCase):
    def prepared(self, folder):
        from darkroom.storage import db

        values = {"DARKROOM_DATA_DIR": folder, "DARKROOM_PHOTOS_ROOT": folder, "DATABASE_URL": f"sqlite:///{folder}/i.db"}
        with mock.patch.dict(os.environ, values, clear=True):
            self.assertEqual(run.main(["migrate"]), 0)
            engine = db.make_engine(values["DATABASE_URL"])
            with db.session(engine) as opened:
                from tests.shared import add_person

                add_person(opened, "jo")
            engine.dispose()
        return values

    def test_indexes_everyone_or_one_person(self):
        from tests.fixtures import media as fixtures

        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()) as err:
            values = self.prepared(folder)
            fixtures.make_jpeg(__import__("pathlib").Path(folder) / "jo" / "originals" / "a.jpg", taken="2019:07:04 12:30:15")
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(run.main(["index"]), 0)
                self.assertEqual(run.main(["index", "--person", "jo"]), 0)
                self.assertEqual(run.main(["index", "--person", "nobody"]), 1)
        self.assertIn('"new": 1', err.getvalue())
        self.assertIn("no person called nobody", err.getvalue())


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


if __name__ == "__main__":
    unittest.main()
