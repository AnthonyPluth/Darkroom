import dataclasses
import hashlib
import unittest
import uuid
from pathlib import PurePosixPath
from unittest import mock

from sqlalchemy import select, update

from darkroom.domain import backfill, managed_metadata, xmp
from darkroom.domain.ingest import place
from darkroom.providers import exiftool
from darkroom.storage.models import (Alert, DuplicateRemoval, Export, ImmichAsset, ManagedAssetOrigin, ManagedAssetVersion, ManagedBackfillItem, ManagedImportIntent,
                                     ManagedImportReceipt, ManagedPixelSource, MediaItem, PixelCopy)
from tests.fakes.immich import FakeAsset, extract
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

    def extracted(self, item):
        path = self.root / item.path
        intended = managed_metadata.intend(exiftool.read([path])[path], xmp.GoogleMetadata.of(item.sidecar_json), "IMAGE")
        packet = managed_metadata.sidecar_source(intended)
        return extract(self.data(item), None if packet is None else packet.reader.read())

    def backfill(self, opt_in=backfill.ON):
        jo = self.jo()
        jo.managed_backfill = opt_in
        self.opened.commit()
        return backfill.run_all(self.opened, self.settings, self.photos, NOW + 100, tools=NO_PAUSE)

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
        self.assertEqual(world.report(), {"live_photo": ["IMG_3001.HEIC", "IMG_3001.MOV"], "same_shot": ["IMG_6001.JPG"], "no_instant": ["IMG_5005.JPG"],
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

    def test_live_photo_halves_renamed_on_a_name_collision_are_listed_and_not_uploaded(self):
        world = BackfillWorld(self)
        world.run_managed(live_only, parts=1)
        renamed = []
        for name, new_name in (("IMG_3001.HEIC", "IMG_3001-0a1b2c3d.HEIC"), ("IMG_3001.MOV", "IMG_3001-4e5f6a7b.MOV")):
            item = world.google_only_item(name)
            moved = str(PurePosixPath(item.path).with_name(new_name))
            (world.root / item.path).rename(world.root / moved)
            if (world.root / f"{item.path}.xmp").exists():
                (world.root / f"{item.path}.xmp").rename(world.root / f"{moved}.xmp")
            world.opened.execute(update(MediaItem).where(MediaItem.id == item.id).values(path=moved, sidecar_from_still=False, captured_utc=None))
            renamed.append(new_name)
        world.opened.commit()
        world.backfill()
        self.assertEqual((world.report(), world.uploads()), ({"live_photo": sorted(renamed)}, []))

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
    builder.live_photo("IMG_3001", part=0)
