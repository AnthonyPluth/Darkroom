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

    def test_migrating_twice_changes_nothing(self):
        db.migrate(self.engine)
        db.migrate(self.engine)
        self.assertIn("people", inspect(self.engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
