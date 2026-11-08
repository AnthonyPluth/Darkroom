import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from darkroom.domain import takeout_copies
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import (
    DedupeDecision, DuplicateRemoval, Event, Export, GoogleAccount, ImmichAsset, MediaItem, Person, Pixel, PixelCopy, PixelPerson, ReviewItem,
)
from tests.shared import add_person, own_database

MOMENT = 1_615_000_000.0
GOOGLE = json.dumps({"description": "A made-up canal", "geoData": {"latitude": 45.4, "longitude": 12.3}})
VIDEO = {"duration_ms": 5000, "width": 1920, "height": 1080}


def sha1(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


class FakeExif:
    def __init__(self):
        self.failing = False

    def read(self, found):
        return {path: json.loads(path.read_text()) if path.suffix == ".xmp" else {} for path in found}

    def write(self, sidecar, fields):
        if self.failing:
            raise exiftool.WriteFailed("exiftool could not write the sidecar")
        if not fields:
            return False
        sidecar.write_text(json.dumps((json.loads(sidecar.read_text()) if sidecar.exists() else {}) | dict(fields)))
        return True


class CopiesCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.jo = self.opened.get(Person, add_person(self.opened, "jo"))
        account, self.other = (GoogleAccount(email=f"{name}@x.test", default_person_id=self.jo.id, created=1.0) for name in ("family", "other"))
        self.opened.add_all([account, self.other])
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
        self.exif = FakeExif()

    def item(self, relative, data, **fields):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        values = {"width": 4000, "height": 3000, "captured_utc": MOMENT, "offset_minutes": 0, "make": "Apple", "model": "iPhone 11"} | fields
        item = MediaItem(person_id=self.jo.id, path=relative, sha1=sha1(data), bytes=len(data), indexed_at=1.0, **values)
        self.opened.add(item)
        self.opened.flush()
        return item

    def decide(self, item, source, tier=5, decided_by="auto"):
        decision = DedupeDecision(export_id=self.export.id, account_id=self.account, source_path=source, person_id=self.jo.id, takeout_item_id=item.id,
                                  tier=tier, reason="no match", decided_by=decided_by, decided_at=1.0)
        self.opened.add(decision)
        self.opened.commit()
        return decision

    def takeout(self, data, name="IMG_0001.jpg", tier=5, decided_by="auto", source=None, **fields):
        item = self.item(f"jo/google-only/2021/03/{name}", data, source="takeout", export_id=self.export.id, sidecar_json=GOOGLE, **fields)
        (self.root / f"{item.path}.xmp").write_text("{}")
        return item, self.decide(item, source or f"Takeout/{name}", tier, decided_by)

    def phone(self, data, name="IMG_0001-aaaa.jpg", folder="immich", **fields):
        item = self.item(f"jo/originals/{folder}/{name}", data, source="phone", **fields)
        self.counter += 1
        self.opened.add(ImmichAsset(person_id=self.jo.id, asset_id=f"a{self.counter}", kind="photo", status="stored", sha1=item.sha1, original_item_id=item.id, seen_at=1.0))
        self.opened.commit()
        return item

    def takeout_wins(self, number=0):
        takeout, _ = self.takeout(b"google 4k %d" % number, name=f"VID_{number}.mp4", captured_utc=MOMENT + 100 * number, **VIDEO | {"width": 3840, "height": 2160})
        return takeout, self.phone(b"phone 1080p %d" % number, name=f"VID_{number}-a.mp4", captured_utc=MOMENT + 100 * number, **VIDEO)

    def resolve(self, at=2.0):
        return takeout_copies.resolve_person(self.opened, self.photos, self.jo, at, takeout_copies.Tools(self.exif.read, self.exif.write))

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def removals(self):
        return list(self.opened.execute(select(DuplicateRemoval).order_by(DuplicateRemoval.id)).scalars())

    def events(self, kind):
        return [e.detail for e in self.opened.execute(select(Event).where(Event.type == kind).order_by(Event.id)).scalars()]


class QualityRuleTests(CopiesCase):
    def test_only_more_pixels_make_a_takeout_copy_beat_the_phone_original(self):
        phone = MediaItem(sha1="a", bytes=100, width=4000, height=3000, captured_utc=1.0, offset_minutes=0, make="Apple", model="iPhone")
        for changed, wanted in [({"width": 4032}, "takeout_better"), ({"width": 2000, "bytes": 999}, "phone_better"), ({"bytes": 10_000}, "phone_better"),
                                ({"bytes": 10_000, "lat": 1.0, "lon": 2.0}, "phone_better"), ({}, "phone_better"), ({"sha1": "a", "width": 9000}, "identical")]:
            with self.subTest(changed=changed):
                takeout = MediaItem(**({"sha1": "b", "bytes": 100, "width": 4000, "height": 3000} | changed))
                self.assertEqual(takeout_copies.rule_for(phone, takeout), wanted)
        self.assertEqual(takeout_copies.rule_for(MediaItem(sha1="a", width=None, height=None), MediaItem(sha1="b", width=4000, height=3000)), "phone_better")

    def test_a_same_resolution_bigger_takeout_file_never_deletes_the_phone_original(self):
        takeout, _ = self.takeout(b"google's re-encoded copy, many more bytes than the phone's")
        phone = self.phone(b"phone original")
        self.assertEqual(self.resolve().removed, 1)
        self.assertEqual((self.root / phone.path).read_bytes(), b"phone original")
        self.assertFalse((self.root / takeout.path).exists())
        self.assertEqual([(r.rule, r.removed_source) for r in self.removals()], [("phone_better", "takeout")])


class ResolveTests(CopiesCase):
    def test_an_identical_takeout_copy_leaves_and_its_google_metadata_moves_to_the_originals_sidecar(self):
        takeout, decision = self.takeout(b"same bytes")
        second = self.decide(takeout, "Takeout/Album/IMG_0001.jpg")
        phone = self.phone(b"same bytes")
        phone_id, takeout_id = phone.id, takeout.id
        resolved = self.resolve()
        self.assertEqual((resolved.removed, resolved.refused), (1, 0))
        self.assertEqual(self.files(), ["jo/originals/immich/IMG_0001-aaaa.jpg", "jo/originals/immich/IMG_0001-aaaa.jpg.xmp"])
        self.assertEqual((self.root / phone.path).read_bytes(), b"same bytes")
        carried = json.loads((self.root / f"{phone.path}.xmp").read_text())
        self.assertEqual((carried["XMP-dc:Description"], carried["XMP-exif:GPSLatitude"]), ("A made-up canal", "45.4000000"))
        self.assertIsNone(self.opened.get(MediaItem, takeout_id))
        self.assertIsNotNone(self.opened.get(MediaItem, phone_id))
        row, tombstone = self.removals()
        self.assertEqual((row.state, row.rule, row.tier, row.removed_source, row.decision_id, row.kept_item_id), ("removed", "identical", 1, "takeout", decision.id, phone_id))
        self.assertEqual((tombstone.state, tombstone.decision_id, tombstone.removed_path), ("removed", second.id, row.removed_path))
        self.assertTrue(all(takeout_copies.resolved_away(self.opened, d) for d in (decision.id, second.id)))
        self.assertEqual(self.events("duplicate_removed"), ["takeout copy, identical, tier 1"])
        files, again = self.files(), self.resolve(at=3.0)
        self.assertEqual(((again.removed, again.kept_both, again.refused, again.failed), self.files(), len(self.removals())), ((0, 0, 0, 0), files, 2))

    def test_a_value_the_keeper_already_has_is_kept(self):
        self.takeout(b"same bytes")
        phone = self.phone(b"same bytes")
        (self.root / f"{phone.path}.xmp").write_text(json.dumps({"XMP-dc:Description": "Jo's own caption"}))
        self.assertEqual(self.resolve().removed, 1)
        carried = json.loads((self.root / f"{phone.path}.xmp").read_text())
        self.assertEqual((carried["XMP-dc:Description"], carried["XMP-exif:GPSLatitude"]), ("Jo's own caption", "45.4000000"))

    def test_a_sidecar_that_cannot_be_written_refuses_and_removes_nothing(self):
        takeout, _ = self.takeout(b"same bytes")
        self.phone(b"same bytes")
        self.exif.failing = True
        before = self.files()
        resolved = self.resolve()
        self.assertEqual((resolved.removed, resolved.refused), (0, 1))
        self.assertEqual(self.files(), before)
        self.assertIsNotNone(self.opened.get(MediaItem, takeout.id))
        self.assertEqual(self.events("duplicate_removal_refused"), ["takeout copy, identical: the kept copy's sidecar could not be written"])

    def test_a_takeout_copy_with_more_pixels_beats_the_phone_original_which_is_not_sent_to_the_pixel(self):
        takeout, phone = self.takeout_wins()
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

    def dependencies(self):
        return {
            "matched": lambda phone, takeout: self.opened.add(DedupeDecision(export_id=self.export.id, account_id=self.account, source_path="Takeout/m.mp4",
                                                                             person_id=self.jo.id, matched_item_id=phone.id, tier=1, reason="same SHA-1", decided_by="auto", decided_at=1.0)),
            "placed": lambda phone, takeout: self.opened.add(PixelCopy(pixel_id=self.pixel.id, item_id=phone.id, person_id=self.jo.id, state="placed", bytes=1, sha1=phone.sha1,
                                                                       queued_at=1.0, placed_at=1.5)),
            "other_account": lambda phone, takeout: setattr(self.pixel, "google_account_id", self.other.id),
            "confirmed": lambda phone, takeout: setattr(phone, "confirmed_in_google", True),
            "export_gone": lambda phone, takeout: setattr(takeout, "export_id", None),
        }

    def test_the_phone_original_stays_while_anything_depends_on_it_even_when_that_appears_after_the_removal_is_recorded(self):
        for number, (depend, later) in enumerate((depend, later) for depend in self.dependencies().items() for later in (False, True)):
            with self.subTest(dependency=depend[0], after_recording=later):
                self.setUp()
                takeout, phone = self.takeout_wins(number)
                if later:
                    (pair,) = takeout_copies.pairs(self.opened, self.jo)
                    with mock.patch.object(takeout_copies, "finish"):
                        takeout_copies.resolve(self.opened, self.photos, self.jo, pair, 2.0, takeout_copies.Resolved(), takeout_copies.Tools())
                depend[1](phone, takeout)
                self.opened.commit()
                before = self.files()
                resolved = self.resolve(at=3.0)
                self.assertEqual((resolved.removed, resolved.refused, resolved.kept_both), (0, 1, 0) if later else (0, 0, 1))
                self.assertEqual(self.files(), before)

    def test_an_original_the_reconcile_did_not_store_never_pairs(self):
        self.takeout(b"same bytes")
        self.phone(b"same bytes", name="IMG_0001.jpg", folder="2021")
        self.assertEqual(self.resolve().removed, 0)

    def test_a_copy_with_any_doubtful_decision_never_pairs_even_beside_a_later_tier_five(self):
        for setup in ("kept_both", "owner", "reversed", "matched_by_a_later_export"):
            with self.subTest(setup=setup):
                self.setUp()
                item, first = self.takeout(b"same bytes", tier=4 if setup == "kept_both" else 5, decided_by="owner" if setup == "owner" else "auto")
                if setup == "kept_both":
                    self.opened.add(ReviewItem(decision_id=first.id, status="kept_both"))
                first.reversed_at = 1.5 if setup == "reversed" else None
                later = Export(account_id=self.account, kind="incremental", google_id="g-2", created_at=2.0, status="imported", owner_person_id=self.jo.id)
                self.opened.add(later)
                self.opened.flush()
                matched = setup == "matched_by_a_later_export"
                self.opened.add(DedupeDecision(export_id=later.id, account_id=self.account, source_path="Takeout/IMG_0001.jpg", person_id=self.jo.id,
                                               takeout_item_id=None if matched else item.id, matched_item_id=item.id if matched else None,
                                               tier=1 if matched else 5, reason="later", decided_by="auto", decided_at=2.0))
                self.phone(b"same bytes")
                before = self.files()
                self.assertEqual(self.resolve().removed, 0)
                self.assertEqual(self.files(), before)

    def test_an_ineligible_takeout_peer_of_the_same_shot_keeps_the_match_ambiguous(self):
        for kind in ("owner", "review", "reversed"):
            with self.subTest(kind=kind):
                self.setUp()
                self.takeout(b"google one", name="a.jpg")
                _, other = self.takeout(b"google two, a different frame", name="b.jpg", tier=4 if kind == "review" else 5, decided_by="owner" if kind == "owner" else "auto")
                other.reversed_at = 1.5 if kind == "reversed" else None
                self.phone(b"phone shot")
                before = self.files()
                self.assertEqual((takeout_copies.pairs(self.opened, self.jo), self.resolve().removed), ([], 0))
                self.assertEqual(self.files(), before)

    def test_an_ineligible_peer_that_appears_after_the_removal_is_recorded_refuses_it(self):
        takeout, _ = self.takeout(b"google one", name="a.jpg")
        self.phone(b"phone shot")
        (pair,) = takeout_copies.pairs(self.opened, self.jo)
        with mock.patch.object(takeout_copies, "finish"):
            takeout_copies.resolve(self.opened, self.photos, self.jo, pair, 2.0, takeout_copies.Resolved(), takeout_copies.Tools())
        self.takeout(b"google two, a different frame", name="b.jpg", decided_by="owner")
        self.assertEqual(self.resolve(at=3.0).refused, 1)
        self.assertTrue((self.root / takeout.path).exists())

    def test_an_edited_variant_of_the_same_shot_is_not_a_rival_of_its_original(self):
        takeout, _ = self.takeout(b"google original", name="IMG_0001.jpg")
        self.takeout(b"google's edit", name="IMG_0001-edited~abcd1234.jpg", source="Takeout/Photos from 2021/IMG_0001-edited.jpg")
        self.phone(b"the phone's own bigger original")
        (pair,) = takeout_copies.pairs(self.opened, self.jo)
        self.assertEqual((pair.takeout.id, pair.tier), (takeout.id, 2))

    def test_an_edited_copy_renamed_on_a_name_collision_never_pairs(self):
        self.takeout(b"google's edit", name="IMG_0001-edited~abcd1234.jpg", source="Takeout/Photos from 2021/IMG_0001-edited.jpg")
        self.phone(b"the phone's own bigger original")
        self.opened.add(DedupeDecision(export_id=self.export.id, account_id=self.account, source_path=None, person_id=self.jo.id,
                                       takeout_item_id=self.takeout(b"no source", name="IMG_0002.jpg", captured_utc=MOMENT + 500)[0].id,
                                       tier=5, reason="no match", decided_by="auto", decided_at=1.0))
        self.phone(b"phone two", name="IMG_0002-b.jpg", captured_utc=MOMENT + 500)
        before = self.files()
        self.assertEqual(takeout_copies.pairs(self.opened, self.jo), [])
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
        self.phone(b"same bytes").person_id = sam.id
        self.assertEqual([takeout_copies.resolve_person(self.opened, self.photos, who, 2.0).removed for who in (self.jo, sam)], [0, 0])


class CrashTests(CopiesCase):
    def pending(self):
        takeout, _ = self.takeout(b"same bytes")
        phone = self.phone(b"same bytes")
        (pair,) = takeout_copies.pairs(self.opened, self.jo)
        with mock.patch.object(takeout_copies, "finish"):
            takeout_copies.resolve(self.opened, self.photos, self.jo, pair, 2.0, takeout_copies.Resolved(), takeout_copies.Tools())
        return takeout, phone

    def test_a_removal_recorded_before_a_crash_is_finished_on_the_next_run_even_with_the_losers_folder_gone(self):
        for gone in ("file", "folder"):
            with self.subTest(gone=gone):
                self.setUp()
                takeout, phone = self.pending()
                self.assertEqual([r.state for r in self.removals()], ["pending"])
                (self.root / takeout.path).unlink()
                if gone == "folder":
                    (self.root / f"{takeout.path}.xmp").unlink()
                    (self.root / takeout.path).parent.rmdir()
                self.assertEqual(self.resolve(at=3.0).removed, 1)
                self.assertEqual(self.files(), [phone.path, f"{phone.path}.xmp"])
                self.assertEqual([(r.state, r.removed_at) for r in self.removals()], [("removed", 3.0)])

    def test_a_crash_after_the_unlink_is_recorded_as_removed_even_when_a_check_now_fails(self):
        for keeper_there in (True, False):
            with self.subTest(keeper_there=keeper_there):
                self.setUp()
                takeout, phone = self.pending()
                takeout_id = takeout.id
                (self.root / takeout.path).unlink()
                self.opened.add(DedupeDecision(export_id=self.export.id, account_id=self.account, source_path="Takeout/later.jpg", person_id=self.jo.id,
                                               matched_item_id=takeout_id, tier=1, reason="same SHA-1", decided_by="owner", decided_at=2.5))
                self.opened.commit()
                if not keeper_there:
                    (self.root / phone.path).unlink()
                resolved = self.resolve(at=3.0)
                (row,) = self.removals()
                if keeper_there:
                    self.assertEqual((resolved.removed, row.state, self.opened.get(MediaItem, takeout_id)), (1, "removed", None))
                    self.assertEqual(self.events("duplicate_removed"), ["takeout copy, identical, tier 1"])
                else:
                    self.assertEqual((resolved.failed, resolved.refused, row.state), (1, 0, "pending"))
                    self.assertEqual(len(self.events("duplicate_removal_failed")), 1)

    def test_a_gone_loser_whose_records_changed_is_never_marked_refused(self):
        takeout, _ = self.pending()
        (self.root / takeout.path).unlink()
        takeout.sha1 = "d" * 40
        self.opened.commit()
        resolved = self.resolve(at=3.0)
        self.assertEqual((resolved.failed, resolved.refused, [r.state for r in self.removals()]), (1, 0, ["pending"]))

    def test_a_loser_hidden_in_its_claim_is_never_marked_refused(self):
        takeout, phone = self.pending()
        loser = self.root / takeout.path
        private = loser.parent / paths.claim_name(takeout.sha1, loser.name)
        private.mkdir(mode=paths.PRIVATE_MODE)
        loser.rename(private / paths.CLAIMED)
        loser.write_bytes(b"another file now at the name")
        phone.sha1 = "c" * 40
        self.opened.commit()
        resolved = self.resolve(at=3.0)
        self.assertEqual((resolved.refused, resolved.failed, [r.state for r in self.removals()]), (0, 1, ["pending"]))
        self.assertEqual(self.events("duplicate_removal_failed"), ["takeout copy, identical: LoserNotBackAtItsName"])
        self.assertTrue((private / paths.CLAIMED).exists())

    def test_a_busy_keeper_leaves_the_removal_pending_for_the_next_run(self):
        takeout, phone = self.pending()
        with paths.checked_keeper(self.photos, "jo", phone.path, phone.sha1):
            resolved = self.resolve(at=3.0)
        self.assertEqual((resolved.refused, resolved.failed, [r.state for r in self.removals()]), (0, 1, ["pending"]))
        self.assertTrue((self.root / takeout.path).exists())
        self.assertEqual(self.resolve(at=4.0).removed, 1)

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

    def test_a_filesystem_error_is_reported_and_leaves_the_removal_pending_for_the_next_run(self):
        takeout, _ = self.pending()
        with mock.patch.object(paths, "remove_losing_copy", side_effect=PermissionError("denied")):
            self.assertEqual(self.resolve(at=3.0).failed, 1)
        self.assertEqual([r.state for r in self.removals()], ["pending"])
        self.assertEqual(self.events("duplicate_removal_failed"), ["takeout copy, identical: PermissionError"])
        self.assertEqual(self.resolve(at=4.0).removed, 1)
        self.assertFalse((self.root / takeout.path).exists())

    def test_a_claim_a_crash_left_is_put_back_when_the_removal_is_refused(self):
        takeout, phone = self.pending()
        real = paths.os.unlink
        with mock.patch.object(paths.os, "unlink", lambda name, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt) if name == paths.CLAIMED else real(name, *a, **k)), \
                self.assertRaises(KeyboardInterrupt):
            self.resolve(at=3.0)
        self.assertFalse((self.root / takeout.path).exists())
        phone.sha1 = "c" * 40
        self.opened.commit()
        self.assertEqual(self.resolve(at=4.0).refused, 1)
        self.assertTrue((self.root / takeout.path).exists())

    def test_a_tier_two_removal_whose_phone_item_changed_since_it_was_recorded_refuses(self):
        for field, value in (("width", 4032), ("captured_utc", MOMENT + 10), ("model", "iPhone 15")):
            with self.subTest(field=field):
                self.setUp()
                takeout, _ = self.takeout(b"google recompressed")
                phone = self.phone(b"phone original")
                (pair,) = takeout_copies.pairs(self.opened, self.jo)
                with mock.patch.object(takeout_copies, "finish"):
                    takeout_copies.resolve(self.opened, self.photos, self.jo, pair, 2.0, takeout_copies.Resolved(), takeout_copies.Tools())
                setattr(phone, field, value)
                self.opened.commit()
                self.assertEqual(self.resolve(at=3.0).refused, 1)
                self.assertTrue((self.root / takeout.path).exists())

    def test_a_keeper_turned_into_a_link_gets_no_sidecar_and_nothing_is_removed(self):
        takeout, phone = self.pending()
        elsewhere = self.item("sam/originals/immich/other.jpg", b"same bytes", source="phone")
        (self.root / phone.path).unlink()
        (self.root / phone.path).symlink_to(self.root / elsewhere.path)
        self.assertEqual(self.resolve(at=3.0).refused, 1)
        self.assertEqual((self.root / f"{elsewhere.path}.xmp").exists(), False)
        self.assertTrue((self.root / takeout.path).exists())

    def test_finishing_a_removal_queries_the_same_however_many_other_google_only_items_there_are(self):
        counts = []
        for others in (0, 20):
            self.setUp()
            [self.takeout(b"other %d" % n, name=f"o{n}.jpg", captured_utc=MOMENT + 1000 + n) for n in range(others)]
            self.pending()
            (row,), seen = self.removals(), []
            event.listen(self.opened.get_bind(), "before_cursor_execute", lambda *_, seen=seen: seen.append(1))
            takeout_copies.finish(self.opened, self.photos, self.jo, row, 3.0, takeout_copies.Resolved(), takeout_copies.Tools(self.exif.read, self.exif.write))
            counts.append((row.state, len(seen)))
        self.assertEqual(counts, [("removed", counts[0][1])] * 2)
