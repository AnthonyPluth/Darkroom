import dataclasses
import hashlib
import os
import re
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import managed_ingest, managed_metadata, managed_upload, review
from darkroom.domain.ingest import cleanup, finish_export, part_free, place
from darkroom.storage import secretbox
from darkroom.storage.models import Alert, Export, ImportRun, ManagedImportIntent, ManagedImportReceipt, Person, ReviewItem, TakeoutEntry
from tests.fakes.immich import FakeAsset, FakePerson, extract
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.takeout_fixtures import GOOGLE_PHOTOS
from tests.test_finish_export import FinishWorld
from tests.test_part_ingest import Crash, short

OWNER = "11111111-1111-4111-8111-111111111111"
KEY = "managed-person-key-canary"
SECRET = "managed-secret-key-canary"
GRANTS = ("asset.download", "asset.read", "asset.upload", "server.about", "user.read")
ALLOWED = re.compile(r"GET /api/(users/me|server/about|api-keys/me|assets/[0-9a-f-]{36}(/original)?)|POST /api/(assets|assets/bulk-upload-check|search/metadata)")
PHOTOS = "Photos from 2019/"


def single(name):
    def build(builder):
        builder.photo(name, gps=(45.4, 12.3), description="A made-up quay", part=0)
    return build


class ManagedWorld(FinishWorld):
    def __init__(self, test, state=managed_ingest.ON):
        super().__init__(test)
        self.settings = dataclasses.replace(self.settings, secret_key=SECRET)
        jo = self.jo()
        jo.immich_user_id, jo.immich_api_key_sealed, jo.managed_imports = OWNER, secretbox.seal(KEY, SECRET), state
        self.opened.commit()
        self.fake.state.people.append(FakePerson("jo-managed", OWNER, "lib-managed", KEY))
        self.fake.state.version, self.fake.state.permissions = "v3.4.1", GRANTS

    def jo(self):
        self.opened.expire_all()
        return self.opened.get(Person, self.ids["jo"])

    def scenario_with_live_photo(self, builder):
        self.scenario(builder)
        builder.live_photo("IMG_3001", part=1)

    def receipts(self):
        self.opened.expire_all()
        names = {f"takeout-entry:{row.id}": short(row.name).removeprefix(PHOTOS) for row in self.opened.execute(select(TakeoutEntry)).scalars()}
        rows = self.opened.execute(select(ManagedImportReceipt).order_by(ManagedImportReceipt.id)).scalars()
        return {names.get(row.resource_key, row.resource_key): (row.state, row.upload_outcome, row.metadata_hold) for row in rows}

    def assets(self):
        return sorted((asset.name, hashlib.sha1(asset.data, usedforsecurity=False).hexdigest()) for asset in self.fake.state.assets if asset.owner_id == OWNER)

    def bytes_of(self, export, name):
        with zipfile.ZipFile(Path(export.inbox_dir) / self.part(export, 1).filename) as archive:
            return archive.read(f"{GOOGLE_PHOTOS}/{PHOTOS}{name}")

    def zips(self, export):
        return sorted(path.name for path in Path(export.inbox_dir).glob("*.zip"))

    def alert(self, key):
        self.opened.expire_all()
        return self.opened.execute(select(Alert).where(Alert.key == key).order_by(Alert.id.desc())).scalars().first()

    def status(self, export):
        self.opened.expire_all()
        found = self.opened.get(Export, export.id)
        return found.status, found.stage

    def result(self, export):
        self.opened.expire_all()
        run_row = self.opened.execute(select(ImportRun).order_by(ImportRun.id.desc())).scalars().first()
        return (self.status(export), self.assets(), {name: state for name, (state, _, hold) in self.receipts().items()}, self.outcomes(),
                self.google_only(), run_row.added, self.zips(export))

    def run_managed(self, build=None, parts=3):
        export = self.create(build or self.scenario, parts=parts)
        self.ingest_all(export, range(1, parts + 1))
        self.finish(export)
        return export


class UploadTests(unittest.TestCase):
    def test_takeout_only_photos_are_uploaded_and_verified_and_nothing_is_written_to_the_old_folders(self):
        world = ManagedWorld(self)
        with no_leaks(self, KEY, SECRET, "IMG_2001.JPG", world.fake.url):
            export = world.run_managed(world.scenario_with_live_photo)
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertEqual([name for name, _ in world.assets()], ["IMG_2001.JPG", "IMG_2002.JPG"])
        self.assertEqual(world.receipts(), {"IMG_2001.JPG": ("committed", "created", None), "IMG_2002.JPG": ("committed", "created", None),
                                            "IMG_2003.JPG": ("prepared", None, "no_instant")})
        self.assertEqual(world.google_only(), set())
        self.assertEqual([key for key in world.files() if key.endswith(".xmp")], [])
        self.assertEqual(len(world.zips(export)), 3)
        self.assertTrue((world.root / "_inbox/jo/review/export-1/IMG_1005.JPG").is_file())
        self.assertEqual(world.outcomes()[f"{PHOTOS}IMG_2001.JPG"], "placed")
        self.assertEqual(world.run_row()[2], 2)
        self.assertIn("3 items of export 1 were not uploaded", world.alert("managed:held:1").message)
        self.assertIsNone(world.alert("managed:pending:1"))
        self.assertEqual([f"{m} {p}" for m, p in world.fake.requests() if not ALLOWED.fullmatch(f"{m} {p}")], [])
        self.assertEqual(world.opened.execute(select(ManagedImportIntent.person_id, ManagedImportIntent.owner_id)).one(), (world.ids["jo"], OWNER))

    def test_with_the_setting_off_an_export_is_placed_in_the_old_folders_as_before(self):
        world = ManagedWorld(self, state=None)
        world.run_managed()
        twin = FinishWorld(self)
        twin.finished(twin.scenario)
        self.assertEqual(world.comparable(), twin.comparable())
        self.assertEqual(world.count(ManagedImportIntent), 0)
        self.assertNotIn(("POST", "/api/assets"), world.fake.requests())
        self.assertIn("IMG_2001.JPG", {key.rsplit("/", 1)[-1] for key in world.google_only()})

    def test_a_photo_immich_already_holds_is_not_uploaded_again_and_is_held_when_it_lacks_the_metadata(self):
        world = ManagedWorld(self)
        export = world.create(single("IMG_5001.JPG"), parts=1)
        data = world.bytes_of(export, "IMG_5001.JPG")
        world.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, data, name="IMG_9001.JPG", exif=extract(data, None)))
        world.ingest_all(export, (1,))
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual(world.assets(), [("IMG_9001.JPG", hashlib.sha1(data, usedforsecurity=False).hexdigest())])
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("bytes_verified", "duplicate", "not_enriched")})
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertIsNotNone(world.alert("managed:held:1"))

    def test_metadata_immich_has_not_settled_keeps_the_export_open_until_it_matches(self):
        world = ManagedWorld(self)
        export = world.create(single("IMG_5004.JPG"), parts=1)
        world.ingest_all(export, (1,))
        with mock.patch.object(managed_ingest.time, "sleep"), mock.patch("tests.fakes.immich.extract", return_value={"dateTimeOriginal": None}):
            world.finish(export)
        self.assertEqual((world.status(export), world.receipts()), (("downloading", "placed"), {"IMG_5004.JPG": ("bytes_verified", "created", "mismatch")}))
        uploaded = world.fake.state.assets[0]
        uploaded.exif = extract(uploaded.data, uploaded.sidecar)
        world.finish(export, at=NOW + 10)
        self.assertEqual((world.status(export), world.receipts()), (("imported", "done"), {"IMG_5004.JPG": ("committed", "created", None)}))

    def test_a_mismatch_that_never_settles_is_held_after_a_day_and_the_export_completes(self):
        world = ManagedWorld(self)
        export = world.create(single("IMG_5011.JPG"), parts=1)
        world.ingest_all(export, (1,))
        with mock.patch.object(managed_ingest.time, "sleep"), mock.patch("tests.fakes.immich.extract", return_value={"dateTimeOriginal": None}):
            world.finish(export)
            world.finish(export, at=NOW + 2 * DAY)
        self.assertEqual((world.status(export), world.receipts()), (("imported", "done"), {"IMG_5011.JPG": ("bytes_verified", "created", "mismatch")}))
        self.assertIsNotNone(world.alert("managed:held:1"))

    def test_keep_both_on_a_managed_review_goes_to_immich_and_is_not_taken_back(self):
        world = ManagedWorld(self)
        export = world.run_managed()
        held = world.opened.execute(select(ReviewItem.id)).scalar_one()
        real = managed_metadata.intend
        with mock.patch.object(managed_metadata, "intend", lambda *args, **kwargs: dataclasses.replace(real(*args, **kwargs), hold="unknown_offset")), \
                self.assertRaisesRegex(review.WrongState, "offset"):
            review.resolve(world.opened, world.photos, held, "keep_both", NOW + 5, review.FileEffects())
        world.reopen()
        review.resolve(world.opened, world.photos, held, "keep_both", NOW + 5, review.FileEffects())
        world.opened.commit()
        hold = world.root / world.opened.get(ReviewItem, held).hold_path
        os.link(hold, hold.with_name("linked.JPG"))
        finish_export.finish_ready(world.opened, world.settings, NOW + 10, world.tools)
        self.assertEqual(world.receipts()[f"review:{held}"][0], "prepared")
        hold.with_name("linked.JPG").unlink()
        finish_export.finish_ready(world.opened, world.settings, NOW + 10, world.tools)
        self.assertEqual(world.receipts()[f"review:{held}"], ("committed", "created", None))
        self.assertIn("IMG_1005.JPG", [name for name, _ in world.assets()])
        self.assertEqual((world.google_only(), world.opened.get(ReviewItem, held).status), (set(), "kept_both"))
        with self.assertRaisesRegex(review.WrongState, "kept in Immich"):
            review.undo(world.opened, world.photos, held, NOW + 20, review.FileEffects())
        self.assertEqual(cleanup.clean(world.opened, world.photos, NOW + 30 * DAY).holds, 0)
        self.assertEqual((len(world.zips(export)), (world.root / world.opened.get(ReviewItem, held).hold_path).is_file()), (3, True))

    def test_a_kept_photo_that_never_shows_its_metadata_alerts_once_and_is_not_polled_again(self):
        world = ManagedWorld(self)
        world.run_managed()
        held = world.opened.execute(select(ReviewItem.id)).scalar_one()
        review.resolve(world.opened, world.photos, held, "keep_both", NOW + 5, review.FileEffects())
        world.opened.commit()
        with mock.patch.object(managed_ingest.time, "sleep"), mock.patch("tests.fakes.immich.extract", return_value={"dateTimeOriginal": None}):
            for at in (NOW + 10, NOW + 3 * DAY, NOW + 4 * DAY):
                finish_export.finish_ready(world.opened, world.settings, at, world.tools)
        self.assertEqual(world.receipts()[f"review:{held}"], ("bytes_verified", "created", managed_ingest.NOT_SETTLED))
        self.assertEqual(len(world.opened.execute(select(Alert).where(Alert.key == f"managed:kept:{held}")).scalars().all()), 1)

    def test_keep_both_refuses_a_live_photo_half_and_records_no_receipt(self):
        world = ManagedWorld(self)
        export = world.opened.get(Export, world.run_managed(single("IMG_5014.JPG"), parts=1).id)
        for hold_path, from_still in (("_inbox/jo/review/export-1/IMG_5015.MOV", False), ("_inbox/jo/review/export-1/IMG_5016.JPG", True)):
            with self.subTest(), self.assertRaisesRegex(managed_ingest.Refused, "Live Photo"):
                managed_ingest.keep_for_immich(world.opened, world.photos, export, ReviewItem(id=90, decision_id=1, hold_path=hold_path, sidecar_from_still=from_still),
                                               world.root / hold_path, "a" * 40, "IMG_5016.JPG", NOW, mock.Mock())
        self.assertEqual(world.count(ManagedImportReceipt), 1)

    def test_a_refused_upload_keeps_every_source_and_the_export_waits_until_it_goes_through(self):
        world = ManagedWorld(self)
        export = world.create(world.scenario)
        world.ingest_all(export)
        world.fake.state.quotas[OWNER] = 1
        world.finish(export)
        self.assertEqual(world.status(export), ("downloading", "placed"))
        self.assertEqual(world.assets(), [])
        self.assertEqual(world.receipts()["IMG_2001.JPG"][0], "outcome_unknown")
        self.assertIn("2 uploads of export 1", world.alert("managed:pending:1").message)
        self.assertEqual(cleanup.clean(world.opened, world.photos, NOW + 30 * DAY).parts, 0)
        self.assertEqual(part_free.free_ready(world.opened, world.settings, NOW + 30 * DAY).parts, 0)
        self.assertEqual(len(world.zips(export)), 3)
        world.fake.state.quotas.clear()
        world.finish(export, at=NOW + 10)
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertEqual(len(world.assets()), 2)
        self.assertEqual(world.opened.execute(select(Alert).where(Alert.key == "managed:pending:1")).scalars().all(), [world.alert("managed:pending:1")])

    def test_a_crash_after_an_upload_resumes_to_the_uninterrupted_result(self):
        world = ManagedWorld(self)
        export = world.create(world.scenario)
        world.ingest_all(export)
        real, crashed = managed_upload.returned, []

        def crash_once(*args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise Crash()
            return real(*args, **kwargs)
        with mock.patch.object(managed_upload, "returned", crash_once), self.assertRaises(Crash):
            world.finish(export)
        world.reopen()
        world.finish(export, at=NOW + 60)
        self.assertEqual(world.status(export), ("downloading", "placed"))
        world.finish(export, at=NOW + 2 * managed_upload.LEASE_SECONDS)
        twin = ManagedWorld(self)
        self.assertEqual(world.result(export), twin.result(twin.run_managed()))
        self.assertEqual(sorted(str(outcome) for _, outcome, _ in world.receipts().values()), ["None", "created", "duplicate"])

    def test_an_immich_managed_export_never_places_into_google_only(self):
        world = ManagedWorld(self)
        export = world.create(single("IMG_5002.JPG"), parts=1)
        world.ingest_all(export, (1,))
        with self.assertRaises(place.PlaceFailed):
            place.place_google_only(world.opened, world.photos, world.opened.get(Export, export.id), mock.Mock(), place.PlaceReport(), mock.Mock(), mock.Mock())
