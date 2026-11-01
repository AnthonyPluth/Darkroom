import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from darkroom.storage import db
from darkroom.storage.schema import metadata


class MigrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = db.make_engine(f"sqlite:///{Path(folder.name) / 'm.db'}")
        self.addCleanup(self.engine.dispose)

    def test_0001_baseline_applies_to_an_empty_database_and_matches_the_models(self):
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            drift = compare_metadata(MigrationContext.configure(connection), metadata)
        self.assertEqual(drift, [])
        self.assertEqual(set(inspect(self.engine).get_table_names()) - {"alembic_version"}, set(metadata.tables))

    def test_0001_baseline_can_be_undone(self):
        db.migrate(self.engine)
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
        self.assertEqual(inspect(self.engine).get_table_names(), ["alembic_version"])

    def test_0002_user_email_verified_adds_an_unverified_flag_to_existing_users_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0001")
            connection.exec_driver_sql("INSERT INTO users (sub, email) VALUES ('u1', 'jo@example.test')")
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT email_verified FROM users").scalar(), 0)
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0001")
        self.assertNotIn("email_verified", {column["name"] for column in inspect(self.engine).get_columns("users")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT sub FROM users").scalar(), "u1")

    def test_0003_session_recheck_keeps_old_sessions_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0002")
            connection.exec_driver_sql("INSERT INTO users (sub) VALUES ('u1')")
            connection.exec_driver_sql("INSERT INTO auth_sessions (token_hash, sub, created, expires) VALUES ('h', 'u1', 1.0, 2.0)")
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            row = connection.exec_driver_sql("SELECT expires, refresh_token_sealed, last_checked, cookie_renewed FROM auth_sessions").one()
            self.assertEqual(tuple(row), (2.0, None, None, None))
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO auth_sessions (token_hash, sub, created) VALUES ('open', 'u1', 3.0)")
            config.attributes["connection"] = connection
            command.downgrade(config, "0002")
        columns = {column["name"] for column in inspect(self.engine).get_columns("auth_sessions")}
        self.assertEqual(columns, {"token_hash", "sub", "created", "expires"})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT token_hash FROM auth_sessions").scalars().all(), ["h"])

    def test_migrating_twice_changes_nothing(self):
        db.migrate(self.engine)
        db.migrate(self.engine)
        self.assertIn("people", inspect(self.engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
