import json
import unittest
import uuid
from unittest import mock

from sqlalchemy import select

from darkroom.domain import hold_resolutions, holds, managed_ingest, managed_metadata, review
from darkroom.domain.ingest import finish_export, hold_readback
from darkroom.domain.managed_upload import Report
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import ManagedBackfillItem, ManagedHoldResolution, ManagedImportReceipt, Person, ReviewItem
from tests.fakes.immich import FakeAsset, extract
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_backfill import BackfillWorld
from tests.test_managed_ingest import GRANTS, KEY, OWNER, SECRET, ManagedWorld, single

NAME = "IMG_6301.JPG"
OTHER = "IMG_6302.JPG"


class PostUploadResolutionTests(unittest.TestCase):
    def duplicate(self, names=(NAME,), trash=False):
        world = ManagedWorld(self)

        def build(builder):
            for name in names:
                single(name)(builder)

        export = world.create(build, parts=1)
        assets = []
        for name in names:
            data = world.bytes_of(export, name)
            exif = extract(data, None)
            exif["timeZone"] = "UTC"
            asset = FakeAsset(str(uuid.uuid4()), OWNER, data, name=name, exif=exif, trashed=trash)
            world.fake.add_asset(asset)
            assets.append(asset)
        world.ingest_all(export, (1,))
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual(world.status(export), ("imported", "done"))
        return world, export, assets

    def receipt(self, world, asset):
        world.opened.expire_all()
        return world.opened.scalars(select(ManagedImportReceipt).where(ManagedImportReceipt.asset_id == asset.id)).one()

    def record(self, world, receipt, action, offset=None):
        row = hold_resolutions.resolve(world.opened, receipt.id, action, offset, None, None, NOW + 60)
        world.opened.commit()
        return row.id

    def tick(self, world, at=NOW + 120):
        start = len(world.fake.requests())
        with mock.patch.object(hold_readback.time, "sleep"), no_leaks(self, NAME, KEY, SECRET, world.fake.url):
            finish_export.finish_ready(world.opened, world.settings, at, world.tools)
        world.opened.expire_all()
        requests = world.fake.requests()[start:]
        self.assertTrue(all(method == "GET" for method, _ in requests), requests)
        return [path for _, path in requests]

    def assert_committed(self, world, asset, resolution, result, intended):
        row = self.receipt(world, asset)
        decision = world.opened.get(ManagedHoldResolution, resolution)
        self.assertEqual((decision.state, decision.result, row.state, row.metadata_hold, row.expected_metadata, row.send_attempts),
                         ("applied", result, "committed", None, intended, 1))
        self.assertFalse(hold_resolutions.undoable(row, decision))
        return json.loads(row.metadata_evidence)

    def correct(self, asset, receipt):
        intended = managed_metadata.intended_of(receipt.expected_metadata)
        packet = managed_metadata.sidecar_source(intended)
        asset.exif = extract(asset.data, packet.reader.read())

    def test_accept_and_record_offset_store_fresh_evidence_and_close_only_after_the_last_hold(self):
        world, export, (asset, other) = self.duplicate((NAME, OTHER))
        before = world.files()
        key = f"managed:held:{export.id}"
        self.assertIsNone(world.alert(key).closed)
        first, second = self.receipt(world, asset), self.receipt(world, other)
        expected = first.expected_metadata, second.expected_metadata
        resolution = self.record(world, first, "accept")
        asset.exif["description"] = "A newer Immich description"
        paths = self.tick(world)
        self.assertIn(f"/api/assets/{asset.id}/original", paths)
        self.assertEqual(self.assert_committed(world, asset, resolution, "cleared", expected[0])["description"], "A newer Immich description")
        self.assertIsNone(world.alert(key).closed)
        recorded = self.record(world, second, "record_offset", -30)
        self.tick(world, NOW + 130)
        self.assertEqual(self.assert_committed(world, other, recorded, "cleared", expected[1])["zone"], "UTC")
        self.assertEqual(world.opened.get(ManagedHoldResolution, recorded).offset_minutes, -30)
        self.assertEqual(world.alert(key).closed, NOW + 130)
        self.assertEqual(world.files(), before)

    def test_a_recheck_that_still_differs_is_listed_and_can_be_retried_after_a_manual_fix(self):
        world, _, (asset,) = self.duplicate()
        receipt = self.receipt(world, asset)
        expected = receipt.expected_metadata
        first = self.record(world, receipt, "recheck")
        self.tick(world)
        row = self.receipt(world, asset)
        found, _ = holds.listing(world.opened, None, None, None)
        hold_resolutions.attach(world.opened, found)
        result = found[0]["resolution"]
        self.assertEqual((row.state, row.metadata_hold, result["id"], result["result"], result["can_undo"]),
                         ("bytes_verified", "not_enriched", first, "still_differs", False))
        self.correct(asset, row)
        second = self.record(world, row, "recheck")
        self.tick(world, NOW + 130)
        self.assert_committed(world, asset, second, "rechecked", expected)

    def test_accept_refuses_an_asset_trashed_after_recording_then_rechecks_its_restored_bytes(self):
        world, _, (asset,) = self.duplicate()
        receipt = self.receipt(world, asset)
        expected = receipt.expected_metadata
        resolution = self.record(world, receipt, "accept")
        asset.trashed = True
        self.tick(world)
        receipt = self.receipt(world, asset)
        self.assertEqual((receipt.metadata_hold, json.loads(receipt.hold_facts)), ("unusable", {"kind": "trashed"}))
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution).result, "still_unusable")
        asset.trashed = False
        self.correct(asset, receipt)
        resolution = self.record(world, receipt, "recheck")
        self.tick(world, NOW + 130)
        self.assert_committed(world, asset, resolution, "rechecked", expected)

    def test_a_restored_kept_receipt_resumes_from_returned_and_closes_its_alert(self):
        world = ManagedWorld(self)
        world.run_managed()
        pair = world.opened.scalars(select(ReviewItem)).one()
        data = (world.root / pair.hold_path).read_bytes()
        asset = FakeAsset(str(uuid.uuid4()), OWNER, data, name=NAME, exif=extract(data, None), trashed=True)
        world.fake.add_asset(asset)
        review.resolve(world.opened, world.photos, pair.id, "keep_both", NOW + 5, review.FileEffects())
        world.opened.commit()
        finish_export.finish_ready(world.opened, world.settings, NOW + 10, world.tools)
        before = world.files()
        receipt = self.receipt(world, asset)
        self.assertEqual((receipt.state, receipt.metadata_hold), ("returned", "unusable"))
        key = f"managed:kept:{pair.id}"
        self.assertIsNone(world.alert(key).closed)
        asset.trashed = False
        self.correct(asset, receipt)
        resolution = self.record(world, receipt, "recheck")
        self.tick(world)
        self.assert_committed(world, asset, resolution, "rechecked", receipt.expected_metadata)
        self.assertEqual(world.alert(key).closed, NOW + 120)
        self.assertEqual(world.files(), before)

    def test_an_outage_keeps_acceptance_recorded_and_the_ordinary_commit_is_retryable(self):
        world, _, (asset,) = self.duplicate()
        receipt = self.receipt(world, asset)
        expected = receipt.expected_metadata
        resolution = self.record(world, receipt, "accept")
        world.fake.state.permissions = (*GRANTS, "asset.update")
        paths = self.tick(world, NOW + 2 * DAY)
        self.assertNotIn(f"/api/assets/{asset.id}", paths)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution).state, "recorded")
        self.assertIsNotNone(world.alert(f"managed:resolution-pending:{resolution}"))
        world.fake.state.permissions = GRANTS
        with mock.patch.object(managed_metadata, "commit_compound", return_value=Report("pending", "temporary outage")):
            self.tick(world, NOW + 2 * DAY + 10)
        self.assertEqual(self.receipt(world, asset).state, "metadata_verified")
        self.tick(world, NOW + 2 * DAY + 20)
        self.assert_committed(world, asset, resolution, "cleared", expected)

    def test_undo_between_the_fresh_read_and_the_write_keeps_the_receipt_held(self):
        world, _, (asset,) = self.duplicate()
        receipt = self.receipt(world, asset)
        resolution = self.record(world, receipt, "accept")
        inspect = hold_readback.inspect

        def undo(*args):
            result = inspect(*args)
            with db.session(world.engine) as opened:
                hold_resolutions.undo(opened, receipt.id, None, NOW + 100)
            return result

        with mock.patch.object(hold_readback, "inspect", side_effect=undo):
            self.tick(world)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution).state, "undone")
        self.assertEqual(self.receipt(world, asset).metadata_hold, "not_enriched")

    def test_a_new_owner_between_the_read_and_the_write_keeps_the_receipt_held(self):
        world, _, (asset,) = self.duplicate()
        receipt = self.receipt(world, asset)
        resolution = self.record(world, receipt, "accept")
        inspect = hold_readback.inspect

        def reassign(*args):
            result = inspect(*args)
            with db.session(world.engine) as opened:
                opened.get(Person, world.ids["jo"]).immich_user_id = None
            return result

        with mock.patch.object(hold_readback, "inspect", side_effect=reassign):
            self.tick(world)
        self.assertEqual(world.opened.get(ManagedHoldResolution, resolution).state, "recorded")
        self.assertEqual(self.receipt(world, asset).metadata_hold, "not_enriched")

    def test_an_asset_trashed_between_observation_and_commit_is_still_unusable(self):
        world, export, (asset,) = self.duplicate()
        receipt = self.receipt(world, asset)
        resolution = self.record(world, receipt, "accept")
        commit = managed_metadata.commit_compound

        def trash(*args, **kwargs):
            asset.trashed = True
            return commit(*args, **kwargs)

        with mock.patch.object(managed_metadata, "commit_compound", side_effect=trash):
            self.tick(world)
        receipt = self.receipt(world, asset)
        decision = world.opened.get(ManagedHoldResolution, resolution)
        self.assertEqual((receipt.state, receipt.metadata_hold, decision.state, decision.result),
                         ("metadata_verified", "unusable", "superseded", "still_unusable"))
        self.assertIsNone(world.alert(f"managed:held:{export.id}").closed)

    def test_backfill_updates_the_exception_then_maps_the_same_committed_receipt(self):
        world = BackfillWorld(self)
        world.run_managed(single(NAME), parts=1)
        item = world.google_only_item(NAME)
        data = world.data(item)
        asset = FakeAsset(str(uuid.uuid4()), OWNER, data, name=NAME, exif=extract(data, None))
        world.fake.add_asset(asset)
        world.backfill()
        before = world.files()
        receipt = self.receipt(world, asset)
        expected = receipt.expected_metadata
        backfilled = world.opened.scalars(select(ManagedBackfillItem)).one()
        self.assertEqual(backfilled.reason, "not_enriched")
        backfilled.reason = "unknown_offset"
        world.opened.commit()
        self.record(world, receipt, "accept")
        original = immich.ManagedReader.verify_original

        def different_bytes(*args):
            original(*args)
            raise immich.ChecksumMismatch("the downloaded original changed")

        with mock.patch.object(immich.ManagedReader, "verify_original", new=different_bytes):
            self.tick(world)
        self.assertEqual(world.opened.scalars(select(ManagedBackfillItem)).one().reason, "unusable")
        receipt = self.receipt(world, asset)
        self.correct(asset, receipt)
        resolution = self.record(world, receipt, "recheck")
        self.tick(world, NOW + 130)
        self.assert_committed(world, asset, resolution, "rechecked", expected)
        self.assertEqual(world.backfill(at=NOW + 140).mapped, 1)
        found = world.opened.scalars(select(ManagedBackfillItem)).one()
        self.assertEqual((found.outcome, found.reason, found.receipt_id), ("mapped", None, receipt.id))
        self.assertEqual(world.uploads(), [("POST", "/api/assets")])
        self.assertEqual(world.files(), before)
