import dataclasses
import hashlib
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from darkroom.domain import accounts, dedupe, pixel_folder, pixel_source, pixels
from darkroom.providers import immich
from darkroom.server import jobs
from darkroom.storage import paths, secretbox
from darkroom.storage.models import (Alert, DedupeDecision, Event, Export, GoogleAccount, ManagedAssetOrigin, ManagedAssetVersion, ManagedBackfillItem, ManagedImportIntent,
                                     ManagedImportReceipt, ManagedPixelSource, MediaItem, Person, Pixel, PixelCopy, PixelPerson)
from darkroom.storage.paths import Photos
from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson
from tests.privacy import no_leaks
from tests.review_world import GOOGLE_ONLY_NAME, PAIR_NAME, ReviewCase
from tests.shared import add_person, own_database, settings_from
from tests.test_pixel_folder import transport_references

NOW = 1_700_000_000.0
OWNER = "11111111-1111-4111-8111-111111111111"
KEY = "pixel-source-person-key-canary"
ADMIN_KEY = "pixel-source-admin-key-canary"
CANARY = "IMG_name_canary"
SECRET = "pixel-source-secret"
ROOT = Path(__file__).resolve().parent.parent


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


class PixelImmichSourceTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.opened.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = Photos(self.root)
        self.counter = itertools.count(1)
        self.jo = add_person(self.opened, "jo")
        self.opened.get(Person, self.jo).immich_user_id = OWNER
        self.jo_account, self.jo_pixel = self.account("jo@x.test")
        self.opened.commit()
        self.fake = FakeImmich([FakePerson("jo", OWNER, "lib-jo", KEY)], ADMIN_KEY).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version = "v3.4.1"
        client = immich.Client(self.fake.url, ADMIN_KEY, timeout=5)
        self.readers = lambda person: immich.ManagedReader(client, KEY, str(person.immich_user_id))
        self.assets: dict[str, FakeAsset] = {}

    def account(self, email: str) -> tuple[int, int]:
        account = GoogleAccount(email=email, default_person_id=self.jo, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
        self.opened.add(pixel)
        self.opened.flush()
        self.opened.add(PixelPerson(pixel_id=pixel.id, person_id=self.jo))
        self.opened.flush()
        return account.id, pixel.id

    def managed(self, origins: tuple[tuple[str, int | None], ...] = (), state: str = "bytes_verified", on_disk: bool = False) -> tuple[MediaItem, int, str]:
        number = next(self.counter)
        data = f"generated managed original {number}".encode()
        asset_id = f"aaaaaaaa-aaaa-4aaa-8aaa-{number:012d}"
        self.assets[asset_id] = self.fake.add_asset(FakeAsset(asset_id, OWNER, data, name=f"{CANARY}_{number}.HEIC"))
        item = MediaItem(person_id=self.jo, source="phone", path=f"jo/originals/2024/{CANARY}_{number}.HEIC", sha1=sha1(data), bytes=len(data), indexed_at=1.0)
        if on_disk:
            (self.root / item.path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / item.path).write_bytes(data)
        version = ManagedAssetVersion(person_id=self.jo, owner_id=OWNER, asset_id=asset_id, sha1=sha1(data), bytes=len(data), kind="IMAGE", state=state,
                                      metadata_snapshot="{}", seen_at=1.0, bytes_verified_at=1.0 if state == "bytes_verified" else None)
        self.opened.add_all([item, version])
        self.opened.flush()
        for kind, account_id in origins:
            self.origin(version, kind, account_id)
        self.opened.commit()
        return item, version.id, asset_id

    def origin(self, version: ManagedAssetVersion, kind: str, account_id: int | None) -> None:
        values = {"version_id": version.id, "person_id": version.person_id, "owner_id": version.owner_id, "asset_id": version.asset_id, "sha1": version.sha1,
                  "kind": kind, "source_key": f"{kind}-{next(self.counter)}", "evidence_snapshot": "{}", "recorded_at": 1.0}
        if kind == "takeout":
            export = Export(account_id=account_id, kind="manual")
            self.opened.add(export)
            self.opened.flush()
            intent = ManagedImportIntent(person_id=version.person_id, export_id=export.id, owner_id=version.owner_id, resource_manifest="{}", created_at=1.0)
            self.opened.add(intent)
            self.opened.flush()
            receipt = ManagedImportReceipt(intent_id=intent.id, resource_key="r", compound_key="r", source_sha1=version.sha1, expected_bytes=version.bytes,
                                           expected_metadata="{}", state="committed", asset_id=version.asset_id, upload_outcome="created",
                                           verified_sha1=version.sha1, verified_bytes=version.bytes, bytes_verified_at=1.0, metadata_evidence="{}",
                                           metadata_verified_at=1.0, committed_at=1.0, updated_at=1.0)
            self.opened.add(receipt)
            self.opened.flush()
            values |= {"intent_id": intent.id, "receipt_id": receipt.id, "receipt_state": "committed"}
        self.opened.add(ManagedAssetOrigin(**values))
        self.opened.flush()

    def queue(self, item: MediaItem, version_id: int) -> int:
        queued = pixel_folder.queue_managed(self.opened, item, version_id, NOW)
        self.opened.commit()
        return queued

    def tend(self, readers=True, at: float = NOW) -> pixel_folder.Tended:
        return pixel_folder.tend(self.opened, self.photos, pixel_folder.DEFAULT_CAP_BYTES, at, self.readers if readers else None)

    def copy(self, pixel_id: int, item: MediaItem) -> PixelCopy | None:
        return self.opened.execute(select(PixelCopy).where(PixelCopy.pixel_id == pixel_id, PixelCopy.item_id == item.id)
                                   .execution_options(populate_existing=True)).scalar_one_or_none()

    def copy_path(self, pixel_id: int, item: MediaItem) -> Path:
        return self.root / "_pixels" / str(pixel_id) / "jo" / "2024" / Path(item.path).name

    def staged(self) -> list[str]:
        incoming = self.root / "_pixels" / ".incoming"
        return sorted(entry.name for entry in incoming.iterdir()) if incoming.exists() else []

    def held_events(self, row: PixelCopy) -> int:
        return len(self.opened.execute(select(Event.id).where(Event.type == "pixel_copy_source_held", Event.detail.startswith(f"copy {row.id}: "))).all())

    def match(self, item: MediaItem, account_id: int, at: float) -> int:
        export = Export(account_id=account_id, kind="manual")
        self.opened.add(export)
        self.opened.flush()
        decision = DedupeDecision(export_id=export.id, account_id=account_id, source_path=f"Takeout/{next(self.counter)}.HEIC", person_id=self.jo,
                                  matched_item_id=item.id, tier=1, reason="test", decided_by="auto", decided_at=at)
        self.opened.add(decision)
        self.opened.commit()
        return decision.id

    def test_a_managed_copy_is_placed_from_the_verified_immich_original_with_reads_only(self):
        item, version_id, asset_id = self.managed()
        self.assertEqual(self.queue(item, version_id), 1)
        with no_leaks(self, KEY, ADMIN_KEY, CANARY, self.fake.url):
            tended = self.tend()
        self.assertEqual((tended.placed, tended.blocked), (1, 0))
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), self.assets[asset_id].data)
        self.assertFalse((self.root / "jo").exists())
        self.assertEqual(self.staged(), [])
        row = self.copy(self.jo_pixel, item)
        self.assertEqual((row.state, row.name, row.placed_at), ("placed", f"2024/{Path(item.path).name}", NOW))
        source = self.opened.execute(select(ManagedPixelSource)).scalar_one()
        self.assertEqual((source.copy_id, source.generation, source.google_account_id, source.destination_email, source.asset_id),
                         (row.id, 0, self.jo_account, "jo@x.test", asset_id))
        self.assertEqual({method for method, _ in self.fake.requests()}, {"GET"})
        self.assertIn(("GET", f"/api/assets/{asset_id}/original"), self.fake.requests())

    def test_a_changed_missing_or_unavailable_asset_holds_the_row(self):
        cases = {"changed": lambda a: setattr(a, "data", b"other generated bytes"), "missing": self.fake.state.assets.remove,
                 "trashed": lambda a: setattr(a, "trashed", True), "offline": lambda a: setattr(a, "offline", True),
                 "external": lambda a: setattr(a, "library_id", "lib-jo"), "another owner": lambda a: setattr(a, "owner_id", "22222222-2222-4222-8222-222222222222")}
        for name, change in cases.items():
            with self.subTest(name):
                item, version_id, asset_id = self.managed(on_disk=True)
                self.queue(item, version_id)
                change(self.assets[asset_id])
                with no_leaks(self, KEY, CANARY, self.fake.url):
                    self.tend()
                    self.tend(at=NOW + 1)
                row = self.copy(self.jo_pixel, item)
                self.assertEqual((row.state, row.placed_at, row.name), ("queued", None, None))
                self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
                self.assertEqual(self.staged(), [])
                self.assertEqual(self.held_events(row), 1)

    def test_with_the_source_off_a_managed_copy_is_neither_placed_nor_pruned(self):
        item, version_id, _ = self.managed(on_disk=True)
        self.queue(item, version_id)
        self.assertEqual((self.tend(readers=False).blocked, self.fake.requests()), (1, []))
        self.assertEqual(self.copy(self.jo_pixel, item).state, "queued")
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
        self.assertEqual(self.tend().placed, 1)
        self.match(item, self.jo_account, NOW + 10)
        self.assertEqual((self.tend(readers=False, at=NOW + 20).pruned, self.copy(self.jo_pixel, item).state), (0, "placed"))
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        self.assertEqual((self.tend(at=NOW + 30).pruned, self.copy(self.jo_pixel, item).state), (1, "pruned"))
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())

    def test_a_placed_copy_whose_reference_no_longer_maps_is_never_pruned(self):
        breaks = {
            "version no longer verified": lambda row, version_id: update(ManagedAssetVersion).where(ManagedAssetVersion.id == version_id).values(state="trashed"),
            "person bound to another owner": lambda row, version_id: update(Person).where(Person.id == self.jo).values(immich_user_id="22222222-2222-4222-8222-222222222222"),
            "a generation without a reference": lambda row, version_id: update(PixelCopy).where(PixelCopy.id == row.id).values(requeued=PixelCopy.requeued + 1),
            "another account recorded": lambda row, version_id: update(ManagedPixelSource).where(ManagedPixelSource.copy_id == row.id).values(google_account_id=self.other_account()),
        }
        for name, broken in breaks.items():
            with self.subTest(name):
                self.opened.execute(update(Person).where(Person.id == self.jo).values(immich_user_id=OWNER))
                self.opened.commit()
                item, version_id, _ = self.managed()
                self.queue(item, version_id)
                self.tend()
                row = self.copy(self.jo_pixel, item)
                self.match(item, self.jo_account, NOW + 10)
                self.opened.execute(broken(row, version_id))
                self.opened.commit()
                self.assertEqual(self.tend(at=NOW + 20).pruned, 0)
                self.assertEqual(self.copy(self.jo_pixel, item).state, "placed")
                self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
                self.assertEqual(self.held_events(row), 1)

    def test_a_takeout_upload_is_not_queued_back_to_its_own_google_account(self):
        work_account, work_pixel = self.account("jo-work@x.test")
        self.opened.commit()
        item, version_id, _ = self.managed(origins=(("takeout", self.jo_account),))
        self.assertEqual(self.queue(item, version_id), 1)
        self.assertIsNone(self.copy(self.jo_pixel, item))
        self.assertEqual(self.tend().placed, 1)
        self.assertTrue(self.copy_path(work_pixel, item).is_file())
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
        self.match(item, work_account, NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 1)
        self.opened.execute(update(Pixel).where(Pixel.id == work_pixel).values(google_account_id=self.jo_account))
        self.opened.commit()
        self.assertEqual(pixel_folder.requeue_unconfirmed(self.opened, self.opened.get(Pixel, work_pixel, populate_existing=True), None, NOW + 30), 0)
        self.assertEqual(self.copy(work_pixel, item).state, "pruned")

    def test_a_phone_arrival_still_goes_to_every_pixel_even_with_a_takeout_origin(self):
        _, work_pixel = self.account("jo-work@x.test")
        self.opened.commit()
        item, version_id, _ = self.managed(origins=(("takeout", self.jo_account), ("phone", None)))
        self.assertEqual(self.queue(item, version_id), 2)
        self.assertEqual([self.copy(pixel, item).always_send for pixel in (self.jo_pixel, work_pixel)], [True, True])
        self.assertEqual(self.tend().placed, 2)

    def test_only_a_verified_managed_original_of_the_person_is_queued(self):
        item, version_id, _ = self.managed(state="observed")
        with self.assertRaises(pixel_source.NotManaged):
            pixel_folder.queue_managed(self.opened, item, version_id, NOW)
        self.opened.rollback()
        item, version_id, _ = self.managed()
        self.opened.execute(update(Person).where(Person.id == self.jo).values(immich_user_id="22222222-2222-4222-8222-222222222222"))
        with self.assertRaises(pixel_source.NotManaged):
            pixel_folder.queue_managed(self.opened, item, version_id, NOW)
        self.opened.rollback()
        self.assertEqual(self.opened.execute(select(PixelCopy)).all(), [])

    def test_queueing_again_adds_no_reference_and_a_copy_with_new_contents_stays_held(self):
        item, version_id, _ = self.managed()
        self.queue(item, version_id)
        self.assertEqual(self.queue(item, version_id), 0)
        self.assertEqual(len(self.opened.execute(select(ManagedPixelSource.id)).all()), 1)
        self.opened.execute(update(MediaItem).where(MediaItem.id == item.id).values(sha1="f" * 40))
        pixel_folder.requeue_new_contents(self.opened, self.opened.get(MediaItem, item.id, populate_existing=True), NOW + 1)
        self.opened.commit()
        tended = self.tend(at=NOW + 2)
        self.assertEqual((tended.placed, tended.blocked), (0, 1))
        self.assertEqual(self.fake.requests(), [])
        self.assertEqual(self.held_events(self.copy(self.jo_pixel, item)), 1)

    def test_a_held_copy_records_a_fixed_reason_and_one_held_for_a_day_opens_one_alert(self):
        item, version_id, asset_id = self.managed()
        self.queue(item, version_id)
        self.assets[asset_id].trashed = True
        sends = lambda settings, title, message, link: "sent"
        settings = settings_from()
        with no_leaks(self, KEY, CANARY, asset_id, self.fake.url):
            self.assertEqual(self.tend().blocked, 1)
            row = self.copy(self.jo_pixel, item)
            details = self.opened.execute(select(Event.detail).where(Event.type == "pixel_copy_source_held")).scalars().all()
            self.assertEqual(details, [f"copy {row.id}: source_not_custody"])
            self.assertEqual(pixel_folder.alert_held(self.opened, settings, NOW + 3600, sends), 0)
            self.assertEqual(pixel_folder.alert_held(self.opened, settings, NOW + 2 * 86400, sends), 1)
            self.assertEqual(pixel_folder.alert_held(self.opened, settings, NOW + 3 * 86400, sends), 0)
        alert = self.opened.execute(select(Alert).where(Alert.key.startswith("pixel:source-held:"))).scalar_one()
        self.assertIn("missing, trashed, offline", alert.message)
        self.assertNotIn(asset_id, alert.message)

    def test_a_hold_followed_by_a_local_failure_opens_no_held_alert(self):
        item, version_id, asset_id = self.managed()
        self.queue(item, version_id)
        self.assets[asset_id].trashed = True
        self.assertEqual(self.tend().blocked, 1)
        self.assets[asset_id].trashed = False
        with mock.patch.object(paths, "publish_pixel_copy", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.tend(at=NOW + 60).failed, 1)
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 2 * 86400, lambda settings, title, message, link: "sent"), 0)

    def test_held_copies_deferred_by_the_size_cap_open_no_held_alert(self):
        first, first_version, _ = self.managed()
        self.queue(first, first_version)
        self.assertEqual(self.tend().placed, 1)
        later = [self.managed() for _ in range(2)]
        for item, version_id, asset_id in later:
            self.queue(item, version_id)
            self.assets[asset_id].trashed = True
        self.assertEqual(self.tend(at=NOW + 60).blocked, 2)
        for _, _, asset_id in later:
            self.assets[asset_id].trashed = False
        tended = pixel_folder.tend(self.opened, self.photos, first.bytes + 1, NOW + 120, self.readers)
        self.assertEqual((tended.placed, tended.deferred), (0, 2))
        deferrals = self.opened.execute(select(Event.detail).where(Event.type == "pixel_copy_deferred")).scalars().all()
        self.assertEqual(sorted(deferrals), sorted(f"copy {self.copy(self.jo_pixel, item).id}" for item, _, _ in later))
        last = self.copy(self.jo_pixel, later[1][0]).id
        with mock.patch.object(pixel_folder, "lock_copy", wraps=pixel_folder.lock_copy) as locked:
            self.assertEqual(pixel_folder.tend(self.opened, self.photos, first.bytes + 1, NOW + 180, self.readers).deferred, 2)
        self.assertNotIn(last, [call.args[1] for call in locked.call_args_list])
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 2 * 86400, lambda settings, title, message, link: "sent"), 0)

    def test_a_copy_held_because_the_source_is_turned_off_alerts_after_a_day(self):
        item, version_id, _ = self.managed()
        self.queue(item, version_id)
        self.assertEqual(self.tend(readers=False).blocked, 1)
        sends = lambda settings, title, message, link: "sent"
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 2 * 86400, sends), 1)
        alert = self.opened.execute(select(Alert).where(Alert.key.startswith("pixel:source-held:"))).scalar_one()
        self.assertIn("turned off", alert.message)

    def test_a_replacement_that_fails_locally_after_a_hold_opens_no_held_alert(self):
        item, version_id, asset_id = self.managed()
        self.queue(item, version_id)
        self.tend()
        decision = self.match(item, self.jo_account, NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 1)
        dedupe.reverse(self.opened, decision, NOW + 30, "owner")
        self.opened.commit()
        full = mock.patch.object(paths, "publish_pixel_copy", side_effect=OSError(28, "No space left on device"))
        with full:
            self.assertEqual(self.tend(at=NOW + 40).failed, 1)
        self.assets[asset_id].trashed = True
        self.assertEqual(self.tend(at=NOW + 50).blocked, 1)
        self.assets[asset_id].trashed = False
        with full:
            self.assertEqual(self.tend(at=NOW + 60).failed, 1)
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 2 * 86400, lambda settings, title, message, link: "sent"), 0)

    def test_a_hold_whose_reason_changes_keeps_its_start_and_alerts_after_a_day(self):
        item, version_id, asset_id = self.managed()
        self.queue(item, version_id)
        self.assets[asset_id].trashed = True
        self.assertEqual(self.tend().blocked, 1)
        self.fake.mode("500")
        self.assertEqual(self.tend(at=NOW + 20 * 3600).blocked, 1)
        self.fake.mode("ok")
        reasons = self.opened.execute(select(Event.detail).where(Event.type == "pixel_copy_source_held").order_by(Event.id)).scalars().all()
        self.assertEqual([reason.rpartition(": ")[2] for reason in reasons], ["source_not_custody", "source_unavailable"])
        sends = lambda settings, title, message, link: "sent"
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 25 * 3600, sends), 1)

    def test_a_hold_after_the_first_placement_alerts_even_when_an_older_hold_came_before_it(self):
        item, version_id, asset_id = self.managed()
        self.queue(item, version_id)
        self.assets[asset_id].trashed = True
        self.assertEqual(self.tend().blocked, 1)
        self.assets[asset_id].trashed = False
        self.assertEqual(self.tend(at=NOW + 10).placed, 1)
        self.match(item, self.jo_account, NOW + 15)
        self.opened.execute(update(ManagedAssetVersion).where(ManagedAssetVersion.id == version_id).values(state="trashed"))
        self.opened.commit()
        self.assertEqual(self.tend(at=NOW + 20).blocked, 1)
        sends = lambda settings, title, message, link: "sent"
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 20 + 3600, sends), 0)
        self.assertEqual(pixel_folder.alert_held(self.opened, settings_from(), NOW + 20 + 25 * 3600, sends), 1)

    def test_a_hold_with_the_same_reason_before_and_after_the_first_placement_alerts_after_a_day(self):
        sends = lambda settings, title, message, link: "sent"
        for reason in ("source_off", "source_unrecorded"):
            with self.subTest(reason=reason):
                self.setUp()
                item, version_id, _ = self.managed()
                self.queue(item, version_id)
                verified = update(ManagedAssetVersion).where(ManagedAssetVersion.id == version_id)
                if reason == "source_off":
                    self.assertEqual(self.tend(readers=False, at=NOW).blocked, 1)
                    self.assertEqual(self.tend(at=NOW + 10).placed, 1)
                    self.match(item, self.jo_account, NOW + 15)
                    self.assertEqual(self.tend(readers=False, at=NOW + 20).blocked, 1)
                else:
                    self.opened.execute(verified.values(state="trashed"))
                    self.opened.commit()
                    self.assertEqual(self.tend(at=NOW).blocked, 1)
                    self.opened.execute(verified.values(state="bytes_verified"))
                    self.opened.commit()
                    self.assertEqual(self.tend(at=NOW + 10).placed, 1)
                    self.match(item, self.jo_account, NOW + 15)
                    self.opened.execute(verified.values(state="trashed"))
                    self.opened.commit()
                    self.assertEqual(self.tend(at=NOW + 20).blocked, 1)
                self.assertEqual(self.tend(readers=reason != "source_off", at=NOW + 30).blocked, 1)
                row = self.copy(self.jo_pixel, item)
                holds = self.opened.execute(select(Event.detail).where(Event.type == "pixel_copy_source_held", Event.detail.startswith(f"copy {row.id}: "))).scalars().all()
                self.assertEqual(holds, [f"copy {row.id}: {reason}"] * 2)
                key = f"pixel:source-held:{self.jo}:{int(NOW + 20)}"
                pixel_folder.alert_held(self.opened, settings_from(), NOW + 20 + 25 * 3600, sends)
                self.assertIsNotNone(self.opened.execute(select(Alert.id).where(Alert.key == key)).first())

    def test_a_local_write_failure_counts_as_failed_not_as_a_held_source(self):
        item, version_id, _ = self.managed()
        self.queue(item, version_id)
        with mock.patch.object(paths, "publish_pixel_copy", side_effect=OSError(28, "No space left on device")):
            tended = self.tend()
        self.assertEqual((tended.placed, tended.blocked, tended.failed), (0, 0, 1))
        row = self.copy(self.jo_pixel, item)
        self.assertEqual((row.state, self.held_events(row), self.staged()), ("queued", 0, []))
        self.assertEqual(self.tend(at=NOW + 1).placed, 1)

    def test_a_photo_outside_originals_gets_a_name_from_its_asset(self):
        self.assertEqual(pixel_source.copy_name(self.photos, "jo", "jo/google-only/2024/01/x.JPG", "a-1"), "immich/a-1.jpg")
        self.assertEqual(pixel_source.copy_name(self.photos, "jo", "jo/google-only/2024/01/x.toolongsuffix", "a-1"), "immich/a-1")

    def test_a_reversed_match_requeues_from_the_immich_original_and_reverifies_it(self):
        item, version_id, asset_id = self.managed()
        self.queue(item, version_id)
        self.tend()
        first = self.match(item, self.jo_account, NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 1)
        dedupe.reverse(self.opened, first, NOW + 30, "owner")
        self.opened.commit()
        self.assertEqual(self.tend(at=NOW + 40).placed, 1)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        generations = self.opened.execute(select(ManagedPixelSource.generation, ManagedPixelSource.version_id).order_by(ManagedPixelSource.id)).all()
        self.assertEqual(generations, [(0, version_id), (1, version_id)])
        second = self.match(item, self.jo_account, NOW + 50)
        self.assertEqual(self.tend(at=NOW + 60).pruned, 1)
        self.assets[asset_id].data = b"other generated bytes"
        dedupe.reverse(self.opened, second, NOW + 70, "owner")
        self.opened.commit()
        tended = self.tend(at=NOW + 80)
        self.assertEqual((tended.placed, tended.blocked), (0, 1))
        self.assertEqual(self.copy(self.jo_pixel, item).state, "queued")
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())

    def test_a_copy_that_changed_while_its_original_downloaded_is_not_published(self):
        item, version_id, _ = self.managed()
        self.queue(item, version_id)

        def changed(method, path, slug):
            if path.endswith("/original"):
                with self.engine.begin() as connection:
                    connection.execute(delete(PixelPerson).where(PixelPerson.pixel_id == self.jo_pixel))
        self.fake.state.intercept = changed
        tended = self.tend()
        self.assertEqual((tended.placed, tended.blocked), (0, 0))
        self.assertEqual(self.copy(self.jo_pixel, item).state, "queued")
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
        self.assertEqual(self.staged(), [])

    def other_account(self) -> int:
        account = GoogleAccount(email=f"other-{next(self.counter)}@x.test", default_person_id=self.jo, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        return account.id

    def test_editing_the_accounts_email_keeps_its_copies_placeable_and_prunable(self):
        placed, placed_version, _ = self.managed()
        self.queue(placed, placed_version)
        self.tend()
        waiting, waiting_version, _ = self.managed()
        self.queue(waiting, waiting_version)
        accounts.update(self.opened, self.jo_account, "jo-renamed@x.test", None, NOW + 1)
        self.opened.commit()
        self.match(placed, self.jo_account, NOW + 10)
        tended = self.tend(at=NOW + 20)
        self.assertEqual((tended.pruned, tended.placed, tended.blocked), (1, 1, 0))
        self.assertEqual((self.copy(self.jo_pixel, placed).state, self.copy(self.jo_pixel, waiting).state), ("pruned", "placed"))
        emails = self.opened.execute(select(ManagedPixelSource.destination_email).order_by(ManagedPixelSource.id)).scalars().all()
        self.assertEqual(emails, ["jo@x.test", "jo@x.test"])

    def references(self, pixel_id: int) -> int:
        return len(self.opened.execute(select(ManagedPixelSource.id).where(ManagedPixelSource.pixel_id == pixel_id)).all())

    def test_removing_a_person_drops_their_queued_managed_copy_with_its_reference_only(self):
        sam = add_person(self.opened, "sam")
        self.opened.add(PixelPerson(pixel_id=self.jo_pixel, person_id=sam))
        _, work_pixel = self.account("jo-work@x.test")
        self.opened.commit()
        item, version_id, _ = self.managed()
        self.assertEqual(self.queue(item, version_id), 2)
        pixels.update(self.opened, self.jo_pixel, {"people_ids": [sam]}, NOW + 1)
        self.opened.commit()
        self.assertIsNone(self.copy(self.jo_pixel, item))
        self.assertEqual((self.references(self.jo_pixel), self.references(work_pixel)), (0, 1))
        self.assertEqual(self.copy(work_pixel, item).state, "queued")
        self.assertEqual(self.tend(at=NOW + 2).placed, 1)

    def test_a_pixel_is_deleted_with_its_pruned_managed_copies_and_their_references(self):
        _, work_pixel = self.account("jo-work@x.test")
        self.opened.commit()
        item, version_id, _ = self.managed()
        self.queue(item, version_id)
        self.tend()
        with self.assertRaises(pixels.StillHasCopies):
            pixels.delete(self.opened, self.jo_pixel, NOW + 1)
        self.opened.rollback()
        self.match(item, self.jo_account, NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 1)
        pixels.delete(self.opened, self.jo_pixel, NOW + 30)
        self.opened.commit()
        self.assertIsNone(self.opened.get(Pixel, self.jo_pixel, populate_existing=True))
        self.assertEqual((self.references(self.jo_pixel), self.references(work_pixel)), (0, 1))
        self.assertEqual(self.copy(work_pixel, item).state, "placed")

    def test_an_account_a_pixel_left_is_deleted_only_once_its_copies_have_another_reference(self):
        item, version_id, _ = self.managed()
        self.queue(item, version_id)
        moved_to = GoogleAccount(email="jo-new@x.test", default_person_id=self.jo, created=1.0)
        self.opened.add(moved_to)
        self.opened.commit()
        pixels.update(self.opened, self.jo_pixel, {"google_account_id": moved_to.id}, NOW + 1)
        self.opened.commit()
        with self.assertRaises(accounts.InUse):
            accounts.delete(self.opened, self.photos, self.jo_account, NOW + 2)
        self.opened.rollback()
        self.assertEqual(self.tend(at=NOW + 3).placed, 1)
        accounts.delete(self.opened, self.photos, self.jo_account, NOW + 4)
        self.opened.commit()
        self.assertIsNone(self.opened.get(GoogleAccount, self.jo_account, populate_existing=True))
        self.assertEqual(self.opened.execute(select(ManagedPixelSource.google_account_id)).scalars().all(), [moved_to.id])
        self.match(item, moved_to.id, NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 1)

    def test_the_source_module_stays_apart_from_the_transport(self):
        self.assertEqual(transport_references((ROOT / "darkroom/domain/pixel_source.py").read_text()), set())


class ReviewUndoTests(ReviewCase):
    def kept_with_a_managed_copy(self) -> tuple[int, MediaItem]:
        review_id = self.pending().id
        status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        self.assertEqual(status, 200, body)
        return review_id, self.with_a_managed_copy(self.item_named(PAIR_NAME))

    def with_a_managed_copy(self, item: MediaItem) -> MediaItem:
        opened = self.world.opened
        owner = opened.get(Person, item.person_id).immich_user_id
        version = ManagedAssetVersion(person_id=item.person_id, owner_id=owner, asset_id="generated-review-asset", sha1=item.sha1, bytes=item.bytes,
                                      kind="IMAGE", state="bytes_verified", metadata_snapshot="{}", seen_at=1.0, bytes_verified_at=1.0)
        opened.add(version)
        opened.flush()
        self.assertEqual(pixel_folder.queue_managed(opened, item, version.id, NOW), 1)
        opened.commit()
        return item

    def test_a_photo_with_a_managed_pixel_reference_is_reassigned_only_once_none_is_left(self):
        item = self.with_a_managed_copy(self.item_named(GOOGLE_ONLY_NAME))
        owner, sam, before = item.person_id, self.world.ids["sam"], self.tree()
        status, body = self.call("POST", f"/api/media/{item.id}/reassign", {"person_id": sam})
        self.assertEqual(status, 409, body)
        self.assertIn("deleting the Pixel clears any other", str(body))
        self.assertEqual((self.opened.get(MediaItem, item.id).person_id, self.tree()), (owner, before))
        opened = self.opened
        pixels.update(opened, self.world.pixel, {"people_ids": [sam]}, NOW + 1)
        opened.commit()
        self.assertEqual(opened.execute(select(ManagedPixelSource.id)).all(), [])
        status, body = self.call("POST", f"/api/media/{item.id}/reassign", {"person_id": sam})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.opened.get(MediaItem, item.id).person_id, sam)

    def test_a_photo_the_backfill_copied_into_immich_is_not_reassigned(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        opened = self.world.opened
        owner = opened.get(Person, item.person_id).immich_user_id
        version = ManagedAssetVersion(person_id=item.person_id, owner_id=owner, asset_id="generated-backfill-asset", sha1=item.sha1, bytes=item.bytes,
                                      kind="IMAGE", state="bytes_verified", metadata_snapshot="{}", seen_at=1.0, bytes_verified_at=1.0)
        opened.add(version)
        opened.flush()
        opened.add(ManagedBackfillItem(person_id=item.person_id, item_id=item.id, outcome="mapped", version_id=version.id, checked_at=NOW, since=NOW))
        opened.commit()
        before = self.tree()
        status, body = self.call("POST", f"/api/media/{item.id}/reassign", {"person_id": self.world.ids["sam"]})
        self.assertEqual(status, 409, body)
        self.assertIn("never takes a photo back out of Immich", str(body))
        self.assertEqual((self.opened.get(MediaItem, item.id).person_id, self.tree()), (item.person_id, before))

    def test_a_photo_with_a_backfill_upload_under_way_or_held_after_it_is_not_reassigned(self):
        sent = {"asset_id": "generated-backfill-asset", "upload_outcome": "created"}
        cases = {
            "about to be sent": ({"state": "prepared"}, 409),
            "answer not known": ({"state": "outcome_unknown", "send_attempts": 1}, 409),
            "metadata pending": (sent | {"state": "bytes_verified", "metadata_hold": "mismatch"}, 409),
            "metadata held": (sent | {"state": "bytes_verified", "metadata_hold": "not_enriched"}, 409),
            "held before sending": ({"state": "prepared", "metadata_hold": "unknown_offset"}, 200),
        }
        for name, (values, expected) in cases.items():
            with self.subTest(name):
                self.setUp()
                item = self.item_named(GOOGLE_ONLY_NAME)
                opened = self.world.opened
                owner = opened.get(Person, item.person_id).immich_user_id
                intent = ManagedImportIntent(person_id=item.person_id, export_id=None, owner_id=owner, kind="backfill", resource_manifest="{}", created_at=1.0)
                opened.add(intent)
                opened.flush()
                verified = {"verified_sha1": item.sha1, "verified_bytes": item.bytes, "bytes_verified_at": 1.0} if values["state"] == "bytes_verified" else {}
                opened.add(ManagedImportReceipt(intent_id=intent.id, resource_key=f"backfill:media-item:{item.id}", compound_key=f"backfill:media-item:{item.id}",
                                                source_sha1=item.sha1, expected_bytes=item.bytes, expected_metadata="{}", updated_at=1.0, **values, **verified))
                opened.commit()
                status, body = self.call("POST", f"/api/media/{item.id}/reassign", {"person_id": self.world.ids["sam"]})
                self.assertEqual(status, expected, body)

    def test_undoing_a_kept_photo_the_backfill_may_have_sent_to_immich_is_refused(self):
        for state, hold, expected in (("committed", None, 409), ("prepared", None, 409), ("prepared", "unknown_offset", 200)):
            with self.subTest(state=state, hold=hold):
                self.setUp()
                review_id = self.pending().id
                status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
                self.assertEqual(status, 200, body)
                item = self.item_named(PAIR_NAME)
                opened = self.world.opened
                owner = opened.get(Person, item.person_id).immich_user_id
                intent = ManagedImportIntent(person_id=item.person_id, export_id=None, owner_id=owner, kind="backfill", resource_manifest="{}", created_at=1.0)
                opened.add(intent)
                opened.flush()
                done = {"asset_id": "generated-backfill-asset", "upload_outcome": "created", "verified_sha1": item.sha1, "verified_bytes": item.bytes,
                        "bytes_verified_at": 1.0, "metadata_evidence": "{}", "metadata_verified_at": 1.0, "committed_at": 1.0} if state == "committed" else {}
                opened.add(ManagedImportReceipt(intent_id=intent.id, resource_key=f"backfill:media-item:{item.id}", compound_key=f"backfill:media-item:{item.id}",
                                                source_sha1=item.sha1, expected_bytes=item.bytes, expected_metadata="{}", updated_at=1.0, state=state,
                                                metadata_hold=hold, **done))
                opened.commit()
                before = self.tree()
                status, body = self.call("POST", f"/api/review/{review_id}/undo")
                self.assertEqual(status, expected, body)
                if expected == 409:
                    self.assertIn("never takes a photo back out of Immich", str(body))
                    self.assertEqual(self.tree(), before)

    def test_undoing_a_kept_photo_waits_until_its_managed_pixel_copy_is_pruned(self):
        review_id, item = self.kept_with_a_managed_copy()
        item_id = item.id
        status, body = self.call("POST", f"/api/review/{review_id}/undo")
        self.assertEqual(status, 409, body)
        opened = self.opened
        self.assertIsNotNone(opened.get(MediaItem, item_id))
        opened.execute(update(PixelCopy).where(PixelCopy.item_id == item_id).values(state="pruned", pruned_at=NOW))
        opened.commit()
        status, body = self.call("POST", f"/api/review/{review_id}/undo")
        self.assertEqual(status, 200, body)
        opened = self.opened
        self.assertIsNone(opened.get(MediaItem, item_id))
        self.assertEqual(opened.execute(select(ManagedPixelSource.id)).all(), [])


class StagedCopyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = Photos(Path(folder.name).resolve())

    def test_a_staged_copy_must_hash_and_size_as_recorded_and_is_always_discarded(self):
        data = b"generated staged bytes"
        for chunks, size, wanted in (([data], len(data), "0" * 40), ([data], len(data) - 1, sha1(data)), ([data[:4]], len(data), sha1(data))):
            with self.subTest(size=size), self.assertRaises(paths.NotAPixelCopy), paths.staged_pixel_copy(self.photos, chunks, wanted, size, NOW):
                pass
        with paths.staged_pixel_copy(self.photos, [data[:5], data[5:]], sha1(data), len(data), NOW) as staged:
            name = paths.publish_pixel_copy(self.photos, 1, "jo", "managed/a.jpg", staged, sha1(data), len(data))
        self.assertEqual((self.photos.root / "_pixels" / "1" / "jo" / name).read_bytes(), data)
        self.assertEqual(list((self.photos.root / "_pixels" / ".incoming").iterdir()), [])
        with self.assertRaises(paths.NotAPixelCopy):
            paths.publish_pixel_copy(self.photos, 1, "jo", "managed/b.jpg", "../a.jpg", sha1(data), len(data))


class PixelReadersTests(unittest.TestCase):
    def test_the_source_is_off_by_default_and_reads_with_the_persons_own_key(self):
        self.assertFalse(settings_from().pixel_immich_source)
        self.assertIsNone(jobs.pixel_readers(settings_from()))
        settings = dataclasses.replace(settings_from(DARKROOM_PIXEL_IMMICH_SOURCE="1", IMMICH_URL="http://127.0.0.1:9"), secret_key=SECRET)
        readers = jobs.pixel_readers(settings)
        reader = readers(Person(slug="jo", immich_user_id=OWNER, immich_api_key_sealed=secretbox.seal(KEY, SECRET)))
        self.assertEqual((reader.owner_id, reader.key, reader.access), (OWNER, KEY, "read_or_import"))
        with self.assertRaises(immich.ImmichFailed):
            readers(Person(slug="sam"))


if __name__ == "__main__":
    unittest.main()
