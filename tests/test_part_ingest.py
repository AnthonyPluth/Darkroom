import json
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import dedupe, exports
from darkroom.domain.ingest import metadata, part_ingest, part_reader, place, run
from darkroom.storage import db, paths
from darkroom.storage.models import (
    Alert, DedupeDecision, DuplicateRemoval, Event, Export, ExportPart, MediaItem, ReviewItem, TakeoutEntry,
)
from tests.import_world import DAY, NOW, World, tree
from tests.privacy import no_leaks
from tests.takeout_fixtures import GOOGLE_PHOTOS, TakeoutBuilder

PREFIX = f"{GOOGLE_PHOTOS}/"
TRASH = "Trash"


class Crash(BaseException):
    pass


def short(name):
    return name.removeprefix(PREFIX)


class PartWorld(World):
    def create(self, build, parts=3, seed=7, status="downloading", google_id="google-123", created=NOW - DAY, queue=True):
        inbox = self.photos.inbox(self.people[0]) / google_id
        builder = TakeoutBuilder(inbox, seed=seed, parts=parts, stamp=f"2026010{len(google_id) % 9 + 1}T000000Z")
        build(builder)
        zips = builder.build().parts
        self.index_all()
        export = Export(account_id=self.account, kind="initial", google_id=google_id, created_at=created, ready_at=NOW, expires_at=NOW + 7 * DAY,
                        part_count=len(zips), status=status, inbox_dir=str(inbox))
        self.opened.add(export)
        self.opened.flush()
        self.opened.add_all(ExportPart(export_id=export.id, number=number, filename=path.name, bytes=path.stat().st_size, status="done",
                                       ingest_state="queued" if queue else "not_queued", ingest_queued_at=NOW if queue else None)
                            for number, path in enumerate(zips, start=1))
        self.opened.commit()
        return export

    def part(self, export, number):
        return self.opened.execute(select(ExportPart).where(ExportPart.export_id == export.id, ExportPart.number == number)).scalar_one()

    def ingest(self, export, number, at=NOW, tools=None):
        self.opened.expire_all()
        found = self.opened.get(Export, export.id)
        return part_ingest.ingest_part(self.opened, self.settings, found, self.part(found, number), at, tools or self.tools)

    def ingest_all(self, export, numbers=(1, 2, 3)):
        return [self.ingest(export, number) for number in numbers]

    def entry(self, name):
        self.opened.expire_all()
        return self.opened.execute(select(TakeoutEntry).where(TakeoutEntry.name == f"{PREFIX}{name}").order_by(TakeoutEntry.id)).scalars().first()

    def ledger(self):
        self.opened.expire_all()
        names = {row.id: short(row.name) for row in self.opened.execute(select(TakeoutEntry)).scalars()}
        tiers = {row.id: row.tier for row in self.opened.execute(select(DedupeDecision)).scalars()}
        items = {row.id: row.path for row in self.opened.execute(select(MediaItem)).scalars()}
        rows = self.opened.execute(select(TakeoutEntry).order_by(TakeoutEntry.id)).scalars()
        return [(row.part_number, short(row.name), row.kind, row.outcome, row.sha1, names.get(row.sidecar_entry_id), names.get(row.copy_of_id),
                 tiers.get(row.decision_id), items.get(row.item_id), row.metadata_pending, row.is_edited) for row in rows]

    def outcomes(self):
        return {name: outcome for _, name, kind, outcome, *_ in self.ledger() if kind == "media"}

    def decisions(self):
        self.opened.expire_all()
        paths_of = {row.id: row.path for row in self.opened.execute(select(MediaItem)).scalars()}
        rows = self.opened.execute(select(DedupeDecision).order_by(DedupeDecision.source_path)).scalars()
        return [(short(row.source_path), row.tier, row.person_id, paths_of.get(row.matched_item_id), paths_of.get(row.takeout_item_id), row.decided_by)
                for row in rows]

    def files(self):
        return {key: value for key, value in tree(self.root, "manual", "google-123", "parts", "google-456").items()}

    def state(self):
        self.opened.expire_all()
        reviews = [(row.status, row.hold_path, row.hold_sha1) for row in self.opened.execute(select(ReviewItem).order_by(ReviewItem.id)).scalars()]
        return {"ledger": self.ledger(), "decisions": self.decisions(), "files": self.files(), "reviews": reviews,
                "items": [(row.person_id, row.source, row.path, row.sha1, row.confirmed_in_google, row.lat, row.lon, row.sidecar_json)
                          for row in self.opened.execute(select(MediaItem).order_by(MediaItem.path)).scalars()]}

    def scenario(self, builder):
        exact = builder.image_bytes(".JPG")
        self.original("jo", "IMG_1001.JPG", exact)
        builder.photo("IMG_1001.JPG", data=exact, gps=None, part=0)
        late = builder.image_bytes(".JPG")
        self.original("sam", "2019/IMG_1002.JPG", late)
        builder.photo("IMG_1002.JPG", data=late, part=0, sidecar_part=2, gps=(46.0, 11.0), description="a late description")
        moment = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
        self.original("jo", "IMG_1003.JPG", self.scratch_jpeg("t2a.jpg", colour=(10, 120, 30), **moment))
        builder.photo("IMG_1003.JPG", data=self.scratch_jpeg("t2b.jpg", colour=(12, 118, 33), **moment), gps=None, taken=1562221815, part=1)
        edited_of = builder.image_bytes(".JPG")
        self.original("sam", "IMG_1004.JPG", edited_of)
        builder.photo("IMG_1004.JPG", data=edited_of, part=1)
        builder.add("IMG_1004-edited.JPG", builder.image_bytes(".JPG"), "Photos from 2019", 2, None, None, None, "")
        near = {"offset": "+02:00", "make": "Apple", "model": "iPhone 11", "colour": (90, 90, 200)}
        self.original("jo", "IMG_1005.JPG", self.scratch_jpeg("t4a.jpg", taken="2019:08:01 10:00:00", **near))
        builder.photo("IMG_1005.JPG", data=self.scratch_jpeg("t4b.jpg", taken="2019:08:01 10:00:01", **near), gps=None, part=2)
        builder.photo("IMG_2001.JPG", gps=(45.4, 12.3), description="A made-up canal", part=0)
        builder.photo("IMG_2002.JPG", gps=(47.0, 12.0), part=1, sidecar_part=2)
        builder.photo("IMG_2003.JPG", form=None, part=1)
        never = builder.image_bytes(".JPG")
        self.original("jo", "IMG_4001.JPG", never)
        builder.photo("IMG_4001.JPG", data=never, form=None, part=1)

    def whole_export_twin(self):
        twin = PartWorld(self.test)
        export = twin.create(twin.scenario, status="downloaded", queue=False)
        twin.run_import(export)
        return twin

    def google_only(self):
        return {key for key in self.files() if "/google-only/" in f"/{key}" and not key.endswith(".xmp")}

    def xmp_of(self, key):
        return self.root / f"{key}.xmp"


class ThreePartTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)
        self.export = self.world.create(self.world.scenario)

    def test_parts_ingest_one_by_one_and_hold_what_waits_for_something_later(self):
        reports = self.world.ingest_all(self.export)
        self.assertEqual([report.ingested for report in reports], [True, True, True])
        self.assertEqual(self.world.outcomes(), {
            "Photos from 2019/IMG_1001.JPG": "skipped", "Photos from 2019/IMG_1002.JPG": "skipped", "Photos from 2019/IMG_1003.JPG": "waiting_export",
            "Photos from 2019/IMG_1004.JPG": "skipped", "Photos from 2019/IMG_1004-edited.JPG": "waiting_export", "Photos from 2019/IMG_1005.JPG": "review",
            "Photos from 2019/IMG_2001.JPG": "placed", "Photos from 2019/IMG_2002.JPG": "placed", "Photos from 2019/IMG_2003.JPG": "waiting_sidecar",
            "Photos from 2019/IMG_4001.JPG": "skipped",
        })
        self.assertEqual({(name, tier) for name, tier, *_ in self.world.decisions()},
                         {(f"Photos from 2019/{name}", tier) for name, tier in (("IMG_1001.JPG", 1), ("IMG_1002.JPG", 1), ("IMG_1004.JPG", 1), ("IMG_1005.JPG", 4),
                                                                              ("IMG_2001.JPG", 5), ("IMG_2002.JPG", 5), ("IMG_4001.JPG", 1))})
        self.assertEqual({key.rsplit("/", 1)[-1] for key in self.world.google_only()}, {"IMG_2001.JPG", "IMG_2002.JPG"})
        review = self.world.opened.execute(select(ReviewItem)).scalar_one()
        self.assertEqual((review.status, review.hold_path), ("pending", "_inbox/jo/review/export-1/IMG_1005.JPG"))

    def test_held_entries_keep_their_rows_and_their_bytes_stay_in_the_zip(self):
        self.world.ingest_all(self.export)
        for name in ("IMG_1003.JPG", "IMG_1004-edited.JPG", "IMG_2003.JPG"):
            row = self.world.entry(f"Photos from 2019/{name}")
            self.assertIsNotNone(row.sha1)
            self.assertIsNone(row.decision_id)
            self.assertIsNone(row.item_id)
            zipped = Path(self.export.inbox_dir) / self.world.part(self.export, row.part_number).filename
            with zipfile.ZipFile(zipped) as archive:
                self.assertEqual(len(archive.read(row.name)), row.bytes)
        placed = {key for key in self.world.files() if "/originals/" not in f"/{key}"}
        self.assertEqual({key for key in placed if "IMG_2003" in key or "IMG_1003" in key or "-edited" in key}, set())

    def test_each_part_records_its_state_and_counts(self):
        self.assertEqual([self.world.part(self.export, n).ingest_state for n in (1, 2, 3)], ["queued"] * 3)
        self.world.ingest(self.export, 1, at=NOW + 5)
        first = self.world.part(self.export, 1)
        self.assertEqual((first.ingest_state, first.ingest_queued_at, first.ingested_at, first.entries_done, first.held, first.failed_items),
                         ("ingested", NOW, NOW + 5, first.entries, 0, 0))
        self.assertEqual(self.world.part(self.export, 2).ingest_state, "queued")
        self.world.ingest(self.export, 2)
        self.world.ingest(self.export, 3)
        self.assertEqual([(p.ingest_state, p.entries_done, p.held) for p in (self.world.part(self.export, n) for n in (1, 2, 3))],
                         [("ingested", self.world.part(self.export, n).entries, held) for n, held in ((1, 0), (2, 2), (3, 1))])
        self.assertEqual(self.export.status, "downloading")

    def test_the_export_stays_downloading_and_no_run_is_created(self):
        self.world.ingest_all(self.export)
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "downloading")
        self.assertEqual(self.world.count(run.ImportRun), 0)

    def test_nothing_is_left_in_the_scratch_folder(self):
        self.world.ingest_all(self.export)
        scratch = self.world.photos.scratch("jo", f"export-{self.export.id}")
        self.assertEqual(list(scratch.glob("*")) if scratch.exists() else [], [])

    def test_a_tier_1_entry_whose_sidecar_comes_later_waits_and_is_filled_when_it_arrives(self):
        original = "sam/originals/2019/IMG_1002.JPG"
        self.world.ingest(self.export, 1)
        self.assertTrue(self.world.entry("Photos from 2019/IMG_1002.JPG").metadata_pending)
        self.assertFalse(self.world.xmp_of(original).exists())
        self.world.ingest(self.export, 2)
        self.assertFalse(self.world.xmp_of(original).exists())
        self.world.ingest(self.export, 3)
        row = self.world.entry("Photos from 2019/IMG_1002.JPG")
        self.assertFalse(row.metadata_pending)
        self.assertIn("a late description", self.world.xmp_of(original).read_text())

    def test_a_tier_1_entry_whose_sidecar_never_comes_is_never_filled(self):
        self.world.ingest_all(self.export)
        row = self.world.entry("Photos from 2019/IMG_4001.JPG")
        self.assertEqual((row.outcome, row.metadata_pending, row.sidecar_entry_id), ("skipped", True, None))
        self.assertFalse(self.world.xmp_of("jo/originals/IMG_4001.JPG").exists())

    def test_a_sidecar_that_arrives_later_pairs_with_held_media_and_it_is_decided_on_arrival(self):
        self.world.ingest(self.export, 1)
        self.world.ingest(self.export, 2)
        self.assertEqual(self.world.entry("Photos from 2019/IMG_2002.JPG").outcome, "waiting_sidecar")
        self.assertEqual({key.rsplit("/", 1)[-1] for key in self.world.google_only()}, {"IMG_2001.JPG"})
        self.world.ingest(self.export, 3)
        row = self.world.entry("Photos from 2019/IMG_2002.JPG")
        self.assertEqual(row.outcome, "placed")
        placed = self.world.opened.get(MediaItem, row.item_id)
        self.assertEqual((placed.lat, placed.lon, placed.gps_source), (47.0, 12.0, "google"))
        self.assertEqual(self.world.part(self.export, 2).held, 3 - 1)

    def test_what_is_decided_equals_whole_export_ingest(self):
        self.world.ingest_all(self.export)
        twin = self.world.whole_export_twin()
        per_part, whole = self.world.state(), twin.state()
        self.assertEqual(set(per_part["decisions"]) - set(whole["decisions"]), set())
        self.assertEqual({name for name, *_ in whole["decisions"]} - {name for name, *_ in per_part["decisions"]},
                         {"Photos from 2019/IMG_1003.JPG", "Photos from 2019/IMG_1004-edited.JPG", "Photos from 2019/IMG_2003.JPG"})
        left = {key: value for key, value in per_part["files"].items() if "/scratch/" not in key}
        right = {key: value for key, value in whole["files"].items() if "/staging/" not in key}
        self.assertEqual({key: value for key, value in left.items() if key in right and right[key] != value}, {})
        self.assertEqual(set(left) - set(right), set())
        self.assertEqual({key.rsplit("/", 1)[-1] for key in set(right) - set(left) if "/google-only/" in f"/{key}"}, {"IMG_2003.JPG"})
        self.assertEqual(set(per_part["reviews"]), set(whole["reviews"]))
        self.assertEqual({row for row in per_part["items"] if row[1] == "takeout"} - {row for row in whole["items"] if row[1] == "takeout"}, set())

    def test_the_parts_ingest_in_any_order_to_the_same_decisions(self):
        self.world.ingest_all(self.export, (3, 1, 2))
        other = PartWorld(self)
        export = other.create(other.scenario)
        other.ingest_all(export)
        self.assertEqual(self.world.decisions(), other.decisions())
        self.assertEqual(self.world.outcomes(), other.outcomes())
        self.assertEqual({key: value for key, value in self.world.files().items() if "/scratch/" not in key},
                         {key: value for key, value in other.files().items() if "/scratch/" not in key})


class CopyAndHoldTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)

    def test_an_album_copy_in_a_later_part_attaches_to_its_item_and_places_nothing(self):
        def build(builder):
            data = builder.image_bytes(".JPG")
            builder.add("IMG_5001.JPG", data, "Photos from 2019", 0, "json", None, (45.4, 12.3), "")
            builder.add("IMG_5001.JPG", data, "Holiday", 1, "json", None, (45.4, 12.3), "")

        export = self.world.create(build, parts=2)
        self.world.ingest_all(export, (1, 2))
        ledger = {(part, name): (outcome, copy_of) for part, name, kind, outcome, _, _, copy_of, *_ in self.world.ledger() if kind == "media"}
        self.assertEqual(ledger, {(1, "Photos from 2019/IMG_5001.JPG"): ("placed", None),
                                  (2, "Holiday/IMG_5001.JPG"): ("copy", "Photos from 2019/IMG_5001.JPG")})
        self.assertEqual(len(self.world.decisions()), 1)
        self.assertEqual(len(self.world.google_only()), 1)

    def test_a_copy_arriving_after_its_item_was_removed_is_never_placed_again(self):
        def build(builder):
            data = builder.image_bytes(".JPG")
            builder.add("IMG_5002.JPG", data, "Photos from 2019", 0, "json", None, (45.4, 12.3), "")
            builder.add("IMG_5002.JPG", data, "Holiday", 1, "json", None, (45.4, 12.3), "")

        export = self.world.create(build, parts=2)
        self.world.ingest(export, 1)
        decision = self.world.opened.execute(select(DedupeDecision)).scalar_one()
        item = self.world.opened.get(MediaItem, decision.takeout_item_id)
        self.world.opened.add(DuplicateRemoval(person_id=item.person_id, decision_id=decision.id, kept_item_id=None, removed_item_id=item.id,
                                               kept_path="x", kept_sha1="a" * 40, removed_path=item.path, removed_sha1=item.sha1,
                                               removed_source="takeout", rule="identical", tier=1, state="removed", created_at=NOW))
        self.world.opened.commit()
        (self.world.root / item.path).unlink()
        before = self.world.files()
        self.world.ingest(export, 2)
        self.assertEqual(self.world.files(), before)
        self.assertEqual(len(self.world.decisions()), 1)
        self.assertEqual(self.world.outcomes()["Holiday/IMG_5002.JPG"], "copy")

    def test_a_trashed_item_waits_for_the_export(self):
        def build(builder):
            data = builder.image_bytes(".JPG")
            builder.add("IMG_5003.JPG", data, TRASH, 0, "json", None, (45.4, 12.3), "")

        export = self.world.create(build, parts=1)
        self.world.ingest(export, 1)
        self.assertEqual(self.world.outcomes(), {f"{TRASH}/IMG_5003.JPG": "waiting_export"})
        self.assertEqual(self.world.decisions(), [])
        self.assertEqual(self.world.google_only(), set())

    def test_a_live_photo_video_without_its_own_sidecar_waits_and_borrows_nothing(self):
        def build(builder):
            builder.live_photo("IMG_6001", part=0, video_sidecar=False)

        export = self.world.create(build, parts=2)
        self.world.ingest_all(export, (1, 2))
        outcomes = self.world.outcomes()
        self.assertEqual((outcomes["Photos from 2019/IMG_6001.HEIC"], outcomes["Photos from 2019/IMG_6001.MOV"]), ("placed", "waiting_sidecar"))
        self.assertEqual(len(self.world.decisions()), 1)


class HeldAcrossPartsTests(unittest.TestCase):
    def test_a_burst_whose_rival_is_in_another_part_is_held_by_both_until_the_export_finishes(self):
        world = PartWorld(self)
        moment = {"taken": "2019:09:09 09:09:09", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}

        def build(builder):
            world.original("jo", "IMG_1006.JPG", world.scratch_jpeg("b0.jpg", colour=(200, 10, 10), **moment))
            builder.photo("IMG_1006.JPG", data=world.scratch_jpeg("b1.jpg", colour=(201, 12, 10), **moment), gps=None, part=0)
            builder.photo("IMG_1007.JPG", data=world.scratch_jpeg("b2.jpg", colour=(202, 14, 12), **moment), gps=None, part=1)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual(world.outcomes(), {"Photos from 2019/IMG_1006.JPG": "waiting_export", "Photos from 2019/IMG_1007.JPG": "waiting_export"})
        self.assertEqual(world.decisions(), [])
        self.assertEqual(world.opened.execute(select(ReviewItem)).first(), None)
        self.assertEqual(world.google_only(), set())


class ExportsApiFieldTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)
        self.export = self.world.create(self.world.scenario)

    def view(self):
        self.world.opened.expire_all()
        return exports.as_dict(self.world.opened, self.world.opened.get(Export, self.export.id), NOW)

    def ledger_held(self, number):
        rows = self.world.opened.execute(select(TakeoutEntry).where(TakeoutEntry.part_number == number)).scalars()
        counts = [row.outcome for row in rows]
        return counts.count("waiting_sidecar"), counts.count("waiting_export")

    def test_before_any_part_ingests_every_part_is_queued_with_zeros(self):
        export = self.view()
        self.assertEqual((export["parts_ingested"], export["held_waiting_sidecar"], export["held_waiting_export"], export["bytes_freed"]), (0, 0, 0, 0))
        self.assertEqual([(part["ingest_state"], part["entries"], part["held"], part["held_waiting_sidecar"], part["held_waiting_export"], part["kept_reason"])
                          for part in export["parts"]], [("queued", 0, 0, 0, 0, None)] * 3)

    def test_the_view_matches_the_ledger_after_each_part(self):
        self.world.ingest_all(self.export)
        export = self.view()
        self.assertEqual(export["parts_ingested"], 3)
        for part in export["parts"]:
            row = self.world.part(self.export, part["number"])
            sidecar, held_export = self.ledger_held(part["number"])
            self.assertEqual((part["ingest_state"], part["entries"], part["entries_done"], part["held"], part["failed_items"]),
                             (row.ingest_state, row.entries, row.entries_done, row.held, row.failed_items))
            self.assertEqual((part["held_waiting_sidecar"], part["held_waiting_export"]), (sidecar, held_export))
            self.assertEqual(part["held"], sidecar + held_export)
        self.assertEqual((export["held_waiting_sidecar"], export["held_waiting_export"]), (1, 2))
        self.assertEqual(sum(part["held_waiting_sidecar"] for part in export["parts"]), 1)

    def test_a_part_with_a_failed_item_is_kept_for_a_failed_item(self):
        real = place.place

        def broken(opened, photos, export, entries, *args, **kwargs):
            if any(entry.named is not None and entry.named.name == "IMG_2001.JPG" for entry in entries):
                raise place.PlaceFailed("both names are taken by other files")
            return real(opened, photos, export, entries, *args, **kwargs)

        with mock.patch.object(place, "place", side_effect=broken):
            self.world.ingest(self.export, 1)
        first = self.view()["parts"][0]
        self.assertEqual((first["ingest_state"], first["failed_items"], first["kept_reason"]), ("ingested", 1, "failed_item"))

    def test_a_refused_part_has_no_reason_code_and_no_entry_name(self):
        self.world.ingest(self.export, 1)
        row = self.world.part(self.export, 1)
        with zipfile.ZipFile(Path(self.export.inbox_dir) / row.filename, "w") as archive:
            archive.writestr(f"{PREFIX}Photos from 2019/OTHER_CANARY_NAME.JPG", b"other bytes")
        row.ingest_state = "queued"
        self.world.opened.commit()
        self.world.ingest(self.export, 1)
        first = self.view()["parts"][0]
        self.assertEqual((first["ingest_state"], first["kept_reason"]), ("refused", None))

    def test_no_entry_name_or_sidecar_text_reaches_the_view(self):
        self.world.ingest_all(self.export)
        text = json.dumps(self.view())
        for canary in ("IMG_", "canal", "late description", "Photos from 2019", ".JPG", ".json"):
            self.assertNotIn(canary, text)
        with no_leaks(self, "IMG_2003.JPG", "A made-up canal", "a late description", replies=[text.encode()]):
            pass


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)
        self.export = self.world.create(self.world.scenario)

    def test_an_item_that_cannot_be_measured_is_failed_counted_and_the_part_is_still_ingested(self):
        real = metadata.candidate

        def candidate(item, *args, **kwargs):
            if item.path.name == "IMG_2001.JPG":
                raise ValueError("IMG_2001.JPG-is-private")
            return real(item, *args, **kwargs)

        with mock.patch.object(metadata, "candidate", side_effect=candidate), no_leaks(self, "IMG_2001.JPG-is-private", database=self.world.engine):
            self.world.ingest(self.export, 1)
        part = self.world.part(self.export, 1)
        self.assertEqual((part.ingest_state, part.failed_items), ("ingested", 1))
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2001.JPG"], "failed")
        failed = [row for row in self.world.opened.execute(select(Event).where(Event.type == "import_item_failed")).scalars()]
        self.assertEqual([(row.subject, row.detail) for row in failed], [("export 1", "metadata: ValueError")])

    def test_an_item_that_cannot_be_placed_is_failed_and_the_rest_carry_on(self):
        real = place.place

        def broken(opened, photos, export, entries, *args, **kwargs):
            if any(entry.named is not None and entry.named.name == "IMG_2001.JPG" for entry in entries):
                raise place.PlaceFailed("both names are taken by other files")
            return real(opened, photos, export, entries, *args, **kwargs)

        with mock.patch.object(place, "place", side_effect=broken):
            self.world.ingest(self.export, 1)
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2001.JPG"], "failed")
        self.assertEqual(self.world.part(self.export, 1).failed_items, 1)
        self.assertEqual(self.world.part(self.export, 1).ingest_state, "ingested")
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_1001.JPG"], "skipped")

    def test_a_part_the_reader_refuses_is_refused_writes_nothing_and_alerts(self):
        self.world.ingest(self.export, 1)
        before = self.world.state()
        row = self.world.part(self.export, 1)
        zipped = Path(self.export.inbox_dir) / row.filename
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr(f"{PREFIX}Photos from 2019/OTHER_CANARY_NAME.JPG", b"other bytes")
        row.ingest_state = "queued"
        self.world.opened.commit()
        with no_leaks(self, "OTHER_CANARY_NAME"):
            report = self.world.ingest(self.export, 1)
        self.assertEqual((report.ingested, report.refused), (False, True))
        self.assertEqual(self.world.part(self.export, 1).ingest_state, "refused")
        self.assertEqual({key: value for key, value in self.world.state().items() if key != "ledger"},
                         {key: value for key, value in before.items() if key != "ledger"})
        refused = self.world.opened.execute(select(Event).where(Event.type == "export_part_refused")).scalar_one()
        self.assertEqual((refused.subject, refused.detail), (f"export {self.export.id}", "part 1: part 1 doesn't match the part listed before"))
        (alert,) = self.world.opened.execute(select(Alert).where(Alert.kind == "failure")).scalars()
        self.assertNotIn("CANARY", alert.message)
        self.assertEqual(self.world.sent("Darkroom needs a look"), 1)


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)
        self.export = self.world.create(self.world.scenario)

    def test_an_ingested_part_is_not_read_again(self):
        self.world.ingest(self.export, 1)
        before = self.world.state()
        with mock.patch.object(part_reader, "read_part") as read:
            report = self.world.ingest(self.export, 1)
        read.assert_not_called()
        self.assertEqual((report.ingested, report.refused), (False, False))
        self.assertEqual(self.world.state(), before)

    def test_a_part_ingested_twice_at_once_is_skipped_as_busy(self):
        with run.account_lock(self.world.settings, self.export.account_id):
            with self.assertRaises(run.Busy):
                self.world.ingest(self.export, 1)
        self.assertEqual(self.world.part(self.export, 1).ingest_state, "queued")
        self.assertEqual(self.world.count(TakeoutEntry), 0)

    def test_a_part_that_is_not_queued_is_not_ingested(self):
        other = PartWorld(self)
        export = other.create(other.scenario, queue=False)
        with self.assertRaises(run.NotReady):
            other.ingest(export, 1)
        self.assertEqual(other.count(TakeoutEntry), 0)

    def test_a_part_that_is_not_downloaded_is_not_ingested(self):
        self.world.part(self.export, 1).status = "downloading"
        self.world.opened.commit()
        with self.assertRaises(run.NotReady):
            self.world.ingest(self.export, 1)


def recording_writes(writes):
    real = paths.write_once

    def recording(destination, chunks, *args, **kwargs):
        writes.append(str(destination))
        return real(destination, chunks, *args, **kwargs)

    return mock.patch.object(paths, "write_once", side_effect=recording)


def counting(counted):
    return mock.patch.object(part_ingest, "COMMIT_HOOK", lambda: counted.__setitem__(0, counted[0] + 1))


def crashing_at(target):
    seen = [0]

    def hook():
        seen[0] += 1
        if seen[0] == target:
            raise Crash

    return mock.patch.object(part_ingest, "COMMIT_HOOK", hook)


class CrashTests(unittest.TestCase):
    def crash_at(self, world, export, number, target):
        commits = [0]

        def hook():
            commits[0] += 1
            if commits[0] == target:
                raise Crash

        writes = []
        with mock.patch.object(part_ingest, "COMMIT_HOOK", hook), recording_writes(writes):
            try:
                world.ingest(export, number)
            except Crash:
                world.opened.rollback()
        return commits[0], writes

    def uninterrupted(self):
        world = PartWorld(self)
        export = world.create(world.scenario)
        counts = []
        for number in (1, 2, 3):
            counted = [0]
            with counting(counted):
                world.ingest(export, number)
            counts.append(counted[0])
        return world.state(), counts

    def resume(self, world, export, number):
        resumed = []
        with recording_writes(resumed):
            for later in range(number, 4):
                world.ingest(export, later)
        return resumed

    def test_a_crash_after_any_commit_resumes_to_the_same_result_and_writes_no_file_twice(self):
        expected, counts = self.uninterrupted()
        for number, commits in enumerate(counts, start=1):
            for target in range(1, commits + 1):
                with self.subTest(part=number, commit=target):
                    world = PartWorld(self)
                    export = world.create(world.scenario)
                    for earlier in range(1, number):
                        world.ingest(export, earlier)
                    reached, writes = self.crash_at(world, export, number, target)
                    self.assertEqual(reached, target)
                    self.assertNotEqual(world.part(export, number).ingest_state, "ingested")
                    resumed = self.resume(world, export, number)
                    self.assertEqual(set(writes) & set(resumed), set())
                    self.assertEqual(len(writes + resumed), len(set(writes + resumed)))
                    self.assertEqual(world.state(), expected)


class BoundedScratchTests(unittest.TestCase):
    def test_a_late_part_that_unblocks_many_entries_restreams_them_in_bounded_batches(self):
        world = PartWorld(self)

        def build(builder):
            for number in range(5):
                builder.photo(f"IMG_80{number}.JPG", part=0, sidecar_part=2, gps=(45.0 + number, 12.0))
            builder.photo("IMG_8100.JPG", part=1, form=None)

        export = world.create(build)
        world.ingest_all(export, (1, 2))
        held = [name for name, outcome in world.outcomes().items() if outcome == "waiting_sidecar"]
        self.assertEqual(len(held), 6)
        scratch = world.photos.scratch("jo", f"export-{export.id}")
        high, sweeps = [0], []
        real_stream, real_sweep = part_reader.stream_one, part_reader.sweep

        def stream(*args, **kwargs):
            streamed = real_stream(*args, **kwargs)
            high[0] = max(high[0], len(list(scratch.glob("*"))))
            return streamed

        def sweep(*args, **kwargs):
            sweeps.append(len(list(scratch.glob("*"))))
            return real_sweep(*args, **kwargs)

        with (mock.patch.object(part_reader, "BATCH_FILES", 2), mock.patch.object(part_reader, "stream_one", side_effect=stream),
              mock.patch.object(part_reader, "sweep", side_effect=sweep)):
            world.ingest(export, 3)
        self.assertEqual(sum(1 for outcome in world.outcomes().values() if outcome == "placed"), 5)
        self.assertLessEqual(high[0], 2)
        self.assertGreaterEqual(len(sweeps), 3)
        self.assertEqual(list(scratch.glob("*")), [])

    def test_the_bytes_bound_also_splits_a_late_parts_entries(self):
        world = PartWorld(self)

        def build(builder):
            for number in range(3):
                builder.photo(f"IMG_81{number}.JPG", part=0, sidecar_part=1, gps=(45.0 + number, 12.0))

        export = world.create(build, parts=2)
        world.ingest(export, 1)
        scratch = world.photos.scratch("jo", f"export-{export.id}")
        high = [0]
        real_stream = part_reader.stream_one

        def stream(*args, **kwargs):
            streamed = real_stream(*args, **kwargs)
            high[0] = max(high[0], len(list(scratch.glob("*"))))
            return streamed

        with mock.patch.object(part_reader, "BATCH_BYTES", 1), mock.patch.object(part_reader, "stream_one", side_effect=stream):
            world.ingest(export, 2)
        self.assertEqual((high[0], sum(1 for outcome in world.outcomes().values() if outcome == "placed")), (1, 3))


class RescanCrashTests(unittest.TestCase):
    def counts(self):
        world = PartWorld(self)
        export = world.create(world.scenario)
        world.ingest(export, 1, at=NOW - 100, tools=world.immich_tools())
        world.ingest(export, 2, at=NOW - 100, tools=world.immich_tools())
        counted = [0]
        with counting(counted):
            world.ingest(export, 3, at=NOW + 10, tools=world.immich_tools())
        return counted[0]

    def test_owners_changed_before_a_crash_are_rescanned_when_the_part_resumes(self):
        commits = self.counts()
        for target in range(1, commits + 1):
            with self.subTest(commit=target):
                world = PartWorld(self)
                export = world.create(world.scenario)
                world.ingest(export, 1, at=NOW - 100, tools=world.immich_tools())
                world.ingest(export, 2, at=NOW - 100, tools=world.immich_tools())
                with crashing_at(target):
                    try:
                        world.ingest(export, 3, at=NOW + 10, tools=world.immich_tools())
                    except Crash:
                        world.opened.rollback()
                before = len(world.scanned())
                world.ingest(export, 3, at=NOW + 20, tools=world.immich_tools())
                self.assertEqual(sorted(set(world.scanned()[before:])), ["lib-jo", "lib-sam"])


class ReversalTests(unittest.TestCase):
    def reversing(self, world, method, wanted):
        real = getattr(part_ingest.Ingest, method)
        done = []

        def reverse_first(ingest, entry, *args, **kwargs):
            if wanted(entry) and not done:
                done.append(entry.name)
                with db.session(world.engine, writing=True) as other:
                    row = dedupe.stored_decision(other, entry.export_id, entry.name) if method == "place_decided" else other.get(
                        DedupeDecision, entry.decision_id)
                    dedupe.reverse(other, row.id, NOW, "owner")
            return real(ingest, entry, *args, **kwargs)

        return mock.patch.object(part_ingest.Ingest, method, reverse_first), done

    def test_a_reversal_that_arrives_before_a_placement_stops_it(self):
        world = PartWorld(self)
        export = world.create(world.scenario)
        patched, done = self.reversing(world, "place_decided", lambda entry: entry.name.endswith("IMG_2001.JPG"))
        with patched:
            world.ingest(export, 1)
        self.assertEqual(len(done), 1)
        self.assertEqual({key.rsplit("/", 1)[-1] for key in world.google_only()}, set())
        self.assertEqual(world.outcomes()["Photos from 2019/IMG_2001.JPG"], "skipped")
        decision = world.opened.execute(select(DedupeDecision).where(DedupeDecision.source_path.endswith("IMG_2001.JPG"))).scalar_one()
        self.assertIsNotNone(decision.reversed_at)

    def test_a_reversal_that_arrives_before_a_late_xmp_fill_stops_it(self):
        world = PartWorld(self)
        export = world.create(world.scenario)
        world.ingest(export, 1)
        world.ingest(export, 2)
        patched, done = self.reversing(world, "fill", lambda entry: entry.name.endswith("IMG_1002.JPG"))
        with patched:
            world.ingest(export, 3)
        self.assertEqual(len(done), 1)
        self.assertFalse(world.xmp_of("sam/originals/2019/IMG_1002.JPG").exists())

    def test_a_reversal_that_arrives_before_a_review_hold_stops_the_hold(self):
        world = PartWorld(self)
        export = world.create(world.scenario)
        world.ingest(export, 1)
        world.ingest(export, 2)
        patched, done = self.reversing(world, "place_decided", lambda entry: entry.name.endswith("IMG_1005.JPG"))
        with patched:
            world.ingest(export, 3)
        self.assertEqual(len(done), 1)
        self.assertEqual([path for path in world.files() if "/review/" in f"/{path}"], [])


class QueueingTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)

    def run_queued(self, at=NOW):
        self.world.opened.expire_all()
        return part_ingest.run_queued(self.world.opened, self.world.settings, at, self.world.tools)

    def test_run_queued_ingests_queued_parts_in_order(self):
        export = self.world.create(self.world.scenario)
        self.assertEqual(self.run_queued(), [(export.id, 1), (export.id, 2), (export.id, 3)])
        self.assertEqual(self.run_queued(), [])
        self.assertEqual([self.world.part(export, n).ingest_state for n in (1, 2, 3)], ["ingested"] * 3)

    def test_a_part_of_a_later_export_waits_for_every_earlier_export_of_the_account(self):
        first = self.world.create(self.world.scenario, google_id="google-123", created=NOW - 2 * DAY, queue=False)
        second = self.world.create(lambda builder: builder.photo("IMG_9001.JPG", part=0), parts=1, google_id="google-456", created=NOW - DAY)
        for status in ("downloading", "downloaded", "unpacking", "importing", "announced"):
            with self.subTest(status=status):
                self.world.opened.get(Export, first.id).status = status
                self.world.opened.commit()
                self.assertEqual(self.run_queued(), [])
                self.assertEqual(self.world.part(second, 1).ingest_state, "queued")
        for status in ("imported", "failed", "expired"):
            other = PartWorld(self)
            one = other.create(other.scenario, google_id="google-123", created=NOW - 2 * DAY, queue=False, status=status)
            two = other.create(lambda builder: builder.photo("IMG_9001.JPG", part=0), parts=1, google_id="google-456", created=NOW - DAY)
            with self.subTest(status=status):
                self.assertEqual(part_ingest.run_queued(other.opened, other.settings, NOW, other.tools), [(two.id, 1)])
                self.assertEqual(one.status, status)

    def test_an_earlier_export_of_another_account_does_not_hold_a_part_back(self):
        export = self.world.create(lambda builder: builder.photo("IMG_9001.JPG", part=0), parts=1)
        self.assertEqual(self.run_queued(), [(export.id, 1)])

    def test_a_busy_account_leaves_its_parts_queued(self):
        export = self.world.create(self.world.scenario)
        with run.account_lock(self.world.settings, export.account_id):
            self.assertEqual(self.run_queued(), [])
        self.assertEqual(self.world.part(export, 1).ingest_state, "queued")

    def test_a_part_that_raises_is_failed_logged_and_alerted_once(self):
        export = self.world.create(self.world.scenario)
        with mock.patch.object(part_ingest, "ingest_part", side_effect=RuntimeError("IMG_secret_name")):
            with no_leaks(self, "IMG_secret_name"):
                self.assertEqual(self.run_queued(), [])
        failed = self.world.opened.execute(select(Event).where(Event.type == "export_part_ingest_failed")).scalars().all()
        self.assertEqual({(row.subject, row.detail) for row in failed}, {(f"export {export.id}", f"part {n}: RuntimeError") for n in (1, 2, 3)})
        self.assertEqual(len(self.world.opened.execute(select(Alert).where(Alert.kind == "failure")).scalars().all()), 3)
        self.assertEqual([self.world.part(export, n).ingest_state for n in (1, 2, 3)], ["failed"] * 3)

    def test_the_import_tick_runs_queued_parts(self):
        from darkroom.server import jobs

        export = self.world.create(self.world.scenario)
        scheduler = jobs.build(self.world.settings, self.world.engine, NOW)
        scheduler.run_one("imports", NOW)
        self.assertEqual(self.world.part(export, 1).ingest_state, "ingested")


class RescanTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)
        self.export = self.world.create(self.world.scenario)
        self.tools = self.world.immich_tools()

    def test_each_part_that_changed_something_rescans_those_libraries(self):
        self.world.ingest(self.export, 1, tools=self.tools)
        self.assertEqual(sorted(self.world.scanned()), ["lib-jo"])
        self.world.ingest(self.export, 2, tools=self.tools)
        self.assertEqual(sorted(self.world.scanned()), ["lib-jo", "lib-sam"])
        self.world.ingest(self.export, 3, tools=self.tools)
        self.assertEqual(sorted(self.world.scanned()), ["lib-jo", "lib-jo", "lib-sam", "lib-sam"])

    def test_a_part_that_changed_nothing_rescans_nothing(self):
        other = PartWorld(self)
        export = other.create(lambda builder: builder.photo("IMG_9002.JPG", form=None, part=0), parts=1)
        other.ingest(export, 1, tools=other.immich_tools())
        self.assertEqual(other.scanned(), [])


class PrivacyTests(unittest.TestCase):
    def test_events_logs_and_alerts_carry_counts_and_ids_only(self):
        world = PartWorld(self)
        names = ("beach-canary-name-5521.JPG", "late-canary-name-5522.JPG", "tier1-canary-name-5523.JPG")
        text = "sidecar-canary-words-5524"

        def build(builder):
            builder.photo(names[0], part=0, gps=(45.4, 12.3), description=text)
            builder.photo(names[1], part=0, sidecar_part=1, description=text)
            exact = builder.image_bytes(".JPG")
            world.original("jo", names[2], exact)
            builder.photo(names[2], data=exact, part=1, description=text)

        export = world.create(build, parts=2)
        with no_leaks(self, *names, text):
            world.ingest_all(export, (1, 2))
        for row in world.opened.execute(select(Event)).scalars():
            self.assertFalse(any(canary in f"{row.type} {row.subject} {row.detail}" for canary in (*names, text)), row.type)
        for row in world.opened.execute(select(Alert)).scalars():
            self.assertFalse(any(canary in f"{row.subject} {row.message}" for canary in (*names, text)))
        done = world.opened.execute(select(Event).where(Event.type == "export_part_ingested")).scalars().all()
        self.assertEqual([row.detail.startswith("part ") for row in done], [True, True])


if __name__ == "__main__":
    unittest.main()
