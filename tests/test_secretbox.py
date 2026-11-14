import contextlib
import errno
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy.exc import OperationalError

import run
from darkroom import config
from darkroom.providers import exiftool
from darkroom.storage import db, secret_key, secretbox
from darkroom.storage.models import AuthSession, Base, Person, User
from tests.privacy import no_leaks
from tests.shared import add_person, fetch, own_database, settings_from, start_server


class SecretboxTests(unittest.TestCase):
    def test_round_trip(self):
        sealed = secretbox.seal("immich-key-123", "secret")
        self.assertTrue(sealed.startswith("enc:v1:"))
        self.assertNotIn("immich-key-123", sealed)
        self.assertEqual(secretbox.unseal(sealed, "secret"), "immich-key-123")

    def test_each_seal_differs(self):
        self.assertNotEqual(secretbox.seal("x", "secret"), secretbox.seal("x", "secret"))

    def test_the_wrong_key_or_a_plain_value_is_unreadable(self):
        sealed = secretbox.seal("x", "secret")
        with self.assertRaises(secretbox.Unreadable):
            secretbox.unseal(sealed, "other")
        with self.assertRaises(secretbox.Unreadable):
            secretbox.unseal("plain", "secret")
        with self.assertRaises(secretbox.Unreadable):
            secretbox.seal("x", "")

    def test_is_sealed(self):
        self.assertTrue(secretbox.is_sealed(secretbox.seal("x", "k")))
        self.assertFalse(secretbox.is_sealed("x"))


class GeneratedKeyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.key_file = self.data / config.SECRET_KEY_FILE
        self.engine = own_database(self)

    def settings(self, **values):
        return settings_from(DARKROOM_DATA_DIR=str(self.data), DARKROOM_PHOTOS_ROOT=str(self.data), **values)

    def ensure(self, settings, engine):
        with contextlib.redirect_stderr(io.StringIO()):
            return secret_key.ensure(settings, engine)

    def store_sealed_immich_key(self):
        with db.session(self.engine) as opened:
            person_id = add_person(opened, "jo")
            opened.get(Person, person_id).immich_api_key_sealed = secretbox.seal("immich-key-canary", "the-lost-key")
            opened.commit()

    def store_sealed_refresh_token(self):
        with db.session(self.engine) as opened:
            opened.add(User(sub="sub-1"))
            opened.flush()
            opened.add(AuthSession(token_hash="h", sub="sub-1", created=1.0, refresh_token_sealed=secretbox.seal("refresh", "the-lost-key")))
            opened.commit()

    def test_the_first_start_writes_a_key_only_its_owner_can_read(self):
        previous = os.umask(0)
        try:
            settings = self.ensure(self.settings(), self.engine)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        self.assertEqual(settings.key_origin, "file")
        self.assertGreaterEqual(len(settings.secret_key), 40)
        self.assertEqual(self.key_file.read_text(), settings.secret_key + "\n")
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), [config.SECRET_KEY_FILE])
        again = self.settings()
        self.assertEqual((again.secret_key, again.key_origin), (settings.secret_key, "file"))
        self.assertEqual(secretbox.unseal(secretbox.seal("x", settings.secret_key), again.secret_key), "x")

    def test_an_existing_key_is_never_regenerated(self):
        first = self.ensure(self.settings(), self.engine).secret_key
        written = self.key_file.read_bytes(), self.key_file.stat().st_mtime_ns
        self.store_sealed_immich_key()
        for _ in range(3):
            self.assertEqual(self.ensure(self.settings(), self.engine).secret_key, first)
        self.assertEqual((self.key_file.read_bytes(), self.key_file.stat().st_mtime_ns), written)

    def test_a_key_file_that_holds_no_key_is_refused_and_left_alone(self):
        for content in (b"", b"\n", b"\xff\xfe"):
            with self.subTest(content=content):
                self.key_file.write_bytes(content)
                with self.assertRaises(secret_key.Refused) as refused:
                    self.ensure(self.settings(), self.engine)
                self.assertEqual(str(refused.exception), secret_key.UNREADABLE_FILE_NOTHING_SEALED)
                self.assertEqual(self.key_file.read_bytes(), content)

    def test_an_unreadable_key_file_is_only_called_deletable_when_nothing_sealed_is_stored(self):
        self.key_file.write_bytes(b"")
        self.store_sealed_immich_key()
        with self.assertRaises(secret_key.Refused) as refused:
            self.ensure(self.settings(), self.engine)
        self.assertEqual(str(refused.exception), secret_key.UNREADABLE_FILE)
        self.assertNotIn("delete", str(refused.exception))
        self.assertEqual(self.key_file.read_bytes(), b"")
        with self.assertRaises(secret_key.Refused) as refused:
            self.ensure(self.settings(RESEND_API_KEY=secretbox.seal("re_x", "the-lost-key")), own_database(self))
        self.assertEqual(str(refused.exception), secret_key.UNREADABLE_FILE)
        self.assertEqual(self.key_file.read_bytes(), b"")

    def test_an_empty_key_file_left_by_an_interrupted_first_start_can_be_deleted_and_a_key_made(self):
        def killed_after_creating_the_file(path, key):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            raise KeyboardInterrupt

        refused = OSError(errno.EPERM, "hard links are not allowed here")
        with mock.patch.object(secret_key.os, "link", side_effect=refused), \
                mock.patch.object(secret_key, "write_in_place", side_effect=killed_after_creating_the_file), self.assertRaises(KeyboardInterrupt):
            self.ensure(self.settings(), self.engine)
        self.assertEqual(self.key_file.read_bytes(), b"")
        with self.assertRaises(secret_key.Refused) as refused:
            self.ensure(self.settings(), self.engine)
        self.assertIn("delete the file and start again", str(refused.exception))
        self.assertEqual(self.key_file.read_bytes(), b"")
        self.key_file.unlink()
        settings = self.ensure(self.settings(), self.engine)
        self.assertEqual(self.key_file.read_text(), settings.secret_key + "\n")

    def test_a_key_another_start_wrote_first_is_used_instead_of_a_new_one(self):
        def written_by_another_start(path):
            path.write_text("the-other-start-key\n")
            raise FileExistsError(path)

        with mock.patch.object(secret_key, "write_new_key", side_effect=written_by_another_start):
            settings = self.ensure(self.settings(), self.engine)
        self.assertEqual(settings.secret_key, "the-other-start-key")

    def test_a_key_that_appears_between_loading_the_settings_and_ensuring_is_used(self):
        loaded_before_the_key_existed = self.settings()
        self.assertEqual(loaded_before_the_key_existed.secret_key, "")
        self.key_file.write_text("the-other-start-key\n")
        settings = self.ensure(loaded_before_the_key_existed, self.engine)
        self.assertEqual((settings.secret_key, settings.key_origin), ("the-other-start-key", "file"))
        self.assertEqual(self.key_file.read_text(), "the-other-start-key\n")

    def test_a_data_folder_that_refuses_hard_links_still_gets_its_key(self):
        refused = OSError(errno.EPERM, "hard links are not allowed here")
        with mock.patch.object(secret_key.os, "link", side_effect=refused):
            settings = self.ensure(self.settings(), self.engine)
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        self.assertEqual(self.key_file.read_text(), settings.secret_key + "\n")
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), [config.SECRET_KEY_FILE])

    def test_a_key_is_never_written_over_an_existing_file_or_link(self):
        refused = OSError(errno.EPERM, "hard links are not allowed here")
        for name, link in (("with hard links", None), ("without hard links", refused)):
            for kind in ("file", "dangling link"):
                with self.subTest(name=name, kind=kind):
                    target = self.data / "nowhere"
                    if kind == "file":
                        self.key_file.write_bytes(b"keep")
                    else:
                        self.key_file.symlink_to(target)
                    patch = mock.patch.object(secret_key.os, "link", side_effect=link) if link else contextlib.nullcontext()
                    with patch, self.assertRaises(FileExistsError):
                        secret_key.write_new_key(self.key_file)
                    self.assertFalse(target.exists())
                    if kind == "file":
                        self.assertEqual(self.key_file.read_bytes(), b"keep")
                    self.key_file.unlink()
                    self.assertEqual([path.name for path in self.data.iterdir() if path.name.startswith(".")], [])

    def test_every_sealed_column_is_checked_before_a_key_is_made(self):
        stored = {(table.name, column.name) for table in Base.metadata.tables.values() for column in table.columns if column.name.endswith("_sealed")}
        checked = {(model.__tablename__, column.key) for model, column in secret_key.SEALED_COLUMNS}
        self.assertEqual(stored, checked)

    def test_no_key_is_made_while_sealed_values_are_stored(self):
        for store in (self.store_sealed_immich_key, self.store_sealed_refresh_token):
            with self.subTest(store=store.__name__):
                self.engine = own_database(self)
                store()
                with self.assertRaises(secret_key.Refused) as refused:
                    self.ensure(self.settings(), self.engine)
                self.assertIn("will not make a new key", str(refused.exception))
                self.assertFalse(self.key_file.exists())
        with self.assertRaises(secret_key.Refused):
            self.ensure(self.settings(RESEND_API_KEY=secretbox.seal("re_x", "the-lost-key")), own_database(self))
        self.assertFalse(self.key_file.exists())

    def test_empty_sealed_columns_do_not_count(self):
        with db.session(self.engine) as opened:
            opened.get(Person, add_person(opened, "jo")).immich_api_key_sealed = ""
            opened.commit()
        self.assertFalse(secret_key.sealed_values_exist(self.settings(), self.engine))

    def test_the_environment_variable_still_overrides_the_file(self):
        settings = self.settings(DARKROOM_SECRET_KEY="the-environment-key")
        self.assertEqual((settings.secret_key, settings.key_origin), ("the-environment-key", "environment"))
        self.assertIs(self.ensure(settings, self.engine), settings)
        self.assertFalse(self.key_file.exists())
        self.key_file.write_text("the-file-key\n")
        self.store_sealed_immich_key()
        settings = self.settings(DARKROOM_SECRET_KEY="the-environment-key")
        self.assertEqual((settings.secret_key, settings.key_origin), ("the-environment-key", "environment"))
        self.assertEqual(self.ensure(settings, self.engine).secret_key, "the-environment-key")
        self.assertEqual(self.key_file.read_text(), "the-file-key\n")


class ServeAndCheckConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.env = {"DARKROOM_DATA_DIR": str(self.data), "DARKROOM_PHOTOS_ROOT": str(self.data), "DARKROOM_PUBLIC_URL": "http://127.0.0.1:8000",
                    "DARKROOM_ALLOW_NO_AUTH": "1"}

    def check_config(self, **extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **extra}, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(["check-config"])
        return code, out.getvalue(), err.getvalue()

    def serve(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(run.handler, "make_server") as made, \
                mock.patch.object(run.jobs, "build"), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            made.return_value.serve_forever.side_effect = KeyboardInterrupt
            made.return_value.server_address = ("127.0.0.1", 1234)
            code = run.main(["serve"])
        return code, made, err.getvalue()

    def test_check_config_names_where_the_key_comes_from(self):
        code, out, _ = self.check_config()
        self.assertEqual(code, 0)
        self.assertIn("Secret key: none yet; `python run.py serve` generates one", out)
        self.assertEqual(self.serve()[0], 0)
        self.assertIn("Secret key: from the secret-key file in DARKROOM_DATA_DIR.", self.check_config()[1])
        self.assertIn("Secret key: from DARKROOM_SECRET_KEY.", self.check_config(DARKROOM_SECRET_KEY="the-environment-key")[1])

    def test_serve_refuses_to_start_on_sealed_values_without_their_key(self):
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        engine = db.make_engine(db.database_url(settings_from(**self.env)))
        self.addCleanup(engine.dispose)
        with db.session(engine) as opened:
            opened.get(Person, add_person(opened, "jo")).immich_api_key_sealed = secretbox.seal("immich-key", "the-lost-key")
            opened.commit()
        code, made, err = self.serve()
        self.assertEqual(code, 1)
        made.assert_not_called()
        self.assertIn("will not make a new key", err)
        self.assertFalse((self.data / config.SECRET_KEY_FILE).exists())
        code, out, err = self.check_config()
        self.assertEqual(code, 1)
        self.assertIn("will not make a new key", err)
        self.assertIn("will not generate one until the problem above is fixed", out)

    def test_check_config_reports_a_sealed_resend_key_and_an_unreadable_file_without_a_database(self):
        sealed = secretbox.seal("re_x", "the-lost-key")
        code, _, err = self.check_config(RESEND_API_KEY=sealed, ALERT_EMAIL_FROM="a@example.test", ALERT_EMAIL_TO="b@example.test")
        self.assertEqual(code, 1)
        self.assertIn("will not make a new key", err)
        (self.data / config.SECRET_KEY_FILE).write_text("")
        code, out, err = self.check_config()
        self.assertEqual(code, 1)
        self.assertIn(f"config: {secret_key.UNREADABLE_FILE_NOTHING_SEALED}", err)
        self.assertIn("will not generate one until the problem above is fixed", out)
        self.assertFalse((self.data / "darkroom.db").exists())
        code, _, err = self.check_config(RESEND_API_KEY=sealed, ALERT_EMAIL_FROM="a@example.test", ALERT_EMAIL_TO="b@example.test")
        self.assertEqual(code, 1)
        self.assertIn(f"config: {secret_key.UNREADABLE_FILE}", err)
        self.assertNotIn("delete the file", err)

    def test_check_config_calls_an_unreadable_key_file_deletable_only_when_the_database_holds_nothing_sealed(self):
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        (self.data / config.SECRET_KEY_FILE).write_text("")
        code, _, err = self.check_config()
        self.assertEqual(code, 1)
        self.assertIn(f"config: {secret_key.UNREADABLE_FILE_NOTHING_SEALED}", err)
        engine = db.make_engine(db.database_url(settings_from(**self.env)))
        self.addCleanup(engine.dispose)
        with db.session(engine) as opened:
            opened.get(Person, add_person(opened, "jo")).immich_api_key_sealed = secretbox.seal("immich-key", "the-lost-key")
            opened.commit()
        code, _, err = self.check_config()
        self.assertEqual(code, 1)
        self.assertIn(f"config: {secret_key.UNREADABLE_FILE}", err)
        self.assertNotIn("delete the file", err)
        code, _, err = self.serve()
        self.assertEqual(code, 1)
        self.assertIn(f"config: {secret_key.UNREADABLE_FILE}", err)
        with mock.patch.object(secret_key, "sealed_in_database", side_effect=OperationalError("SELECT", {}, Exception("down"))):
            code, _, err = self.check_config()
        self.assertIn(f"config: {secret_key.UNREADABLE_FILE}", err)
        self.assertEqual((self.data / config.SECRET_KEY_FILE).read_text(), "")

    def test_check_config_says_when_the_database_cannot_be_reached(self):
        with mock.patch.object(secret_key, "sealed_in_database", side_effect=OperationalError("SELECT", {}, Exception("down"))):
            code, _, err = self.check_config(DATABASE_URL="postgresql://u:p@db.invalid/darkroom")
        self.assertEqual(code, 1)
        self.assertIn("could not be reached to check for sealed values", err)
        for unusable in ("mysql://u:p@db.invalid/darkroom", "postgresql://u:p@db.invalid:port/darkroom"):
            code, _, err = self.check_config(DATABASE_URL=unusable)
            self.assertEqual(code, 1)
            self.assertIn("could not be reached to check for sealed values", err)

    def test_the_generated_key_is_in_no_log_reply_or_check_config_output(self):
        engine = own_database(self)
        replies = []
        with no_leaks(self, "placeholder-canary", database=engine, replies=replies) as watched:
            code, _, err = self.serve()
            self.assertEqual(code, 0, err)
            settings = settings_from(**self.env)
            self.assertEqual(settings.key_origin, "file")
            watched.add(settings.secret_key)
            print(err, file=sys.stderr)
            base, _ = start_server(self, settings, engine)
            for path in ("/healthz", "/api/status", "/api/people", "/api/me", "/api/events"):
                status, _, reply = fetch(base, "GET", path)
                self.assertEqual(status, 200, path)
                replies.append(reply)
            for extra in ({}, {"DARKROOM_SECRET_KEY": settings.secret_key}):
                code, out, err = self.check_config(**extra)
                print(out)
                print(err, file=sys.stderr)
            with mock.patch.dict(os.environ, self.env, clear=True), mock.patch("sys.stdin", io.StringIO("re_value\n")):
                self.assertEqual(run.main(["seal-secret"]), 0)


if __name__ == "__main__":
    unittest.main()
