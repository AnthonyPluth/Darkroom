import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from darkroom import config
from darkroom.storage import db
from darkroom.storage.models import Person, Setting
from tests.shared import add_person, own_database


class EngineTests(unittest.TestCase):
    def test_wal_and_foreign_keys_are_on(self):
        engine = own_database(self)
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


if __name__ == "__main__":
    unittest.main()
