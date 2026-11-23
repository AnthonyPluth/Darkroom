import contextlib
import dataclasses
import hashlib
import io
import re
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from sqlalchemy import select, update

import run as cli
from darkroom.domain import immich_reconcile, managed_ingest, managed_upload
from darkroom.domain.ingest import cleanup, part_free, place, run
from darkroom.storage import db, secretbox
from darkroom.storage.models import Alert, Event, Export, ImportRun, ManagedImportIntent, ManagedImportReceipt, Person, TakeoutEntry
from tests.fakes.immich import FakeAsset, FakePerson, extract
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.takeout_fixtures import GOOGLE_PHOTOS
from tests.test_cli_import import CliCase
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
        return {names[row.resource_key]: (row.state, row.upload_outcome, row.metadata_hold) for row in rows}

    def assets(self):
        return sorted((asset.name, hashlib.sha1(asset.data, usedforsecurity=False).hexdigest()) for asset in self.fake.state.assets if asset.owner_id == OWNER)

    def bytes_of(self, export, name):
        with zipfile.ZipFile(Path(export.inbox_dir) / self.part(export, 1).filename) as archive:
            return archive.read(f"{GOOGLE_PHOTOS}/{PHOTOS}{name}")

    def zips(self, export):
        return sorted(path.name for path in Path(export.inbox_dir).glob("*.zip"))

    def alert(self, key):
        self.opened.expire_all()
        return self.opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalar_one_or_none()

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
        intent = world.opened.execute(select(ManagedImportIntent)).scalar_one()
        self.assertEqual((intent.person_id, intent.owner_id, intent.resource_manifest), (world.ids["jo"], OWNER, managed_ingest.MANIFEST))

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

    def test_a_photo_immich_may_hold_as_another_file_of_the_same_shot_is_held_and_not_uploaded(self):
        world = ManagedWorld(self)
        export = world.create(single("IMG_5003.JPG"), parts=1)
        world.ingest_all(export, (1,))
        phone = world.scratch_jpeg("phone.jpg", colour=(1, 2, 3))
        world.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, phone, name="IMG_9002.JPG", exif={"dateTimeOriginal": "2017-07-14T02:40:00.400Z"}))
        world.finish(export)
        self.assertEqual(world.receipts(), {"IMG_5003.JPG": ("prepared", None, "same_shot")})
        self.assertEqual([name for name, _ in world.assets()], ["IMG_9002.JPG"])
        self.assertEqual(world.status(export), ("imported", "done"))

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
        self.assertIsNone(world.alert("managed:pending:1"))

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

    def test_stopped_imports_upload_nothing(self):
        world = ManagedWorld(self)
        export = world.create(world.scenario)
        world.ingest_all(export)
        jo = world.jo()
        self.assertEqual(managed_ingest.request(world.opened, jo, False, NOW), managed_ingest.STOPPED)
        world.opened.commit()
        world.finish(export)
        self.assertEqual((world.status(export), world.assets()), (("downloading", "placed"), []))
        self.assertIsNotNone(world.alert("managed:pending:1"))

    def test_an_immich_managed_export_never_places_into_google_only(self):
        world = ManagedWorld(self)
        export = world.create(single("IMG_5002.JPG"), parts=1)
        world.ingest_all(export, (1,))
        with self.assertRaises(place.PlaceFailed):
            place.place_google_only(world.opened, world.photos, world.opened.get(Export, export.id), mock.Mock(), place.PlaceReport(), mock.Mock(), mock.Mock())


class SwitchTests(unittest.TestCase):
    def test_the_switch_waits_for_a_legacy_import_and_the_reconcile_and_then_pauses_them(self):
        world = ManagedWorld(self, state=None)
        legacy = world.create(single("IMG_5101.JPG"), parts=1, google_id="google-123")
        world.ingest(legacy, 1)
        jo = world.jo()
        self.assertEqual(managed_ingest.request(world.opened, jo, True, NOW), managed_ingest.PAUSING)
        world.opened.commit()
        export = world.create(single("IMG_5102.JPG"), parts=1, google_id="google-456", created=NOW - DAY / 2)
        with self.assertRaises(run.NotReady):
            world.ingest(export, 1)
        world.finish(legacy)
        self.assertEqual(world.status(legacy), ("imported", "done"))
        with immich_reconcile.person_lock(world.settings, world.jo()), self.assertRaises(run.NotReady):
            world.ingest(export, 1)
        self.assertEqual((world.jo().managed_imports, world.count(ManagedImportIntent)), (managed_ingest.PAUSING, 0))
        world.ingest(export, 1)
        world.finish(export)
        self.assertEqual((world.jo().managed_imports, world.status(export), [name for name, _ in world.assets()]),
                         (managed_ingest.ON, ("imported", "done"), ["IMG_5102.JPG"]))
        self.assertEqual({key.rsplit("/", 1)[-1] for key in world.google_only()}, {"IMG_5101.JPG"})
        with self.assertRaises(immich_reconcile.NotReady) as raised:
            immich_reconcile.reconcile_person(world.opened, world.settings, world.jo(), NOW)
        self.assertIn("paused", str(raised.exception))
        self.assertTrue(part_free.managed(world.opened, world.opened.get(Export, legacy.id)))
        cleanup.clean(world.opened, world.photos, NOW + 15 * DAY)
        kept = {row.subject: row.detail for row in world.opened.execute(select(Event).where(Event.type == "cleanup_kept")).scalars()}
        self.assertEqual(kept, {f"export {legacy.id}": managed_ingest.PAUSED_CLEANUP, f"export {export.id}": managed_ingest.GATE_OFF})


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.world = ManagedWorld(self)
        self.export = self.world.run_managed(single("IMG_5201.JPG"), parts=1)
        self.intent = self.world.opened.execute(select(ManagedImportIntent)).scalar_one()
        self.asset = self.world.opened.execute(select(ManagedImportReceipt.asset_id)).scalar_one()
        self.later = NOW + 15 * DAY

    def gate(self, **changes):
        values = {"rehearsed_at": NOW, "boundary_accepted_at": NOW, "recovery_point": lambda intent: managed_ingest.RecoveryPoint(NOW + DAY, True, frozenset({self.asset})),
                  "reader": lambda intent: managed_ingest.reader_of(self.world.settings, self.world.jo(), intent, None)}
        return managed_ingest.Retention(**values | changes)

    def refusal(self, **changes):
        return managed_ingest.release_refusal(self.world.opened, self.world.opened.get(Export, self.export.id), self.later, self.gate(**changes))

    def point(self, taken_at, healthy=True, assets=None):
        return lambda intent: managed_ingest.RecoveryPoint(taken_at, healthy, frozenset({self.asset}) if assets is None else assets)

    def test_cleanup_stays_off_without_the_owners_gate_and_keeps_every_zip(self):
        report = cleanup.clean(self.world.opened, self.world.photos, self.later)
        self.assertEqual((report.parts, report.kept, len(self.world.zips(self.export))), (0, 1, 1))
        self.assertEqual(managed_ingest.release_refusal(self.world.opened, self.world.opened.get(Export, self.export.id), self.later, None), managed_ingest.GATE_OFF)

    def test_each_condition_of_the_gate_refuses_on_its_own(self):
        refusals = [self.refusal(rehearsed_at=None), self.refusal(boundary_accepted_at=None), self.refusal(recovery_point=lambda intent: None),
                    self.refusal(recovery_point=self.point(NOW + DAY, healthy=False)), self.refusal(recovery_point=self.point(NOW - DAY)),
                    self.refusal(recovery_point=self.point(NOW + DAY, assets=frozenset()))]
        self.assertEqual(refusals[0], managed_ingest.GATE_OFF)
        self.assertNotIn(None, refusals)
        self.assertEqual(len(set(refusals)), 4)
        asset = next(asset for asset in self.world.fake.state.assets if asset.id == self.asset)
        asset.trashed = True
        self.assertEqual(self.refusal(), "Immich did not confirm every committed asset")
        asset.trashed, asset.checksum = False, "A" * 27 + "="
        self.assertEqual(self.refusal(), "Immich did not confirm every committed asset")
        asset.checksum = None
        self.world.opened.execute(update(ManagedImportReceipt).values(state="metadata_verified", committed_at=None))
        self.world.opened.commit()
        self.assertEqual(self.refusal(), "an item of this export has no committed Immich receipt")
        self.assertEqual(len(self.world.zips(self.export)), 1)

    def test_only_a_passing_gate_lets_the_14_day_cleanup_remove_the_zip(self):
        self.assertIsNone(self.refusal())
        self.assertEqual(cleanup.clean(self.world.opened, self.world.photos, NOW + 13 * DAY, self.gate()).parts, 0)
        self.assertEqual(cleanup.clean(self.world.opened, self.world.photos, self.later, self.gate()).parts, 1)
        self.assertEqual(self.world.zips(self.export), [])
        self.assertEqual(len(self.world.assets()), 1)


class ManagedImportsCommandTests(CliCase):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["managed-imports", *args])
        return code, out.getvalue()

    def state(self):
        with db.session(self.engine) as opened:
            return opened.execute(select(Person.managed_imports).where(Person.slug == "jo")).scalar_one()

    def test_turning_it_on_needs_the_persons_immich_key_and_turning_it_off_after_an_import_stops_it(self):
        self.assertEqual(self.call("on", "--person", "jo")[0], 1)
        self.assertEqual(self.call("on")[0], 1)
        self.assertEqual(self.call("on", "--person", "lee")[0], 1)
        with db.session(self.engine) as opened:
            jo = opened.execute(select(Person).where(Person.slug == "jo")).scalar_one()
            jo.immich_user_id, jo.immich_api_key_sealed = OWNER, secretbox.seal(KEY, "k")
        self.assertEqual(self.call("on", "--person", "jo"), (0, "jo: Immich-managed imports pausing\n"))
        self.assertEqual(self.call("off", "--person", "jo"), (0, "jo: Immich-managed imports off\n"))
        self.assertIsNone(self.state())
        with db.session(self.engine) as opened:
            jo = opened.execute(select(Person).where(Person.slug == "jo")).scalar_one()
            opened.add(Export(id=9, account_id=1, kind="manual"))
            opened.flush()
            opened.add(ManagedImportIntent(person_id=jo.id, export_id=9, owner_id=OWNER, resource_manifest=managed_ingest.MANIFEST, created_at=NOW))
        self.call("on", "--person", "jo")
        self.assertEqual(self.call("off", "--person", "jo")[0], 0)
        self.assertEqual(self.state(), managed_ingest.STOPPED)
        self.assertEqual(self.call("on", "--person", "jo"), (0, "jo: Immich-managed imports on\n"))
