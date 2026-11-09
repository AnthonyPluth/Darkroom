import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from darkroom.storage import db
from darkroom.storage.schema import metadata
from tests.shared import on_postgres, own_database_url


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

    def test_0026_managed_import_receipts_preserves_existing_records_and_downgrades_an_empty_journal(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0025")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', -0.125)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'jo@example.test', 1, 1.25)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
            connection.exec_driver_sql("INSERT INTO media_items (person_id, source, path, sha1, bytes, indexed_at) VALUES (1, 'phone', 'jo/originals/generated.jpg', 'ab', 7, -0.125)")
            before = {table: connection.exec_driver_sql(f"SELECT * FROM {table}").all() for table in ("people", "google_accounts", "exports", "media_items")}
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            for table, rows in before.items():
                self.assertEqual(connection.exec_driver_sql(f"SELECT * FROM {table}").all(), rows)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0025")
        self.assertNotIn("managed_import_intents", inspect(self.engine).get_table_names())
        self.assertNotIn("managed_import_receipts", inspect(self.engine).get_table_names())
        with self.engine.connect() as connection:
            for table, rows in before.items():
                self.assertEqual(connection.exec_driver_sql(f"SELECT * FROM {table}").all(), rows)
        db.migrate(self.engine)

    def test_0027_managed_asset_catalog_preserves_a_populated_journal_and_downgrades_empty_catalogue(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0026")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', -0.125)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'jo@example.test', 1, 1.25)")
            connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
            connection.exec_driver_sql("INSERT INTO managed_import_intents (id, person_id, owner_id, export_id, resource_manifest, created_at) VALUES (1, 1, 'generated-owner', 1, ' { } ', -0.125)")
            connection.execute(text("INSERT INTO managed_import_receipts (intent_id, resource_key, compound_key, source_sha1, expected_bytes, expected_metadata, updated_at) "
                                    "VALUES (1, 'generated-resource', 'generated-group', :sha1, :bytes, ' { } ', 1.125)"), {"sha1": "a" * 40, "bytes": 5 * 1024 ** 4 + 17})
            tables = ("people", "google_accounts", "exports", "managed_import_intents", "managed_import_receipts")
            before = {table: connection.exec_driver_sql(f"SELECT * FROM {table}").all() for table in tables}
            before_indexes = {table: inspect(connection).get_indexes(table) for table in ("managed_import_intents", "managed_import_receipts")}
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            for table, rows in before.items():
                self.assertEqual(connection.exec_driver_sql(f"SELECT * FROM {table}").all(), rows)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0026")
        self.assertNotIn("managed_asset_versions", inspect(self.engine).get_table_names())
        self.assertNotIn("managed_asset_origins", inspect(self.engine).get_table_names())
        for table in ("managed_import_intents", "managed_import_receipts"):
            self.assertFalse(any(index["name"].endswith("catalog_binding") for index in inspect(self.engine).get_indexes(table)))
            self.assertEqual(inspect(self.engine).get_indexes(table), before_indexes[table])
        with self.engine.connect() as connection:
            for table, rows in before.items():
                self.assertEqual(connection.exec_driver_sql(f"SELECT * FROM {table}").all(), rows)
        db.migrate(self.engine)

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
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertIsNone(connection.exec_driver_sql("SELECT sub FROM devices").scalar_one())
            connection.exec_driver_sql("UPDATE devices SET sub = 'user-1'")
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
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, cleanup_enabled) VALUES (1, 1, 'iPhone', 'h', 1.0, FALSE)")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT eligibility_checked_at, cleanup_enabled FROM devices").one(), (None, 0))
            connection.exec_driver_sql("UPDATE devices SET eligibility_checked_at = 12.5")
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


    def pixels_sequence(self):
        with self.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'pixels'").scalars().all()

    def test_0022_pixel_ids_never_reused_on_a_database_with_no_pixels_keeps_one_sequence_row(self):
        db.migrate(self.engine)
        self.assertEqual(self.pixels_sequence(), [0])

    def pixels_schema(self):
        with self.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'pixels'").scalar_one()

    def test_0022_pixel_ids_never_reused_keeps_every_pixels_people_and_copies_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0021")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (1, 'One', 1, 1.0), (2, 'Two', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixel_people (pixel_id, person_id) VALUES (1, 1), (2, 1)")
            connection.exec_driver_sql("INSERT INTO media_items (id, person_id, source, path, sha1, bytes, indexed_at) VALUES (1, 1, 'phone', 'jo/originals/a.jpg', 'ab', 3, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixel_copies (id, pixel_id, item_id, person_id, state, name, bytes, sha1, queued_at, placed_at, always_send, seen_again_at) "
                                       "VALUES (7, 2, 1, 1, 'placed', 'a.jpg', 3, 'ab', 2.0, 3.0, 1, 4.0), (9, 1, 1, 1, 'queued', NULL, 3, 'ab', 2.0, NULL, 0, NULL)")
            connection.exec_driver_sql("INSERT INTO events (time, type, subject) VALUES (1.0, 'pixel_created', '3'), (2.0, 'pixel_deleted', '3'), (3.0, 'import', '40')")
        self.assertNotIn("AUTOINCREMENT", self.pixels_schema())
        kept = ("SELECT id, pixel_id, item_id, person_id, state, name, bytes, sha1, queued_at, placed_at, pruned_at, requeued, always_send, seen_again_at "
                "FROM pixel_copies ORDER BY id")
        with self.engine.connect() as connection:
            copies = connection.exec_driver_sql(kept).all()
        db.migrate(self.engine)
        self.assertIn("AUTOINCREMENT", self.pixels_schema())
        self.assertEqual(self.pixels_sequence(), [3])
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT id, name, google_account_id FROM pixels ORDER BY id").all(), [(1, "One", 1), (2, "Two", 1)])
            self.assertEqual(connection.exec_driver_sql("SELECT pixel_id, person_id FROM pixel_people ORDER BY pixel_id").all(), [(1, 1), (2, 1)])
            self.assertEqual(connection.exec_driver_sql(kept).all(), copies)
            self.assertEqual(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_kept_%'").all(), [])
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            connection.exec_driver_sql("DELETE FROM pixels WHERE id = 2")
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM pixel_copies WHERE pixel_id = 2").scalar_one(), 0)
            connection.exec_driver_sql("INSERT INTO pixels (name, google_account_id, created) VALUES ('New', 1, 5.0)")
            self.assertEqual(connection.exec_driver_sql("SELECT id FROM pixels WHERE name = 'New'").scalar_one(), 4)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0021")
        self.assertNotIn("AUTOINCREMENT", self.pixels_schema())
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT id, name FROM pixels ORDER BY id").all(), [(1, "One"), (4, "New")])
            self.assertEqual(connection.exec_driver_sql(kept).all(), copies[1:])
            self.assertEqual(connection.exec_driver_sql("SELECT pixel_id, person_id FROM pixel_people").all(), [(1, 1)])

    def test_0022_pixel_ids_never_reused_starts_after_the_highest_pixel_when_none_was_deleted(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0021")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (5, 'Five', 1, 1.0)")
        db.migrate(self.engine)
        self.assertEqual(self.pixels_sequence(), [5])
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM pixels")
            connection.exec_driver_sql("INSERT INTO pixels (name, google_account_id, created) VALUES ('New', 1, 5.0)")
            self.assertEqual(connection.exec_driver_sql("SELECT id FROM pixels").scalar_one(), 6)

    def test_0025_drop_iphone_tables_removes_the_device_tables_and_columns_keeps_the_rest_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0024")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, push_token_sealed, push_token_digest) VALUES (1, 1, 'iPhone', 'h', 1.0, 'enc:v1:x', 'd')")
            connection.exec_driver_sql("INSERT INTO phone_assets (id, device_id, photokit_id, created, eligible_at) VALUES (1, 1, 'p', 1.0, 2.0)")
            connection.exec_driver_sql("INSERT INTO phone_resources (asset_id, kind, sha1, bytes, filename) VALUES (1, 'photo', 'a', 1, 'a.jpg')")
            connection.exec_driver_sql("INSERT INTO alerts (key, kind, subject, message, opened, sent_count, push_due, pushed_devices) VALUES ('k', 'failure', 's', 'm', 1.0, 1, 2.0, '[1]')")
            connection.exec_driver_sql("INSERT INTO auth_pending (state, nonce, verifier, kind, created) VALUES ('web', 'n', 'v', 'web', 1.0)")
            connection.exec_driver_sql("INSERT INTO auth_pending (state, nonce, verifier, kind, device_name, client_state, created) VALUES ('dev', 'n', 'v', 'device', 'iPhone', 'c', 1.0)")
        db.migrate(self.engine)
        tables = set(inspect(self.engine).get_table_names())
        self.assertFalse({"devices", "phone_assets", "phone_resources"} & tables)
        self.assertEqual({"push_due"}, {"push_due", "pushed_devices"} & {column["name"] for column in inspect(self.engine).get_columns("alerts")})
        self.assertFalse({"device_name", "client_state"} & {column["name"] for column in inspect(self.engine).get_columns("auth_pending")})
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT key, sent_count, push_due FROM alerts").all(), [("k", 1, 2.0)])
            self.assertEqual(connection.exec_driver_sql("SELECT state, kind FROM auth_pending").all(), [("web", "web")])
            self.assertEqual(connection.exec_driver_sql("SELECT slug FROM people").all(), [("jo",)])
            with self.assertRaises(IntegrityError):
                connection.exec_driver_sql("INSERT INTO auth_pending (state, nonce, verifier, kind, created) VALUES ('d2', 'n', 'v', 'device', 1.0)")
        with self.engine.begin() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            config.attributes["connection"] = connection
            command.downgrade(config, "0024")
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue({"devices", "phone_assets", "phone_resources"} <= tables)
        self.assertTrue({"push_due", "pushed_devices"} <= {column["name"] for column in inspect(self.engine).get_columns("alerts")})
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM devices").scalar(), 0)
            connection.exec_driver_sql("INSERT INTO auth_pending (state, nonce, verifier, kind, device_name, created) VALUES ('d3', 'n', 'v', 'device', 'iPhone', 1.0)")

    def test_0025_drop_iphone_tables_upgrades_an_empty_database(self):
        db.migrate(self.engine)
        self.assertFalse({"devices", "phone_assets", "phone_resources"} & set(inspect(self.engine).get_table_names()))

    def test_0023_immich_assets_adds_an_empty_table_and_the_window_column_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0022")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT immich_reconciled_until FROM people").all(), [(None,)])
        with self.engine.begin() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            config.attributes["connection"] = connection
            command.downgrade(config, "0022")
        self.assertNotIn("immich_assets", inspect(self.engine).get_table_names())
        self.assertNotIn("immich_reconciled_until", {column["name"] for column in inspect(self.engine).get_columns("people")})

    def test_0024_duplicate_removals_adds_an_empty_table_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM duplicate_removals").scalar(), 0)
            with self.assertRaises(IntegrityError), connection.begin_nested():
                connection.exec_driver_sql("INSERT INTO duplicate_removals (person_id, kept_path, kept_sha1, removed_path, removed_sha1, removed_source, rule, tier, state, created_at) "
                                           "VALUES (1, 'k', 'a', 'r', 'b', 'takeout', 'identical', 1, 'gone', 1.0)")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
            config.attributes["connection"] = connection
            command.downgrade(config, "0023")
        self.assertNotIn("duplicate_removals", inspect(self.engine).get_table_names())

    def test_0021_pixel_copy_seen_again_leaves_existing_copies_unseen_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0020")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (1, 'Pixel', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO media_items (id, person_id, source, path, sha1, bytes, indexed_at) VALUES (1, 1, 'phone', 'jo/originals/a.jpg', 'ab', 3, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixel_copies (pixel_id, item_id, person_id, state, bytes, sha1, queued_at, placed_at, always_send) VALUES (1, 1, 1, 'placed', 3, 'ab', 2.0, 3.0, TRUE)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT state, placed_at, seen_again_at FROM pixel_copies").all(), [("placed", 3.0, None)])
            connection.exec_driver_sql("UPDATE pixel_copies SET seen_again_at = 4.0")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0020")
        self.assertNotIn("seen_again_at", {column["name"] for column in inspect(self.engine).get_columns("pixel_copies")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT state, placed_at FROM pixel_copies").all(), [("placed", 3.0)])

    def test_0020_pixel_copies_adds_an_empty_queue_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0019")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (1, 'Pixel', 1, 1.0)")
            connection.exec_driver_sql("INSERT INTO media_items (id, person_id, source, path, sha1, bytes, indexed_at) VALUES (1, 1, 'phone', 'jo/originals/a.jpg', 'ab', 3, 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM pixel_copies").scalar_one(), 0)
            connection.exec_driver_sql("INSERT INTO pixel_copies (pixel_id, item_id, person_id, bytes, sha1, queued_at) VALUES (1, 1, 1, 3, 'ab', 2.0)")
            self.assertEqual(connection.exec_driver_sql("SELECT state, requeued, always_send FROM pixel_copies").all(), [("queued", 0, 0)])
            with self.assertRaises(IntegrityError), connection.begin_nested():
                connection.exec_driver_sql("INSERT INTO pixel_copies (pixel_id, item_id, person_id, bytes, sha1, queued_at) VALUES (1, 1, 1, 3, 'ab', 2.0)")
            with self.assertRaises(IntegrityError), connection.begin_nested():
                connection.exec_driver_sql("UPDATE pixel_copies SET state = 'gone'")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0019")
        self.assertNotIn("pixel_copies", inspect(self.engine).get_table_names())
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT path FROM media_items").all(), [("jo/originals/a.jpg",)])

    def test_0019_legacy_import_adds_a_folder_per_person_and_a_table_of_files_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0018")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT slug, legacy_path FROM people").all(), [("jo", None)])
            connection.exec_driver_sql("INSERT INTO legacy_files (person_id, path, bytes, mtime, sha1, status, tier, seen_at) VALUES (1, 'a.jpg', 3, 1.0, 'x', 'unmatched', 5, 2.0)")
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0018")
        self.assertNotIn("legacy_files", inspect(self.engine).get_table_names())
        self.assertNotIn("legacy_path", {column["name"] for column in inspect(self.engine).get_columns("people")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT slug FROM people").all(), [("jo",)])

    def test_0018_import_run_repeated_counts_no_repeat_for_old_runs_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0017")
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
            command.downgrade(config, "0017")
        self.assertNotIn("repeated", {column["name"] for column in inspect(self.engine).get_columns("import_runs")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT added, failed FROM import_runs").all(), [(3, 1)])

    def test_0017_auth_pending_client_state_leaves_old_sign_ins_without_one_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0016")
            connection.exec_driver_sql("INSERT INTO auth_pending (state, nonce, verifier, kind, device_name, created) VALUES ('s', 'n', 'v', 'device', 'iPhone', 1.0)")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT state, client_state FROM auth_pending").all(), [("s", None)])
            connection.exec_driver_sql("UPDATE auth_pending SET client_state = 'abc'")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0016")
        self.assertNotIn("client_state", {column["name"] for column in inspect(self.engine).get_columns("auth_pending")})
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT state, device_name FROM auth_pending").all(), [("s", "iPhone")])

    def test_0016_device_wake_pushed_leaves_old_devices_unwoken_and_can_be_undone(self):
        config = db.alembic_config(self.engine)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0015")
            connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, last_checkin) VALUES (1, 1, 'iPhone', 'h', 1.0, 5.0)")
            connection.exec_driver_sql("INSERT INTO phone_assets (device_id, photokit_id, created) VALUES (1, 'A', 1.0)")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT last_checkin, wake_pushed_at FROM devices").all(), [(5.0, None)])
            connection.exec_driver_sql("UPDATE devices SET wake_pushed_at = 6.5")
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
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT key, push_due, pushed_devices FROM alerts").all(), [("k", 2.0, None)])
            connection.exec_driver_sql("UPDATE alerts SET pushed_devices = '{}'")
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
                connection.execute(text("INSERT INTO alerts (key, kind, subject, message, opened, sent_count) VALUES (:key, :kind, :subject, 'm', 1.0, 1)"),
                                   {"key": key, "kind": kind, "subject": subject})
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT key, person_id, account_id, push_due FROM alerts ORDER BY id").all(),
                             [("sign-in:2", None, 2, None), ("unmatched:1", 1, None, None), ("immich:failed:3:1", 1, None, None),
                              ("expiring:3", None, 2, None), ("browser:failed:4", None, 2, None), ("other", None, None, None)])
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
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, cleanup_enabled) VALUES (1, 1, 'iPhone', 'h', 1.0, TRUE)")
            connection.exec_driver_sql("INSERT INTO devices (id, person_id, name, token_hash, created, cleanup_enabled) VALUES (2, 1, 'Old', 'h2', 1.0, FALSE)")
            connection.exec_driver_sql("INSERT INTO phone_assets (device_id, photokit_id, created) VALUES (1, 'A', 1.0)")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT push_token_sealed, push_token_digest, push_token_at, cleanup_enabled FROM devices WHERE id = 1").one(),
                             (None, None, None, 1))
            connection.exec_driver_sql("UPDATE devices SET push_token_sealed = 'enc:v1:x', push_token_digest = 'd', push_token_at = 2.5 WHERE id = 1")
            with self.assertRaises(IntegrityError):
                connection.exec_driver_sql("UPDATE devices SET push_token_digest = 'd' WHERE id = 2")
        with self.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), "0023")
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0011")
        columns = {column["name"] for column in inspect(self.engine).get_columns("devices")}
        self.assertFalse({"push_token_sealed", "push_token_digest", "push_token_at"} & columns)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name, cleanup_enabled FROM devices ORDER BY id").all(), [("iPhone", 1), ("Old", 0)])
            self.assertEqual(connection.exec_driver_sql("SELECT photokit_id FROM phone_assets").scalars().all(), ["A"])


if on_postgres():

    class PostgresMigrationTests(MigrationTests):
        def setUp(self):
            self.engine = db.make_engine(own_database_url(self))
            self.addCleanup(self.engine.dispose)

        def pixels_sequence(self):
            with self.engine.connect() as connection:
                return connection.exec_driver_sql("SELECT COALESCE(pg_sequence_last_value('pixels_id_seq'), 0)").scalars().all()

        def test_0022_pixel_ids_never_reused_keeps_every_pixels_people_and_copies_and_can_be_undone(self):
            config = db.alembic_config(self.engine)
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "0021")
                connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
                connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
                connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (1, 'One', 1, 1.0), (2, 'Two', 1, 1.0)")
                connection.exec_driver_sql("INSERT INTO pixel_people (pixel_id, person_id) VALUES (1, 1), (2, 1)")
                connection.exec_driver_sql("INSERT INTO media_items (id, person_id, source, path, sha1, bytes, indexed_at) VALUES (1, 1, 'phone', 'jo/originals/a.jpg', 'ab', 3, 1.0)")
                connection.exec_driver_sql("INSERT INTO pixel_copies (id, pixel_id, item_id, person_id, state, name, bytes, sha1, queued_at, placed_at, always_send, seen_again_at) "
                                           "VALUES (7, 2, 1, 1, 'placed', 'a.jpg', 3, 'ab', 2.0, 3.0, true, 4.0), (9, 1, 1, 1, 'queued', NULL, 3, 'ab', 2.0, NULL, false, NULL)")
                connection.exec_driver_sql("INSERT INTO events (time, type, subject) VALUES (1.0, 'pixel_created', '3'), (2.0, 'pixel_deleted', '3'), (3.0, 'import', '40')")
            kept = ("SELECT id, pixel_id, item_id, person_id, state, name, bytes, sha1, queued_at, placed_at, pruned_at, requeued, always_send, seen_again_at "
                    "FROM pixel_copies ORDER BY id")
            with self.engine.connect() as connection:
                copies = connection.exec_driver_sql(kept).all()
            db.migrate(self.engine)
            self.assertEqual(self.pixels_sequence(), [3])
            with self.engine.begin() as connection:
                self.assertEqual(connection.exec_driver_sql("SELECT id, name, google_account_id FROM pixels ORDER BY id").all(), [(1, "One", 1), (2, "Two", 1)])
                self.assertEqual(connection.exec_driver_sql("SELECT pixel_id, person_id FROM pixel_people ORDER BY pixel_id").all(), [(1, 1), (2, 1)])
                self.assertEqual(connection.exec_driver_sql(kept).all(), copies)
                self.assertEqual(compare_metadata(MigrationContext.configure(connection), metadata), [])
                connection.exec_driver_sql("DELETE FROM pixels WHERE id = 2")
                self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM pixel_copies WHERE pixel_id = 2").scalar_one(), 0)
                connection.exec_driver_sql("INSERT INTO pixels (name, google_account_id, created) VALUES ('New', 1, 5.0)")
                self.assertEqual(connection.exec_driver_sql("SELECT id FROM pixels WHERE name = 'New'").scalar_one(), 4)
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.downgrade(config, "0021")
            with self.engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("SELECT id, name FROM pixels ORDER BY id").all(), [(1, "One"), (4, "New")])
                self.assertEqual(connection.exec_driver_sql(kept).all(), copies[1:])
                self.assertEqual(connection.exec_driver_sql("SELECT pixel_id, person_id FROM pixel_people").all(), [(1, 1)])

        def test_the_deleted_pixel_of_a_database_with_no_pixel_left_is_not_reused_after_migrating(self):
            config = db.alembic_config(self.engine)
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "0021")
                connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
                connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
                connection.exec_driver_sql("INSERT INTO events (time, type, subject) VALUES (1.0, 'pixel_created', '7'), (2.0, 'pixel_deleted', '7')")
            db.migrate(self.engine)
            with self.engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO pixels (name, google_account_id, created) VALUES ('New', 1, 5.0)")
                self.assertEqual(connection.exec_driver_sql("SELECT id FROM pixels").scalar_one(), 8)

        def test_byte_counts_above_two_gibibytes_fit_after_migrating(self):
            db.migrate(self.engine)
            big = 5 * 1024**3
            with self.engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO people (id, name, slug, originals_path, google_only_path, created) VALUES (1, 'Jo', 'jo', 'o', 'g', 1.0)")
                connection.exec_driver_sql("INSERT INTO google_accounts (id, email, default_person_id, created) VALUES (1, 'a@x.test', 1, 1.0)")
                connection.exec_driver_sql("INSERT INTO exports (id, account_id, kind) VALUES (1, 1, 'manual')")
                connection.exec_driver_sql(f"INSERT INTO export_parts (export_id, number, bytes, bytes_done) VALUES (1, 1, {big}, {big})")
                connection.exec_driver_sql(f"INSERT INTO media_items (id, person_id, source, path, sha1, bytes, indexed_at) VALUES (1, 1, 'phone', 'jo/originals/a.jpg', 'ab', {big}, 1.0)")
                connection.exec_driver_sql(f"INSERT INTO legacy_files (person_id, path, bytes, mtime, sha1, status, seen_at) VALUES (1, 'p', {big}, 1.0, 'ab', 'unmatched', 1.0)")
                connection.exec_driver_sql("INSERT INTO pixels (id, name, google_account_id, created) VALUES (1, 'One', 1, 1.0)")
                connection.exec_driver_sql(f"INSERT INTO pixel_copies (pixel_id, item_id, person_id, bytes, sha1, queued_at) VALUES (1, 1, 1, {big}, 'ab', 1.0)")

        def test_migrating_a_second_database_in_the_same_server_leaves_the_first_alone(self):
            other = db.make_engine(own_database_url(self))
            self.addCleanup(other.dispose)
            db.migrate(self.engine)
            self.assertEqual(inspect(other).get_table_names(), [])


if __name__ == "__main__":
    unittest.main()
