import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.domain import exports
from darkroom.storage import db
from darkroom.storage.models import Export, ExportPart, GoogleAccount, MediaItem, Person, Setting
from tests.shared import add_person, impatient_engine, on_postgres, own_database, own_sqlite_database


class EngineTests(unittest.TestCase):
    def test_wal_and_foreign_keys_are_on(self):
        engine = own_sqlite_database(self)
        with db.connect(engine) as connection:
            self.assertEqual(connection.execute(text("PRAGMA journal_mode")).scalar(), "wal")
            self.assertEqual(connection.execute(text("PRAGMA foreign_keys")).scalar(), 1)

    def test_the_default_url_is_in_the_data_folder_and_database_url_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            base = config.load()
            local = base.__class__(**{**base.__dict__, "data_dir": Path(folder), "database_url": ""})
            self.assertEqual(db.database_url(local), f"sqlite:///{Path(folder) / 'darkroom.db'}")
            other = base.__class__(**{**base.__dict__, "database_url": "postgresql://x/y"})
            self.assertEqual(db.database_url(other), "postgresql://x/y")

    def test_session_commits_and_rolls_back(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            opened.add(Setting(key="a", value="1"))
        with self.assertRaises(RuntimeError), db.session(engine) as opened:
            opened.add(Setting(key="b", value="2"))
            raise RuntimeError
        with db.session(engine) as opened:
            self.assertEqual(sorted(s.key for s in opened.query(Setting)), ["a"])

    def test_upsert_updates_or_leaves_alone(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            db.upsert(opened, Setting, {"key": "k", "value": "1"}, ("key",))
            db.upsert(opened, Setting, {"key": "k", "value": "2"}, ("key",))
            db.upsert(opened, Setting, {"key": "k"}, ("key",))
        with db.session(engine) as opened:
            self.assertEqual(opened.get(Setting, "k").value, "2")

    def test_create_fresh_makes_every_table(self):
        engine = db.create_fresh()
        with db.session(engine) as opened:
            add_person(opened, "jo")
            self.assertEqual(opened.query(Person).count(), 1)


class UrlTests(unittest.TestCase):
    def test_postgresql_urls_get_the_psycopg_driver(self):
        for url in ("postgresql://u:p@h:5432/d", "postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"):
            self.assertEqual(db.normalise_url(url), "postgresql+psycopg://u:p@h:5432/d")

    def test_other_urls_are_left_alone(self):
        for url in ("sqlite://", "sqlite:////data/darkroom.db", "mysql://u:p@h/d"):
            self.assertEqual(db.normalise_url(url), url)

    def test_a_postgresql_engine_checks_connections_before_using_them_and_is_made_without_connecting(self):
        engine = db.make_engine("postgres://u:p@127.0.0.1:1/d")
        self.addCleanup(engine.dispose)
        self.assertEqual((engine.dialect.name, engine.dialect.driver, engine.pool._pre_ping), ("postgresql", "psycopg", True))

    def test_a_sqlite_engine_keeps_its_timeout_and_pragmas(self):
        engine = db.make_engine("sqlite://")
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.dialect.name, "sqlite")
        self.assertFalse(engine.pool._pre_ping)

    def test_another_database_is_refused_by_name_and_without_its_password(self):
        with self.assertRaises(db.UnsupportedDatabase) as refused:
            db.make_engine("mysql://u:hunter2@h/d")
        self.assertIn("mysql", str(refused.exception))
        self.assertIn("SQLite", str(refused.exception))
        self.assertNotIn("hunter2", str(refused.exception))

    def test_an_upsert_on_another_database_is_refused(self):
        opened = mock.Mock(spec=Session)
        opened.get_bind.return_value.dialect.name = "oracle"
        with self.assertRaises(db.UnsupportedDatabase):
            db.upsert(opened, Setting, {"key": "k", "value": "1"}, ("key",))
        opened.execute.assert_not_called()


class BusyTests(unittest.TestCase):
    def test_a_locked_sqlite_database_is_busy(self):
        self.assertTrue(db.is_busy(OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))))

    def test_other_errors_are_not_busy(self):
        self.assertFalse(db.is_busy(OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error"))))

    def test_a_postgresql_lock_timeout_is_busy_and_other_postgresql_errors_are_not(self):
        class LockNotAvailable(Exception):
            sqlstate = "55P03"

        class Unreachable(Exception):
            sqlstate = "08006"

        self.assertTrue(db.is_busy(OperationalError("UPDATE", {}, LockNotAvailable("canceling statement due to lock timeout"))))
        self.assertFalse(db.is_busy(OperationalError("UPDATE", {}, Unreachable("connection lost"))))


class DialectTests(unittest.TestCase):
    def test_byte_counts_above_two_gibibytes_are_stored(self):
        engine = own_database(self)
        big = 5 * 1024**3
        with db.session(engine) as opened:
            person = add_person(opened, "jo")
            opened.add(MediaItem(person_id=person, source="phone", path="jo/originals/a.jpg", sha1="ab", bytes=big, indexed_at=1.0))
            account = GoogleAccount(email="a@x.test", default_person_id=person, created=1.0)
            opened.add(account)
            opened.flush()
            export = Export(account_id=account.id, kind="manual")
            opened.add(export)
            opened.flush()
            opened.add(ExportPart(export_id=export.id, number=1, bytes=big, bytes_done=big + 1))
        with db.session(engine) as opened:
            self.assertEqual(opened.scalars(select(MediaItem.bytes)).one(), big)
            self.assertEqual(opened.execute(select(ExportPart.bytes, ExportPart.bytes_done)).one(), (big, big + 1))

    def test_exports_without_a_creation_time_are_listed_last_whatever_the_database(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="a@x.test", default_person_id=person, created=1.0)
            opened.add(account)
            opened.flush()
            for created in (None, 5.0, 7.0, None):
                opened.add(Export(account_id=account.id, kind="manual", created_at=created))
        with db.session(engine) as opened:
            self.assertEqual([e.created_at for e in exports.listing(opened)], [7.0, 5.0, None, None])


if on_postgres():

    class PostgresEngineTests(unittest.TestCase):
        def test_the_engine_waits_a_bounded_time_for_locks(self):
            engine = own_database(self)
            with db.connect(engine) as connection:
                self.assertEqual(connection.execute(text("SHOW lock_timeout")).scalar(), "30s")

        def test_every_byte_count_column_is_a_bigint(self):
            engine = own_database(self)
            inspector = inspect(engine)
            for table, column in (("media_items", "bytes"), ("export_parts", "bytes"), ("export_parts", "bytes_done"), ("legacy_files", "bytes"), ("pixel_copies", "bytes")):
                with self.subTest(table=table, column=column):
                    found = next(each for each in inspector.get_columns(table) if each["name"] == column)
                    self.assertEqual(str(found["type"]), "BIGINT")

        def test_the_write_lock_makes_a_second_taker_wait_and_then_fail_as_busy(self):
            engine = own_database(self)
            with db.session(engine) as opened:
                db.write_lock(opened)
                with db.session(impatient_engine(self, engine, 0.1)) as elsewhere, self.assertRaises(OperationalError) as busy:
                    db.write_lock(elsewhere)
                self.assertTrue(db.is_busy(busy.exception))

        def test_the_write_lock_is_released_with_its_transaction(self):
            engine = own_database(self)
            with db.session(engine) as opened:
                db.write_lock(opened)
            with db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                db.write_lock(elsewhere)

        def test_upsert_updates_on_conflict_and_leaves_alone_without_changes(self):
            engine = own_database(self)
            with db.session(engine) as opened:
                db.upsert(opened, Setting, {"key": "k", "value": "1"}, ("key",))
                db.upsert(opened, Setting, {"key": "k", "value": "2"}, ("key",))
                db.upsert(opened, Setting, {"key": "k"}, ("key",))
            with db.session(engine) as opened:
                self.assertEqual(opened.get(Setting, "k").value, "2")


if __name__ == "__main__":
    unittest.main()
