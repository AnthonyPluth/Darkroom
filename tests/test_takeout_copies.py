import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import takeout_copies
from darkroom.storage import paths
from darkroom.storage.models import (
    DedupeDecision, DuplicateRemoval, Event, Export, GoogleAccount, ImmichAsset, MediaItem, Person, Pixel, PixelCopy, PixelPerson, ReviewItem,
)
from tests.shared import add_person, own_database

MOMENT = 1_615_000_000.0


def sha1(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


class CopiesCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.jo = self.opened.get(Person, add_person(self.opened, "jo"))
        account = GoogleAccount(email="family@x.test", default_person_id=self.jo.id, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        self.account = account.id
        self.export = Export(account_id=account.id, kind="initial", google_id="g-1", created_at=1.0, status="imported", owner_person_id=self.jo.id)
        self.opened.add(self.export)
        self.pixel = Pixel(name="Hall Pixel", google_account_id=account.id, created=1.0)
        self.opened.add(self.pixel)
        self.opened.flush()
        self.opened.add(PixelPerson(pixel_id=self.pixel.id, person_id=self.jo.id))
        self.opened.commit()
        self.counter = 0

    def item(self, relative, data, **fields):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        values = {"width": 4000, "height": 3000, "captured_utc": MOMENT, "offset_minutes": 0, "make": "Apple", "model": "iPhone 11"} | fields
        item = MediaItem(person_id=self.jo.id, path=relative, sha1=sha1(data), bytes=len(data), indexed_at=1.0, **values)
        self.opened.add(item)
        self.opened.flush()
        return item

    def takeout(self, data, name="IMG_0001.jpg", tier=5, decided_by="auto", **fields):
        item = self.item(f"jo/google-only/2021/03/{name}", data, source="takeout", export_id=self.export.id, **fields)
        (self.root / f"{item.path}.xmp").write_bytes(b"<xmp/>")
        self.counter += 1
        decision = DedupeDecision(export_id=self.export.id, account_id=self.account, source_path=f"Takeout/{name}", person_id=self.jo.id, takeout_item_id=item.id,
                                  tier=tier, reason="no match", decided_by=decided_by, decided_at=1.0)
        self.opened.add(decision)
        self.opened.commit()
        return item, decision

    def phone(self, data, name="IMG_0001-aaaa.jpg", folder="immich", **fields):
        item = self.item(f"jo/originals/{folder}/{name}", data, source="phone", **fields)
        self.counter += 1
        self.opened.add(ImmichAsset(person_id=self.jo.id, asset_id=f"a{self.counter}", kind="photo", status="stored", sha1=item.sha1, original_item_id=item.id, seen_at=1.0))
        self.opened.commit()
        return item

    def resolve(self, at=2.0):
        return takeout_copies.resolve_person(self.opened, self.photos, self.jo, at)

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def removals(self):
        return list(self.opened.execute(select(DuplicateRemoval).order_by(DuplicateRemoval.id)).scalars())

    def events(self, kind):
        return [e.detail for e in self.opened.execute(select(Event).where(Event.type == kind).order_by(Event.id)).scalars()]


class QualityRuleTests(CopiesCase):
    def test_resolution_then_file_size_then_metadata_and_a_tie_keeps_the_phone_original(self):
        phone = MediaItem(sha1="a", bytes=100, width=4000, height=3000, captured_utc=1.0, offset_minutes=0, make="Apple", model="iPhone")
        cases = [
            ({"width": 4032}, "takeout_better"), ({"width": 2000, "bytes": 999}, "phone_better"),
            ({"bytes": 101}, "takeout_better"), ({"bytes": 99, "lat": 1.0, "lon": 2.0}, "phone_better"),
            ({"lat": 1.0, "lon": 2.0}, "takeout_better"), ({}, "phone_better"), ({"sha1": "a", "width": 9000}, "identical"),
        ]
        for changed, wanted in cases:
            with self.subTest(changed=changed):
                takeout = MediaItem(**({"sha1": "b", "bytes": 100, "width": 4000, "height": 3000, "captured_utc": 1.0, "offset_minutes": 0,
                                        "make": "Apple", "model": "iPhone"} | changed))
                self.assertEqual(takeout_copies.rule_for(phone, takeout), wanted)


class ResolveTests(CopiesCase):
    def test_an_identical_takeout_copy_leaves_and_the_original_is_never_rewritten(self):
        takeout, decision = self.takeout(b"same bytes")
        phone = self.phone(b"same bytes")
        phone_id, takeout_id = phone.id, takeout.id
        resolved = self.resolve()
        self.assertEqual((resolved.removed, resolved.refused), (1, 0))
        self.assertEqual(self.files(), ["jo/originals/immich/IMG_0001-aaaa.jpg"])
        self.assertEqual((self.root / phone.path).read_bytes(), b"same bytes")
        self.assertIsNone(self.opened.get(MediaItem, takeout_id))
        self.assertIsNotNone(self.opened.get(MediaItem, phone_id))
        (row,) = self.removals()
        self.assertEqual((row.state, row.rule, row.tier, row.removed_source, row.decision_id, row.kept_item_id), ("removed", "identical", 1, "takeout", decision.id, phone_id))
        self.assertTrue(takeout_copies.resolved_away(self.opened, decision.id))
        self.assertEqual(self.events("duplicate_removed"), ["takeout copy, identical, tier 1"])

    def test_rerunning_changes_nothing(self):
        self.takeout(b"same bytes")
        self.phone(b"same bytes")
        self.resolve()
        files, removals = self.files(), [(r.id, r.state) for r in self.removals()]
        again = self.resolve(at=3.0)
        self.assertEqual((again.removed, again.kept_both, again.refused, again.failed), (0, 0, 0, 0))
        self.assertEqual((self.files(), [(r.id, r.state) for r in self.removals()]), (files, removals))
        self.assertEqual(len(self.events("duplicate_removed")), 1)

    def test_a_better_phone_original_beats_googles_copy_at_tier_two(self):
        self.takeout(b"google recompressed", width=4000)
        phone = self.phone(b"the phone's own bigger original")
        self.assertEqual(self.resolve().removed, 1)
        self.assertEqual(self.files(), [phone.path])
        self.assertEqual([(r.rule, r.tier, r.removed_source) for r in self.removals()], [("phone_better", 2, "takeout")])

    def test_a_better_takeout_copy_beats_the_phone_original_and_the_phone_copy_is_not_sent_to_the_pixel(self):
        takeout, _ = self.takeout(b"google's larger copy of it")
        phone = self.phone(b"phone copy")
        phone_id = phone.id
        self.opened.add(PixelCopy(pixel_id=self.pixel.id, item_id=phone_id, person_id=self.jo.id, state="queued", bytes=1, sha1=phone.sha1, queued_at=1.0, always_send=True))
        self.opened.commit()
        self.assertEqual(self.resolve().removed, 1)
        self.assertEqual(self.files(), [takeout.path, f"{takeout.path}.xmp"])
        self.assertIsNone(self.opened.get(MediaItem, phone_id))
        asset = self.opened.execute(select(ImmichAsset)).scalar_one()
        self.assertEqual((asset.status, asset.original_item_id), ("stored", None))
        self.assertEqual(self.opened.execute(select(PixelCopy)).scalars().all(), [])
        self.assertEqual([(r.rule, r.removed_source, r.decision_id) for r in self.removals()], [("takeout_better", "phone", None)])

    def test_the_phone_original_stays_when_it_is_matched_on_its_way_to_a_pixel_or_bound_for_another_account(self):
        def matched(phone):
            self.opened.add(DedupeDecision(export_id=self.export.id, account_id=self.account, source_path=f"Takeout/m{phone.id}.jpg", person_id=self.jo.id,
                                           matched_item_id=phone.id, tier=1, reason="same SHA-1", decided_by="auto", decided_at=1.0))

        def placed(phone):
            self.opened.add(PixelCopy(pixel_id=self.pixel.id, item_id=phone.id, person_id=self.jo.id, state="placed", bytes=1, sha1=phone.sha1, queued_at=1.0, placed_at=1.5))

        def other_account(phone):
            other = GoogleAccount(email="other@x.test", default_person_id=self.jo.id, created=1.0)
            self.opened.add(other)
            self.opened.flush()
            self.pixel.google_account_id = other.id

        for number, reason in enumerate((matched, placed, other_account)):
            with self.subTest(reason=reason.__name__):
                self.takeout(b"google's larger copy %d" % number, name=f"IMG_{number}.jpg", captured_utc=MOMENT + 100 * number)
                phone = self.phone(b"phone %d" % number, name=f"IMG_{number}-a.jpg", captured_utc=MOMENT + 100 * number)
                reason(phone)
                self.opened.commit()
                before = self.files()
                resolved = self.resolve()
                self.assertEqual((resolved.removed, resolved.kept_both), (0, number + 1))
                self.assertEqual(self.files(), before)
                self.pixel.google_account_id = self.account
                self.opened.commit()

    def test_only_an_automatic_tier_five_google_only_copy_is_a_takeout_item_that_can_go(self):
        self.takeout(b"same bytes", name="kept.jpg", tier=4, decided_by="auto")
        self.opened.add(ReviewItem(decision_id=self.opened.execute(select(DedupeDecision)).scalar_one().id, status="kept_both"))
        self.takeout(b"same bytes", name="owner.jpg", decided_by="owner")
        _, reversed_decision = self.takeout(b"same bytes", name="reversed.jpg")
        reversed_decision.reversed_at = 1.5
        self.phone(b"same bytes")
        self.phone(b"same bytes", name="IMG_0001.jpg", folder="2021")
        self.opened.commit()
        before = self.files()
        self.assertEqual(self.resolve().removed, 0)
        self.assertEqual(self.files(), before)

    def test_ambiguous_and_review_level_matches_remove_nothing(self):
        self.takeout(b"google one", name="a.jpg")
        self.takeout(b"google two", name="b.jpg")
        self.phone(b"phone shot")
        self.takeout(b"near", name="c.jpg", captured_utc=MOMENT + 50, phash="0" * 16)
        self.phone(b"far", name="far.jpg", captured_utc=MOMENT + 51, phash="0" * 15 + "1", make="Canon")
        before = self.files()
        self.assertEqual(self.resolve().removed, 0)
        self.assertEqual(self.files(), before)

    def test_another_persons_copies_are_never_paired(self):
        sam = self.opened.get(Person, add_person(self.opened, "sam"))
        self.takeout(b"same bytes")
        phone = self.phone(b"same bytes")
        phone.person_id = sam.id
        self.opened.commit()
        self.assertEqual(self.resolve().removed, 0)
        self.assertEqual(takeout_copies.resolve_person(self.opened, self.photos, sam, 2.0).removed, 0)


class CrashTests(CopiesCase):
    def pending(self):
        takeout, _ = self.takeout(b"same bytes")
        phone = self.phone(b"same bytes")
        (pair,) = takeout_copies.pairs(self.opened, self.jo)
        with mock.patch.object(takeout_copies, "finish"):
            takeout_copies.resolve(self.opened, self.photos, self.jo, pair, 2.0, takeout_copies.Resolved())
        return takeout, phone

    def test_a_removal_recorded_before_a_crash_is_finished_on_the_next_run(self):
        takeout, phone = self.pending()
        self.assertEqual([r.state for r in self.removals()], ["pending"])
        (self.root / takeout.path).unlink()
        self.assertEqual(self.resolve(at=3.0).removed, 1)
        self.assertEqual(self.files(), [phone.path])
        self.assertEqual([(r.state, r.removed_at) for r in self.removals()], [("removed", 3.0)])

    def test_a_missing_or_changed_keeper_refuses_and_leaves_the_last_copy(self):
        for change in ("missing", "changed"):
            with self.subTest(change=change):
                self.setUp()
                takeout, phone = self.pending()
                if change == "missing":
                    (self.root / phone.path).unlink()
                else:
                    (self.root / phone.path).write_bytes(b"other bytes")
                resolved = self.resolve(at=3.0)
                self.assertEqual((resolved.removed, resolved.refused), (0, 1))
                self.assertTrue((self.root / takeout.path).exists())
                self.assertIsNotNone(self.opened.get(MediaItem, takeout.id))
                self.assertEqual([r.state for r in self.removals()], ["refused"])
                self.assertEqual(len(self.events("duplicate_removal_refused")), 1)
                self.assertEqual(self.resolve(at=4.0).refused, 0)

    def test_records_that_changed_since_the_removal_was_written_refuse(self):
        takeout, phone = self.pending()
        phone.sha1 = "c" * 40
        self.opened.commit()
        self.assertEqual(self.resolve(at=3.0).refused, 1)
        self.assertTrue((self.root / takeout.path).exists())

    def test_a_filesystem_error_leaves_the_removal_pending_for_the_next_run(self):
        takeout, _ = self.pending()
        with mock.patch.object(paths, "remove_losing_copy", side_effect=PermissionError("denied")):
            self.assertEqual(self.resolve(at=3.0).failed, 1)
        self.assertEqual([r.state for r in self.removals()], ["pending"])
        self.assertEqual(self.resolve(at=4.0).removed, 1)
        self.assertFalse((self.root / takeout.path).exists())
