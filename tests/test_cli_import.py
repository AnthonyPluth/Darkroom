import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run
from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount, ImportRun
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
        self.assertIn("added", out)
        self.assertTrue((elsewhere / "takeout-20260101T000000Z-001.zip").is_file())

    def test_imports_the_manual_inbox_through_every_stage_and_once(self):
        standard_export(Photos(self.photos).manual_inbox("jo"))
        code, out, _ = self.call("--person", "jo")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"Export \d+: 16 added, 0 skipped as duplicates, 0 in review, 4 missing GPS")
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            self.assertEqual((export.status, export.stage), ("imported", "done"))
            run_row = opened.query(ImportRun).one()
            self.assertEqual((run_row.status, run_row.added), ("done", 16))
        self.assertTrue(any((self.photos / "jo/google-only").rglob("*.JPG")))
        code, out, _ = self.call("--person", "jo")
        self.assertEqual(code, 0)
        self.assertIn("already imported", out)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(ImportRun).count(), 1)

    def test_an_import_already_running_for_the_account_is_refused(self):
        from darkroom import config
        from darkroom.domain.ingest import run as ingest_run
        from darkroom.storage import locks

        standard_export(Photos(self.photos).manual_inbox("jo"))
        with db.session(self.engine) as opened:
            account = opened.query(GoogleAccount).one().id
        with locks.exclusive(ingest_run.lock_file(config.load(), account)):
            code, _, err = self.call("--person", "jo")
        self.assertEqual(code, 1)
        self.assertIn("already running", err)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(ImportRun).count(), 0)
            self.assertEqual(opened.query(Export).one().status, "downloaded")

    def test_a_run_left_to_resume_is_reported_as_resuming_and_finishes_when_run_again(self):
        from sqlalchemy.exc import OperationalError

        from darkroom.domain.ingest import run as ingest_run

        standard_export(Photos(self.photos).manual_inbox("jo"))
        real = ingest_run.mark
        refused = {"once": True}

        def failing(opened, export, run_row, stage):
            if stage == "reported" and refused["once"]:
                refused["once"] = False
                raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
            return real(opened, export, run_row, stage)

        with mock.patch.object(ingest_run, "mark", failing):
            code, out, err = self.call("--person", "jo")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("did not fail", err)
        self.assertIn("will resume", err)
        self.assertNotIn("the import failed", err)
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            self.assertEqual(export.status, "importing")
            self.assertEqual(opened.query(ImportRun).one().status, "running")
        code, out, err = self.call("--person", "jo")
        self.assertEqual(code, 0)
        self.assertNotIn("will resume", err)
        self.assertRegex(out, r"Export \d+: 16 added")
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(Export).one().status, "imported")
            self.assertEqual(opened.query(ImportRun).count(), 1)

    def test_running_the_command_again_retries_the_items_that_failed(self):
        from darkroom.domain.ingest import place

        standard_export(Photos(self.photos).manual_inbox("jo"))
        real = place.copy_into
        refused = {"left": 1}

        def failing(folder, source, sha1):
            if refused["left"]:
                refused["left"] -= 1
                raise place.PlaceFailed("taken")
            return real(folder, source, sha1)

        with mock.patch.object(place, "copy_into", failing):
            code, out, _ = self.call("--person", "jo")
        self.assertEqual(code, 0)
        self.assertIn("15 added", out)
        self.assertIn("1 could not be imported", out)
        code, out, _ = self.call("--person", "jo")
        self.assertEqual(code, 0)
        self.assertIn("16 added", out)
        self.assertNotIn("could not be imported", out)
        self.assertNotIn("already imported", out)
        code, out, _ = self.call("--person", "jo")
        self.assertEqual(code, 0)
        self.assertIn("already imported", out)
        with db.session(self.engine) as opened:
            self.assertEqual([(r.status, r.failed) for r in opened.query(ImportRun).order_by(ImportRun.id)], [("done", 1), ("done", 0)])

    def test_a_failed_run_still_reports_the_failure(self):
        from darkroom.domain.ingest import run as ingest_run

        standard_export(Photos(self.photos).manual_inbox("jo"))
        with mock.patch.object(ingest_run, "stage_report", side_effect=RuntimeError("boom")):
            code, _, err = self.call("--person", "jo")
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError during the import", err)
        self.assertNotIn("will resume", err)

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

    def test_an_empty_folder_is_an_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        code, out, err = self.call("--person", "jo", "--dir", str(empty))
        self.assertEqual(code, 1)
        self.assertIn("no Takeout zip files", err)
        self.assertNotIn("Unpacked", out)

    def test_account_option_picks_between_several(self):
        with db.session(self.engine) as opened:
            person_id = opened.query(GoogleAccount).one().default_person_id
            opened.add(GoogleAccount(email="jo2@example.com", default_person_id=person_id, created=2.0))
        elsewhere = self.root / "elsewhere"
        standard_export(elsewhere)
        code, _, err = self.call("--person", "jo", "--dir", str(elsewhere))
        self.assertEqual(code, 1)
        self.assertIn("--account", err)
        code, _, _ = self.call("--person", "jo", "--dir", str(elsewhere), "--account", "jo2@example.com", "--unpack-only")
        self.assertEqual(code, 0)
