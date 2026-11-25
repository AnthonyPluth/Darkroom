import json
import unittest
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import delete, select, update

from darkroom.domain import managed_ingest, retention_gate
from darkroom.domain.ingest import cleanup
from darkroom.storage import db
from darkroom.storage.models import (
    DedupeDecision,
    Event,
    Export,
    ExportPart,
    GoogleAccount,
    ImportRun,
    ManagedAssetVersion,
    ManagedBackfillItem,
    ManagedCompound,
    ManagedCompoundMember,
    ManagedImportIntent,
    ManagedImportReceipt,
    MediaItem,
    Person,
    RecoveryPoint,
    RecoveryPointAsset,
    RetentionAttestation,
    ReviewItem,
    TakeoutEntry,
    TakeoutSidecar,
)
from darkroom.storage.paths import Photos
from tests import test_retention_gate as base
from tests.privacy import no_leaks

NOW = 2_000_000.0
DAY = retention_gate.DAY_SECONDS
OWNER = "retention-owner-canary"
ASSET = "retention-asset-canary"
DIGEST = "a" * 40
SCOPES = ",".join(retention_gate.SCOPES)
SIDECAR = {"photoTakenTime": {"timestamp": "1000000"}, "description": "description-canary", "geoData": {"latitude": 45.4, "longitude": 12.3}}
METADATA = {"taken": datetime.fromtimestamp(1_000_000.0, UTC).isoformat(), "zone": "UTC+01:00", "description": "description-canary", "latitude": 45.4, "longitude": 12.3}


class ProofWorld(base.RetentionWorld):
    def setUp(self):
        super().setUp()
        with db.session(self.engine, writing=True) as opened:
            opened.add(Person(id=1, name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0, immich_user_id=OWNER))
            opened.flush()
            opened.add(GoogleAccount(id=1, email="jo@example.test", default_person_id=1, created=1.0))
            opened.flush()
            opened.add(Export(id=1, account_id=1, kind="manual", status="imported"))
            opened.flush()
            opened.add_all([
                ImportRun(id=1, export_id=1, started=0, finished=NOW - 14 * DAY, status="done"),
                ManagedImportIntent(id=1, person_id=1, owner_id=OWNER, export_id=1, resource_manifest="{}", kind="import", created_at=1.0),
                ExportPart(id=1, export_id=1, number=1, filename="fixture.zip", status="done", ingest_state="ingested", entries=1),
                DedupeDecision(id=1, export_id=1, account_id=1, person_id=1, tier=5, reason="fixture", decided_by="auto", decided_at=1.0),
            ])
            opened.flush()
            opened.add(TakeoutEntry(id=1, export_id=1, part_number=1, name="generated-canary.jpg", kind="media", bytes=2, crc32=0,
                                    outcome="placed", decision_id=1, sha1=DIGEST, listed_at=1.0, held_path="held-path-canary"))

    def update(self, model, **values):
        with self.engine.begin() as connection:
            connection.execute(update(model).values(**values))

    def reason(self, *, unit=ExportPart, at=NOW, release_enabled=True):
        with db.session(self.engine) as opened:
            return retention_gate.refusal(opened, opened.get_one(unit, 1), at, release_enabled=release_enabled)

    def owner_records(self, rehearsal=True, boundary=True):
        with db.session(self.engine, writing=True) as opened:
            if rehearsal:
                checked = retention_gate.checked_rehearsal(base.EVIDENCE, "2026-11-20", "v3.4.1", "v5.9.0", "sqlite", "2026-11-24")
                retention_gate.record_rehearsal(opened, 1.0, checked)
            if boundary:
                retention_gate.accept_boundary(opened, 1.0, base.OTHER_EVIDENCE)

    def withdraw(self, which):
        with db.session(self.engine, writing=True) as opened:
            retention_gate.withdraw(opened, NOW, which)

    def receipt(self, state="committed", key="takeout-entry:1", at=3.0, hold=None, size=2, *, group=None, asset=ASSET, digest=DIGEST):
        with db.session(self.engine, writing=True) as opened:
            opened.add(ManagedImportReceipt(intent_id=1, resource_key=key, compound_key=group or key, source_sha1=digest, expected_bytes=size,
                                            expected_metadata="{}", state=state, asset_id=asset, upload_outcome="created", verified_sha1=digest,
                                            verified_bytes=size, bytes_verified_at=2.0, metadata_evidence="{}", metadata_verified_at=2.0,
                                            committed_at=at if state == "committed" else None, metadata_hold=hold, updated_at=at))

    def point(self, rows=((OWNER, ASSET, DIGEST),), *, state="succeeded", opened_at=NOW - DAY, taken_at=NOW - DAY, kept_until=NOW + DAY,
              scopes=SCOPES):
        with db.session(self.engine, writing=True) as opened:
            point = RecoveryPoint(state=state, opened_at=opened_at, manifest_count=len(rows), manifest_sha256=retention_gate.manifest_sha256(rows),
                                  taken_at=taken_at if state == "succeeded" else None, kept_until=kept_until if state == "succeeded" else None,
                                  finished_at=None if state == "open" else opened_at + 60.0, scopes=scopes if state == "succeeded" else None,
                                  failure="reported_failed" if state in ("failed", "abandoned") else None)
            opened.add(point)
            opened.flush()
            opened.add_all(RecoveryPointAsset(recovery_point_id=point.id, owner_id=owner, asset_id=asset, sha1=digest, source="receipt") for owner, asset, digest in rows)

    def keeper(self, *, owner=OWNER, state="bytes_verified", mapped=True, snapshot=None):
        with db.session(self.engine, writing=True) as opened:
            opened.add(MediaItem(id=1, person_id=1, source="phone", path="keeper-path-canary", sha1=DIGEST, bytes=2, indexed_at=1.0))
            opened.flush()
            found = opened.get_one(DedupeDecision, 1)
            found.tier, found.matched_item_id = 1, 1
            opened.get_one(TakeoutEntry, 1).outcome = "skipped"
            if mapped:
                opened.add(ManagedAssetVersion(id=1, person_id=1, owner_id=owner, asset_id=ASSET, sha1=DIGEST, bytes=2, state=state, kind="IMAGE",
                                               metadata_snapshot=json.dumps(METADATA) if snapshot is None else snapshot, seen_at=1.0, bytes_verified_at=2.0))
                opened.flush()
                opened.add(ManagedBackfillItem(id=1, person_id=1, item_id=1, outcome="mapped", version_id=1, checked_at=2.0, since=2.0))

    def google(self, raw):
        with db.session(self.engine, writing=True) as opened:
            opened.add(TakeoutEntry(id=2, export_id=1, part_number=1, name="sidecar-canary.json", kind="sidecar", bytes=1, crc32=0, listed_at=1.0))
            opened.flush()
            opened.add(TakeoutSidecar(export_id=1, part_number=1, entry_id=2, folder="", name="sidecar-canary.json", text=raw))
            opened.get_one(ExportPart, 1).entries = 2
            opened.get_one(TakeoutEntry, 1).sidecar_entry_id = 2

    def review(self, status="pending", hold="review-path-canary"):
        with db.session(self.engine, writing=True) as opened:
            opened.get_one(TakeoutEntry, 1).outcome = "review"
            opened.get_one(DedupeDecision, 1).tier = 4
            opened.add(ReviewItem(id=1, decision_id=1, status=status, hold_sha1=DIGEST, hold_path=hold,
                                  resolved_at=None if status == "pending" else NOW - 14 * DAY))


class AttestationAndReceiptTests(ProofWorld):
    def test_age_and_latest_run_are_checked_before_the_release_setting(self):
        self.assertEqual(self.reason(at=NOW - 1, release_enabled=False), "retention_clock")
        self.assertEqual(self.reason(release_enabled=False), "release_off")
        self.assertEqual(self.reason(), "no_rehearsal")
        with db.session(self.engine, writing=True) as opened:
            opened.add(ImportRun(id=2, export_id=1, started=NOW - 60.0, status="running"))
        self.assertEqual(self.reason(), "retention_clock")
        self.update(Export, status="failed")
        self.assertEqual(self.reason(), "retention_clock")

    def test_withdrawing_a_rehearsal_refuses_before_boundary_or_receipts(self):
        self.owner_records()
        self.withdraw("rehearsal")
        self.assertEqual(self.reason(), "no_rehearsal")

    def test_absent_withdrawn_or_outdated_boundary_cannot_count(self):
        self.owner_records(boundary=False)
        self.assertEqual(self.reason(), "boundary_not_accepted")
        self.owner_records(rehearsal=False)
        with self.engine.begin() as connection:
            connection.execute(update(RetentionAttestation).where(RetentionAttestation.kind == retention_gate.BOUNDARY).values(boundary_text_sha256="0" * 64))
        self.assertEqual(self.reason(), "boundary_not_accepted")
        self.owner_records(rehearsal=False)
        self.withdraw("boundary")
        self.assertEqual(self.reason(), "boundary_not_accepted")

    def test_a_missing_receipt_and_a_metadata_verified_receipt_keep_both_files(self):
        self.owner_records()
        for found in (None, "metadata_verified"):
            if found is not None:
                self.receipt(state=found)
            self.assertEqual(self.reason(), "receipt_not_committed")
            self.assertEqual(self.reason(unit=TakeoutEntry), "receipt_not_committed")

    def test_a_held_receipt_and_a_receipt_for_other_bytes_are_refused(self):
        self.owner_records()
        self.receipt(hold="not_enriched")
        self.assertEqual(self.reason(), "receipt_not_committed")
        self.update(ManagedImportReceipt, metadata_hold=None)
        self.update(TakeoutEntry, bytes=10)
        self.assertEqual(self.reason(), "receipt_not_committed")

    def test_an_import_intent_whose_owner_changed_cannot_prove_custody(self):
        self.owner_records()
        self.receipt()
        self.point()
        self.update(Person, immich_user_id="another-owner")
        self.assertEqual(self.reason(), "keeper_not_managed")


class DependencyTests(ProofWorld):
    def setUp(self):
        super().setUp()
        self.owner_records()
        self.point()

    def test_failed_unknown_or_missing_ledger_entries_keep_the_zip(self):
        for outcome in ("failed", "listed", "waiting_sidecar", "waiting_export"):
            self.update(TakeoutEntry, outcome=outcome)
            self.assertEqual(self.reason(), "keeper_not_managed")
        self.update(ExportPart, entries=2)
        self.assertEqual(self.reason(), "keeper_not_managed")

    def test_a_tier_1_keeper_in_an_old_folder_pins_the_zip(self):
        self.keeper(mapped=False)
        self.assertEqual(self.reason(), "keeper_not_managed")

    def test_a_keeper_version_of_another_owner_or_wrong_checksum_pins_the_zip(self):
        self.keeper(owner="other-owner")
        self.assertEqual(self.reason(), "keeper_not_managed")
        self.update(ManagedAssetVersion, owner_id=OWNER, sha1="2" * 40)
        self.assertEqual(self.reason(), "keeper_not_managed")

    def test_an_unverified_keeper_version_pins_the_zip(self):
        self.keeper(state="observed")
        self.assertEqual(self.reason(), "keeper_not_managed")

    def test_a_keeper_without_all_google_values_keeps_the_zip(self):
        self.keeper()
        self.google(json.dumps(SIDECAR))
        self.assertIsNone(self.reason())
        for field in ("latitude", "longitude", "taken", "zone", "description"):
            with self.subTest(field=field):
                self.update(ManagedAssetVersion, metadata_snapshot=json.dumps(METADATA | {field: None}))
                self.assertEqual(self.reason(), "metadata_not_in_immich")
        for snapshot in ("broken", "[]", "{}"):
            self.update(ManagedAssetVersion, metadata_snapshot=snapshot)
            self.assertEqual(self.reason(), "metadata_not_in_immich")
        self.update(TakeoutSidecar, text="broken")
        self.assertEqual(self.reason(), "metadata_not_in_immich")

    def test_a_keeper_without_google_metadata_can_pass_and_a_missing_sidecar_cannot(self):
        self.keeper(snapshot="{}")
        self.assertIsNone(self.reason())
        self.google(json.dumps({"title": "generated"}))
        self.assertIsNone(self.reason())
        with self.engine.begin() as connection:
            connection.execute(delete(TakeoutSidecar))
        self.assertEqual(self.reason(), "metadata_not_in_immich")

    def test_a_copy_requires_the_matching_root_and_uses_its_receipt(self):
        with db.session(self.engine, writing=True) as opened:
            opened.get_one(ExportPart, 1).entries = 2
            opened.add(TakeoutEntry(id=2, export_id=1, part_number=1, name="copy.jpg", kind="media", bytes=2, crc32=0,
                                    outcome="copy", copy_of_id=1, sha1=DIGEST, listed_at=1.0))
        self.assertEqual(self.reason(), "receipt_not_committed")
        self.receipt()
        self.assertIsNone(self.reason())
        with self.engine.begin() as connection:
            connection.execute(update(TakeoutEntry).where(TakeoutEntry.id == 2).values(sha1="b" * 40))
        self.assertEqual(self.reason(), "keeper_not_managed")

    def test_a_live_photo_part_needs_coverage_for_both_members(self):
        group, video_asset, video_digest = "live-pair", "video-asset", "b" * 40
        self.receipt(group=group)
        self.receipt(key="takeout-entry:2", group=group, asset=video_asset, digest=video_digest)
        self.assertEqual(self.reason(), "receipt_not_committed")
        with db.session(self.engine, writing=True) as opened:
            opened.add(ManagedCompound(id=1, intent_id=1, compound_key=group, kind="live_photo", frozen_at=1.0, committed_at=3.0))
            opened.flush()
            opened.add_all([
                ManagedCompoundMember(compound_id=1, intent_id=1, resource_key="takeout-entry:1", role="live_still", source_sha1=DIGEST, expected_bytes=2),
                ManagedCompoundMember(compound_id=1, intent_id=1, resource_key="takeout-entry:2", role="live_video", source_sha1=video_digest, expected_bytes=2),
            ])
        self.assertEqual(self.reason(), "not_covered")
        self.point(((OWNER, ASSET, DIGEST), (OWNER, video_asset, video_digest)))
        self.assertIsNone(self.reason())

    def test_pending_review_needs_a_hold_on_record_and_is_not_a_release_candidate(self):
        self.review()
        self.assertIsNone(self.reason())
        self.assertEqual(self.reason(unit=ReviewItem), "retention_clock")
        self.update(ReviewItem, hold_path=None)
        self.assertEqual(self.reason(), "keeper_not_managed")

    def test_a_kept_review_hold_requires_its_committed_receipt(self):
        self.review(status="kept_both")
        self.assertEqual(self.reason(unit=ReviewItem), "receipt_not_committed")
        self.receipt(key="review:1")
        self.assertIsNone(self.reason(unit=ReviewItem))
        self.assertIsNone(self.reason())

    def test_a_resolved_review_for_the_other_copy_needs_the_keeper(self):
        self.keeper()
        self.review(status="skipped")
        self.assertIsNone(self.reason(unit=ReviewItem))
        self.update(DedupeDecision, reversed_at=1.0)
        self.assertEqual(self.reason(unit=ReviewItem), "keeper_not_managed")

    def test_a_recorded_owner_skip_needs_no_asset(self):
        self.update(TakeoutEntry, outcome="skipped")
        self.update(DedupeDecision, decided_by="owner")
        self.assertIsNone(self.reason())


class PointTests(ProofWorld):
    def setUp(self):
        super().setUp()
        self.owner_records()
        self.receipt()

    def test_zero_eligible_points_and_points_outside_the_window_keep_files(self):
        self.assertEqual(self.reason(), "no_recovery_point")
        self.point()
        self.update(RecoveryPoint, taken_at=NOW - 15 * DAY, opened_at=NOW - 15 * DAY, finished_at=NOW - 15 * DAY + 60.0)
        self.assertEqual(self.reason(), "no_recovery_point")
        self.update(RecoveryPoint, taken_at=NOW - 14 * DAY, finished_at=NOW - 13 * DAY)
        self.assertIsNone(self.reason())
        self.update(RecoveryPoint, kept_until=NOW)
        self.assertEqual(self.reason(), "no_recovery_point")

    def test_every_scope_is_required(self):
        self.point()
        for missing in retention_gate.SCOPES:
            with self.subTest(missing=missing):
                self.update(RecoveryPoint, scopes=",".join(scope for scope in retention_gate.SCOPES if scope != missing))
                self.assertEqual(self.reason(), "no_recovery_point")

    def test_a_failed_or_abandoned_latest_attempt_overrides_an_older_success(self):
        self.point()
        self.point(state="failed", opened_at=NOW - 100)
        self.assertEqual(self.reason(), "backup_failed")
        with self.engine.begin() as connection:
            connection.execute(update(RecoveryPoint).where(RecoveryPoint.id == 2).values(state="abandoned"))
        self.assertEqual(self.reason(), "backup_failed")
        self.point(opened_at=NOW - 30, taken_at=NOW - 30)
        self.assertIsNone(self.reason())

    def test_a_stale_open_point_refuses_before_abandonment(self):
        self.point(opened_at=NOW - 2 * DAY, taken_at=NOW - 2 * DAY)
        self.point(state="open", opened_at=NOW - retention_gate.OPEN_LIMIT)
        self.update(ImportRun, finished=NOW - 15 * DAY)
        self.assertEqual(self.reason(at=NOW - 1), None)
        self.assertEqual(self.reason(), "backup_failed")

    def test_coverage_requires_the_same_owner_asset_and_checksum_in_an_intact_manifest(self):
        for rows in ((), (("wrong-owner", ASSET, DIGEST),), ((OWNER, "wrong-asset", DIGEST),), ((OWNER, ASSET, "b" * 40),)):
            with self.subTest(rows=rows):
                self.point(rows)
                self.assertEqual(self.reason(), "not_covered")
        self.point()
        self.assertIsNone(self.reason())
        self.update(RecoveryPoint, manifest_sha256="0" * 64)
        self.assertEqual(self.reason(), "not_covered")

    def test_an_asset_committed_after_begin_cannot_be_covered_by_it(self):
        self.point()
        self.update(ManagedImportReceipt, committed_at=NOW - DAY + 1)
        self.assertEqual(self.reason(), "not_covered")

    def test_status_and_cleanup_use_the_current_refusal_and_only_safe_counts(self):
        self.withdraw("boundary")
        self.point()
        photos = Photos(Path(self.environment["DARKROOM_PHOTOS_ROOT"]))
        with db.session(self.engine, writing=True) as opened:
            self.assertEqual(retention_gate.export_refusals(opened, 1, NOW), Counter({"boundary_not_accepted": 2}))
            report = cleanup.CleanupReport()
            cleanup.clean_export(opened, photos, opened.get_one(Export, 1), NOW, report)
            cleanup.clean_export(opened, photos, opened.get_one(Export, 1), NOW, report)
            self.assertEqual((report.kept, report.parts, report.holds), (2, 0, 0))
            statuses = [event.detail for event in opened.execute(select(Event).where(Event.type == "cleanup_kept")).scalars()]
            self.assertEqual(statuses, ["boundary_not_accepted"])
        self.owner_records(rehearsal=False)
        with db.session(self.engine, writing=True) as opened:
            cleanup.clean_export(opened, photos, opened.get_one(Export, 1), NOW, cleanup.CleanupReport())
            statuses = [event.detail for event in opened.execute(select(Event).where(Event.type == "cleanup_kept").order_by(Event.id)).scalars()]
            self.assertEqual(statuses, ["boundary_not_accepted", "release_off"])
            self.assertEqual(retention_gate.recovery_status(opened, NOW), ["Latest recovery points: 1 (succeeded)", "Managed files waiting:", "  release_off: 2"])
        with no_leaks(self, OWNER, ASSET, DIGEST, "held-path-canary", "generated-canary.jpg"), \
                mock.patch("run.utc_now", return_value=datetime.fromtimestamp(NOW, UTC)):
            code, out, err = self.retention("status")
            self.assertEqual((code, err), (0, ""))
            self.assertIn("  release_off: 2", out)
            print(out)

    def test_no_rehearsal_keeps_the_existing_cleanup_message(self):
        self.withdraw("rehearsal")
        with db.session(self.engine) as opened:
            self.assertEqual(retention_gate.cleanup_detail(opened, 1, NOW), managed_ingest.KEPT_ONLY)


if __name__ == "__main__":
    unittest.main()
