import contextlib
import io
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import delete, select, update

import run
from darkroom.domain import alerts, retention_gate
from darkroom.domain.ingest import cleanup, part_free
from darkroom.storage import db
from darkroom.storage.models import Alert, Event, Export, ImportRun, ManagedImportIntent, RecoveryPoint, RecoveryPointAsset
from tests import test_recovery_points as base
from tests.import_world import tree
from tests.privacy import no_leaks
from tests.test_managed_ingest import ManagedWorld

NOW = base.NOW
DAY = retention_gate.DAY_SECONDS
OPENED = NOW - 7200.0
POINT_OPENED = datetime(2026, 11, 24, 3, 15, tzinfo=UTC).timestamp() - 3600.0
TAKEN = "2026-11-24T03:15:00Z"
TAKEN_AT = datetime(2026, 11, 24, 3, 15, tzinfo=UTC).timestamp()
FINISH_AT = TAKEN_AT + 600.0
ALL = ",".join(retention_gate.SCOPES)
TOOL = "canary-backup-tool-7c1"
SNAPSHOT_REF = "snap:canary-ref-9f3a1"


def stamp(at):
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def report(**changed):
    values = {"status": "succeeded", "taken_at": TAKEN, "kept_for_days": "30", "scopes": ALL, "tool": "kopia", "snapshot_ref": "snap:abc.1"} | changed
    return retention_gate.checked_report(values["status"], values["taken_at"], values["kept_for_days"], values["scopes"], values["tool"], values["snapshot_ref"])


class FinishWorld(base.RecoveryWorld):
    def open_point(self, opened_at=POINT_OPENED, assets=1):
        for number in range(1, assets + 1):
            self.stored(number)
            self.receipt(number, committed_at=opened_at - 10.0)
        return self.begin(opened_at).point_id

    def finish(self, point, now=FINISH_AT, **changed):
        with db.session(self.engine) as opened:
            return retention_gate.finish(opened, now, point, report(**changed))

    def succeed(self, point, now, **changed):
        return self.finish(point, now, taken_at=stamp(now - 30.0), **changed)

    def fail(self, point, now):
        return self.finish(point, now, status="failed", taken_at=None, kept_for_days=None, scopes=None)

    def row(self, point=1):
        with db.session(self.engine) as opened:
            found = opened.get(RecoveryPoint, point)
            return (found.state, found.failure, found.finished_at, found.taken_at, found.kept_until, found.scopes, found.tool, found.snapshot_ref)

    def watch(self, now, notifier=None):
        with db.session(self.engine, writing=True) as opened:
            return retention_gate.watch(opened, self.settings, now, notifier or (lambda *_: alerts.SENT))

    def alert_rows(self):
        with db.session(self.engine) as opened:
            return [(row.key, row.kind, row.message, row.closed is None, row.person_id, row.account_id)
                    for row in opened.execute(select(Alert).order_by(Alert.id)).scalars()]

    def points_before(self):
        with db.session(self.engine) as opened:
            return [(row.id, row.state, row.failure, row.finished_at, row.taken_at, row.kept_until, row.scopes, row.tool, row.snapshot_ref)
                    for row in opened.execute(select(RecoveryPoint).order_by(RecoveryPoint.id)).scalars()]

    def assert_refused(self, reason, point=1, now=FINISH_AT, **changed):
        before = self.points_before()
        events = self.events()
        with self.assertRaises(retention_gate.Refused) as caught:
            self.finish(point, now, **changed)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual((self.points_before(), self.events()), (before, events))

    def import_waiting(self, finished, kind="import", status="imported", run_status="done"):
        with db.session(self.engine, writing=True) as opened:
            opened.execute(delete(ImportRun))
            opened.execute(update(Export).where(Export.id == 1).values(status=status))
            opened.execute(update(ManagedImportIntent).values(kind=kind))
            opened.add(ImportRun(export_id=1, started=finished - 60, finished=finished, status=run_status))


class FinishSucceededTests(FinishWorld):
    def test_a_succeeded_report_stores_the_times_scopes_tool_and_snapshot_and_changes_nothing_else(self):
        point = self.open_point()
        manifest, events = self.manifest(point), self.events()
        done = self.finish(point)
        self.assertEqual(done, retention_gate.Finished(point, "succeeded", None, TAKEN_AT + 30 * DAY))
        self.assertEqual(self.row(point), ("succeeded", None, FINISH_AT, TAKEN_AT, TAKEN_AT + 30 * DAY, ALL, "kopia", "snap:abc.1"))
        self.assertEqual(self.manifest(point), manifest)
        self.assertEqual(self.events()[len(events):], [("recovery_point_finished", f"recovery point {point}", "succeeded")])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(RecoveryPoint, point).manifest_count, 1)

    def test_scopes_are_stored_sorted_and_without_repeats(self):
        point = self.open_point()
        self.finish(point, scopes="immich-media,darkroom-db,immich-db,darkroom-secret,immich-db")
        self.assertEqual(self.row(point)[5], ALL)

    def test_a_time_with_an_offset_is_stored_as_the_utc_instant(self):
        point = self.open_point()
        self.finish(point, taken_at="2026-11-24T05:15:00+02:00")
        self.assertEqual(self.row(point)[3], TAKEN_AT)

    def test_a_time_equal_to_the_opening_and_one_equal_to_now_are_accepted(self):
        point = self.open_point(opened_at=TAKEN_AT)
        self.finish(point, now=TAKEN_AT)
        self.assertEqual(self.row(point)[:2], ("succeeded", None))

    def test_the_snapshot_and_tool_are_optional_for_a_failed_report_only(self):
        point = self.open_point()
        self.finish(point, status="failed", taken_at=None, kept_for_days=None, scopes=None, tool=None, snapshot_ref=None)
        self.assertEqual(self.row(point), ("failed", "reported_failed", FINISH_AT, None, None, None, None, None))

    def test_a_snapshot_reference_is_optional_for_a_succeeded_report(self):
        point = self.open_point()
        self.finish(point, snapshot_ref=None)
        self.assertEqual(self.row(point)[:2], ("succeeded", None))


class FinishFailedTests(FinishWorld):
    def test_a_failed_report_is_stored_as_reported_failed_and_never_counts(self):
        point = self.open_point()
        done = self.finish(point, status="failed")
        self.assertEqual((done.state, done.failure, done.kept_until), ("failed", "reported_failed", None))
        self.assertEqual(self.row(point)[2:5], (FINISH_AT, None, None))

    def test_a_succeeded_report_missing_any_one_scope_is_stored_as_failed_with_scopes_missing(self):
        for missing in retention_gate.SCOPES:
            with self.subTest(missing=missing):
                point = self.begin(POINT_OPENED).point_id
                kept = ",".join(scope for scope in retention_gate.SCOPES if scope != missing)
                done = self.finish(point, scopes=kept)
                self.assertEqual((done.state, done.failure, done.kept_until), ("failed", "scopes_missing", None))
                self.assertEqual(self.row(point)[3:6], (None, None, kept))

    def test_a_succeeded_report_with_no_scopes_is_stored_as_failed_with_scopes_missing(self):
        point = self.begin(POINT_OPENED).point_id
        self.assertEqual(self.finish(point, scopes="").failure, "scopes_missing")
        self.assertEqual(self.row(point)[5], None)


class FinishRefusalTests(FinishWorld):
    def setUp(self):
        super().setUp()
        self.open_point()

    def test_a_point_that_does_not_exist_is_refused(self):
        self.assert_refused("point_unknown", point=2)

    def test_a_point_number_that_is_not_a_plain_number_is_refused(self):
        for text in ("", "0", "01", "-1", "1.5", "abc", "1 ", " 1", "١", "9" * 30):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                retention_gate.point_number(text)
            self.assertEqual(caught.exception.reason, "point_unknown")

    def test_a_point_that_is_not_open_is_never_finished_again(self):
        self.finish(1)
        for status in ("succeeded", "failed"):
            with self.subTest(status=status):
                self.assert_refused("point_not_open", status=status)
        self.assertEqual(self.row()[:2], ("succeeded", None))

    def test_a_failed_point_is_not_turned_into_a_success(self):
        self.finish(1, status="failed")
        self.assert_refused("point_not_open")
        self.assertEqual(self.row()[:2], ("failed", "reported_failed"))

    def test_an_abandoned_point_is_not_revived_by_a_late_success(self):
        self.watch(POINT_OPENED + retention_gate.OPEN_LIMIT)
        self.assertEqual(self.row()[:2], ("abandoned", "not_finished"))
        self.assert_refused("point_not_open")
        self.assertEqual(self.row()[:2], ("abandoned", "not_finished"))

    def test_a_point_open_longer_than_the_limit_is_refused_before_a_tick_abandons_it(self):
        self.assert_refused("point_expired", now=POINT_OPENED + retention_gate.OPEN_LIMIT, taken_at=TAKEN)
        self.assertEqual(self.row()[0], "open")

    def test_a_time_before_the_point_opened_is_refused(self):
        self.assert_refused("taken_before_opened", taken_at="2026-11-24T02:14:59Z")

    def test_a_time_after_now_is_refused(self):
        self.assert_refused("taken_in_future", now=TAKEN_AT - 1.0, taken_at=TAKEN)

    def test_a_time_that_is_not_a_utc_time_with_an_offset_is_refused(self):
        for text in ("2026-11-24T03:15:00", "2026-11-24", "yesterday", "", "2026-13-40T00:00:00Z", "2026-11-24T03:15:00Z" + " " * 40, "9999-12-31T23:59:59-23:59"):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                report(taken_at=text)
            self.assertEqual(caught.exception.reason, "taken_at")

    def test_kept_for_days_must_be_a_whole_number_in_range(self):
        for text in ("0", "-1", "1.5", "abc", "", "3651", "030", "1e3"):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                report(kept_for_days=text)
            self.assertEqual(caught.exception.reason, "kept_for_days")
        self.assertEqual(report(kept_for_days="3650").kept_for_days, 3650)

    def test_an_unknown_or_badly_written_scope_is_refused(self):
        for text in ("everything", "immich-db,other", "immich-db, immich-media", "immich-db,", ",immich-db", "IMMICH-DB", "immich-db\\n"):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                report(scopes=text)
            self.assertEqual(caught.exception.reason, "scopes")

    def test_a_malformed_tool_label_is_refused(self):
        for text in ("", "Kopia", "a" * 33, "a/b", "a b", "kopia\\n", "ké"):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                report(tool=text)
            self.assertEqual(caught.exception.reason, "tool")
        self.assertEqual(report(tool="a" * 32).tool, "a" * 32)

    def test_a_snapshot_reference_that_is_not_a_plain_identifier_is_refused(self):
        for text in ("", "/mnt/user/backup/snap", "a b", "a" * 129, "snap\\n", "../x", "a;b"):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                report(snapshot_ref=text)
            self.assertEqual(caught.exception.reason, "snapshot")
        self.assertEqual(report(snapshot_ref="a" * 128).snapshot_ref, "a" * 128)

    def test_a_status_other_than_succeeded_or_failed_is_refused(self):
        for text in ("ok", "SUCCEEDED", "", "open", "abandoned"):
            with self.subTest(text=text), self.assertRaises(retention_gate.Refused) as caught:
                report(status=text)
            self.assertEqual(caught.exception.reason, "status")

    def test_a_succeeded_report_must_carry_each_of_its_fields(self):
        for name in ("taken_at", "kept_for_days", "scopes", "tool"):
            with self.subTest(name=name), self.assertRaises(retention_gate.Refused) as caught:
                report(**{name: None})
            self.assertEqual(caught.exception.reason, "succeeded_needs")

    def test_a_manifest_that_changed_since_the_point_opened_is_refused_in_each_way(self):
        changes = {
            "a row removed": lambda opened: opened.execute(delete(RecoveryPointAsset)),
            "a row's checksum changed": lambda opened: opened.execute(update(RecoveryPointAsset).values(sha1="0" * 40)),
            "a row's asset changed": lambda opened: opened.execute(update(RecoveryPointAsset).values(asset_id="other")),
            "a row's owner changed": lambda opened: opened.execute(update(RecoveryPointAsset).values(owner_id="other")),
            "a row added": lambda opened: opened.add(RecoveryPointAsset(recovery_point_id=1, owner_id=base.SAM, asset_id=base.asset_id(9), sha1="1" * 40, source="receipt")),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                with db.session(self.engine, writing=True) as opened:
                    change(opened)
                self.assert_refused("manifest_changed")
                with db.session(self.engine, writing=True) as opened:
                    opened.execute(delete(RecoveryPointAsset))
                    opened.add(RecoveryPointAsset(recovery_point_id=1, owner_id=base.JO, asset_id=base.asset_id(1), sha1=base.sha1_of(1), source="receipt"))
        self.assertEqual(self.finish(1).state, "succeeded")

    def test_a_refused_report_for_a_failed_status_is_still_checked_for_its_fields(self):
        self.assert_refused("tool", status="failed", tool="Bad Label")
        self.assert_refused("point_unknown", point=7, status="failed")


class AbandonTests(FinishWorld):
    def test_an_open_point_older_than_the_limit_is_abandoned_with_not_finished(self):
        point = self.open_point(OPENED)
        self.assertEqual(self.watch(OPENED + retention_gate.OPEN_LIMIT).abandoned, 1)
        self.assertEqual(self.row(point), ("abandoned", "not_finished", OPENED + retention_gate.OPEN_LIMIT, None, None, None, None, None))
        self.assertIn(("recovery_point_abandoned", f"recovery point {point}", "not_finished"), self.events())

    def test_a_point_younger_than_the_limit_stays_open_and_its_manifest_is_untouched(self):
        point = self.open_point(OPENED)
        manifest = self.manifest(point)
        self.assertEqual(self.watch(OPENED + retention_gate.OPEN_LIMIT - 1).abandoned, 0)
        self.assertEqual(self.row(point)[:2], ("open", None))
        self.assertEqual(self.manifest(point), manifest)

    def test_abandoning_keeps_the_point_and_its_manifest_for_the_audit(self):
        point = self.open_point(OPENED)
        self.watch(OPENED + 2 * retention_gate.OPEN_LIMIT)
        self.assertEqual(len(self.points_before()), 1)
        self.assertEqual(len(self.manifest(point)), 1)

    def test_a_point_already_finished_or_failed_is_not_touched_and_nothing_is_abandoned_twice(self):
        done, failed = self.open_point(OPENED), self.begin(OPENED + 1).point_id
        self.succeed(done, OPENED + 3000.0)
        self.fail(failed, OPENED + 3000.0)
        before = self.points_before()
        self.assertEqual(self.watch(OPENED + 10 * retention_gate.OPEN_LIMIT).abandoned, 0)
        self.assertEqual(self.points_before(), before)
        point = self.begin(OPENED + 5).point_id
        self.assertEqual(self.watch(OPENED + 5 + retention_gate.OPEN_LIMIT).abandoned, 1)
        self.assertEqual(self.watch(OPENED + 6 + retention_gate.OPEN_LIMIT).abandoned, 0)
        self.assertEqual(self.row(point)[2], OPENED + 5 + retention_gate.OPEN_LIMIT)

    def test_a_failed_begin_is_not_abandoned_or_changed(self):
        self.stored(1)
        self.receipt(1, committed_at=OPENED - 10.0)
        self.fake.mode("down")
        point = self.begin(OPENED).point_id
        self.watch(OPENED + 5 * retention_gate.OPEN_LIMIT)
        self.assertEqual(self.row(point)[:2], ("failed", "immich_unreachable"))


class BackupFailedAlertTests(FinishWorld):
    FAILED = ("retention:backup_failed", "backup_failed", retention_gate.FAILED_MESSAGE)

    def failed_rows(self, alive=True):
        return [row[:3] for row in self.alert_rows() if row[0] == "retention:backup_failed" and row[3] == alive]

    def test_a_failed_report_opens_the_alert_once_however_often_the_tick_runs(self):
        point = self.begin(OPENED).point_id
        self.fail(point, OPENED + 600.0)
        for step in range(3):
            self.watch(OPENED + 700.0 + step)
        self.assertEqual(self.failed_rows(), [self.FAILED])
        self.assertEqual(self.alert_rows()[0][4:], (None, None))

    def test_an_abandoned_point_opens_it(self):
        self.begin(OPENED)
        self.assertTrue(self.watch(OPENED + retention_gate.OPEN_LIMIT).backup_failed)
        self.assertEqual(self.failed_rows(), [self.FAILED])

    def test_a_point_reported_without_all_four_scopes_opens_it(self):
        point = self.begin(OPENED).point_id
        self.succeed(point, OPENED + 600.0, scopes="immich-db,immich-media")
        self.assertTrue(self.watch(OPENED + 700.0).backup_failed)

    def test_a_begin_that_could_not_reach_immich_after_an_older_success_opens_it(self):
        old = self.open_point(OPENED - 10 * DAY)
        self.succeed(old, OPENED - 10 * DAY + 600.0)
        self.assertFalse(self.watch(OPENED - 10 * DAY + 700.0).backup_failed)
        self.stored(2)
        self.receipt(2, committed_at=OPENED - 10.0)
        self.fake.mode("down")
        self.assertEqual(self.begin(OPENED).state, "failed")
        self.assertTrue(self.watch(OPENED + 5.0).backup_failed)
        self.assertEqual(self.failed_rows(), [self.FAILED])

    def test_the_latest_failure_counts_even_with_an_older_success(self):
        old = self.begin(OPENED - 3 * DAY).point_id
        self.succeed(old, OPENED - 3 * DAY + 600.0)
        newer = self.begin(OPENED).point_id
        self.fail(newer, OPENED + 600.0)
        self.assertTrue(self.watch(OPENED + 700.0).backup_failed)

    def test_a_point_still_running_does_not_hide_an_earlier_failure(self):
        point = self.begin(OPENED - 3 * DAY).point_id
        self.fail(point, OPENED - 3 * DAY + 600.0)
        self.begin(OPENED)
        self.assertTrue(self.watch(OPENED + 60.0).backup_failed)
        self.assertEqual(self.points_before()[1][1], "open")

    def test_a_running_point_after_a_success_opens_no_alert(self):
        point = self.begin(OPENED - 3 * DAY).point_id
        self.succeed(point, OPENED - 3 * DAY + 600.0)
        self.begin(OPENED)
        self.assertFalse(self.watch(OPENED + 60.0).backup_failed)
        self.assertEqual(self.alert_rows(), [])

    def test_a_later_success_closes_it_and_a_later_failure_opens_it_again(self):
        failed = self.begin(OPENED).point_id
        self.fail(failed, OPENED + 600.0)
        self.watch(OPENED + 700.0)
        good = self.begin(OPENED + 3600.0).point_id
        self.succeed(good, OPENED + 4200.0)
        self.assertFalse(self.watch(OPENED + 4300.0).backup_failed)
        self.assertEqual((self.failed_rows(True), len(self.failed_rows(False))), ([], 1))
        worse = self.begin(OPENED + 7200.0).point_id
        self.fail(worse, OPENED + 7800.0)
        self.watch(OPENED + 7900.0)
        self.assertEqual((len(self.failed_rows(True)), len(self.failed_rows(False))), (1, 1))

    def test_no_point_at_all_opens_nothing(self):
        self.assertEqual(self.watch(NOW), retention_gate.Watched(0, False, False))
        self.assertEqual(self.alert_rows(), [])

    def test_the_alert_says_nothing_about_names_paths_or_assets(self):
        self.stored(1, name="IMG_PRIVATE_CANARY.JPG")
        self.receipt(1, committed_at=OPENED - 10.0)
        point = self.begin(OPENED).point_id
        self.finish(point, OPENED + 600.0, status="failed", tool=TOOL, snapshot_ref=SNAPSHOT_REF, taken_at=None, kept_for_days=None, scopes=None)
        self.watch(OPENED + 700.0)
        message = self.alert_rows()[0][2]
        for private in (TOOL, SNAPSHOT_REF, "IMG_PRIVATE_CANARY", base.asset_id(1), base.JO, "Jo ", " jo ", "jo@"):
            self.assertNotIn(private, message)
        self.assertEqual(message, "The last Immich backup Darkroom was told about did not succeed, so Darkroom keeps every Immich import's files in the inbox. Check the backup job.")


class BackupStaleAlertTests(FinishWorld):
    CANDIDATE = NOW - 20 * DAY

    def setUp(self):
        super().setUp()
        self.import_waiting(self.CANDIDATE)
        self.waiting_since = self.CANDIDATE + retention_gate.WINDOW

    def stale_rows(self, alive=True):
        return [row for row in self.alert_rows() if row[0] == "retention:backup_stale" and row[3] == alive]

    def covering(self, taken_days_before_now=1, kept_days=30, scopes=ALL):
        taken = NOW - taken_days_before_now * DAY
        point = self.begin(taken - 60.0).point_id
        self.finish(point, taken + 60.0, taken_at=stamp(taken), kept_for_days=str(kept_days), scopes=scopes)
        return point

    def test_units_waiting_past_the_limit_with_no_covering_point_open_the_alert_once(self):
        at = self.waiting_since + retention_gate.STALE_AFTER
        self.assertEqual(self.watch(at), retention_gate.Watched(0, False, True))
        self.watch(at + 10.0)
        rows = self.stale_rows()
        self.assertEqual([row[:3] for row in rows], [("retention:backup_stale", "backup_stale", "No recent Immich backup covers the photos Darkroom is keeping, so their inbox files stay.")])
        self.assertEqual(rows[0][4:], (None, None))
        self.assertEqual(len([row for row in self.alert_rows() if row[1] == "backup_failed"]), 0)

    def test_nothing_opens_before_the_limit_has_passed(self):
        self.assertFalse(self.watch(self.waiting_since + retention_gate.STALE_AFTER - 1).backup_stale)
        self.assertEqual(self.alert_rows(), [])

    def test_a_covering_point_keeps_it_from_opening_and_closes_it_when_one_arrives(self):
        at = self.waiting_since + retention_gate.STALE_AFTER
        self.watch(at)
        self.assertEqual(len(self.stale_rows()), 1)
        self.covering()
        self.assertFalse(self.watch(NOW + 60.0).backup_stale)
        self.assertEqual((self.stale_rows(True), len(self.stale_rows(False))), ([], 1))

    def test_a_recent_point_that_lists_only_another_persons_photos_does_not_cover_and_one_that_lists_them_all_does(self):
        jo = self.stored(1, trashed=True)
        self.stored(2, base.SAM)
        self.receipt(1, committed_at=self.CANDIDATE)
        self.receipt(2, intent=2, committed_at=self.CANDIDATE)
        taken = NOW - DAY
        only_sam = self.begin(taken - 60.0).point_id
        self.finish(only_sam, taken + 60.0, taken_at=stamp(taken))
        self.assertEqual([(owner, asset) for owner, asset, *_ in self.manifest(only_sam)], [(base.SAM, base.asset_id(2))])
        self.assertTrue(self.watch(NOW).backup_stale)
        self.watch(NOW + 10.0)
        self.assertEqual((len(self.stale_rows()), len(self.stale_rows(False))), (1, 0))
        self.fake.state.assets[:] = [asset for asset in self.fake.state.assets if asset.id != jo]
        self.stored(1)
        both = self.begin(NOW - 600.0).point_id
        self.finish(both, NOW - 500.0, taken_at=stamp(NOW - 550.0))
        self.assertEqual(len(self.manifest(both)), 2)
        self.assertFalse(self.watch(NOW + 20.0).backup_stale)
        self.assertEqual((self.stale_rows(True), len(self.stale_rows(False))), ([], 1))

    def test_a_recent_point_taken_before_a_receipt_committed_does_not_cover_it(self):
        self.stored(1)
        self.receipt(1, committed_at=NOW - 50.0)
        taken = NOW - DAY
        point = self.begin(taken - 60.0).point_id
        self.finish(point, taken + 60.0, taken_at=stamp(taken))
        self.assertTrue(self.watch(NOW).backup_stale)

    def test_a_point_older_than_14_days_or_past_its_keeping_or_missing_a_scope_does_not_cover(self):
        for name, point in (("old", lambda: self.covering(taken_days_before_now=15)), ("kept for a day", lambda: self.covering(taken_days_before_now=3, kept_days=1)),
                            ("missing scope", lambda: self.covering(scopes="immich-db,immich-media,darkroom-db"))):
            with self.subTest(name=name):
                point()
                self.assertTrue(self.watch(NOW + 60.0).backup_stale)
                self.assertEqual(len(self.stale_rows()), 1)

    def test_a_point_that_is_only_open_or_failed_does_not_cover(self):
        self.begin(NOW - 100.0)
        failed = self.begin(NOW - 90.0).point_id
        self.fail(failed, NOW - 80.0)
        self.assertTrue(self.watch(NOW).backup_stale)

    def test_no_alert_without_a_managed_export_that_is_waiting(self):
        for name, kwargs in (("a backfill intent", {"kind": "backfill"}), ("an export still downloading", {"status": "downloading"}),
                             ("a run that did not finish", {"run_status": "failed"})):
            with self.subTest(name=name):
                self.import_waiting(self.CANDIDATE, **kwargs)
                self.assertFalse(self.watch(NOW + 30 * DAY).backup_stale)
                self.assertEqual(self.alert_rows(), [])

    def test_an_export_imported_less_than_14_days_ago_is_not_waiting_until_it_has_been_a_candidate_for_the_limit(self):
        self.import_waiting(NOW - 10 * DAY)
        candidate_from = NOW - 10 * DAY + retention_gate.WINDOW
        self.assertFalse(self.watch(candidate_from + retention_gate.STALE_AFTER - 1).backup_stale)
        self.assertTrue(self.watch(candidate_from + retention_gate.STALE_AFTER).backup_stale)

    def test_the_oldest_waiting_export_sets_the_clock(self):
        self.import_waiting(NOW - 10 * DAY)
        self.assertFalse(self.watch(self.waiting_since + retention_gate.STALE_AFTER).backup_stale)


class CommandTests(FinishWorld):
    def setUp(self):
        super().setUp()
        self.environment = {"DARKROOM_PHOTOS_ROOT": self.folder, "DARKROOM_DATA_DIR": self.folder, "DARKROOM_SECRET_KEY": base.SECRET, "IMMICH_URL": self.fake.url,
                            "IMMICH_ADMIN_KEY": base.ADMIN, "DATABASE_URL": self.engine.url.render_as_string(hide_password=False)}

    def command(self, *argv, now=FINISH_AT):
        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(run, "utc_now", return_value=datetime.fromtimestamp(now, UTC)), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = run.main(["recovery-point", *argv])
        return code, out.getvalue(), err.getvalue()

    def finish_args(self, point="1", **changed):
        values = {"--status": "succeeded", "--taken-at": TAKEN, "--kept-for-days": "30", "--scopes": ALL, "--tool": "kopia", "--snapshot": "snap-1"} | changed
        return ["finish", point, *[part for pair in values.items() if pair[1] is not None for part in pair]]

    def test_finish_records_a_success_and_says_until_when_it_counts(self):
        self.open_point()
        code, out, err = self.command(*self.finish_args())
        self.assertEqual((code, err.count("recovery point finished")), (0, 1))
        self.assertEqual(out, "recovery point 1 recorded as succeeded; it counts until 2026-12-24 03:15 UTC\n")
        self.assertEqual(self.row()[:2], ("succeeded", None))

    def test_finish_records_a_reported_failure_and_exits_zero(self):
        self.open_point()
        code, out, _ = self.command(*self.finish_args(**{"--status": "failed", "--taken-at": None, "--kept-for-days": None, "--scopes": None}))
        self.assertEqual(code, 0)
        self.assertIn("recorded as failed, as the backup job reported", out)
        self.assertEqual(self.row()[:2], ("failed", "reported_failed"))

    def test_finish_exits_non_zero_when_a_scope_is_missing_and_says_why(self):
        self.open_point()
        code, out, err = self.command(*self.finish_args(**{"--scopes": "immich-db"}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("did not name all of darkroom-db, darkroom-secret, immich-db, immich-media", err)
        self.assertEqual(self.row()[:2], ("failed", "scopes_missing"))

    def test_finish_refuses_each_bad_report_with_the_reason_and_records_nothing(self):
        self.open_point()
        cases = (("2", {}, "the recovery point is not a number Darkroom has recorded"), ("x", {}, "the recovery point is not a number Darkroom has recorded"),
                 ("1", {"--status": "done"}, "--status must be succeeded or failed"), ("1", {"--taken-at": "yesterday"}, "--taken-at must be a UTC time"),
                 ("1", {"--taken-at": "2026-11-24T02:14:59Z"}, "--taken-at is before the recovery point was opened"),
                 ("1", {"--kept-for-days": "0"}, "--kept-for-days must be a whole number"), ("1", {"--scopes": "nope"}, "--scopes must be a comma-separated list"),
                 ("1", {"--tool": "Bad Tool"}, "--tool must be a label"), ("1", {"--snapshot": "/mnt/user/x"}, "--snapshot must be"),
                 ("1", {"--tool": None}, "a succeeded report needs"))
        for point, changed, said in cases:
            with self.subTest(point=point, changed=changed):
                code, out, err = self.command(*self.finish_args(point, **changed))
                self.assertEqual((code, out), (1, ""))
                self.assertIn(said, err)
                self.assertIn("nothing was recorded", err)
                self.assertEqual(self.row()[:2], ("open", None))

    def test_finish_refuses_a_point_that_is_already_finished(self):
        self.open_point()
        self.assertEqual(self.command(*self.finish_args())[0], 0)
        code, out, err = self.command(*self.finish_args(**{"--status": "failed"}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is not open", err)
        self.assertEqual(self.row()[:2], ("succeeded", None))

    def test_finish_asks_to_migrate_first_when_the_table_is_missing(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.environment["DATABASE_URL"] = f"sqlite:///{other.name}/old.db"
        code, out, err = self.command(*self.finish_args())
        self.assertEqual((code, out, err.strip()), (1, "", run.RECOVERY_NOT_MIGRATED))

    def test_finish_needs_a_point_and_a_status(self):
        for argv in (["finish"], ["finish", "1"], ["finish", "--status", "failed"], ["frobnicate"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.command(*argv)

    def test_a_reported_tool_and_snapshot_are_neither_printed_nor_logged_nor_in_an_event_or_an_alert(self):
        self.stored(1, name="IMG_PRIVATE_CANARY.JPG")
        self.receipt(1)
        good = self.begin(TAKEN_AT - 3000.0).point_id
        bad = self.begin(TAKEN_AT - 2000.0).point_id
        canaries = (base.JO_KEY, base.SAM_KEY, base.SECRET, base.ADMIN, "IMG_PRIVATE_CANARY.JPG", self.fake.url, TOOL, SNAPSHOT_REF)
        with no_leaks(self, *canaries):
            self.assertEqual(self.command(*self.finish_args(str(good), **{"--tool": TOOL, "--snapshot": SNAPSHOT_REF}))[0], 0)
            self.assertEqual(self.command(*self.finish_args(str(bad), **{"--status": "failed", "--tool": TOOL, "--snapshot": SNAPSHOT_REF}))[0], 0)
            self.assertEqual(self.command(*self.finish_args(str(bad), **{"--tool": TOOL, "--snapshot": "bad snapshot"}))[0], 1)
            self.watch(NOW)
        self.assertEqual(self.row(good)[6:], (TOOL, SNAPSHOT_REF))
        with db.session(self.engine) as opened:
            texts = [f"{row.subject} {row.detail}" for row in opened.execute(select(Event)).scalars()]
            texts += [f"{row.subject} {row.message} {row.key}" for row in opened.execute(select(Alert)).scalars()]
        self.assertEqual([text for text in texts if any(canary in text for canary in canaries)], [])


class NothingIsRemovedTests(unittest.TestCase):
    def test_recording_every_record_and_a_covering_point_still_leaves_a_managed_exports_files_and_holds_in_place(self):
        world = ManagedWorld(self)
        export = world.run_managed(lambda builder: builder.photo("IMG_9001.JPG", gps=(45.4, 12.3), description="A made-up quay", part=0), parts=1)
        before = tree(world.root)
        zips = world.zips(export)
        self.assertTrue(zips)
        url = "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-7100000001"
        with db.session(world.engine, writing=True) as opened:
            retention_gate.record_rehearsal(opened, NOW, retention_gate.checked_rehearsal(url, "2026-11-20", "v3.4.1", "v5.9.0", "sqlite", "2026-11-24"))
            retention_gate.accept_boundary(opened, NOW, url)
        engine = world.engine
        begun = retention_gate.begin(engine, world.settings, NOW + 60.0, None)
        with db.session(engine) as opened:
            retention_gate.finish(opened, NOW + 3000.0, begun.point_id, report(taken_at=datetime.fromtimestamp(NOW + 120.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
        for at in (NOW + 30 * DAY, NOW + 90 * DAY):
            with db.session(engine, writing=True) as opened:
                retention_gate.watch(opened, world.settings, at, lambda *_: alerts.SENT)
            self.assertEqual(cleanup.clean(world.opened, world.photos, at).parts, 0)
            self.assertEqual(part_free.free_ready(world.opened, world.settings, at).parts, 0)
        self.assertEqual((tree(world.root), world.zips(export)), (before, zips))


class NoImmichRequestTests(FinishWorld):
    def test_the_watch_and_finish_make_no_request_of_immich(self):
        point = self.open_point()
        requests = len(self.fake.requests())
        self.finish(point)
        self.watch(NOW + 90 * DAY)
        self.assertEqual(len(self.fake.requests()), requests)


class HookExampleTests(unittest.TestCase):
    DOC = Path(__file__).resolve().parent.parent / "docs/deploy/backup-hook.md"

    def script(self):
        text = self.DOC.read_text()
        start = text.index("```sh\n#!/bin/sh") + len("```sh\n")
        return text[start:text.index("\n```", start)] + "\n"

    def run_script(self, backup_exit, begin_exit=0):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        log = root / "calls.log"
        (bin_dir / "docker").write_text(f'#!/bin/sh\nprintf "%s\\n" "docker $*" >> "{log}"\nif [ "$6" = begin ]; then echo "recovery point 7 opened: 3 photos listed, 3 confirmed"; exit {begin_exit}; fi\nexit 0\n')
        (bin_dir / "backup-tool").write_text(f'#!/bin/sh\nprintf "%s\\n" "backup-tool $*" >> "{log}"\nexit {backup_exit}\n')
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        script = root / "hook.sh"
        script.write_text(self.script())
        import subprocess
        done = subprocess.run(["/bin/sh", str(script)], env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(root)}, capture_output=True, text=True, check=False)  # nosemgrep
        return done.returncode, log.read_text().splitlines() if log.exists() else []

    def test_the_example_passes_a_good_backup_to_finish_as_succeeded_with_the_point_it_began(self):
        code, calls = self.run_script(0)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], "docker exec darkroom python run.py recovery-point begin")
        self.assertTrue(calls[1].startswith("backup-tool "))
        self.assertRegex(calls[2], r"^docker exec darkroom python run\.py recovery-point finish 7 --status succeeded --taken-at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ --kept-for-days \d+ "
                                   r"--scopes darkroom-db,darkroom-secret,immich-db,immich-media --tool [a-z0-9-]+")
        self.assertEqual(len(calls), 3)

    def test_the_example_reports_a_failed_backup_as_failed_and_exits_non_zero(self):
        code, calls = self.run_script(3)
        self.assertNotEqual(code, 0)
        self.assertRegex(calls[-1], r"recovery-point finish 7 --status failed")
        self.assertNotIn("--status succeeded", " ".join(calls))

    def test_the_example_stops_before_any_backup_when_begin_fails(self):
        code, calls = self.run_script(0, begin_exit=1)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, ["docker exec darkroom python run.py recovery-point begin"])

    def test_the_example_names_no_real_host_share_or_person(self):
        text = self.DOC.read_text().replace('"$@"', "")
        for private in ("192.168.", "10.0.", "/mnt/user", ".local", "@"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
