import contextlib
import dataclasses
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

import run
from darkroom import config
from darkroom.providers import exiftool
from darkroom.storage import db, secret_key, secretbox, stored_settings
from darkroom.storage.models import StoredSetting
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, own_database_url, settings_from, start_server

KEY = "the-test-secret-key"
CLIENT_SECRET = "client-secret-canary-0042"
RESEND_KEY = "re_resend-canary-0042"
STORED = {"DARKROOM_PUBLIC_URL": "https://stored.example.test/", "OIDC_ISSUER": "https://id.example.test/", "OIDC_CLIENT_ID": "stored-client",
          "OIDC_CLIENT_SECRET": CLIENT_SECRET, "OIDC_ALLOWED_GROUPS": "family, friends", "OIDC_ALLOWED_EMAILS": "jo@example.test",
          "IMMICH_URL": "http://immich.stored.test/", "RESEND_API_KEY": RESEND_KEY, "ALERT_EMAIL_FROM": "from@example.test", "ALERT_EMAIL_TO": "to@example.test"}


def store_all(engine, values, key=KEY):
    with db.session(engine) as opened:
        for variable, plain in values.items():
            stored_settings.store(opened, variable, plain, key, 1.0)
        opened.commit()


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)

    def effective(self, **env):
        return stored_settings.effective(settings_from(DARKROOM_SECRET_KEY=KEY, **env), self.engine)

    def test_every_entry_is_a_setting_listed_in_env_example(self):
        fields = {field.name for field in dataclasses.fields(config.Settings)}
        listed = (Path(__file__).parent.parent / ".env.example").read_text()
        for entry in stored_settings.REGISTRY:
            self.assertIn(entry.field, fields)
            self.assertIn(f"{entry.variable}=", listed)
        self.assertEqual(set(STORED), set(stored_settings.BY_VARIABLE))

    def test_with_nothing_stored_every_value_is_the_environment_or_the_default(self):
        found = self.effective(DARKROOM_PUBLIC_URL="https://env.example.test")
        self.assertEqual(found.settings, settings_from(DARKROOM_SECRET_KEY=KEY, DARKROOM_PUBLIC_URL="https://env.example.test"))
        self.assertEqual(found.sources["DARKROOM_PUBLIC_URL"], "environment")
        self.assertEqual({source for variable, source in found.sources.items() if variable != "DARKROOM_PUBLIC_URL"}, {"default"})
        self.assertEqual(found.settings.oidc_allowed_groups, ("parents",))
        self.assertEqual(found.problems, ())

    def test_a_stored_value_is_used_only_where_the_environment_sets_nothing(self):
        store_all(self.engine, STORED)
        found = self.effective(DARKROOM_PUBLIC_URL="https://env.example.test/", OIDC_CLIENT_SECRET="env-secret", OIDC_ALLOWED_GROUPS="admins", IMMICH_URL="  ")
        settings = found.settings
        self.assertEqual((settings.public_url, settings.oidc_client_secret, settings.oidc_allowed_groups), ("https://env.example.test", "env-secret", ("admins",)))
        self.assertEqual((settings.oidc_issuer, settings.oidc_client_id, settings.oidc_allowed_emails), ("https://id.example.test", "stored-client", ("jo@example.test",)))
        self.assertEqual((settings.immich_url, settings.alert_email_from, settings.alert_email_to), ("http://immich.stored.test", "from@example.test", "to@example.test"))
        self.assertEqual({variable for variable, source in found.sources.items() if source == "environment"}, {"DARKROOM_PUBLIC_URL", "OIDC_CLIENT_SECRET", "OIDC_ALLOWED_GROUPS"})
        self.assertEqual(found.sources["IMMICH_URL"], "stored")
        self.assertEqual(found.problems, ())

    def test_a_stored_value_that_is_blank_falls_back_to_the_default(self):
        store_all(self.engine, {"OIDC_ALLOWED_GROUPS": " , ", "ALERT_EMAIL_TO": "  "})
        found = self.effective()
        self.assertEqual((found.settings.oidc_allowed_groups, found.settings.alert_email_to), (("parents",), ""))
        self.assertEqual((found.sources["OIDC_ALLOWED_GROUPS"], found.sources["ALERT_EMAIL_TO"]), ("default", "default"))

    def test_secrets_are_stored_sealed_and_read_back_with_the_key(self):
        store_all(self.engine, STORED)
        with db.session(self.engine) as opened:
            rows = {row.name: row for row in opened.query(StoredSetting)}
        for variable in ("OIDC_CLIENT_SECRET", "RESEND_API_KEY"):
            self.assertIsNone(rows[variable].value)
            self.assertTrue(secretbox.is_sealed(rows[variable].value_sealed))
            self.assertEqual(secretbox.unseal(rows[variable].value_sealed, KEY), STORED[variable])
        self.assertEqual((rows["IMMICH_URL"].value, rows["IMMICH_URL"].value_sealed), (STORED["IMMICH_URL"], None))
        settings = self.effective().settings
        self.assertEqual(settings.oidc_client_secret, CLIENT_SECRET)
        self.assertTrue(secretbox.is_sealed(settings.resend_api_key))
        self.assertEqual(secretbox.unseal(settings.resend_api_key, KEY), RESEND_KEY)
        self.assertEqual(config.resend_problems(settings), [])

    def test_storing_again_replaces_the_value(self):
        store_all(self.engine, {"IMMICH_URL": "http://first.test"})
        store_all(self.engine, {"IMMICH_URL": "http://second.test"})
        self.assertEqual(self.effective().settings.immich_url, "http://second.test")

    def test_a_stored_secret_this_key_cannot_read_is_reported_and_not_used(self):
        store_all(self.engine, {"OIDC_CLIENT_SECRET": CLIENT_SECRET}, key="the-lost-key")
        found = self.effective()
        self.assertEqual(found.settings.oidc_client_secret, "")
        self.assertEqual(found.sources["OIDC_CLIENT_SECRET"], "stored")
        self.assertEqual(found.problems, (stored_settings.UNREADABLE.format(variable="OIDC_CLIENT_SECRET"),))
        self.assertEqual(self.effective(OIDC_CLIENT_SECRET="env-secret").problems, ())

    def test_without_the_table_every_value_is_the_environment_or_the_default(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        found = self.effective()
        self.assertEqual(found.settings, settings_from(DARKROOM_SECRET_KEY=KEY))
        self.assertEqual(set(found.sources.values()), {"default"})

    def test_only_the_registered_settings_can_be_stored(self):
        with db.session(self.engine) as opened, self.assertRaises(stored_settings.NotStorable):
            stored_settings.store(opened, "DATABASE_URL", "sqlite:///elsewhere.db", KEY, 1.0)

    def test_a_stored_secret_stops_a_new_key_being_made(self):
        store_all(self.engine, {"OIDC_CLIENT_SECRET": CLIENT_SECRET}, key="the-lost-key")
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(secret_key.Refused):
            secret_key.ensure(settings_from(DARKROOM_DATA_DIR=folder), self.engine)
        store_all(own_database(self), {"IMMICH_URL": "http://immich.local"})
        self.assertTrue(secret_key.sealed_in_database(self.engine))


class ServeAndCheckConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.env = {"DARKROOM_DATA_DIR": str(self.data), "DARKROOM_PHOTOS_ROOT": str(self.data), "DARKROOM_PUBLIC_URL": "http://127.0.0.1:8000",
                    "DARKROOM_ALLOW_NO_AUTH": "1"}

    def engine(self):
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        engine = db.make_engine(db.database_url(settings_from(**self.env)))
        self.addCleanup(engine.dispose)
        return engine

    def serve(self, **extra):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **extra}, clear=True), mock.patch.object(run.handler, "make_server") as made, \
                mock.patch.object(run.jobs, "build"), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            made.return_value.serve_forever.side_effect = KeyboardInterrupt
            made.return_value.server_address = ("127.0.0.1", 1234)
            code = run.main(["serve"])
        return code, made.call_args.args[0] if made.called else None, err.getvalue()

    def check_config(self, **extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **extra}, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(["check-config"])
        return code, out.getvalue(), err.getvalue()

    def key(self):
        return settings_from(**self.env).secret_key

    def test_the_environment_alone_still_starts_the_service_with_the_table_present(self):
        engine = self.engine()
        self.assertIn("stored_settings", inspect(engine).get_table_names())
        code, served, err = self.serve()
        self.assertEqual(code, 0, err)
        self.assertEqual(served, settings_from(**self.env))

    def test_stored_values_reach_the_service_and_the_environment_still_wins(self):
        engine = self.engine()
        self.assertEqual(self.serve()[0], 0)
        store_all(engine, {"DARKROOM_PUBLIC_URL": "https://stored.example.test", "IMMICH_URL": "http://immich.stored.test"}, key=self.key())
        code, served, err = self.serve()
        self.assertEqual(code, 0, err)
        self.assertEqual((served.public_url, served.immich_url), ("http://127.0.0.1:8000", "http://immich.stored.test"))

    def test_serve_refuses_stored_values_it_cannot_use(self):
        engine = self.engine()
        self.assertEqual(self.serve()[0], 0)
        store_all(engine, {"OIDC_ISSUER": "https://id.example.test", "OIDC_CLIENT_ID": "stored-client"}, key=self.key())
        code, served, err = self.serve()
        self.assertEqual((code, served), (1, None))
        self.assertIn("cannot be combined with OIDC sign-in", err)
        store_all(engine, {"OIDC_CLIENT_SECRET": CLIENT_SECRET}, key="the-lost-key")
        code, served, err = self.serve(DARKROOM_ALLOW_NO_AUTH="", OIDC_ALLOWED_GROUPS="parents")
        self.assertEqual((code, served), (1, None))
        self.assertIn("OIDC_CLIENT_SECRET is stored sealed but cannot be read", err)
        self.assertNotIn(CLIENT_SECRET, err)

    def test_check_config_names_each_values_source(self):
        engine = self.engine()
        self.assertEqual(self.serve()[0], 0)
        store_all(engine, {"IMMICH_URL": "http://immich.stored.test", "DARKROOM_PUBLIC_URL": "https://stored.example.test"}, key=self.key())
        code, out, err = self.check_config()
        self.assertEqual(code, 0, err)
        self.assertIn(run.SOURCES_HEADING, out)
        for line in ("  DARKROOM_PUBLIC_URL: environment", "  IMMICH_URL: stored", "  OIDC_CLIENT_SECRET: default"):
            self.assertIn(line, out.splitlines())
        self.assertEqual(sum(line.startswith("  ") for line in out.splitlines()), len(stored_settings.REGISTRY))

    def test_check_config_without_a_database_creates_none(self):
        code, out, _ = self.check_config()
        self.assertEqual(code, 0)
        self.assertIn("  IMMICH_URL: default", out.splitlines())
        self.assertFalse((self.data / "darkroom.db").exists())

    def test_check_config_says_when_the_stored_settings_cannot_be_read(self):
        self.engine()
        with mock.patch.object(stored_settings, "stored_values", side_effect=OperationalError("SELECT", {}, Exception("down"))):
            code, _, err = self.check_config()
        self.assertEqual(code, 1)
        self.assertIn(stored_settings.NOT_READ, err)
        code, _, err = self.check_config(DATABASE_URL="mysql://u:p@db.invalid/darkroom")
        self.assertEqual(code, 1)
        self.assertIn(stored_settings.NOT_READ, err)

    def test_stored_secrets_are_in_no_log_reply_check_config_output_or_plain_column(self):
        engine = self.engine()
        self.assertEqual(self.serve()[0], 0)
        store_all(engine, {"OIDC_CLIENT_SECRET": CLIENT_SECRET, "RESEND_API_KEY": RESEND_KEY, "ALERT_EMAIL_FROM": "from@example.test",
                           "ALERT_EMAIL_TO": "to@example.test"}, key=self.key())
        replies = []
        with no_leaks(self, CLIENT_SECRET, RESEND_KEY, database=engine, replies=replies):
            code, served, err = self.serve()
            self.assertEqual(code, 0, err)
            self.assertEqual(served.oidc_client_secret, CLIENT_SECRET)
            print(err, file=sys.stderr)
            base, _ = start_server(self, served, engine)
            for path in ("/healthz", "/api/status", "/api/people", "/api/me", "/api/events"):
                status, _, reply = fetch(base, "GET", path)
                self.assertEqual(status, 200, path)
                replies.append(reply)
            for extra in ({}, {"OIDC_CLIENT_SECRET": "env-secret-canary"}):
                code, out, err = self.check_config(**extra)
                print(out)
                print(err, file=sys.stderr)
            self.assertIn("  RESEND_API_KEY: stored", out.splitlines())


if __name__ == "__main__":
    unittest.main()
