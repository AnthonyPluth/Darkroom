import contextlib
import io
import logging
import os
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select

import run
from darkroom.dates import local_today
from darkroom.domain import retention_gate
from darkroom.storage import db
from darkroom.storage.models import Event, RetentionAttestation
from tests.privacy import no_leaks
from tests.shared import own_database_url

EVIDENCE = "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-7100000001"
OTHER_EVIDENCE = "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-7100000002"
REHEARSAL = ["--evidence", EVIDENCE, "--rehearsed-on", "2026-11-20", "--immich-version", "v3.4.1", "--darkroom-version", "v5.9.0", "--database", "sqlite"]


def rehearsal_with(**changed):
    args = dict(zip(REHEARSAL[::2], REHEARSAL[1::2], strict=True)) | {f"--{name.replace('_', '-')}": value for name, value in changed.items()}
    return ["record-rehearsal", *[part for pair in args.items() for part in pair]]


class RetentionWorld(unittest.TestCase):
    def setUp(self):
        self.url = own_database_url(self)
        self.engine = db.make_engine(self.url)
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.environment = {"DARKROOM_PHOTOS_ROOT": data.name, "DARKROOM_DATA_DIR": data.name, "DATABASE_URL": self.engine.url.render_as_string(hide_password=False)}

    def retention(self, *argv):
        with mock.patch.dict(os.environ, self.environment, clear=True), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = run.main(["retention", *argv])
        return code, out.getvalue(), err.getvalue()

    def rows(self):
        with db.session(self.engine) as opened:
            return [(row.kind, row.evidence_url, row.rehearsed_on, row.immich_version, row.darkroom_version, row.database_kind, row.withdrawn_at is None)
                    for row in opened.execute(select(RetentionAttestation).order_by(RetentionAttestation.id)).scalars()]

    def events(self):
        with db.session(self.engine) as opened:
            return [(event.type, event.subject, event.detail) for event in opened.execute(select(Event).order_by(Event.id)).scalars()]

    def assert_refused(self, found, reason):
        code, out, err = found
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(retention_gate.REASONS[reason], err)
        self.assertIn("nothing was recorded", err)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.events(), [])


class RecordTests(RetentionWorld):
    def test_a_rehearsal_is_recorded_with_its_evidence_and_versions_and_an_event(self):
        code, out, _err = self.retention(*rehearsal_with())
        self.assertEqual(code, 0)
        self.assertEqual(self.rows(), [("restore_rehearsal", EVIDENCE, "2026-11-20", "v3.4.1", "v5.9.0", "sqlite", True)])
        self.assertIn(EVIDENCE, out)
        self.assertIn("rehearsed on 2026-11-20: Immich v3.4.1, Darkroom v5.9.0, sqlite database", out)
        self.assertIn(run.RETENTION_KEPT, out)
        self.assertEqual(self.events(), [("retention_recorded", "attestation 1", "restore_rehearsal")])

    def test_a_new_rehearsal_withdraws_the_old_one_in_the_same_transaction(self):
        self.retention(*rehearsal_with())
        self.assertEqual(self.retention(*rehearsal_with(evidence=OTHER_EVIDENCE, database="postgresql"))[0], 0)
        rows = self.rows()
        self.assertEqual([(row[1], row[5], row[6]) for row in rows], [(EVIDENCE, "sqlite", False), (OTHER_EVIDENCE, "postgresql", True)])
        self.assertEqual([event[0] for event in self.events()], ["retention_recorded", "retention_withdrawn", "retention_recorded"])

    def test_the_boundary_is_only_printed_without_accept(self):
        code, out, _err = self.retention("accept-boundary", "--evidence", EVIDENCE)
        self.assertEqual(code, 0)
        self.assertIn(retention_gate.BOUNDARY_TEXT, out)
        self.assertIn(run.BOUNDARY_NOT_STORED, out)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.events(), [])

    def test_the_boundary_is_recorded_with_accept_and_no_rehearsal_columns(self):
        code, out, _err = self.retention("accept-boundary", "--evidence", EVIDENCE, "--accept")
        self.assertEqual(code, 0)
        self.assertIn(retention_gate.BOUNDARY_TEXT, out)
        self.assertEqual(self.rows(), [("remote_deletion_boundary", EVIDENCE, None, None, None, None, True)])
        self.assertEqual(self.events(), [("retention_recorded", "attestation 1", "remote_deletion_boundary")])

    def test_recording_and_withdrawing_take_the_write_lock_and_status_does_not(self):
        for argv, locks in ((rehearsal_with(), 1), (["accept-boundary", "--evidence", EVIDENCE, "--accept"], 1), (["withdraw", "rehearsal"], 1), (["status"], 0)):
            with self.subTest(argv=argv[0]), mock.patch.object(retention_gate.db, "write_lock", wraps=db.write_lock) as lock:
                self.assertEqual(self.retention(*argv)[0], 0)
            self.assertEqual(lock.call_count, locks)

    def test_recorded_evidence_is_never_logged_nor_put_in_an_event(self):
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        _code, _out, err = self.retention(*rehearsal_with())
        _code, _out, err_boundary = self.retention("accept-boundary", "--evidence", OTHER_EVIDENCE, "--accept")
        _code, _out, err_status = self.retention("status")
        for text in (err, err_boundary, err_status, logged.getvalue(), repr(self.events())):
            self.assertNotIn("issuecomment", text)


class RefusalTests(RetentionWorld):
    def test_evidence_that_is_not_exactly_a_266_comment_is_refused(self):
        for url in ("https://github.com/AnthonyPluth/Darkroom/issues/265#issuecomment-1", "http://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-1",
                    "https://github.com/AnthonyPluth/Darkroom/issues/2660#issuecomment-1", "https://github.com/someone/Darkroom/issues/266#issuecomment-1",
                    "https://github.com/AnthonyPluth/Darkroom/issues/266", "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-0",
                    "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-1?x=1", "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-1\n",
                    " https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-1", "https://github.com/AnthonyPluth/Darkroom/pull/266#issuecomment-1",
                    "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-" + "1" * 21, ""):
            with self.subTest(url=url):
                self.assert_refused(self.retention(*rehearsal_with(evidence=url)), "evidence")
                self.assert_refused(self.retention("accept-boundary", "--evidence", url, "--accept"), "evidence")
                self.assert_refused(self.retention("accept-boundary", "--evidence", url), "evidence")

    def test_a_malformed_or_impossible_rehearsal_date_is_refused(self):
        for day in ("2026-2-03", "20261120", "2026-02-30", "2026-13-01", "yesterday", "2026-11-20T10:00"):
            with self.subTest(day=day):
                self.assert_refused(self.retention(*rehearsal_with(rehearsed_on=day)), "rehearsed_on")

    def test_a_rehearsal_after_today_is_refused(self):
        self.assert_refused(self.retention(*rehearsal_with(rehearsed_on="2999-01-01")), "rehearsed_in_future")

    def test_today_is_the_local_day_of_the_configured_zone(self):
        today = "2026-11-24"
        self.assertEqual(retention_gate.checked_rehearsal(EVIDENCE, today, "v3.4.1", "v5.9.0", "sqlite", today).rehearsed_on, today)
        with self.assertRaises(retention_gate.Refused) as refused:
            retention_gate.checked_rehearsal(EVIDENCE, "2026-11-25", "v3.4.1", "v5.9.0", "sqlite", today)
        self.assertEqual(refused.exception.reason, "rehearsed_in_future")
        self.environment["TZ"] = "Pacific/Kiritimati"
        self.assertEqual(self.retention(*rehearsal_with(rehearsed_on=local_today("Pacific/Kiritimati")))[0], 0)

    def test_an_immich_version_outside_the_managed_versions_is_refused(self):
        for version in ("v3.4.0", "3.4.1", "v3.4.1 ", "latest"):
            with self.subTest(version=version):
                self.assert_refused(self.retention(*rehearsal_with(immich_version=version)), "immich_version")

    def test_a_darkroom_version_that_is_not_a_release_tag_is_refused(self):
        for version in ("5.9.0", "v5.9", "main", "v5.9.0-dirty", "dev"):
            with self.subTest(version=version):
                self.assert_refused(self.retention(*rehearsal_with(darkroom_version=version)), "darkroom_version")

    def test_an_unknown_database_is_refused(self):
        for kind in ("mysql", "SQLite", "postgres", ""):
            with self.subTest(kind=kind):
                self.assert_refused(self.retention(*rehearsal_with(database=kind)), "database")

    def test_withdrawing_needs_rehearsal_or_boundary(self):
        self.retention(*rehearsal_with())
        before = self.rows(), self.events()
        code, _out, err = self.retention("withdraw", "restore_rehearsal")
        self.assertEqual(code, 1)
        self.assertIn(retention_gate.REASONS["withdraw_what"], err)
        self.assertEqual((self.rows(), self.events()), before)

    def test_withdrawing_what_is_not_recorded_is_refused(self):
        for which in ("rehearsal", "boundary"):
            with self.subTest(which=which):
                self.assert_refused(self.retention("withdraw", which), "nothing_active")

    def test_a_withdrawn_record_is_never_withdrawn_again(self):
        self.retention("accept-boundary", "--evidence", EVIDENCE, "--accept")
        self.assertEqual(self.retention("withdraw", "boundary")[0], 0)
        code, _out, err = self.retention("withdraw", "boundary")
        self.assertEqual(code, 1)
        self.assertIn(retention_gate.REASONS["nothing_active"], err)
        self.assertEqual([event[0] for event in self.events()], ["retention_recorded", "retention_withdrawn"])

    def test_an_unmigrated_database_is_refused_without_creating_anything(self):
        with tempfile.TemporaryDirectory() as folder:
            self.environment["DATABASE_URL"] = f"sqlite:///{folder}/empty.db"
            code, out, err = self.retention("status")
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn(run.RETENTION_NOT_MIGRATED, err)

    def test_refused_values_are_never_echoed_logged_or_stored(self):
        canary_url = "https://example.test/CANARY-EVIDENCE-7f3a"
        refused = (rehearsal_with(evidence=canary_url), ["accept-boundary", "--evidence", canary_url, "--accept"], ["accept-boundary", "--evidence", canary_url],
                   rehearsal_with(darkroom_version="CANARY-VERSION-91bd"), rehearsal_with(immich_version="CANARY-VERSION-91bd"),
                   rehearsal_with(database="CANARY-DATABASE-c2e8"), rehearsal_with(rehearsed_on="CANARY-DAY-55a1"), ["withdraw", "CANARY-WHICH-0d4f"])
        with no_leaks(self, "CANARY-EVIDENCE-7f3a", "CANARY-VERSION-91bd", "CANARY-DATABASE-c2e8", "CANARY-DAY-55a1", "CANARY-WHICH-0d4f", database=self.engine), \
                mock.patch.dict(os.environ, self.environment, clear=True):
            codes = [run.main(["retention", *argv]) for argv in refused]
        self.assertEqual(codes, [1] * len(refused))


class StatusTests(RetentionWorld):
    def test_status_says_nothing_is_recorded_on_a_new_installation(self):
        code, out, _err = self.retention("status")
        self.assertEqual(code, 0)
        self.assertIn("Restore rehearsal: not recorded", out)
        self.assertIn("Remote-deletion boundary: not recorded", out)
        self.assertIn("Withdrawn records: 0", out)
        self.assertIn(run.RETENTION_KEPT, out)

    def test_status_shows_the_active_records_and_counts_the_withdrawn(self):
        self.retention(*rehearsal_with())
        self.retention(*rehearsal_with(evidence=OTHER_EVIDENCE, rehearsed_on="2026-11-21"))
        self.retention("accept-boundary", "--evidence", EVIDENCE, "--accept")
        self.retention("withdraw", "boundary")
        code, out, _err = self.retention("status")
        self.assertEqual(code, 0)
        self.assertIn("Restore rehearsal: recorded", out)
        self.assertIn(OTHER_EVIDENCE, out)
        self.assertNotIn("2026-11-20", out)
        self.assertIn("Remote-deletion boundary: not recorded", out)
        self.assertIn("Withdrawn records: 2", out)

    def test_withdrawing_one_leaves_the_other(self):
        self.retention(*rehearsal_with())
        self.retention("accept-boundary", "--evidence", EVIDENCE, "--accept")
        code, out, _err = self.retention("withdraw", "rehearsal")
        self.assertEqual(code, 0)
        self.assertIn("Withdrawn: Restore rehearsal (record 1)", out)
        with db.session(self.engine) as opened:
            self.assertIsNone(retention_gate.active(opened, retention_gate.REHEARSAL))
            self.assertEqual(retention_gate.active(opened, retention_gate.BOUNDARY).evidence_url, EVIDENCE)


if __name__ == "__main__":
    unittest.main()
