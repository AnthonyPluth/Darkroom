import json
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from sqlalchemy import select

from darkroom.domain import backfill, hold_resolutions, managed_ingest, managed_metadata, managed_upload, review, xmp
from darkroom.domain.ingest import finish_export
from darkroom.storage import db
from darkroom.storage.models import Event, ManagedHoldResolution, ManagedImportIntent, ManagedImportReceipt, ReviewItem
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_backfill import MOMENT, BackfillWorld
from tests.test_managed_ingest import ALLOWED, GRANTS, KEY, SECRET, ManagedWorld


NAME = "IMG_6201.JPG"
WALL = "2020-02-03T15:04:05"
OFFSET = -30


def without_time(builder):
    builder.photo(NAME, form=None)


class PreUploadResolutionTests(unittest.TestCase):
    def imports(self, build=without_time):
        world = ManagedWorld(self)
        export = world.run_managed(build, parts=1)
        row = world.opened.scalars(select(ManagedImportReceipt)).one()
        return world, export, row.id

    def resolve(self, world, receipt_id, action="capture_time", offset=OFFSET, wall=WALL):
        row = hold_resolutions.resolve(world.opened, receipt_id, action, offset, wall, None, NOW + 60)
        world.opened.commit()
        return row.id

    def tick(self, world, now=NOW + 100):
        finish_export.finish_ready(world.opened, world.settings, now, world.tools)
        world.opened.expire_all()

    def test_the_import_tick_reads_the_zip_and_uploads_with_the_owners_capture_time(self):
        world, export, receipt_id = self.imports()
        before = world.files()
        resolution_id = self.resolve(world, receipt_id)
        self.assertEqual(world.fake.state.assets, [])
        with no_leaks(self, NAME, KEY, SECRET, world.fake.url):
            self.tick(world)
            self.tick(world)
        receipt = world.opened.get(ManagedImportReceipt, receipt_id)
        row = world.opened.get(ManagedHoldResolution, resolution_id)
        self.assertEqual((row.state, row.result, receipt.state, receipt.metadata_hold), ("applied", "cleared", "committed", None))
        intended = managed_metadata.intended_of(receipt.expected_metadata)
        self.assertEqual((intended.taken_from, intended.offset, managed_metadata.taken_text(intended)), ("owner", OFFSET, WALL + "-00:30"))
        asset = world.fake.state.assets[0]
        self.assertEqual(asset.data, world.bytes_of(export, NAME))
        self.assertIn(b'photoshop:DateCreated="2020-02-03T15:04:05-00:30"', asset.sidecar)
        self.assertEqual(world.files(), before)
        requests = [f"{method} {path}" for method, path in world.fake.requests()]
        self.assertEqual(requests.count("POST /api/assets"), 1)
        self.assertTrue(all(ALLOWED.fullmatch(request) for request in requests))
        self.assertEqual(world.opened.scalars(select(Event.detail).where(Event.type == hold_resolutions.APPLIED_EVENT)).one(), "cleared")

    def test_a_missing_zip_keeps_the_resolution_recorded_until_the_source_returns(self):
        world, export, receipt_id = self.imports()
        resolution_id = self.resolve(world, receipt_id)
        archive = Path(export.inbox_dir) / world.part(export, 1).filename
        detached = archive.with_suffix(".away")
        archive.rename(detached)
        self.tick(world, NOW + 2 * DAY)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "recorded")
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).metadata_hold, "no_instant")
        self.assertEqual(world.fake.state.assets, [])
        self.assertIsNotNone(world.alert(f"managed:resolution-source:{resolution_id}"))
        with ZipFile(archive, "w") as replaced:
            replaced.writestr("Takeout/Google Photos/other.JPG", b"other photo")
        self.tick(world, NOW + 2 * DAY + 5)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "recorded")
        self.assertEqual(world.fake.state.assets, [])
        detached.rename(archive)
        self.tick(world, NOW + 2 * DAY + 10)
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).state, "committed")

    def test_a_stale_resolution_is_superseded_without_sending(self):
        world, _, receipt_id = self.imports()
        resolution_id = self.resolve(world, receipt_id)
        row = world.opened.get(ManagedImportReceipt, receipt_id)
        row.hold_facts = None
        world.opened.commit()
        self.tick(world)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "superseded")
        self.assertEqual(world.fake.state.assets, [])

    def test_a_resolution_recorded_by_the_previous_version_can_be_applied(self):
        world, _, receipt_id = self.imports()
        row = world.opened.get(ManagedHoldResolution, self.resolve(world, receipt_id))
        row.previous_metadata = None
        world.opened.commit()
        self.tick(world)
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).state, "committed")

    def test_extra_key_grants_keep_the_plan_applied_without_an_upload(self):
        world, _, receipt_id = self.imports()
        resolution_id = self.resolve(world, receipt_id)
        world.fake.state.permissions = (*GRANTS, "asset.update")
        self.tick(world, NOW + 2 * DAY)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "applied")
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).state, "prepared")
        self.assertEqual(world.fake.state.assets, [])
        self.assertIsNotNone(world.alert(f"managed:resolution-pending:{resolution_id}"))
        world.fake.state.permissions = GRANTS
        self.tick(world, NOW + 2 * DAY + 10)
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).state, "committed")

    def test_the_receipt_waits_for_the_same_person_and_a_supported_manifest(self):
        world, _, receipt_id = self.imports()
        resolution_id = self.resolve(world, receipt_id)
        person = world.jo()
        owner = person.immich_user_id
        person.immich_user_id = None
        world.opened.commit()
        self.tick(world)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "recorded")
        person.immich_user_id = owner
        intent = world.opened.scalars(select(ManagedImportIntent)).one()
        manifest = intent.resource_manifest
        intent.resource_manifest = '{"schema": 3}'
        world.opened.commit()
        self.tick(world)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "recorded")
        self.assertEqual(world.fake.state.assets, [])
        intent.resource_manifest = manifest
        world.opened.commit()
        self.tick(world)
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).state, "committed")

    def test_a_post_upload_hold_supersedes_the_applied_resolution(self):
        world, _, receipt_id = self.imports()
        resolution_id = self.resolve(world, receipt_id)
        with mock.patch.object(managed_ingest.time, "sleep"), mock.patch("tests.fakes.immich.extract", return_value={"dateTimeOriginal": None}):
            self.tick(world)
            receipt = world.opened.get(ManagedImportReceipt, receipt_id)
            self.assertEqual((receipt.state, receipt.metadata_hold), ("bytes_verified", "mismatch"))
            self.tick(world, NOW + 2 * DAY)
        receipt = world.opened.get(ManagedImportReceipt, receipt_id)
        self.assertEqual((receipt.state, receipt.metadata_hold, receipt.send_attempts), ("bytes_verified", "not_settled", 1))
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "superseded")
        self.resolve(world, receipt_id, "accept", None, None)

    def test_a_new_hold_retires_the_capture_time_resolution_and_the_next_action_preserves_it(self):
        def build(builder):
            item = builder.photo(NAME, gps=None, description="private\x01description")
            item.sidecar_data = item.sidecar_data.replace(b'"photoTakenTime"', b'"unusedPhotoTakenTime"')
        world, _, receipt_id = self.imports(build)
        time_id = self.resolve(world, receipt_id)
        self.tick(world)
        row = world.opened.get(ManagedHoldResolution, time_id)
        self.assertEqual((row.state, row.result), ("superseded", "rehold:unwritable"))
        self.assertEqual(world.fake.state.assets, [])
        self.resolve(world, receipt_id, "drop_description", None, None)
        self.tick(world)
        receipt = world.opened.get(ManagedImportReceipt, receipt_id)
        intended = managed_metadata.intended_of(receipt.expected_metadata)
        self.assertEqual((receipt.state, managed_metadata.taken_text(intended), intended.description), ("committed", WALL + "-00:30", None))
        self.assertNotIn(b"private", world.fake.state.assets[0].sidecar)

    def test_undo_during_the_access_check_refuses_the_stale_upload_claim(self):
        world, _, receipt_id = self.imports()
        resolution_id = self.resolve(world, receipt_id)
        before = world.opened.get(ManagedImportReceipt, receipt_id).expected_metadata
        claim = managed_upload.claim

        def undo_before_claim(opened, row, now):
            with db.session(opened.get_bind()) as concurrent:
                hold_resolutions.undo(concurrent, receipt_id, None, now)
            return claim(opened, row, now)

        with mock.patch.object(managed_upload, "claim", side_effect=undo_before_claim):
            self.tick(world)
        receipt = world.opened.get(ManagedImportReceipt, receipt_id)
        self.assertEqual((receipt.metadata_hold, receipt.expected_metadata, receipt.send_attempts), ("no_instant", before, 0))
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "undone")
        self.assertEqual(world.fake.state.assets, [])

    def test_a_changed_backfill_file_is_never_sent_and_the_matching_copy_can_be_retried(self):
        world = BackfillWorld(self)
        world.run_managed(without_time, parts=1)
        world.backfill()
        item = world.google_only_item(NAME)
        path = world.root / item.path
        copy = path.read_bytes()
        receipt_id = world.opened.scalars(select(ManagedImportReceipt.id)).one()
        resolution_id = self.resolve(world, receipt_id)
        path.write_bytes(b"other bytes")
        self.tick(world, NOW + 2 * DAY)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "recorded")
        self.assertEqual(world.uploads(), [])
        path.write_bytes(copy)
        before = world.files()
        self.tick(world, NOW + 2 * DAY + 10)
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).state, "committed")
        self.assertEqual(world.fake.state.assets[0].data, copy)
        self.assertEqual(world.files(), before)

    def test_a_backfill_resolution_waits_if_a_matching_phone_photo_appears_after_recording(self):
        world = BackfillWorld(self)

        def build(builder):
            photo = world.scratch_jpeg("shot-takeout.jpg", colour=(50, 60, 70), **MOMENT)
            builder.photo(NAME, data=photo, gps=None, taken=1567578615)

        world.run_managed(build, parts=1)
        item = world.google_only_item(NAME)
        item.sidecar_json = json.dumps(json.loads(item.sidecar_json) | {"description": "private\x01description"})
        world.opened.commit()
        world.backfill()
        receipt = world.opened.scalars(select(ManagedImportReceipt)).one()
        self.assertEqual(receipt.metadata_hold, "unwritable")
        resolution_id = self.resolve(world, receipt.id, "drop_description", None, None)

        phone = world.scratch_jpeg("shot-phone.jpg", colour=(52, 58, 73), **MOMENT)
        world.original("jo", NAME, phone)
        world.index_all()
        self.assertEqual(backfill.same_shots(world.opened, world.jo(), [item]), {item.id})
        self.tick(world)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution_id).state, "recorded")
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt.id).metadata_hold, "unwritable")
        self.assertEqual(world.uploads(), [])

    def test_a_kept_receipt_rechecks_its_copy_before_retrying_a_saved_metadata_plan(self):
        world = ManagedWorld(self)
        world.run_managed()
        review_id = world.opened.scalars(select(ReviewItem.id)).one()
        review.resolve(world.opened, world.photos, review_id, "keep_both", NOW + 5, review.FileEffects())
        kept = world.opened.get(ReviewItem, review_id)
        receipt = world.opened.scalars(select(ManagedImportReceipt).where(ManagedImportReceipt.resource_key == f"review:{review_id}")).one()
        held = managed_metadata.intend({}, xmp.GoogleMetadata.of(None), "IMAGE")
        receipt.metadata_hold, receipt.expected_metadata, receipt.updated_at = held.hold, managed_metadata.snapshot(held), NOW + 5
        kept.sidecar_json = None
        world.opened.commit()
        receipt_id = receipt.id
        self.resolve(world, receipt_id)
        path = world.root / kept.hold_path
        copy = path.read_bytes()
        original_send = managed_upload.send
        with mock.patch.object(managed_upload, "send", return_value=managed_upload.Report("pending", "retry")):
            self.tick(world)
        path.write_bytes(b"not the held photo")
        with mock.patch.object(managed_upload, "send", wraps=original_send) as sending:
            self.tick(world, NOW + 200)
        self.assertEqual(sending.call_count, 0)
        path.write_bytes(copy)
        with mock.patch.object(managed_ingest.time, "sleep"):
            self.tick(world, NOW + 300)
        self.assertEqual(world.opened.get(ManagedImportReceipt, receipt_id).send_attempts, 1)

    def test_a_quicktime_instant_keeps_its_utc_value_when_the_owner_supplies_an_offset(self):
        tags = {"QuickTime:CreateDate": "2021:11:01 19:18:17"}
        google = xmp.GoogleMetadata.of(None)
        held = managed_metadata.intend(tags, google, "VIDEO")
        self.assertEqual(held.hold, "unknown_offset")
        intended = managed_metadata.intend(tags, google, "VIDEO", owner=managed_metadata.OwnerValues(offset=-30))
        self.assertEqual(managed_metadata.taken_text(intended), "2021-11-01T18:48:17-00:30")
