import tempfile
import unittest
from pathlib import Path

from darkroom.storage import db


class ConnectTests(unittest.TestCase):
    def test_wal_and_foreign_keys_are_on_and_rows_read_by_name(self):
        with tempfile.TemporaryDirectory() as folder:
            connection = db.connect(Path(folder) / "darkroom.db")
            self.addCleanup(connection.close)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT 5 AS five").fetchone()["five"], 5)


if __name__ == "__main__":
    unittest.main()
