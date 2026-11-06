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

    def test_0010_media_dhash_leaves_old_items_without_one_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0009")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO media_items (id, person_id, source, path, sha1, bytes, phash, indexed_at) VALUES (1, 1, 'phone', 'jo/a.jpg', 'abc', 5, 'f0f0f0f0f0f0f0f0', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT phash, dhash FROM media_items").one(), ("f0f0f0f0f0f0f0f0", None))
            connection.exec_driver_sql("UPDATE media_items SET dhash = '0f0f0f0f0f0f0f0f'")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0009")
        self.assertNotIn("dhash", {column["name"] for column in inspect(self.engine).get_columns("media_items")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT phash FROM media_items").scalars().all(), ["f0f0f0f0f0f0f0f0"])

    def test_0011_export_downloads_starts_old_exports_at_no_tries_and_can_be_undone_without_losing_parts(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0010")
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
            command.downgrade(config, "0010")
        self.assertNotIn("download_tries", {column["name"] for column in inspect(self.engine).get_columns("exports")})
        self.assertNotIn("bytes_done", {column["name"] for column in inspect(self.engine).get_columns("export_parts")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT number, bytes, downloads_used FROM export_parts").one(), (1, 100, 2))

    def test_0013_export_owner_leaves_old_exports_without_one_and_can_be_undone_without_losing_exports(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0012")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind, status) VALUES (1, 1, 'initial', 'downloaded')")
            connection.exec_driver_sql("INSERT INTO export_parts (export_id, number, bytes) VALUES (1, 1, 100)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT owner_person_id, status FROM exports").one(), (None, "downloaded"))
            connection.exec_driver_sql("UPDATE exports SET owner_person_id = 1")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0012")
        self.assertNotIn("owner_person_id", {column["name"] for column in inspect(self.engine).get_columns("exports")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT status FROM exports").scalars().all(), ["downloaded"])
            self.assertEqual(connection.exec_driver_sql("SELECT number FROM export_parts").scalars().all(), [1])


    def test_0017_import_run_repeated_counts_no_repeat_for_old_runs_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0016")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
            connection.exec_driver_sql("INSERT INTO import_runs (export_id, started, added, failed) VALUES (1, 2.0, 3, 1)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT added, failed, repeated FROM import_runs").all(), [(3, 1, 0)])
            connection.exec_driver_sql("UPDATE import_runs SET repeated = 4")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0016")
        self.assertNotIn("repeated", {column["name"] for column in inspect(self.engine).get_columns("import_runs")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT added, failed FROM import_runs").all(), [(3, 1)])

    def test_0016_device_wake_pushed_leaves_old_devices_unwoken_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0015")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, last_checkin) VALUES (1, 1, 'iPhone', 'h', 1.0, 5.0)")
            connection.exec_driver_sql("INSERT INTO phone_assets (device_id, photokit_id, created) VALUES (1, 'A', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT last_checkin, wake_pushed_at FROM devices").all(), [(5.0, None)])
            connection.exec_driver_sql("UPDATE devices SET wake_pushed_at = 6.5")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0015")
        self.assertNotIn("wake_pushed_at", {column["name"] for column in inspect(self.engine).get_columns("devices")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT last_checkin FROM devices").all(), [(5.0,)])
            self.assertEqual(connection.exec_driver_sql("SELECT photokit_id FROM phone_assets").scalars().all(), ["A"])

    def test_0015_alert_pushed_devices_leaves_old_alerts_without_a_record_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0014")
            connection.exec_driver_sql("INSERT INTO alerts (key, kind, subject, message, opened, sent_count, push_due) VALUES ('k', 'failure', 's', 'm', 1.0, 1, 2.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT key, push_due, pushed_devices FROM alerts").all(), [("k", 2.0, None)])
            connection.exec_driver_sql("UPDATE alerts SET pushed_devices = '{}'")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0014")
        self.assertNotIn("pushed_devices", {column["name"] for column in inspect(self.engine).get_columns("alerts")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT key, push_due FROM alerts").all(), [("k", 2.0)])

    def test_0014_alert_recipients_finds_whom_old_alerts_concern_and_can_be_undone_without_losing_alerts(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0013")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (2, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind, status) VALUES (3, 2, 'initial', 'downloaded')")
            connection.exec_driver_sql("INSERT INTO browser_jobs (id, account_id, kind, status, created) VALUES (4, 2, 'export_status', 'failed', 1.0)")
            for key, kind, subject in [("sign-in:2", "sign_in_needed", "a@x.test"), ("unmatched:1", "originals_unmatched", "jo"),
                                       ("immich:failed:3:1", "failure", "jo"), ("expiring:3", "export_expiring", "export 3"),
                                       ("browser:failed:4", "failure", "4"), ("other", "failure", "nobody")]:
                connection.exec_driver_sql("INSERT INTO alerts (key, kind, subject, message, opened, sent_count) VALUES (?, ?, ?, 'm', 1.0, 1)", (key, kind, subject))
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT key, person_id, account_id, push_due FROM alerts ORDER BY id").all(),
                             [("sign-in:2", None, 2, None), ("unmatched:1", 1, None, None), ("immich:failed:3:1", 1, None, None),
                              ("expiring:3", None, 2, None), ("browser:failed:4", None, 2, None), ("other", None, None, None)])
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0013")
        self.assertFalse({"person_id", "account_id", "push_due"} & {column["name"] for column in inspect(self.engine).get_columns("alerts")})
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.exec_driver_sql("SELECT key FROM alerts").all()), 6)

    def test_0012_device_push_tokens_leaves_old_devices_without_a_token_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0011")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, cleanup_enabled) VALUES (1, 1, 'iPhone', 'h', 1.0, 1)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, cleanup_enabled) VALUES (2, 1, 'Old', 'h2', 1.0, 0)")
            connection.exec_driver_sql("INSERT INTO phone_assets (device_id, photokit_id, created) VALUES (1, 'A', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT push_token_sealed, push_token_digest, push_token_at, cleanup_enabled FROM devices WHERE id = 1").one(),
                             (None, None, None, 1))
            connection.exec_driver_sql("UPDATE devices SET push_token_sealed = 'enc:v1:x', push_token_digest = 'd', push_token_at = 2.5 WHERE id = 1")
            with self.assertRaises(IntegrityError):
                connection.exec_driver_sql("UPDATE devices SET push_token_digest = 'd' WHERE id = 2")
        with self.engine.begin() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0011")
        columns = {column["name"] for column in inspect(self.engine).get_columns("devices")}
        self.assertFalse({"push_token_sealed", "push_token_digest", "push_token_at"} & columns)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name, cleanup_enabled FROM devices ORDER BY id").all(), [("iPhone", 1), ("Old", 0)])
            self.assertEqual(connection.exec_driver_sql("SELECT photokit_id FROM phone_assets").scalars().all(), ["A"])


if __name__ == "__main__":
    unittest.main()
