import dataclasses
import hashlib
import json
import unittest
import uuid
from pathlib import PurePosixPath
from unittest import mock

from sqlalchemy import delete, select, update

from darkroom.domain import backfill, managed_metadata, managed_upload, pixel_folder, xmp
from darkroom.domain.ingest import place
from darkroom.providers import exiftool
from darkroom.server import jobs
from darkroom.storage.models import (Alert, DedupeDecision, DuplicateRemoval, Export, ImmichAsset, ManagedAssetOrigin, ManagedAssetVersion, ManagedBackfillItem,
                                     ManagedCompound, ManagedCompoundMember, ManagedImportIntent, ManagedImportReceipt, ManagedPixelSource, MediaItem,
                                     PixelCopy, TakeoutEntry)
from tests.fakes.immich import FakeAsset, extract
from tests.fixtures.media import TINY_VIDEO
from tests.import_world import NOW, tree
from tests.privacy import no_leaks
from tests.test_managed_ingest import KEY, OWNER, SECRET, ManagedWorld
from tests.test_part_ingest import Crash

PHONE = "immich/2024/IMG_7001.JPG"
MOMENT = {"taken": "2019:09:04 08:30:15", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
NO_PAUSE = backfill.Tools(pause=lambda seconds: None)


def sha1(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


class BackfillWorld(ManagedWorld):
    def __init__(self, test):
        super().__init__(test, state=None)
        self.settings = dataclasses.replace(self.settings, pixel_immich_source=True)
        self.phone_data = b""

    def earlier_imports(self, builder):
        self.phone_data = self.scratch_jpeg("phone.jpg", colour=(30, 60, 90), **MOMENT | {"taken": "2024:05:01 09:00:00"})
        self.original("jo", PHONE, self.phone_data)
        builder.photo("IMG_5001.JPG", gps=(45.4, 12.3), description="A made-up quay", part=0)
        builder.photo("IMG_5002.JPG", gps=(46.1, 11.2), part=0)
        builder.photo("IMG_5003.JPG", gps=(44.2, 10.1), part=0)
        builder.photo("IMG_5004.JPG", gps=(43.3, 11.9), part=0)
        builder.photo("IMG_5005.JPG", form=None, part=0)
        builder.photo("IMG_6001.JPG", data=self.scratch_jpeg("shot-takeout.jpg", colour=(50, 60, 70), **MOMENT), gps=None, taken=1567578615, part=0)
        builder.live_photo("IMG_3001", part=0)

    def seeded(self):
        export = self.run_managed(self.earlier_imports, parts=1)
        self.original("jo", "IMG_6001.JPG", self.scratch_jpeg("shot-phone.jpg", colour=(52, 58, 73), **MOMENT))
        self.index_all()
        phone = self.item(f"jo/originals/{PHONE}")
        asset_id = str(uuid.uuid4())
        self.fake.add_asset(FakeAsset(asset_id, OWNER, self.phone_data, name="IMG_7001.JPG"))
        self.opened.add(ImmichAsset(person_id=self.ids["jo"], asset_id=asset_id, kind="photo", status="stored", sha1=phone.sha1, original_item_id=phone.id, seen_at=NOW))
        self.opened.add(PixelCopy(pixel_id=self.pixel, item_id=phone.id, person_id=self.ids["jo"], state="queued", sha1=phone.sha1, bytes=phone.bytes,
                                  queued_at=NOW, always_send=True))
        self.opened.commit()
        held = self.google_only_item("IMG_5002.JPG")
        self.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, self.data(held), name="IMG_9002.JPG", exif=self.extracted(held)))
        external = self.google_only_item("IMG_5004.JPG")
        self.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, self.data(external), name="IMG_5004.JPG", library_id=str(uuid.uuid4())))
        self.opened.execute(update(MediaItem).where(MediaItem.id == self.google_only_item("IMG_5003.JPG").id).values(export_id=None))
        self.opened.commit()
        return export

    def item(self, path):
        self.opened.expire_all()
        return self.opened.execute(select(MediaItem).where(MediaItem.path == path)).scalar_one()

    def google_only_item(self, name):
        self.opened.expire_all()
        rows = self.opened.execute(select(MediaItem).where(MediaItem.path.startswith("jo/google-only/"), MediaItem.source == "takeout")).scalars()
        return next(row for row in rows if PurePosixPath(row.path).name == name)

    def data(self, item):
        return (self.root / item.path).read_bytes()

    def extracted(self, item, still=None):
        path = self.root / item.path
        kind = "VIDEO" if path.suffix.lower() == ".mov" else "IMAGE"
        google = xmp.GoogleMetadata.of(item.sidecar_json if still is None else still.sidecar_json)
        intended = managed_metadata.intend(exiftool.read([path])[path], google, kind, corrects=still is None and not item.sidecar_from_still)
        packet = managed_metadata.sidecar_source(intended)
        return extract(self.data(item), None if packet is None else packet.reader.read())

    def backfill(self, opt_in=backfill.ON, at=NOW + 100, tools=NO_PAUSE):
        jo = self.jo()
        jo.managed_backfill = opt_in
        self.opened.commit()
        return backfill.run_all(self.opened, self.settings, self.photos, at, tools=tools)

    def library(self):
        return tree(self.root / "jo") | {f"sam/{key}": value for key, value in tree(self.root / "sam").items()}

    def names(self, ids):
        self.opened.expire_all()
        return sorted(PurePosixPath(self.opened.get(MediaItem, item_id).path).name for item_id in ids)

    def report(self):
        return {reason: self.names(ids) for reason, ids in backfill.exceptions(self.opened, self.ids["jo"]).items()}

    def uploads(self):
        return [request for request in self.fake.requests() if request == ("POST", "/api/assets")]

    def by_id(self, asset_id):
        return next(asset for asset in self.fake.state.assets if asset.id == asset_id)


class BackfillTests(unittest.TestCase):
    def test_earlier_imports_in_google_only_are_uploaded_or_mapped_with_a_receipt_each_and_no_file_changes(self):
        world = BackfillWorld(self)
        export = world.seeded()
        before = world.library()
        with no_leaks(self, KEY, SECRET, "IMG_5001", "IMG_7001", world.fake.url):
            summary = world.backfill()
        self.assertEqual(world.library(), before)
        self.assertEqual(world.report(), {"unknown_offset": ["IMG_3001.HEIC", "IMG_3001.MOV"], "same_shot": ["IMG_6001.JPG"], "no_instant": ["IMG_5005.JPG"],
                                          "no_source_account": ["IMG_5003.JPG"]})
        self.assertEqual(summary.mapped, 5)
        mapped = {name: world.opened.get(ManagedImportReceipt, row.receipt_id) for row in world.opened.execute(select(ManagedBackfillItem)).scalars()
                  if row.receipt_id is not None for name in world.names([row.item_id])}
        self.assertEqual(sorted(mapped), ["IMG_5001.JPG", "IMG_5002.JPG", "IMG_5003.JPG", "IMG_5004.JPG"])
        self.assertEqual({name: row.upload_outcome for name, row in mapped.items()},
                         {"IMG_5001.JPG": "created", "IMG_5002.JPG": "duplicate", "IMG_5003.JPG": "created", "IMG_5004.JPG": "created"})
        for name, row in mapped.items():
            with self.subTest(name):
                item, asset = world.google_only_item(name), world.by_id(row.asset_id)
                self.assertEqual((row.state, row.source_sha1, row.verified_sha1, row.verified_bytes), ("committed", item.sha1, item.sha1, item.bytes))
                self.assertEqual((asset.owner_id, asset.library_id, sha1(asset.data), len(asset.data), asset.live_video_id), (OWNER, None, item.sha1, item.bytes, None))
                self.assertIsNotNone(row.metadata_evidence)
                version = world.opened.execute(select(ManagedAssetVersion).where(ManagedAssetVersion.asset_id == row.asset_id)).scalar_one()
                self.assertEqual((version.state, version.sha1, version.bytes, version.library_id), ("bytes_verified", item.sha1, item.bytes, None))
        external = [asset for asset in world.fake.state.assets if sha1(asset.data) == world.google_only_item("IMG_5004.JPG").sha1]
        self.assertEqual(sorted(asset.library_id is None for asset in external), [False, True])
        self.assertEqual(len(world.uploads()), 4)
        intents = world.opened.execute(select(ManagedImportIntent.kind, ManagedImportIntent.export_id).order_by(ManagedImportIntent.id)).all()
        self.assertEqual(sorted(intents, key=str), sorted([("backfill", export.id), ("backfill", None)], key=str))
        self.assertFalse(place.managed_import(world.opened, world.opened.get(Export, export.id)))
        origins = dict(world.opened.execute(select(ManagedAssetOrigin.source_key, ManagedAssetOrigin.kind)).all())
        phone = world.item(f"jo/originals/{PHONE}")
        self.assertEqual(origins[f"media-item:{phone.id}"], "phone")
        self.assertEqual(origins[f"media-item:{world.google_only_item('IMG_5003.JPG').id}"], "other")
        self.assertEqual(origins[f"media-item:{world.google_only_item('IMG_5001.JPG').id}"], "takeout")
        references = world.opened.execute(select(ManagedPixelSource.item_id, ManagedPixelSource.copy_id)).all()
        copy_id = world.opened.execute(select(PixelCopy.id).where(PixelCopy.item_id == phone.id)).scalar_one()
        self.assertEqual(references, [(phone.id, copy_id)])
        self.assertEqual(world.opened.execute(select(PixelCopy.item_id)).scalars().all(), [phone.id])
        alert = world.opened.execute(select(Alert).where(Alert.key.startswith("managed:backfill:"))).scalar_one()
        self.assertIn("5 photos of jo need a look", alert.message)
        held = world.opened.execute(select(ManagedImportReceipt.hold_facts).where(ManagedImportReceipt.metadata_hold == "no_instant")).scalar_one()
        self.assertEqual(json.loads(held), {"wall_clock": None, "file_utc": None, "google_utc": None, "place_known": False,
                                            "description_length": None, "description_unwritable": False})

    def test_live_photo_halves_renamed_on_a_name_collision_are_linked_using_recorded_paths(self):
        world = BackfillWorld(self)
        export = world.run_managed(live_only, parts=1)
        renamed = []
        for name, new_name in (("IMG_3001.HEIC", "IMG_3001-0a1b2c3d.HEIC"), ("IMG_3001.MOV", "IMG_3001-4e5f6a7b.MOV")):
            item = world.google_only_item(name)
            moved = str(PurePosixPath(item.path).with_name(new_name))
            (world.root / item.path).rename(world.root / moved)
            if (world.root / f"{item.path}.xmp").exists():
                (world.root / f"{item.path}.xmp").rename(world.root / f"{moved}.xmp")
            world.opened.execute(update(MediaItem).where(MediaItem.id == item.id).values(path=moved, captured_utc=None))
            world.opened.add(ManagedBackfillItem(person_id=item.person_id, item_id=item.id, outcome="exception", reason="live_photo", checked_at=NOW, since=NOW))
            renamed.append(new_name)
        world.opened.commit()
        before = world.library()
        with no_leaks(self, KEY, SECRET, *renamed, world.fake.url):
            self.assertEqual(world.backfill().mapped, 2)
        self.assertEqual(world.report(), {})
        self.assertEqual(world.library(), before)
        video, still = world.fake.state.assets
        self.assertEqual((still.name, video.name, still.live_video_id, video.visibility), (*renamed, video.id, "hidden"))
        self.assertEqual((video.exif["latitude"], video.exif["longitude"]), (still.exif["latitude"], still.exif["longitude"]))
        compound = world.opened.execute(select(ManagedCompound)).scalar_one()
        still_item, video_item = (world.google_only_item(name) for name in renamed)
        self.assertEqual((compound.compound_key, compound.committed_at), (f"backfill-live:{still_item.id}", NOW + 100))
        intent = world.opened.get(ManagedImportIntent, compound.intent_id)
        self.assertEqual((intent.kind, intent.export_id, json.loads(intent.resource_manifest)),
                         ("backfill", export.id, {"compound": "members", "resource": "media_item", "schema": 2}))
        members = world.opened.execute(select(ManagedCompoundMember.resource_key, ManagedCompoundMember.role)).all()
        self.assertCountEqual(members, [(f"media-item:{item.id}", role) for item, role in ((still_item, "live_still"), (video_item, "live_video"))])
        for item, asset in ((still_item, still), (video_item, video)):
            mapped = world.opened.execute(select(ManagedBackfillItem).where(ManagedBackfillItem.item_id == item.id)).scalar_one()
            receipt = world.opened.get(ManagedImportReceipt, mapped.receipt_id)
            version = world.opened.get(ManagedAssetVersion, mapped.version_id)
            self.assertEqual((receipt.state, receipt.asset_id, receipt.verified_sha1, receipt.verified_bytes), ("committed", asset.id, item.sha1, item.bytes))
            self.assertEqual((version.linked_video_asset_id, version.owner_id, version.bytes), (asset.live_video_id, OWNER, item.bytes))
            self.assertEqual((sha1(asset.data), len(asset.data)), (item.sha1, item.bytes))
            self.assertTrue(backfill.copied(world.opened, item.id))
        self.assertEqual((world.backfill(at=NOW + 200).mapped, len(world.uploads())), (0, 2))

    def test_live_photo_backfill_resumes_the_frozen_pair_after_a_failed_still_send(self):
        world = BackfillWorld(self)
        world.run_managed(live_only, parts=1)

        def refuse_still(method, path, slug):
            return 503 if (method, path) == ("POST", "/api/assets") and world.fake.state.assets else None

        world.fake.state.intercept = refuse_still
        self.assertEqual(world.backfill().mapped, 0)
        self.assertEqual(world.report(), {"pending": ["IMG_3001.HEIC", "IMG_3001.MOV"]})
        video, = world.fake.state.assets
        self.assertEqual((video.kind, video.name), ("VIDEO", "IMG_3001.MOV"))
        before = world.library()
        world.opened.execute(delete(DedupeDecision))
        world.opened.commit()
        world.fake.state.intercept = None
        self.assertEqual(world.backfill(at=NOW + 2 * managed_upload.LEASE_SECONDS).mapped, 2)
        self.assertEqual((world.report(), world.library()), ({}, before))
        matched_video, still = world.fake.state.assets
        self.assertEqual((matched_video.id, still.live_video_id), (video.id, video.id))

    def test_a_whole_existing_managed_live_photo_maps_both_duplicates_without_an_upload(self):
        world = BackfillWorld(self)
        world.run_managed(live_only, parts=1)
        still_item, video_item = (world.google_only_item(name) for name in ("IMG_3001.HEIC", "IMG_3001.MOV"))
        still_id, video_id = (str(uuid.uuid4()) for _ in range(2))
        world.fake.add_asset(FakeAsset(video_id, OWNER, world.data(video_item), kind="VIDEO", visibility="hidden", exif=world.extracted(video_item, still_item)))
        world.fake.add_asset(FakeAsset(still_id, OWNER, world.data(still_item), live_video_id=video_id, exif=world.extracted(still_item)))
        self.assertEqual(world.backfill().mapped, 2)
        self.assertEqual((world.report(), world.uploads()), ({}, []))
        rows = world.opened.execute(select(ManagedImportReceipt.asset_id, ManagedImportReceipt.upload_outcome, ManagedImportReceipt.state)).all()
        self.assertCountEqual(rows, [(still_id, "duplicate", "committed"), (video_id, "duplicate", "committed")])

    def test_a_standalone_duplicate_video_is_held_without_being_linked_or_hidden(self):
        world = BackfillWorld(self)
        world.run_managed(live_only, parts=1)
        video = world.google_only_item("IMG_3001.MOV")
        asset = FakeAsset(str(uuid.uuid4()), OWNER, world.data(video), kind="VIDEO")
        world.fake.add_asset(asset)
        self.assertEqual(world.backfill().mapped, 0)
        self.assertEqual((world.report(), world.uploads(), asset.visibility), ({"compound_conflict": ["IMG_3001.HEIC", "IMG_3001.MOV"]}, [], "timeline"))

    def test_a_changed_live_photo_half_blocks_both_uploads(self):
        world = BackfillWorld(self)
        world.run_managed(live_only, parts=1)
        item = world.google_only_item("IMG_3001.HEIC")
        (world.root / item.path).write_bytes(b"the original changed")
        before = world.library()
        self.assertEqual(world.backfill().mapped, 0)
        self.assertEqual((world.report(), world.uploads(), world.library()), ({"changed": ["IMG_3001.HEIC", "IMG_3001.MOV"]}, [], before))

    def test_a_live_photo_whose_placement_decisions_are_missing_is_held_even_when_the_ledger_preserves_the_names(self):
        world = BackfillWorld(self)
        world.run_managed(live_only, parts=1)
        world.opened.execute(delete(DedupeDecision))
        world.opened.commit()
        self.assertEqual(world.backfill().mapped, 0)
        self.assertEqual((world.report(), world.uploads(), world.count(ManagedCompound)),
                         ({"live_photo": ["IMG_3001.HEIC", "IMG_3001.MOV"]}, [], 0))

    def test_two_videos_claiming_one_still_leave_all_halves_held(self):
        world = BackfillWorld(self)

        def ambiguous(builder):
            live_only(builder)
            builder.video("IMG_3001.MP4", form=None)

        world.run_managed(ambiguous, parts=1)
        self.assertEqual(world.backfill().mapped, 0)
        self.assertEqual((world.report(), world.uploads(), world.count(ManagedCompound)),
                         ({"live_ambiguous": ["IMG_3001.HEIC", "IMG_3001.MOV", "IMG_3001.MP4"]}, [], 0))

    def test_phone_live_photo_maps_both_verified_originals_and_link_without_upload(self):
        world = BackfillWorld(self)
        still_name, video_name = "IMG_7002.JPG", "IMG_7002.MOV"
        world.original("jo", f"immich/2024/{still_name}", world.scratch_jpeg("phone-live.jpg", colour=(3, 2, 1), **MOMENT))
        world.original("jo", f"immich/2024/{video_name}", TINY_VIDEO.read_bytes())
        world.index_all()
        still, video = (world.item(f"jo/originals/immich/2024/{name}") for name in (still_name, video_name))
        still_id, video_id = (str(uuid.uuid4()) for _ in range(2))
        for item, asset, linked, role in ((still, still_id, video_id, "photo"), (video, video_id, still_id, "live_video")):
            world.opened.add(ImmichAsset(person_id=item.person_id, asset_id=asset, kind=role, status="stored", sha1=item.sha1, linked_asset_id=linked,
                                        original_item_id=item.id, seen_at=NOW))
        world.opened.commit()
        world.fake.add_asset(FakeAsset(still_id, OWNER, world.data(still), name=still_name, live_video_id=video_id))
        effect = FakeAsset(video_id, OWNER, world.data(video), name=video_name, kind="VIDEO", visibility="timeline")
        world.fake.add_asset(effect)
        before = world.library()
        self.assertEqual(world.backfill().mapped, 0)
        self.assertEqual((world.report(), world.uploads()), ({"unusable": sorted((still_name, video_name))}, []))
        self.assertEqual(world.count(ManagedAssetVersion), 0)
        backfill_rows = world.opened.execute(select(ManagedBackfillItem)).scalars().all()
        for row in backfill_rows:
            world.opened.delete(row)
        world.opened.commit()
        effect.visibility = "hidden"
        self.assertEqual(world.backfill(at=NOW + 200).mapped, 2)
        versions = world.opened.execute(select(ManagedAssetVersion.asset_id, ManagedAssetVersion.linked_video_asset_id)).all()
        self.assertCountEqual(versions, [(still_id, video_id), (video_id, None)])
        kinds = world.opened.execute(select(ManagedAssetOrigin.kind)).scalars().all()
        self.assertEqual((world.report(), world.uploads(), world.library(), kinds), ({}, [], before, ["phone", "phone"]))

    def test_pixel_copies_left_unconverted_are_converted_on_a_later_run_without_another_upload(self):
        world = BackfillWorld(self)
        world.seeded()
        phone = world.item(f"jo/originals/{PHONE}")
        removal = DuplicateRemoval(person_id=world.ids["jo"], kept_item_id=world.google_only_item("IMG_5001.JPG").id, removed_item_id=phone.id,
                                   kept_path="jo/google-only/kept.jpg", kept_sha1="b" * 40, removed_path=phone.path, removed_sha1=phone.sha1,
                                   removed_source="phone", rule="takeout_better", tier=2, state="pending", created_at=NOW)
        for setting, pending, references, reason in ((False, False, 0, None), (True, True, 0, "pixel_refused"), (True, False, 1, None)):
            with self.subTest(setting=setting, pending=pending):
                world.settings = dataclasses.replace(world.settings, pixel_immich_source=setting)
                if pending:
                    world.opened.add(removal)
                elif removal.id is not None:
                    removal.state = "refused"
                world.opened.commit()
                uploads = len(world.uploads())
                world.backfill()
                world.opened.expire_all()
                self.assertEqual(len(world.opened.execute(select(ManagedPixelSource.id)).all()), references)
                self.assertEqual(world.opened.execute(select(ManagedBackfillItem.outcome, ManagedBackfillItem.reason)
                                                      .where(ManagedBackfillItem.item_id == phone.id)).one(), ("mapped", reason))
                if setting:
                    self.assertEqual(len(world.uploads()), uploads)

    def test_a_live_photo_half_whose_companion_matched_a_phone_original_is_listed_and_not_uploaded(self):
        world = BackfillWorld(self)

        def one_half_each(builder):
            still = builder.image_bytes(".HEIC")
            world.original("jo", "IMG_3002.HEIC", still)
            builder.photo("IMG_3002.HEIC", data=still, part=0)
            builder.add("IMG_3002.MOV", builder.video_bytes(), "Photos from 2019", 0, "json", None, None, "")
            builder.manifest.live_pairs["IMG_3002.MOV"] = "IMG_3002.HEIC"
            video = builder.video_bytes()
            world.original("jo", "IMG_3003.MOV", video)
            builder.photo("IMG_3003.JPG", part=0)
            builder.add("IMG_3003.MOV", video, "Photos from 2019", 0, "json", None, None, "")
            builder.manifest.live_pairs["IMG_3003.MOV"] = "IMG_3003.JPG"
        world.run_managed(one_half_each, parts=1)
        placed = sorted(PurePosixPath(path).name for path in world.opened.execute(select(MediaItem.path).where(MediaItem.path.startswith("jo/google-only/"),
                                                                                                                 MediaItem.source == "takeout")).scalars())
        self.assertEqual(placed, ["IMG_3002.MOV", "IMG_3003.JPG"])
        world.backfill()
        self.assertEqual((world.report(), world.uploads()), ({"live_partial": ["IMG_3002.MOV", "IMG_3003.JPG"]}, []))

    def test_settled_items_do_not_rescan_for_live_photo_companions(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        with mock.patch.object(backfill, "live_halves", wraps=backfill.live_halves) as scanned:
            world.backfill()
            world.backfill()
        self.assertEqual(scanned.call_count, 1)

    def test_companions_are_found_by_index_lookups_not_by_comparing_every_pair(self):
        world = BackfillWorld(self)
        items = [MediaItem(id=number, person_id=world.ids["jo"], source="takeout", path=f"jo/google-only/2019/07/IMG_{number:05d}.JPG", sha1="a" * 40,
                           bytes=1, captured_utc=1_500_000_000.0 + number * 10, sidecar_from_still=False, indexed_at=NOW) for number in range(1, 4001)]
        items.append(MediaItem(id=5000, person_id=world.ids["jo"], source="takeout", path="jo/google-only/2019/07/IMG_00007.MOV", sha1="b" * 40, bytes=1,
                               captured_utc=None, sidecar_from_still=False, indexed_at=NOW))
        items.append(MediaItem(id=5001, person_id=world.ids["jo"], source="takeout", path="jo/google-only/2019/07/CLIP_9.MOV", sha1="c" * 40, bytes=1,
                               captured_utc=1_500_000_000.0 + 90.4, sidecar_from_still=False, indexed_at=NOW))
        items[10].export_id = 77
        index = backfill.Companions()
        index.add(("ledger", 77), PurePosixPath("Takeout/Google Photos/Photos from 2017/CLIP_11.MOV"), items[10].captured_utc + 0.3)
        halves = backfill.live_halves(world.opened, world.jo(), items, items, index)
        self.assertEqual(halves, {7, 5000, 9, 5001, 11})
        self.assertLessEqual(index.lookups, 5 * len(items))

    def test_the_scheduled_pixel_job_places_a_backfilled_copy_with_the_same_key_and_only_reads(self):
        world = BackfillWorld(self)
        world.seeded()
        world.backfill()
        world.fake.state.requests.clear()
        readers = jobs.pixel_readers(world.settings)
        self.assertIsNotNone(readers)
        tended = pixel_folder.tend(world.opened, world.photos, pixel_folder.DEFAULT_CAP_BYTES, NOW + 300, readers)
        self.assertEqual((tended.placed, tended.blocked), (1, 0))
        phone = world.item(f"jo/originals/{PHONE}")
        self.assertEqual(world.opened.execute(select(PixelCopy.state).where(PixelCopy.item_id == phone.id)).scalar_one(), "placed")
        self.assertEqual({method for method, _ in world.fake.requests()}, {"GET"})

    def test_a_photo_of_an_export_still_being_imported_waits_for_its_last_part(self):
        world = BackfillWorld(self)

        def split_pair(builder):
            builder.photo("IMG_3005.JPG", part=0)
            builder.add("IMG_3005.MOV", builder.video_bytes(), "Photos from 2019", 1, "json", None, None, "")
            builder.manifest.live_pairs["IMG_3005.MOV"] = "IMG_3005.JPG"
        export = world.create(split_pair, parts=2)
        world.ingest(export, 1)
        world.backfill()
        self.assertEqual((world.report(), world.uploads(), world.count(ManagedBackfillItem)), ({}, [], 0))
        world.ingest(export, 2)
        world.finish(export)
        world.backfill(at=NOW + 200)
        self.assertEqual((world.report(), world.uploads()), ({"unknown_offset": ["IMG_3005.JPG", "IMG_3005.MOV"]}, []))

    def test_retries_rotate_so_every_waiting_photo_is_tried_again(self):
        world = BackfillWorld(self)
        jo = world.jo()
        jo.managed_backfill = backfill.ON
        rows = [MediaItem(person_id=jo.id, source="takeout", path=f"jo/google-only/2030/01/IMG_{number:04d}.JPG", sha1=f"{number:040x}", bytes=1,
                          sidecar_from_still=False, indexed_at=NOW) for number in range(1, 61)]
        world.opened.add_all(rows)
        world.opened.flush()
        world.opened.add_all(ManagedBackfillItem(person_id=jo.id, item_id=row.id, outcome="exception", reason="pending", checked_at=NOW, since=NOW) for row in rows)
        world.opened.commit()
        for at in (NOW + 10, NOW + 20):
            backfill.run_person(world.opened, world.settings, world.photos, world.jo(), at, tools=NO_PAUSE, limit=50)
        world.opened.expire_all()
        checked = world.opened.execute(select(ManagedBackfillItem.checked_at, ManagedBackfillItem.reason)).all()
        self.assertEqual((len(checked), min(at for at, _ in checked), {reason for _, reason in checked}), (60, NOW + 10, {"pending"}))

    def test_running_again_uploads_nothing_more(self):
        world = BackfillWorld(self)
        world.seeded()
        world.backfill()
        uploads, rows = len(world.uploads()), world.count(ManagedImportReceipt)
        second = world.backfill()
        self.assertEqual((len(world.uploads()), world.count(ManagedImportReceipt), second.mapped), (uploads, rows, 0))

    def test_a_crash_after_the_upload_resumes_from_the_receipt_without_a_second_upload(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        real = managed_metadata.verify_metadata

        def crash(*args, **kwargs):
            raise Crash()
        with mock.patch.object(managed_metadata, "verify_metadata", crash), self.assertRaises(Crash):
            world.backfill()
        world.opened.rollback()
        self.assertEqual(world.opened.execute(select(ManagedImportReceipt.state)).scalars().all(), ["bytes_verified"])
        with mock.patch.object(managed_metadata, "verify_metadata", real):
            world.backfill()
        self.assertEqual(world.opened.execute(select(ManagedImportReceipt.state)).scalars().all(), ["committed"])
        self.assertEqual((len(world.uploads()), len(world.fake.state.assets)), (1, 1))

    def test_a_lost_upload_answer_is_recovered_by_checksum_and_not_sent_twice(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        world.fake.state.lose_uploads = 1
        world.backfill()
        self.assertEqual(world.opened.execute(select(ManagedImportReceipt.state)).scalars().all(), ["committed"])
        self.assertEqual(len(world.fake.state.assets), 1)

    def test_a_photo_whose_immich_copy_is_unusable_is_held_with_its_kind_and_is_not_read_again_by_the_next_run(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        item = world.google_only_item("IMG_5001.JPG")
        trashed = FakeAsset(str(uuid.uuid4()), OWNER, world.data(item), name="IMG_9001.JPG", exif=world.extracted(item), trashed=True)
        world.fake.add_asset(trashed)
        world.backfill()
        row = world.opened.execute(select(ManagedImportReceipt)).scalar_one()
        self.assertEqual((row.state, row.metadata_hold, json.loads(str(row.hold_facts))), ("returned", "unusable", {"kind": "trashed"}))
        self.assertEqual(world.report(), {"unusable": ["IMG_5001.JPG"]})
        trashed.trashed = False
        seen = len(world.fake.requests())
        world.backfill(at=NOW + 200)
        self.assertEqual(len(world.fake.requests()), seen)
        self.assertEqual(world.report(), {"unusable": ["IMG_5001.JPG"]})
        self.assertEqual(world.opened.execute(select(ManagedImportReceipt.state, ManagedImportReceipt.metadata_hold)).one(), ("returned", "unusable"))

    def test_nothing_is_backfilled_without_the_persons_own_opt_in(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        jo = world.jo()
        jo.managed_imports = "on"
        world.opened.commit()
        summary = world.backfill(opt_in=None)
        self.assertEqual((summary.mapped, world.uploads(), world.count(ManagedImportIntent), world.count(ManagedBackfillItem)), (0, [], 0, 0))

    def test_an_immich_outage_leaves_the_photo_pending_and_it_is_tried_again(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        world.fake.mode("500")
        world.backfill()
        self.assertEqual((world.report(), world.fake.state.assets), ({"pending": ["IMG_5001.JPG"]}, []))
        world.fake.mode("ok")
        self.assertEqual(world.backfill().mapped, 1)
        self.assertEqual(world.report(), {})

    def test_a_file_that_is_gone_a_link_or_unreadable_is_listed_and_not_uploaded(self):
        cases = {"gone": ("missing", lambda path: path.unlink()), "a link": ("missing", lambda path: (path.unlink(), path.symlink_to(path.with_name("x.JPG")))),
                 "unreadable": ("unreadable", None)}
        for name, (reason, change) in cases.items():
            with self.subTest(name):
                world = BackfillWorld(self)
                world.run_managed(single_photo, parts=1)
                path = world.root / world.google_only_item("IMG_5001.JPG").path
                if change is not None:
                    change(path)
                tools = backfill.Tools(read=lambda found: {key: {"Error": "File format error"} for key in found}, pause=lambda seconds: None)
                world.backfill(tools=tools if change is None else NO_PAUSE)
                self.assertEqual((world.report(), world.uploads()), ({reason: ["IMG_5001.JPG"]}, []))

    def test_a_transient_read_or_exiftool_error_stays_pending_is_retried_and_alerts_after_a_day(self):
        def timed_out(found):
            raise exiftool.TimedOut("exiftool took too long to read the files")
        failures = {"read error": (mock.patch.object(backfill.paths, "open_library_file", side_effect=PermissionError(13, "Permission denied")), NO_PAUSE),
                    "exiftool timeout": (mock.patch.object(backfill, "sha1_of", backfill.sha1_of), backfill.Tools(read=timed_out, pause=lambda seconds: None))}
        for name, (patched, tools) in failures.items():
            with self.subTest(name):
                world = BackfillWorld(self)
                world.run_managed(single_photo, parts=1)
                with patched:
                    world.backfill(tools=tools)
                    world.backfill(tools=tools, at=NOW + 2 * 86400)
                self.assertEqual((world.report(), world.uploads()), ({"pending": ["IMG_5001.JPG"]}, []))
                stuck = world.opened.execute(select(Alert).where(Alert.key.contains(":stuck:"))).scalar_one()
                self.assertIn("1 photo of jo could not be backfilled", stuck.message)
                self.assertEqual(world.backfill(at=NOW + 3 * 86400).mapped, 1)
                self.assertEqual(world.report(), {})

    def test_a_folder_or_photo_root_that_is_not_there_leaves_the_photo_pending_until_it_is_back(self):
        for name, folder in (("person folder", ("jo",)), ("photo root", ())):
            with self.subTest(name):
                world = BackfillWorld(self)
                world.run_managed(single_photo, parts=1)
                gone = world.root.joinpath(*folder)
                aside = gone.with_name(f"{gone.name}-unmounted")
                gone.rename(aside)
                if not folder:
                    gone.mkdir()
                world.backfill()
                self.assertEqual((world.report(), world.uploads()), ({"pending": ["IMG_5001.JPG"]}, []))
                if not folder:
                    gone.rmdir()
                aside.rename(gone)
                self.assertEqual(world.backfill(at=NOW + 200).mapped, 1)
                self.assertEqual(world.report(), {})

    def test_a_photo_taken_back_while_the_run_reads_it_is_left_alone_and_the_run_goes_on(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        item_id, real = world.google_only_item("IMG_5001.JPG").id, backfill.sha1_of

        def taken_back(handle):
            with world.engine.begin() as connection:
                connection.execute(delete(MediaItem).where(MediaItem.id == item_id))
            return real(handle)
        with mock.patch.object(backfill, "sha1_of", taken_back):
            summary = world.backfill()
        self.assertEqual((dict(summary.exceptions), world.uploads(), world.count(ManagedBackfillItem), world.count(ManagedImportReceipt)), ({}, [], 0, 0))

    def test_an_exception_for_a_photo_taken_back_before_it_is_written_is_neither_counted_nor_alerted(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        item_id = world.google_only_item("IMG_5001.JPG").id

        def taken_back(*args):
            with world.engine.begin() as connection:
                connection.execute(delete(MediaItem).where(MediaItem.id == item_id))
            return {item_id}
        with mock.patch.object(backfill, "live_halves", taken_back):
            summary = world.backfill()
        self.assertEqual((dict(summary.exceptions), world.count(ManagedBackfillItem)), ({}, 0))
        self.assertEqual(world.opened.execute(select(Alert.id).where(Alert.key.startswith("managed:backfill:"))).all(), [])

    def test_an_exception_recorded_for_another_person_does_not_settle_the_photo_for_its_owner(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        item = world.google_only_item("IMG_5001.JPG")
        world.opened.add(ManagedBackfillItem(person_id=world.ids["sam"], item_id=item.id, outcome="exception", reason="same_shot", checked_at=NOW, since=NOW))
        world.opened.commit()
        self.assertEqual(world.backfill().mapped, 1)
        world.opened.expire_all()
        self.assertEqual(world.opened.execute(select(ManagedBackfillItem.person_id, ManagedBackfillItem.outcome)).one(), (world.ids["jo"], "mapped"))

    def test_an_edited_copy_is_listed_as_a_same_shot_and_not_uploaded_whatever_became_of_its_original(self):
        for evidence in ("decision", "ledger", "library name"):
            for phone in (False, True):
                with self.subTest(evidence=evidence, phone=phone):
                    world = BackfillWorld(self)
                    world.run_managed(single_photo, parts=1)
                    item = world.google_only_item("IMG_5001.JPG")
                    name = "IMG_5001.JPG"
                    if evidence == "decision":
                        world.opened.execute(update(DedupeDecision).where(DedupeDecision.takeout_item_id == item.id)
                                             .values(source_path="Takeout/Google Photos/Photos from 2019/IMG_5001-edited.JPG"))
                    elif evidence == "ledger":
                        world.opened.execute(update(TakeoutEntry).where(TakeoutEntry.item_id == item.id).values(is_edited=True, edited_of="IMG_5000.JPG"))
                    else:
                        name = "IMG_5001-edited.JPG"
                        moved = str(PurePosixPath(item.path).with_name(name))
                        (world.root / item.path).rename(world.root / moved)
                        world.opened.execute(update(MediaItem).where(MediaItem.id == item.id).values(path=moved))
                    world.opened.commit()
                    if phone:
                        world.original("jo", "IMG_8001.JPG", world.scratch_jpeg("unrelated.jpg", colour=(1, 2, 3)))
                        world.index_all()
                    world.backfill()
                    self.assertEqual((world.report(), world.uploads()), ({"same_shot": [name]}, []))

    def test_a_photo_of_a_failed_or_expired_export_is_listed_and_backfilled_once_the_export_is_imported(self):
        for status in ("failed", "expired"):
            with self.subTest(status=status):
                world = BackfillWorld(self)
                export = world.run_managed(single_photo, parts=1)
                world.opened.execute(update(Export).where(Export.id == export.id).values(status=status))
                world.opened.commit()
                world.backfill()
                self.assertEqual((world.report(), world.uploads()), ({"export_unfinished": ["IMG_5001.JPG"]}, []))
                world.opened.execute(update(Export).where(Export.id == export.id).values(status="imported"))
                world.opened.commit()
                self.assertEqual(world.backfill(at=NOW + 200).mapped, 1)
                self.assertEqual(world.report(), {})

    def test_an_export_retried_while_the_run_decides_is_not_uploaded_from(self):
        world = BackfillWorld(self)
        export = world.run_managed(single_photo, parts=1)
        world.opened.execute(update(Export).where(Export.id == export.id).values(status="failed"))
        world.opened.commit()
        real = backfill.live_halves

        def retried(*args):
            with world.engine.begin() as connection:
                connection.execute(update(Export).where(Export.id == export.id).values(status="downloading"))
            return real(*args)
        with mock.patch.object(backfill, "live_halves", retried):
            world.backfill()
        self.assertEqual((world.uploads(), world.count(ManagedImportReceipt)), ([], 0))
        rows = world.opened.execute(select(ManagedBackfillItem.outcome, ManagedBackfillItem.reason, ManagedBackfillItem.version_id,
                                           ManagedBackfillItem.receipt_id)).tuples().all()
        self.assertEqual(rows, [("exception", "export_unfinished", None, None)])
        self.assertEqual(world.report(), {"export_unfinished": ["IMG_5001.JPG"]})

    def test_an_export_retried_after_the_run_read_it_as_imported_is_refused_under_the_write_lock(self):
        world = BackfillWorld(self)
        export = world.run_managed(single_photo, parts=1)
        real = backfill.sha1_of

        def retried(handle):
            with world.engine.begin() as connection:
                connection.execute(update(Export).where(Export.id == export.id).values(status="importing", stage="unpacked"))
            return real(handle)
        with mock.patch.object(backfill, "sha1_of", retried):
            world.backfill()
        self.assertEqual((world.uploads(), world.count(ManagedImportReceipt), world.count(ManagedBackfillItem), world.report()), ([], 0, 0, {}))
        world.opened.execute(update(Export).where(Export.id == export.id).values(status="imported"))
        world.opened.commit()
        self.assertEqual(world.backfill(at=NOW + 200).mapped, 1)
        self.assertEqual((world.count(ManagedImportReceipt), world.report()), (1, {}))

    def test_an_export_imported_after_the_run_read_its_ledger_waits_for_the_next_run(self):
        world = BackfillWorld(self)
        export = world.run_managed(single_photo, parts=1)
        world.opened.execute(update(Export).where(Export.id == export.id).values(status="failed"))
        world.opened.commit()
        real = backfill.live_halves

        def imported_meanwhile(*args):
            found = real(*args)
            with world.engine.begin() as connection:
                connection.execute(update(Export).where(Export.id == export.id).values(status="imported"))
            return found
        with mock.patch.object(backfill, "live_halves", imported_meanwhile):
            world.backfill()
        self.assertEqual((world.report(), world.uploads()), ({"export_unfinished": ["IMG_5001.JPG"]}, []))
        self.assertEqual(world.backfill(at=NOW + 200).mapped, 1)

    def test_a_changed_file_is_reported_and_not_uploaded(self):
        world = BackfillWorld(self)
        world.run_managed(single_photo, parts=1)
        item = world.google_only_item("IMG_5001.JPG")
        world.opened.execute(update(MediaItem).where(MediaItem.id == item.id).values(sha1="f" * 40))
        world.opened.commit()
        world.backfill()
        self.assertEqual((world.report(), world.uploads()), ({"changed": ["IMG_5001.JPG"]}, []))


def single_photo(builder):
    builder.photo("IMG_5001.JPG", gps=(45.4, 12.3), description="A made-up quay", part=0)


def live_only(builder):
    builder.live_photo("IMG_3001", part=0, video_sidecar=False)
