import base64
import json
from dataclasses import asdict, replace
from pathlib import Path
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.domain import alerts, retention_gate
from darkroom.domain.ingest import part_free, part_reader, unpack
from darkroom.providers import immich
from darkroom.storage import db, paths
from darkroom.storage.models import Alert, DedupeDecision, Event, ManagedAssetVersion, ManagedBackfillItem, ManagedImportReceipt, MediaItem
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_managed_ingest import GRANTS, KEY, ManagedWorld, single
from tests.test_managed_live_photo import live
from tests.test_part_free import swap
from tests.test_part_ingest import Crash
from tests.test_recovery_point_finish import report, stamp

PHOTO = "IMG_9137.JPG"
RELEASE_AT = NOW + 15 * DAY
EVIDENCE = "https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-7100000001"


class ReleaseFixture(unittest.TestCase):
    def imported(self, build):
        self.world = ManagedWorld(self)
        self.export = self.world.create(build, parts=1, google_id="export-1")
        self.world.ingest_all(self.export, (1,))
        self.world.finish(self.export)
        self.world.opened.expire_all()
        self.part = self.world.part(self.export, 1)
        self.zip = Path(self.export.inbox_dir) / self.part.filename

    def authorize(self):
        with db.session(self.world.engine, writing=True) as opened:
            rehearsal = retention_gate.checked_rehearsal(EVIDENCE, "2026-11-20", "v3.4.1", "v5.9.0", db.dialect_of(opened), "2026-11-24")
            retention_gate.record_rehearsal(opened, NOW, rehearsal)
            retention_gate.accept_boundary(opened, NOW, EVIDENCE)
        return self.backup()

    def backup(self, *, healthy=True, ahead=0.0):
        begun = retention_gate.begin(self.world.engine, self.world.settings, RELEASE_AT - 3600.0 + ahead)
        taken = stamp(RELEASE_AT - 120.0 + ahead)
        if healthy:
            result = report(taken_at=taken)
        else:
            result = retention_gate.checked_report("failed", None, None, None, None, None)
        with db.session(self.world.engine) as opened:
            retention_gate.finish(opened, RELEASE_AT - 60.0 + ahead, begun.point_id, result)
        return begun.point_id

    def tick(self, *, enabled=True, at=RELEASE_AT):
        self.world.opened.expire_all()
        return part_free.free_ready(self.world.opened, replace(self.world.settings, managed_release=enabled), at)

    def reasons(self):
        return list(self.world.opened.scalars(select(Event.detail).where(Event.type.in_((part_free.KEPT, part_free.HELD_KEPT))).order_by(Event.id)))


class ReleaseTests(ReleaseFixture):
    def setUp(self):
        self.imported(single(PHOTO))
        self.entry = self.world.entry(f"Photos from 2019/{PHOTO}")
        self.asset = self.world.fake.state.assets[0]
        self.assertEqual(self.world.receipts()[PHOTO][0], "committed")

    def hold(self):
        path = paths.write_held(self.world.photos, "jo", unpack.staging_name(self.export), part_reader.scratch_name(self.entry),
                                [self.asset.data], self.entry.sha1)
        self.entry.held_path = path.relative_to(self.world.root).as_posix()
        self.world.opened.commit()
        return path

    def journal(self):
        self.world.opened.expire_all()
        return (self.part.free_state, self.part.release_point_id, self.entry.held_state, self.entry.held_release_point_id)

    def test_only_every_condition_together_releases_each_file_under_its_own_point(self):
        held = self.hold()
        recovery = self.authorize()
        start = len(self.world.fake.requests())
        confirm = retention_gate.confirm_release

        def custody_under_the_write_lock(*args):
            connection = self.world.opened.connection()
            if connection.dialect.name == "sqlite":
                self.assertTrue(connection.connection.driver_connection.in_transaction)
            else:
                self.assertTrue(connection.exec_driver_sql(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid())").scalar_one())
            return confirm(*args)

        with no_leaks(self, PHOTO, self.asset.id, KEY), mock.patch.object(retention_gate, "confirm_release", side_effect=custody_under_the_write_lock):
            freed = self.tick()
        self.assertEqual((freed.parts, freed.held, freed.failed), (1, 1, 0), self.reasons())
        self.assertEqual(self.journal(), ("freed", recovery, "freed", recovery))
        self.assertFalse(self.zip.exists())
        self.assertFalse(held.exists())
        self.assertEqual(self.world.fake.state.assets, [self.asset])
        requests = self.world.fake.requests()[start:]
        self.assertEqual(requests.count(("GET", "/api/users/me")), 2)
        self.assertEqual(requests.count(("GET", f"/api/assets/{self.asset.id}")), 2)
        self.assertEqual({method for method, _ in requests}, {"GET"})

    def test_the_setting_off_keeps_both_files_and_makes_no_immich_requests(self):
        held = self.hold()
        self.authorize()
        start = len(self.world.fake.requests())
        kept = self.tick(enabled=False)
        self.assertEqual((kept.parts, kept.held, kept.kept), (0, 0, 2))
        self.assertEqual(self.journal(), (None, None, None, None))
        self.assertTrue(self.zip.is_file() and held.is_file())
        self.assertEqual(self.world.fake.requests()[start:], [])
        self.assertEqual(self.reasons(), ["part 1: release_off", "held copy: release_off"])

    def test_the_last_release_closes_the_stale_backup_alert_and_a_later_expiry_does_not_reopen_it(self):
        self.hold()
        self.authorize()

        def watch(at):
            with db.session(self.world.engine, writing=True) as opened:
                return retention_gate.watch(opened, self.world.settings, at, lambda *_: alerts.SENT).backup_stale

        original_expiry = RELEASE_AT + retention_gate.WINDOW
        self.assertTrue(watch(original_expiry))
        self.assertIsNone(self.world.alert(retention_gate.STALE_KEY).closed)
        release_at = original_expiry + DAY
        self.backup(ahead=release_at - RELEASE_AT)
        freed = self.tick(at=release_at)
        self.assertEqual((freed.parts, freed.held), (1, 1))

        self.assertFalse(watch(release_at + retention_gate.WINDOW))
        self.assertTrue(self.world.alert(retention_gate.STALE_KEY).closed)
        self.assertFalse(watch(release_at + retention_gate.WINDOW + DAY))
        self.assertEqual(self.world.opened.scalars(select(Alert.id).where(Alert.key == retention_gate.STALE_KEY)).all(),
                         [self.world.alert(retention_gate.STALE_KEY).id])

    def test_the_files_are_not_candidates_before_14_days(self):
        held = self.hold()
        self.authorize()
        kept = self.tick(at=NOW + 14 * DAY - 10)
        self.assertEqual((kept.parts, kept.held, kept.kept), (0, 0, 0))
        self.assertTrue(self.zip.is_file() and held.is_file())
        self.assertEqual(self.reasons(), [])

    def test_a_failed_latest_backup_keeps_both_files(self):
        held = self.hold()
        self.authorize()
        self.backup(healthy=False, ahead=45)
        kept = self.tick()
        self.assertEqual((kept.parts, kept.held, kept.kept), (0, 0, 2))
        self.assertTrue(self.zip.is_file() and held.is_file())
        self.assertEqual(self.reasons(), ["part 1: backup_failed", "held copy: backup_failed"])

    def test_a_later_unit_uses_the_batch_clock_after_a_recovery_point_ages_out(self):
        held = self.hold()
        recovery = self.authorize()
        tick_started_at = RELEASE_AT - 120 + retention_gate.WINDOW - 1
        elapsed = 0.0
        delete = paths.remove_part_zip

        def remove_then_advance(*args):
            nonlocal elapsed
            removed = delete(*args)
            elapsed += 2.0
            return removed

        clock = mock.Mock(monotonic=lambda: elapsed)
        with mock.patch.object(part_free, "time", clock), mock.patch.object(paths, "remove_part_zip", side_effect=remove_then_advance):
            freed = self.tick(at=tick_started_at)
        self.assertEqual((freed.parts, freed.held, freed.kept), (1, 0, 1))
        self.assertFalse(self.zip.exists())
        self.assertTrue(held.is_file())
        self.assertEqual(self.journal(), ("freed", recovery, None, None))
        self.assertEqual(self.reasons(), ["held copy: no_recovery_point"])

    def test_a_fresh_refusal_of_trashed_offline_external_unowned_or_changed_bytes_clears_the_journal(self):
        held = self.hold()
        self.authorize()
        other_bytes = base64.b64encode(b"x" * 20).decode()
        for field, wrong in (("trashed", True), ("offline", True), ("library_id", "external"), ("owner_id", "elsewhere"), ("checksum", other_bytes)):
            with self.subTest(field=field), mock.patch.object(self.asset, field, wrong):
                kept = self.tick()
                self.assertEqual((kept.parts, kept.held, kept.kept), (0, 0, 2))
                self.assertEqual(self.journal(), (None, None, None, None))
                self.assertTrue(self.zip.is_file() and held.is_file())
        self.assertEqual(self.reasons(), ["part 1: immich_unconfirmed", "held copy: immich_unconfirmed"])

    def test_an_outage_or_a_key_refusal_stops_the_persons_releases_for_the_tick(self):
        held = self.hold()
        self.authorize()
        for refused in ((*GRANTS, "asset.update"), GRANTS):
            with self.subTest(grants=refused), mock.patch.object(self.world.fake.state, "permissions", refused):
                if refused == GRANTS:
                    self.world.fake.mode("500")
                kept = self.tick()
                self.assertEqual((kept.parts, kept.held, kept.kept), (0, 0, 1))
                self.assertTrue(self.zip.is_file() and held.is_file())
                self.assertEqual(self.journal(), (None, None, None, None))

    def test_an_unavailable_immich_alert_opens_after_a_day_and_closes_when_release_succeeds(self):
        self.authorize()
        self.world.fake.mode("500")
        self.assertEqual(self.tick().parts, 0)
        key = f"managed:release:{self.world.jo().id}"
        self.assertIsNone(self.world.alert(key))
        self.tick(at=RELEASE_AT + DAY + 1)
        self.tick(at=RELEASE_AT + DAY + 2)
        found = self.world.opened.scalars(select(Alert.closed).where(Alert.key == key)).all()
        self.assertEqual(found, [None])
        self.world.fake.mode("ok")
        resumed = RELEASE_AT + DAY + 3
        self.assertEqual(self.tick(at=resumed).parts, 1)
        self.assertAlmostEqual(self.world.alert(key).closed, resumed, delta=1.0)

    def test_a_deadline_keeps_the_files_and_never_performs_the_unlink(self):
        held = self.hold()
        self.authorize()
        with mock.patch.object(retention_gate, "READ_DEADLINE", 0):
            kept = self.tick()
        self.assertEqual((kept.parts, kept.held), (0, 0))
        self.assertTrue(self.zip.is_file() and held.is_file())
        self.assertEqual(self.journal(), (None, None, None, None))
        self.assertEqual(self.world.opened.scalars(select(Event.detail).where(Event.type == "managed_release_slow")).all(), ["0"])

    def test_withdrawn_attestation_is_seen_under_the_lock_before_any_network_read(self):
        self.authorize()
        hashed = part_free.hash_first
        start = len(self.world.fake.requests())

        def withdraw(*args):
            hashed(*args)
            with db.session(self.world.engine, writing=True) as opened:
                retention_gate.withdraw(opened, RELEASE_AT, "boundary")

        with mock.patch.object(part_free, "hash_first", side_effect=withdraw):
            kept = self.tick()
        self.assertEqual((kept.parts, kept.kept), (0, 1))
        self.assertTrue(self.zip.is_file())
        self.assertEqual(self.journal(), (None, None, None, None))
        self.assertEqual(self.world.fake.requests()[start:], [])
        self.assertEqual(self.reasons(), ["part 1: boundary_not_accepted"])

    def test_a_crash_before_unlink_requires_fresh_custody_when_retried(self):
        recovery = self.authorize()
        with mock.patch.object(paths, "remove_part_zip", side_effect=Crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        self.assertEqual(self.journal(), ("freeing", recovery, None, None))
        self.asset.trashed = True
        self.assertEqual(self.tick().parts, 0)
        self.assertEqual(self.journal(), (None, None, None, None))
        self.assertTrue(self.zip.is_file())

    def test_a_crash_after_unlink_retains_the_original_point_when_the_file_is_absent(self):
        recovery = self.authorize()
        delete = paths.remove_part_zip

        def remove_then_crash(*args):
            delete(*args)
            raise Crash()

        with mock.patch.object(paths, "remove_part_zip", side_effect=remove_then_crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        self.assertEqual(self.journal(), ("freeing", recovery, None, None))
        self.assertFalse(self.zip.exists())
        self.assertEqual(self.tick(enabled=False).parts, 1)
        self.assertEqual(self.journal(), ("freed", recovery, None, None))

    def test_a_zip_swapped_after_immich_answers_survives_and_the_journal_is_cleared(self):
        self.authorize()
        read = retention_gate.confirm_release
        replacement = b"unrelated generated file"

        def read_then_swap(*args):
            found = read(*args)
            swap(self.zip, replacement)
            return found

        with mock.patch.object(retention_gate, "confirm_release", side_effect=read_then_swap):
            self.assertEqual(self.tick().parts, 0)
        self.assertEqual(self.zip.read_bytes(), replacement)
        self.assertEqual(self.journal(), (None, None, None, None))
        self.assertEqual(self.world.opened.scalars(select(ManagedImportReceipt.state)).all(), ["committed"])

    def test_stale_keeper_metadata_in_the_database_cannot_authorize_removal(self):
        opened = self.world.opened
        person = self.world.jo()
        claimed = MediaItem(person_id=person.id, path="jo/originals/immich/fixture.jpg", source="phone", sha1=self.entry.sha1,
                            bytes=self.entry.bytes, indexed_at=NOW)
        snapshot = immich.extracted_of(self.asset.exif)
        version = ManagedAssetVersion(person_id=person.id, owner_id=self.asset.owner_id, asset_id=self.asset.id, sha1=self.entry.sha1,
                                      bytes=self.entry.bytes, kind="IMAGE", state="bytes_verified", metadata_snapshot=json.dumps(asdict(snapshot)),
                                      seen_at=NOW, bytes_verified_at=NOW)
        opened.add_all([claimed, version])
        opened.flush()
        opened.add(ManagedBackfillItem(person_id=person.id, item_id=claimed.id, outcome="mapped", version_id=version.id, checked_at=NOW, since=NOW))
        decision = opened.get_one(DedupeDecision, self.entry.decision_id)
        decision.tier, decision.matched_item_id = 1, claimed.id
        self.entry.outcome = "skipped"
        opened.commit()
        recovery = self.authorize()
        with mock.patch.object(self.asset, "exif", self.asset.exif | {"description": "changed on the server"}):
            self.assertEqual(self.tick().parts, 0)
        self.assertTrue(self.zip.is_file())
        self.assertEqual(self.reasons(), ["part 1: metadata_not_in_immich"])
        version.metadata_snapshot = "{}"
        opened.commit()
        self.assertEqual(self.tick().parts, 1)
        self.assertEqual(self.journal(), ("freed", recovery, None, None))

    def test_held_copy_crash_after_unlink_recovers_only_after_confirming_absence(self):
        self.assertEqual(self.tick().parts, 0)
        held = self.hold()
        recovery = self.authorize()
        delete = paths.remove_held_copy

        def remove_then_crash(*args):
            delete(*args)
            raise Crash()

        with mock.patch.object(paths, "remove_held_copy", side_effect=remove_then_crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        self.assertEqual(self.journal(), ("freed", recovery, "freeing", recovery))
        self.assertFalse(held.exists())
        self.assertEqual(self.tick(enabled=False).held, 1)
        self.assertEqual(self.journal(), ("freed", recovery, "freed", recovery))


class LiveReleaseTests(ReleaseFixture):
    def test_a_live_photo_still_must_link_to_its_covered_video_at_release(self):
        self.imported(live)
        recovery = self.authorize()
        still = next(asset for asset in self.world.fake.state.assets if asset.kind == "IMAGE")
        self.assertIsNotNone(still.live_video_id)
        with mock.patch.object(still, "live_video_id", None):
            self.assertEqual(self.tick().parts, 0)
        self.assertTrue(self.zip.is_file())
        self.assertEqual(self.reasons(), ["part 1: immich_unconfirmed"])
        self.assertEqual(self.tick().parts, 1)
        self.assertFalse(self.zip.exists())
        self.assertEqual(self.world.part(self.export, 1).release_point_id, recovery)
