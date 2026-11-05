import hashlib
import itertools
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, index
from darkroom.domain.ingest import metadata, pairing, place, sidecar, unpack
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Export, GoogleAccount, MediaItem, Person, Pixel, PixelPerson, ReviewItem
from tests.fixtures import media as fixtures
from tests.shared import add_person, own_database
from tests.takeout_fixtures import TakeoutBuilder, standard_export

TAKEN = 1562243415
OFF = dedupe.DedupeSettings()


def sha1_of(path):
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()


def tree(root):
    return {str(p.relative_to(root)): sha1_of(p) for p in sorted(root.rglob("*")) if p.is_file()}


def takeout_json(gps=(45.434, 12.3388), description="A made-up square", people=()):
    geo = {"latitude": gps[0], "longitude": gps[1]} if gps else {"latitude": 0.0, "longitude": 0.0}
    return json.dumps({"photoTakenTime": {"timestamp": str(TAKEN)}, "geoData": geo, "description": description,
                       "people": [{"name": name} for name in people]})


class Household(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.jo, self.sam = add_person(self.opened, "jo"), add_person(self.opened, "sam")
        account = GoogleAccount(email="family@x.test", default_person_id=self.jo, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
        self.opened.add(pixel)
        self.opened.flush()
        self.opened.add(PixelPerson(pixel_id=pixel.id, person_id=self.sam))
        self.account = account.id
        self.export = Export(account_id=account.id, kind="manual")
        self.opened.add(self.export)
        self.opened.flush()
        self.staging = self.photos.staging("jo", unpack.staging_name(self.export))
        self.counter = itertools.count(1)
        for slug in ("jo", "sam"):
            self.photos.ensure_person_tree(slug)

    def staged(self, name="IMG_0001.JPG", colour=(200, 40, 40), folder="Photos from 2019"):
        return fixtures.make_jpeg(self.staging / "Takeout/Google Photos" / folder / name, colour=colour)

    def entry(self, path, tier=5, captured_local="2019-07-04T08:30:15", captured_utc=float(TAKEN), raw=None, person=None,
              matched=None, decided_by="auto"):
        item = TakeoutItem(path=path, sha1=sha1_of(path), copies=(path,), sidecar=None)
        fields = MediaItem(person_id=self.sam, source="takeout", path="unplaced", sha1=sha1_of(path), bytes=path.stat().st_size,
                           captured_utc=captured_utc, captured_local=captured_local, sidecar_json=takeout_json() if raw is None else raw, indexed_at=1.0)
        action = {1: "skip", 2: "skip", 3: "skip", 4: "review", 5: "google_only"}[tier]
        decision = dedupe.Decision(
            source_path=f"{path.relative_to(self.staging)}#{next(self.counter)}", account_id=self.account, person_id=person or self.jo,
            tier=tier, action=action, reason="test", matched_item_id=None if matched is None else matched.id,
            matched_source=None if matched is None else matched.source, decided_by=decided_by,
        )
        return place.Entry(item, fields, dedupe.apply(self.opened, decision, self.export, 10.0))

    def place(self, *entries, **kwargs):
        return place.place(self.opened, self.photos, self.export, entries, **kwargs)

    def phone(self, person_slug, person_id, name="IMG_0100.JPG", **tags):
        path = fixtures.make_jpeg(self.root / person_slug / "originals" / name, **tags)
        item = MediaItem(person_id=person_id, source="phone", path=str(path.relative_to(self.root)), sha1=sha1_of(path),
                         bytes=path.stat().st_size, indexed_at=1.0)
        self.opened.add(item)
        self.opened.flush()
        return path, item


class GoogleOnlyFolderTests(Household):
    def test_goes_by_the_local_capture_date(self):
        self.place(self.entry(self.staged(), captured_local="2019-12-31T23:30:00", captured_utc=1577838600.0))
        self.assertTrue((self.root / "jo/google-only/2019/12/IMG_0001.JPG").is_file())

    def test_without_a_local_date_goes_by_the_utc_date(self):
        self.place(self.entry(self.staged(), captured_local=None, captured_utc=1577838600.0))
        self.assertTrue((self.root / "jo/google-only/2020/01/IMG_0001.JPG").is_file())

    def test_without_any_date_goes_to_undated(self):
        self.place(self.entry(self.staged(), captured_local=None, captured_utc=None))
        self.assertTrue((self.root / "jo/google-only/undated/IMG_0001.JPG").is_file())

    def test_a_date_outside_the_folder_range_goes_to_undated(self):
        self.place(self.entry(self.staged(), captured_local="1850-01-01T00:00:00", captured_utc=None))
        self.place(self.entry(self.staged("b.jpg"), captured_local=None, captured_utc=1e20))
        self.assertEqual(sorted(p.name for p in (self.root / "jo/google-only/undated").iterdir() if p.suffix != ".xmp"), ["IMG_0001.JPG", "b.jpg"])

    def test_the_item_is_saved_with_its_final_path_for_the_default_person(self):
        entry = self.entry(self.staged(), raw=takeout_json(people=("Sam",)), person=self.jo)
        self.place(entry)
        row = self.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalar_one()
        self.assertEqual((row.person_id, row.path, row.export_id), (self.jo, "jo/google-only/2019/07/IMG_0001.JPG", self.export.id))
        self.assertEqual(entry.decision.takeout_item_id, row.id)
        self.assertFalse((self.root / "sam/google-only").exists() and any((self.root / "sam/google-only").rglob("*.JPG")))

    def test_placement_ignores_who_the_decision_or_sidecar_names(self):
        jo_account = self.opened.get(GoogleAccount, self.account)
        entry = self.entry(self.staged(), raw=takeout_json(people=("Sam", "Lee")))
        entry.decision.person_id = self.sam
        self.place(entry)
        self.assertEqual(jo_account.default_person_id, self.jo)
        self.assertTrue((self.root / "jo/google-only/2019/07/IMG_0001.JPG").is_file())
        self.assertEqual(list((self.root / "sam/google-only").rglob("*")), [])

    def test_the_placed_copy_is_byte_identical_and_staging_is_left_for_cleanup(self):
        staged = self.staged()
        self.place(self.entry(staged))
        self.assertEqual(sha1_of(self.root / "jo/google-only/2019/07/IMG_0001.JPG"), sha1_of(staged))
        self.assertTrue(staged.is_file())

    def test_the_placed_item_gets_a_sidecar_with_googles_metadata(self):
        report = self.place(self.entry(self.staged()))
        tags = exiftool.read([self.root / "jo/google-only/2019/07/IMG_0001.JPG.xmp"])[self.root / "jo/google-only/2019/07/IMG_0001.JPG.xmp"]
        self.assertEqual(tags["XMP-dc:Description"], "A made-up square")
        self.assertEqual(tags["XMP-photoshop:DateCreated"], "2019:07:04 14:30:15+02:00")
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 45.434, places=5)
        self.assertEqual((report.placed, report.sidecars), (1, 1))

    def test_an_account_without_its_default_person_fails(self):
        entry = self.entry(self.staged())
        with mock.patch.object(self.opened, "get", return_value=None), self.assertRaises(place.PlaceFailed):
            place.place_google_only(self.opened, self.photos, self.export, entry, place.PlaceReport(), exiftool.read, exiftool.write_xmp)


class CollisionTests(Household):
    def folder(self):
        return self.root / "jo/google-only/2019/07"

    def test_the_counter_is_removed_when_the_name_is_free(self):
        self.place(self.entry(self.staged("IMG_0008(1).JPG")))
        self.assertTrue((self.folder() / "IMG_0008.JPG").is_file())

    def test_different_bytes_at_the_same_name_get_the_hash_suffix(self):
        first, second = self.staged("IMG_0008.JPG"), self.staged("IMG_0008(1).JPG", colour=(10, 200, 10))
        self.place(self.entry(first), self.entry(second))
        names = sorted(p.name for p in self.folder().iterdir() if p.suffix != ".xmp")
        self.assertEqual(names, ["IMG_0008.JPG", f"IMG_0008~{sha1_of(second)[:8]}.JPG"])
        self.assertEqual(sha1_of(self.folder() / f"IMG_0008~{sha1_of(second)[:8]}.JPG"), sha1_of(second))

    def test_identical_bytes_already_there_are_skipped(self):
        first = self.staged("IMG_0001.JPG")
        again = self.staging / "Takeout/Google Photos/Album/IMG_0001(2).JPG"
        again.parent.mkdir(parents=True)
        again.write_bytes(first.read_bytes())
        report = self.place(self.entry(first), self.entry(again))
        self.assertEqual(sorted(p.name for p in self.folder().iterdir()), ["IMG_0001.JPG", "IMG_0001.JPG.xmp"])
        self.assertEqual((report.placed, report.already_there), (1, 1))
        self.assertEqual(len(list(self.opened.execute(select(MediaItem)).scalars())), 1)

    def test_a_hash_named_copy_already_there_is_skipped(self):
        first, second = self.staged("a.jpg"), self.staged("a(1).jpg", colour=(10, 200, 10))
        self.place(self.entry(first), self.entry(second))
        before = tree(self.root)
        report = self.place(self.entry(second))
        self.assertEqual(tree(self.root), before)
        self.assertEqual(report.already_there, 1)

    def test_both_names_taken_by_other_files_fails_without_writing(self):
        second = self.staged("a(1).jpg", colour=(10, 200, 10))
        self.folder().mkdir(parents=True)
        (self.folder() / "a.jpg").write_bytes(b"one")
        (self.folder() / f"a~{sha1_of(second)[:8]}.jpg").write_bytes(b"two")
        before = tree(self.root)
        with self.assertRaises(place.PlaceFailed):
            self.place(self.entry(second))
        self.assertEqual(tree(self.root), before)

    def test_a_symlink_at_the_name_is_never_followed(self):
        target = self.root / "elsewhere.jpg"
        target.write_bytes(b"other")
        staged = self.staged("a.jpg")
        self.folder().mkdir(parents=True)
        (self.folder() / "a.jpg").symlink_to(target)
        self.place(self.entry(staged))
        self.assertEqual(target.read_bytes(), b"other")
        self.assertTrue((self.folder() / f"a~{sha1_of(staged)[:8]}.jpg").is_file())

    def test_a_name_taken_while_copying_is_rechecked(self):
        staged = self.staged("a.jpg")
        real = paths.write_once

        def racing(destination, chunks, marker=None):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(staged.read_bytes())
            return real(destination, chunks, marker)

        with mock.patch.object(paths, "write_once", side_effect=racing):
            report = self.place(self.entry(staged))
        self.assertEqual(report.already_there, 1)

    def test_a_name_taken_by_another_file_while_copying_moves_on(self):
        staged = self.staged("a.jpg")
        real = paths.write_once
        calls = []

        def racing(destination, chunks, marker=None):
            calls.append(destination.name)
            if len(calls) == 1:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"theirs")
            return real(destination, chunks, marker)

        with mock.patch.object(paths, "write_once", side_effect=racing):
            self.place(self.entry(staged))
        self.assertEqual((self.folder() / "a.jpg").read_bytes(), b"theirs")
        self.assertEqual(sha1_of(self.folder() / f"a~{sha1_of(staged)[:8]}.jpg"), sha1_of(staged))

    def test_a_staged_file_that_changes_while_copying_fails(self):
        entry = self.entry(self.staged("a.jpg"))
        entry.fields.sha1 = "0" * 40
        with self.assertRaises(place.PlaceFailed):
            self.place(entry)

    def test_a_copy_that_does_not_match_its_hash_is_removed(self):
        entry = self.entry(self.staged("a.jpg"))
        entry.fields.sha1 = "0" * 40
        with self.assertRaises(place.PlaceFailed):
            self.place(entry)
        self.assertEqual([p for p in (self.root / "jo/google-only").rglob("*") if p.is_file()], [])

    def test_owner_made_decisions_are_never_placed_or_held(self):
        _, mine = self.phone("sam", self.sam)
        for tier, kwargs in ((5, {}), (4, {"person": self.sam, "matched": mine})):
            entry = self.entry(self.staged(f"o{tier}.jpg", colour=(tier, 9, 9)), tier=tier, decided_by="owner", **kwargs)
            before = tree(self.root)
            report = self.place(entry)
            self.assertEqual(tree(self.root), before)
            self.assertEqual((report.placed, report.held, report.skipped), (0, 0, 1))

    def test_hidden_and_counter_only_names_get_a_plain_name(self):
        self.assertEqual(place.plain_name(Path(".hidden.jpg")), ("_hidden", ".jpg"))
        self.assertEqual(place.plain_name(Path("(1).jpg")), ("(1)", ".jpg"))
        self.assertEqual(place.plain_name(Path("IMG_1(12).HEIC")), ("IMG_1", ".HEIC"))
        self.assertEqual(place.plain_name(Path("...")), ("_", ""))


class ReviewHoldTests(Household):
    def test_a_tier_four_item_is_held_for_review_and_the_hold_path_is_set(self):
        _, mine = self.phone("sam", self.sam)
        entry = self.entry(self.staged(), tier=4, person=self.sam, matched=mine)
        report = self.place(entry)
        held = self.root / "_inbox/sam/review" / f"export-{self.export.id}" / "IMG_0001.JPG"
        self.assertTrue(held.is_file())
        review = self.opened.execute(select(ReviewItem)).scalar_one()
        self.assertEqual(review.hold_path, str(held.relative_to(self.root)))
        self.assertEqual(report.held, 1)
        self.assertFalse(list((self.root / "jo/google-only").rglob("*")))
        self.assertFalse(list((self.root / "sam/google-only").rglob("*")))
        self.assertIsNone(self.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalar_one_or_none())

    def test_holding_twice_keeps_one_copy(self):
        _, mine = self.phone("sam", self.sam)
        entry = self.entry(self.staged(), tier=4, person=self.sam, matched=mine)
        self.place(entry)
        before = tree(self.root)
        self.place(entry)
        self.assertEqual(tree(self.root), before)

    def test_a_resolved_review_is_not_held_again(self):
        _, mine = self.phone("sam", self.sam)
        entry = self.entry(self.staged(), tier=4, person=self.sam, matched=mine)
        self.opened.execute(select(ReviewItem)).scalar_one().status = "skipped"
        report = self.place(entry)
        self.assertEqual((report.held, report.skipped), (0, 1))
        self.assertFalse((self.root / "_inbox/sam/review").exists())


class OriginalSidecarTests(Household):
    def test_a_tier_one_match_writes_the_missing_metadata_next_to_the_original(self):
        original, mine = self.phone("sam", self.sam, taken="2019:07:04 08:30:15", offset="-04:00")
        before = sha1_of(original)
        report = self.place(self.entry(self.staged(), tier=1, person=self.sam, matched=mine))
        sidecar = original.with_name("IMG_0100.JPG.xmp")
        tags = exiftool.read([sidecar])[sidecar]
        self.assertEqual(tags["XMP-dc:Description"], "A made-up square")
        self.assertAlmostEqual(tags["XMP-exif:GPSLongitude"], 12.3388, places=5)
        self.assertNotIn("XMP-photoshop:DateCreated", tags)
        self.assertEqual(sha1_of(original), before)
        self.assertEqual(report.sidecars, 1)
        self.assertIsNone(self.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalar_one_or_none())
        self.assertFalse(list((self.root / "jo/google-only").rglob("*")))

    def test_a_tier_two_match_does_the_same(self):
        original, mine = self.phone("sam", self.sam)
        self.place(self.entry(self.staged(), tier=2, person=self.sam, matched=mine))
        self.assertTrue(original.with_name("IMG_0100.JPG.xmp").is_file())

    def test_an_original_that_has_location_and_description_gets_no_sidecar(self):
        original, mine = self.phone("sam", self.sam, gps=(1.0, 2.0))
        fixtures.set_tags(original, {"ImageDescription": "mine"})
        mine.sha1 = sha1_of(original)
        report = self.place(self.entry(self.staged(), tier=1, person=self.sam, matched=mine))
        self.assertEqual(sorted(p.name for p in original.parent.iterdir()), ["IMG_0100.JPG"])
        self.assertEqual(report.sidecars, 0)

    def test_an_existing_sidecar_next_to_the_original_is_merged(self):
        original, mine = self.phone("sam", self.sam)
        sidecar = original.with_name("IMG_0100.JPG.xmp")
        exiftool.write_xmp(sidecar, {"XMP-dc:Description": "Written earlier"})
        self.place(self.entry(self.staged(), tier=1, person=self.sam, matched=mine))
        tags = exiftool.read([sidecar])[sidecar]
        self.assertEqual(tags["XMP-dc:Description"], "Written earlier")
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 45.434, places=5)

    def test_no_sidecar_for_what_does_not_prove_a_phone_original(self):
        original, mine = self.phone("sam", self.sam)
        placed = MediaItem(person_id=self.jo, source="takeout", path="jo/google-only/2019/07/x.jpg", sha1="f" * 40, bytes=1, indexed_at=1.0,
                           export_id=self.export.id)
        self.opened.add(placed)
        self.opened.flush()
        reversed_entry = self.entry(self.staged("r.jpg"), tier=1, person=self.sam, matched=mine)
        dedupe.reverse(self.opened, reversed_entry.decision.id, 20.0, "owner")
        entries = [
            self.entry(self.staged("t.jpg"), tier=1, person=self.jo, matched=placed),
            self.entry(self.staged("o.jpg"), tier=1, person=self.sam, matched=mine, decided_by="owner"),
            self.entry(self.staged("e-edited.jpg"), tier=3, person=self.sam, matched=mine),
            reversed_entry,
        ]
        report = self.place(*entries)
        self.assertEqual(sorted(p.name for p in original.parent.iterdir()), ["IMG_0100.JPG"])
        self.assertEqual((report.skipped, report.sidecars, report.placed), (4, 0, 0))

    def test_a_missing_original_is_skipped(self):
        original, mine = self.phone("sam", self.sam)
        entry = self.entry(self.staged(), tier=1, person=self.sam, matched=mine)
        original.rename(self.root / "moved-away-by-the-test.jpg")
        self.assertEqual(self.place(entry).skipped, 1)

    def test_an_original_path_outside_the_owners_folder_is_refused(self):
        _, mine = self.phone("sam", self.sam)
        entry = self.entry(self.staged(), tier=1, person=self.sam, matched=mine)
        mine.path = "jo/originals/IMG_0100.JPG"
        with self.assertRaises(paths.OutsideRoot):
            self.place(entry)

    def test_a_decision_from_another_export_is_refused(self):
        entry = self.entry(self.staged())
        other = Export(account_id=self.account, kind="manual")
        self.opened.add(other)
        self.opened.flush()
        with self.assertRaises(place.PlaceFailed):
            place.place(self.opened, self.photos, other, [entry])


class FixtureExportRun(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = paths.Photos(self.root)
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.jo, self.sam = add_person(self.opened, "jo"), add_person(self.opened, "sam")
        account = GoogleAccount(email="family@x.test", default_person_id=self.jo, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
        self.opened.add(pixel)
        self.opened.flush()
        self.opened.add(PixelPerson(pixel_id=pixel.id, person_id=self.sam))
        self.export = Export(account_id=account.id, kind="manual")
        self.opened.add(self.export)
        self.opened.flush()
        for slug in ("jo", "sam"):
            self.photos.ensure_person_tree(slug)
        manifest = standard_export(self.root / "_inbox/jo/parts")
        self.staging = self.photos.staging("jo", unpack.staging_name(self.export))
        for part in manifest.parts:
            with zipfile.ZipFile(part) as archive:
                archive.extractall(self.staging)
        builder = TakeoutBuilder(self.root / "_inbox/jo/other", seed=7)
        self.originals = {
            "jo/originals/IMG_0001.JPG": (self.staging / "Takeout/Google Photos/Photos from 2019/IMG_0001.JPG").read_bytes(),
            "sam/originals/2019/IMG_0012.JPG": (self.staging / "Takeout/Google Photos/Photos from 2019/IMG_0012.JPG").read_bytes(),
            "sam/originals/IMG_5000.JPG": builder.image_bytes(".JPG"),
            "jo/originals/IMG_5001.JPG": builder.image_bytes(".JPG"),
        }
        for relative, data in self.originals.items():
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_bytes(data)
        for person_id in (self.jo, self.sam):
            index.index_person(self.opened, self.photos, self.opened.get(Person, person_id), 5.0)

    def originals_tree(self):
        return {key: value for key, value in tree(self.root).items() if "/originals/" in f"/{key}"}

    def run_once(self):
        report = pairing.pair(self.staging)
        tags = exiftool.read([item.path for item in report.items])
        fields = {}
        for item in report.items:
            raw = item.sidecar.read_text() if item.sidecar else None
            parsed = sidecar.parse(raw) if raw else None
            fields[item.path] = metadata.candidate(item, tags[item.path], parsed, self.jo, "unplaced", 10.0, self.export.id)
        candidates = [dedupe.candidate_of(item, fields[item.path], self.staging) for item in report.items]
        decisions = dedupe.decide_all(dedupe.PersonIndex.load(self.opened, self.export.account_id), candidates, OFF)
        rows = [dedupe.apply(self.opened, decision, self.export, 10.0) for decision in decisions]
        entries = [place.Entry(item, fields[item.path], row) for item, row in zip(report.items, rows, strict=True)]
        return place.place(self.opened, self.photos, self.export, entries)

    def test_every_original_keeps_its_sha1_and_only_sidecars_are_added(self):
        before = self.originals_tree()
        self.assertEqual(len(before), 4)
        report = self.run_once()
        after = self.originals_tree()
        self.assertEqual({key: after[key] for key in before}, before)
        added = set(after) - set(before)
        self.assertEqual(added, {"jo/originals/IMG_0001.JPG.xmp", "sam/originals/2019/IMG_0012.JPG.xmp"})
        self.assertTrue(all(name.endswith(".xmp") for name in added))
        self.assertGreater(report.placed, 5)
        for relative, data in self.originals.items():
            self.assertEqual(sha1_of(self.root / relative), hashlib.sha1(data, usedforsecurity=False).hexdigest())

    def test_sidecars_read_back_with_the_takeout_values(self):
        self.run_once()
        sidecar_path = self.root / "jo/originals/IMG_0001.JPG.xmp"
        tags = exiftool.read([sidecar_path])[sidecar_path]
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 40.7, places=5)
        self.assertAlmostEqual(tags["XMP-exif:GPSLongitude"], -74.0, places=5)
        placed = self.root / "jo/google-only/2017/07/IMG_0002.JPG.xmp"
        tags = exiftool.read([placed])[placed]
        self.assertAlmostEqual(tags["XMP-exif:GPSLatitude"], 40.7, places=5)
        self.assertTrue(str(tags["XMP-photoshop:DateCreated"]).endswith("-04:00"))

    def test_a_second_run_writes_no_second_sidecar_and_changes_nothing(self):
        self.run_once()
        self.opened.flush()
        before = tree(self.root)
        rows = len(list(self.opened.execute(select(MediaItem)).scalars()))
        report = self.run_once()
        self.assertEqual(tree(self.root), before)
        self.assertEqual(len(list(self.opened.execute(select(MediaItem)).scalars())), rows)
        self.assertEqual((report.placed, report.sidecars), (0, 0))
        self.assertEqual(len(list(self.opened.execute(select(DedupeDecision)).scalars())), len(pairing.pair(self.staging).items))

    def test_google_only_items_land_with_the_default_person_only(self):
        self.run_once()
        self.assertTrue(any((self.root / "jo/google-only").rglob("*.JPG")))
        self.assertEqual([p for p in (self.root / "sam/google-only").rglob("*") if p.is_file()], [])
        placed = list(self.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars())
        self.assertTrue(placed)
        self.assertEqual({row.person_id for row in placed}, {self.jo})
        for row in placed:
            self.assertTrue((self.root / row.path).is_file())