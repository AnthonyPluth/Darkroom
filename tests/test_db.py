import logging
import sqlite3
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from sqlalchemy import URL, event, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import exports
from darkroom.storage import db
from darkroom.storage.models import Export, ExportPart, GoogleAccount, MediaItem, Person, Setting
from tests.privacy import no_leaks
from tests.shared import add_person, another_writer_holds_the_lock, impatient_engine, on_postgres, own_database, own_sqlite_database


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


FAKE_PASSWORD = "not/a=real@pw%:canary-5e2b"
PASSWORD_CANARY = "canary-5e2b"


class PasswordInErrorsTests(unittest.TestCase):
    def unreachable_url(self):
        return URL.create("postgresql", username="darkroom", password=FAKE_PASSWORD, host="127.0.0.1", port=1, database="darkroom").render_as_string(hide_password=False)

    def test_a_password_with_url_characters_is_in_no_error_traceback_or_log_when_migrating_fails(self):
        with no_leaks(self, FAKE_PASSWORD, PASSWORD_CANARY, quote(FAKE_PASSWORD, safe="")):
            engine = db.make_engine(self.unreachable_url())
            self.addCleanup(engine.dispose)
            with self.assertRaises(OperationalError) as failed:
                db.migrate(engine)
            monitoring.report(failed.exception, values=True)
            monitoring.report(failed.exception)
            logging.getLogger("darkroom").error("migrating failed", exc_info=failed.exception)
            shown = "".join(traceback.format_exception(failed.exception))
        self.assertNotIn(PASSWORD_CANARY, shown)

    def test_the_migration_config_holds_no_password(self):
        engine = db.make_engine(self.unreachable_url())
        self.addCleanup(engine.dispose)
        config = db.alembic_config(engine)
        self.assertIsNone(config.get_main_option("sqlalchemy.url"))
        self.assertIs(config.attributes["engine"], engine)

    def test_an_unreadable_url_is_refused_without_its_password(self):
        with no_leaks(self, PASSWORD_CANARY), self.assertRaises(db.UnsupportedDatabase) as refused:
            db.make_engine(f"postgresql:/darkroom:{FAKE_PASSWORD}@127.0.0.1/darkroom")
        self.assertIn("DATABASE_URL", str(refused.exception))
        self.assertNotIn(PASSWORD_CANARY, "".join(traceback.format_exception(refused.exception)))


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


class WriteSerialisationTests(unittest.TestCase):
    def test_statements_that_write_or_lock_rows_are_told_from_reads(self):
        for statement in ("INSERT INTO t VALUES (1)", "UPDATE t SET a = 1", "delete from t", "CREATE TABLE t (a int)", "ALTER TABLE t ADD b int", "SELECT a FROM t FOR UPDATE",
                          "SELECT a FROM t FOR NO KEY UPDATE", "WITH x AS (DELETE FROM t RETURNING a) SELECT a FROM x", "TRUNCATE t"):
            with self.subTest(statement=statement):
                self.assertTrue(db.writes(statement))
        for statement in ("SELECT a FROM t", " select count(*) from t where a = 1", "WITH x AS (SELECT 1) SELECT * FROM x", "SHOW lock_timeout", "SET lock_timeout = 1", "BEGIN",
                          "SELECT pg_advisory_xact_lock(1)", "SELECT updated FROM t"):
            with self.subTest(statement=statement):
                self.assertFalse(db.writes(statement))

    def test_the_lock_is_taken_once_at_a_transactions_first_write_and_again_in_the_next_transaction(self):
        connection = mock.Mock(info={})

        def run(statement):
            db.take_write_lock_before_writing(connection, None, statement, None, None, False)

        run("SELECT 1")
        connection.exec_driver_sql.assert_not_called()
        run("INSERT INTO t VALUES (1)")
        run("UPDATE t SET a = 2")
        run("SELECT 2")
        connection.exec_driver_sql.assert_called_once_with(db.WRITE_LOCK_SQL)
        db.forget_write_lock(connection)
        run("DELETE FROM t")
        self.assertEqual(connection.exec_driver_sql.call_count, 2)

    def test_a_lock_that_cannot_be_taken_leaves_the_write_unsent_and_the_next_one_tries_again(self):
        connection = mock.Mock(info={})
        connection.exec_driver_sql.side_effect = [OperationalError("SELECT", {}, Exception("lock timeout")), None]
        with self.assertRaises(OperationalError):
            db.take_write_lock_before_writing(connection, None, "INSERT INTO t VALUES (1)", None, None, False)
        db.take_write_lock_before_writing(connection, None, "INSERT INTO t VALUES (1)", None, None, False)
        self.assertEqual(connection.exec_driver_sql.call_count, 2)

    def test_only_a_postgresql_engine_gets_the_hooks(self):
        sqlite_engine = own_sqlite_database(self)
        self.assertFalse(db.event.contains(sqlite_engine, "before_cursor_execute", db.take_write_lock_before_writing))

    def test_a_write_waits_for_another_writer_and_a_read_does_not(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            add_person(opened, "jo")
        with another_writer_holds_the_lock(engine):
            with db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                self.assertEqual(elsewhere.execute(select(Person.slug)).scalars().all(), ["jo"])
            for name, write in (("update", lambda opened: opened.execute(text("UPDATE people SET name = 'Jo'"))), ("insert", lambda opened: add_person(opened, "sam"))):
                with self.subTest(write=name):
                    with self.assertRaises(OperationalError) as busy, db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                        write(elsewhere)
                    self.assertTrue(db.is_busy(busy.exception))
        with db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
            add_person(elsewhere, "sam")

    def test_writes_through_a_connection_wait_too(self):
        engine = own_database(self)
        with another_writer_holds_the_lock(engine):
            with self.assertRaises(OperationalError) as busy, db.connect(impatient_engine(self, engine, 0.1)) as connection:
                connection.execute(text("UPDATE people SET name = 'Jo'"))
            self.assertTrue(db.is_busy(busy.exception))


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

        def test_a_plain_write_holds_the_write_lock_until_it_commits_and_a_read_never_takes_it(self):
            engine = own_database(self)
            taken = []

            def watch(conn, cursor, statement, parameters, context, executemany):
                if "pg_advisory_xact_lock" in statement:
                    taken.append(statement)

            event.listen(engine, "before_cursor_execute", watch)
            self.addCleanup(event.remove, engine, "before_cursor_execute", watch)
            with db.session(engine) as opened:
                opened.execute(select(Person.slug)).all()
                self.assertEqual(taken, [])
                add_person(opened, "jo")
                add_person(opened, "sam")
                opened.execute(select(Person.slug)).all()
                self.assertEqual(len(taken), 1)
                with self.assertRaises(OperationalError) as busy, db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                    add_person(elsewhere, "kim")
                self.assertTrue(db.is_busy(busy.exception))
            with db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                add_person(elsewhere, "kim")

        def test_an_explicit_write_lock_and_the_writes_after_it_share_one_lock(self):
            engine = own_database(self)
            with db.session(engine) as opened:
                db.write_lock(opened)
                add_person(opened, "jo")
                db.write_lock(opened)
                add_person(opened, "sam")

        def test_a_rolled_back_write_releases_the_lock_and_the_next_transaction_takes_it_again(self):
            engine = own_database(self)
            with self.assertRaises(RuntimeError):
                with db.session(engine) as opened:
                    add_person(opened, "jo")
                    raise RuntimeError("stop")
            with db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                add_person(elsewhere, "sam")
            with db.session(engine) as opened:
                add_person(opened, "kim")
                with self.assertRaises(OperationalError) as busy, db.session(impatient_engine(self, engine, 0.1)) as elsewhere:
                    add_person(elsewhere, "lee")
                self.assertTrue(db.is_busy(busy.exception))

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
