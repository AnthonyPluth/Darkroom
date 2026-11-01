import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run
from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount
from darkroom.storage.paths import Photos
from tests.shared import add_person
from tests.takeout_fixtures import standard_export


class ImportTakeoutTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = self.root / "photos"
        values = {"DARKROOM_PHOTOS_ROOT": str(self.photos), "DARKROOM_DATA_DIR": str(self.root / "data"), "DARKROOM_SECRET_KEY": "k",
                  "DARKROOM_PUBLIC_URL": "https://d.example"}
        patcher = mock.patch.dict(os.environ, values, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        from darkroom import config

        self.engine = db.make_engine(db.database_url(config.load()))
        self.addCleanup(self.engine.dispose)
        with db.session(self.engine) as opened:
            person_id = add_person(opened, "jo")
            opened.add(GoogleAccount(email="jo@example.com", default_person_id=person_id, created=1.0))

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(["import-takeout", *args])
        return code, out.getvalue(), err.getvalue()

    def test_unpacks_the_manual_inbox(self):
        standard_export(Photos(self.photos).manual_inbox("jo"))
        code, _, _ = self.call("--person", "jo", "--unpack-only")
        self.assertEqual(code, 0)
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            self.assertEqual((export.status, export.stage), ("unpacking", "unpacked"))
            staging = Photos(self.photos).staging("jo", f"export-{export.id}")
            self.assertTrue((staging / "Takeout").is_dir())

    def test_dir_option_reads_another_folder(self):
        elsewhere = self.root / "elsewhere"
        standard_export(elsewhere)
        code, out, _ = self.call("--person", "jo", "--dir", str(elsewhere))
        self.assertEqual(code, 0)
        self.assertIn("Unpacked", out)

    def test_person_is_required_and_must_exist(self):
        self.assertEqual(self.call()[0], 1)
        code, _, err = self.call("--person", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("no person called nobody", err)

    def test_a_bad_part_makes_the_command_fail(self):
        inbox = Photos(self.photos).manual_inbox("jo")
        inbox.mkdir(parents=True)
        (inbox / "takeout-20260101T000000Z-001.zip").write_bytes(b"junk")
        code, _, err = self.call("--person", "jo")
        self.assertEqual(code, 1)
        self.assertIn("not a valid zip", err)

    def test_person_without_account_fails(self):
        with db.session(self.engine) as opened:
            add_person(opened, "al")
        code, _, err = self.call("--person", "al")
        self.assertEqual(code, 1)
        self.assertIn("no Google account", err)
