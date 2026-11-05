import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

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

    def test_0004_dedupe_decision_source_keeps_old_decisions_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0003")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
            connection.exec_driver_sql("INSERT INTO dedupe_decisions (export_id, account_id, tier, reason, decided_by, decided_at) VALUES (1, 1, 5, 'no match', 'auto', 2.0)")
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            row = connection.exec_driver_sql("SELECT tier, source_path, person_id FROM dedupe_decisions").one()
            self.assertEqual(tuple(row), (5, None, None))
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO dedupe_decisions (export_id, account_id, source_path, person_id, tier, reason, decided_by, decided_at) VALUES (1, 1, 'a.jpg', 1, 1, 'same SHA-1', 'auto', 3.0)")
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO dedupe_decisions (export_id, account_id, source_path, tier, reason, decided_by, decided_at) VALUES (1, 1, 'a.jpg', 5, 'no match', 'auto', 4.0)")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0003")
        columns = {column["name"] for column in inspect(self.engine).get_columns("dedupe_decisions")}
        self.assertNotIn("source_path", columns)
        self.assertNotIn("person_id", columns)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT tier FROM dedupe_decisions ORDER BY id").scalars().all(), [5, 1])

    def test_0005_import_run_failed_and_libraries_keeps_old_runs_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0004")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
            connection.exec_driver_sql("INSERT INTO import_runs (export_id, started, added) VALUES (1, 2.0, 3)")
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            row = connection.exec_driver_sql("SELECT added, failed, changed_people FROM import_runs").one()
            self.assertEqual(tuple(row), (3, 0, None))
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0004")
        columns = {column["name"] for column in inspect(self.engine).get_columns("import_runs")}
        self.assertNotIn("failed", columns)
        self.assertNotIn("changed_people", columns)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT added FROM import_runs").scalar(), 3)

    def test_migrating_twice_changes_nothing(self):
        db.migrate(self.engine)
        db.migrate(self.engine)
        self.assertIn("people", inspect(self.engine).get_table_names())


    def test_0006_pixel_last_sync_keeps_old_pixels_with_no_sync_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0005")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (1, 'Pixel', 1, 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertIsNone(connection.exec_driver_sql("SELECT last_sync FROM pixels").scalar_one())
            connection.exec_driver_sql("UPDATE pixels SET last_sync = 123.5")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0005")
        self.assertNotIn("last_sync", {column["name"] for column in inspect(self.engine).get_columns("pixels")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name FROM pixels").scalars().all(), ["Pixel"])

    def test_0007_device_sub_leaves_old_devices_unbound_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0006")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created) VALUES (1, 1, 'iPhone', 'h', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertIsNone(connection.exec_driver_sql("SELECT sub FROM devices").scalar_one())
            connection.exec_driver_sql("UPDATE devices SET sub = 'user-1'")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0006")
        self.assertNotIn("sub", {column["name"] for column in inspect(self.engine).get_columns("devices")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name FROM devices").scalars().all(), ["iPhone"])

    def test_0008_review_sidecar_keeps_old_reviews_without_one_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0007")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
            connection.exec_driver_sql("INSERT INTO dedupe_decisions (id, export_id, account_id, source_path, person_id, tier, reason, decided_by, decided_at) "
                                       "VALUES (1, 1, 1, 'Takeout/a.jpg', 1, 4, 'close', 'auto', 1.0)")
            connection.exec_driver_sql("INSERT INTO review_items (id, decision_id, status) VALUES (1, 1, 'pending')")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT sidecar_json, hold_sha1 FROM review_items").one(), (None, None))
            connection.exec_driver_sql("""UPDATE review_items SET sidecar_json = '{"title": "a.jpg"}', hold_sha1 = 'abc'""")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0007")
        self.assertFalse({"sidecar_json", "hold_sha1"} & {column["name"] for column in inspect(self.engine).get_columns("review_items")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT status FROM review_items").scalars().all(), ["pending"])

    def test_0009_device_eligibility_checked_leaves_old_devices_unchecked_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0008")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, cleanup_enabled) VALUES (1, 1, 'iPhone', 'h', 1.0, 0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT eligibility_checked_at, cleanup_enabled FROM devices").one(), (None, 0))
            connection.exec_driver_sql("UPDATE devices SET eligibility_checked_at = 12.5")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0008")
        self.assertNotIn("eligibility_checked_at", {column["name"] for column in inspect(self.engine).get_columns("devices")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name FROM devices").scalars().all(), ["iPhone"])
    def test_0010_export_downloads_starts_old_exports_at_no_tries_and_can_be_undone_without_losing_parts(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0009")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind, status) VALUES (1, 1, 'initial', 'announced')")
            connection.exec_driver_sql("INSERT INTO export_parts (export_id, number, bytes, downloads_used) VALUES (1, 1, 100, 2)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT download_tries FROM exports").one(), (0,))
            self.assertEqual(connection.exec_driver_sql("SELECT bytes_done, downloads_used FROM export_parts").one(), (None, 2))
            connection.exec_driver_sql("UPDATE export_parts SET bytes_done = 40")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0009")
        self.assertNotIn("download_tries", {column["name"] for column in inspect(self.engine).get_columns("exports")})
        self.assertNotIn("bytes_done", {column["name"] for column in inspect(self.engine).get_columns("export_parts")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT number, bytes, downloads_used FROM export_parts").one(), (1, 100, 2))


if __name__ == "__main__":
    unittest.main()
