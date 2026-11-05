import unittest

from sqlalchemy import select

from darkroom.domain import dedupe, exports
from darkroom.storage.models import DedupeDecision, Event, Export, ExportPart, GoogleAccount, MediaItem, PixelPerson
from tests.import_world import DAY, NOW, World
from tests.takeout_fixtures import TakeoutBuilder

CANAL = "A made-up canal"
DAWN = "The canal at dawn"
OLD_PLACE = (45.4, 12.3)
NEW_PLACE = (45.5, 12.4)


class IncrementalCase(unittest.TestCase):
    people = ("jo", "sam")
    pixel_people = ("jo", "sam")

    def setUp(self):
        self.world = World(self, people=self.people, pixel_people=self.pixel_people)
        self.phone = self.take_bytes(1)
        self.canal = self.take_bytes(2)
        self.world.original("jo", "IMG_1.JPG", self.phone)
        if "sam" in self.people:
            self.sam = self.take_bytes(3)
            self.world.original("sam", "IMG_3.JPG", self.sam)
        self.world.index_all()
        self.count = 0

    def take_bytes(self, seed):
        return TakeoutBuilder(self.world.base / f"scratch-{seed}", seed=seed).image_bytes(".JPG")

    def export(self, place=OLD_PLACE, description=CANAL, new=(), at=NOW, owner=None):
        self.count += 1
        google_id = f"google-{self.count}"
        inbox = self.world.photos.inbox("jo") / google_id
        builder = TakeoutBuilder(inbox, seed=40 + self.count, parts=1)
        builder.photo("IMG_1.JPG", data=self.phone, gps=None)
        builder.photo("IMG_2.JPG", data=self.canal, gps=place, description=description)
        if "sam" in self.people:
            builder.photo("IMG_3.JPG", data=self.sam, gps=None)
        for name in new:
            builder.photo(name, gps=None)
        parts = builder.build().parts
        found = Export(account_id=self.world.account, kind="incremental", google_id=google_id, created_at=at - DAY, ready_at=at,
                       expires_at=at + 7 * DAY, part_count=len(parts), status="downloaded", inbox_dir=str(inbox),
                       owner_person_id=None if owner is None else self.world.ids[owner])
        self.world.opened.add(found)
        self.world.opened.flush()
        self.world.opened.add_all(ExportPart(export_id=found.id, number=number, filename=part.name, bytes=part.stat().st_size, status="done")
                                  for number, part in enumerate(parts, start=1))
        self.world.opened.commit()
        return found

    def imported(self, **options):
        return self.world.run_import(self.export(**options), options.get("at", NOW))

    def decisions(self):
        return list(self.world.opened.execute(select(DedupeDecision).order_by(DedupeDecision.id)).scalars())

    def phone_matches(self):
        phone = self.world.opened.execute(select(MediaItem.id).where(MediaItem.source == "phone")).scalars().all()
        return [(d.export_id, d.source_path) for d in self.decisions() if d.matched_item_id in phone and d.tier in (1, 2)]

    def refreshed(self):
        return list(self.world.opened.execute(select(Event).where(Event.type == "metadata_refreshed")).scalars())

    def canal_sidecar(self):
        return (self.world.root / "jo/google-only/2017/07/IMG_2.JPG.xmp")

    def google_only(self):
        return sorted(str(p.relative_to(self.world.root)) for p in (self.world.root / "jo/google-only").rglob("*") if p.is_file())


class IncrementalTests(IncrementalCase):
    def test_a_repeated_phone_match_records_no_second_decision(self):
        first = self.imported()
        self.assertEqual(first.failed, 0)
        before = self.phone_matches()
        again = self.imported(at=NOW + DAY)
        self.assertEqual(again.failed, 0)
        after = self.phone_matches()
        self.assertEqual(after, before)
        self.assertEqual({export_id for export_id, _ in after}, {1})

    def test_the_second_export_adds_only_the_new_item_and_keeps_the_files(self):
        self.imported()
        placed = self.google_only()
        again = self.imported(new=("IMG_4.JPG",), at=NOW + DAY)
        self.assertEqual((again.status, again.added, again.failed, again.skipped_t1), ("done", 1, 0, 1))
        self.assertEqual([p for p in self.google_only() if "IMG_4" not in p], placed)
        self.assertEqual(sum("IMG_4" in p for p in self.google_only()), 2)
        items = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars().all()
        self.assertEqual(len(items), 2)

    def test_a_changed_description_and_location_refresh_the_sidecar_once(self):
        self.imported()
        self.assertIn(CANAL, self.canal_sidecar().read_text())
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        text = self.canal_sidecar().read_text()
        self.assertIn(DAWN, text)
        self.assertNotIn(CANAL, text)
        self.assertEqual(len(self.refreshed()), 1)
        self.assertEqual(self.world.opened.execute(select(MediaItem.id).where(MediaItem.path.like("%IMG_2.JPG"))).scalar_one(), int(self.refreshed()[0].subject.split()[1]))
        self.assertIn("lib-jo", self.world.scanned()[-1:])
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 2 * DAY)
        self.assertEqual(len(self.refreshed()), 1)

    def test_an_unchanged_item_leaves_its_sidecar_alone(self):
        self.imported()
        before = self.canal_sidecar().read_bytes()
        again = self.imported(at=NOW + DAY)
        self.assertEqual((again.added, again.failed, len(self.refreshed())), (0, 0, 0))
        self.assertEqual(self.canal_sidecar().read_bytes(), before)

    def test_a_description_the_owner_edited_since_is_not_overwritten(self):
        self.imported()
        sidecar = self.canal_sidecar()
        sidecar.write_text(sidecar.read_text().replace(CANAL, "Our holiday"))
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        text = sidecar.read_text()
        self.assertIn("Our holiday", text)
        self.assertNotIn(DAWN, text)

    def snapshot(self, folder):
        return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None) for p in sorted(folder.rglob("*"))}

    def assert_refresh_refused(self, outside):
        before = self.snapshot(outside)
        again = self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        self.assertEqual(again.failed, 0)
        self.assertEqual(self.snapshot(outside), before)
        self.assertEqual(self.refreshed(), [])
        stored = self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%IMG_2.JPG"))).scalar_one()
        self.assertIn(CANAL, stored.sidecar_json)

    def test_a_linked_folder_on_the_way_is_not_written_through(self):
        self.imported()
        outside = self.world.base / "elsewhere"
        outside.parent.mkdir(parents=True, exist_ok=True)
        (self.world.root / "jo/google-only/2017").rename(outside)
        (self.world.root / "jo/google-only/2017").symlink_to(outside)
        self.assert_refresh_refused(outside)

    def test_a_linked_month_folder_is_not_written_through(self):
        self.imported()
        outside = self.world.base / "elsewhere-month"
        (self.world.root / "jo/google-only/2017/07").rename(outside)
        (self.world.root / "jo/google-only/2017/07").symlink_to(outside)
        self.assert_refresh_refused(outside)

    def test_a_linked_file_is_not_written_through(self):
        self.imported()
        outside = self.world.base / "elsewhere-file"
        outside.mkdir()
        photo = self.world.root / "jo/google-only/2017/07/IMG_2.JPG"
        photo.rename(outside / "IMG_2.JPG")
        photo.symlink_to(outside / "IMG_2.JPG")
        self.assert_refresh_refused(outside)

    def test_a_stored_path_outside_google_only_is_not_written_beside(self):
        self.imported()
        stored = self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%IMG_2.JPG"))).scalar_one()
        originals = self.world.root / "jo/originals"
        self.world.original("jo", "IMG_9.JPG", self.canal)
        stored.path = "jo/originals/IMG_9.JPG"
        self.world.opened.commit()
        before = self.snapshot(originals)
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        self.assertEqual(self.snapshot(originals), before)
        self.assertEqual(self.refreshed(), [])
        stored.path = "../escape/IMG_2.JPG"
        self.world.opened.commit()
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 2 * DAY)
        self.assertFalse((self.world.root.parent / "escape").exists())
        self.assertEqual(self.refreshed(), [])

    def test_a_reversed_decision_is_made_again_by_the_next_export(self):
        self.imported()
        confirming = next(d for d in self.decisions() if d.matched_item_id is not None and d.tier == 1)
        dedupe.reverse(self.world.opened, confirming.id, NOW + 1, "owner")
        self.world.opened.commit()
        self.imported(at=NOW + DAY)
        again = [d for d in self.decisions() if d.matched_item_id == confirming.matched_item_id and d.reversed_at is None]
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].export_id, 2)


class OwnerNoLongerServedTests(IncrementalCase):
    def test_a_repeated_match_for_an_owner_the_account_no_longer_serves_is_still_a_repeat(self):
        first = self.world.run_import(self.export(owner="sam"), NOW)
        self.assertEqual(first.failed, 0)
        self.world.opened.query(PixelPerson).filter(PixelPerson.person_id == self.world.ids["sam"]).delete()
        self.world.opened.commit()
        before = self.phone_matches()
        again = self.world.run_import(self.export(owner="sam", at=NOW + DAY), NOW + DAY)
        self.assertEqual((again.status, again.failed), ("done", 0))
        self.assertEqual(self.phone_matches(), before)


class SinglePersonTests(IncrementalTests):
    people = ("jo",)
    pixel_people = ("jo",)


class FirstExportAfterRenewalTests(unittest.TestCase):
    def test_it_is_a_full_initial_export_and_the_next_is_incremental(self):
        world = World(self, people=("jo",), pixel_people=("jo",))
        account = world.opened.get(GoogleAccount, world.account)
        year = 365 * DAY
        account.schedule_created = NOW
        world.opened.add(Export(account_id=account.id, kind="initial", google_id="a", created_at=NOW + DAY, status="imported"))
        world.opened.add(Export(account_id=account.id, kind="incremental", google_id="b", created_at=NOW + 30 * DAY, status="imported"))
        world.opened.flush()
        self.assertEqual(exports.kind_for(world.opened, account, NOW + 60 * DAY), "incremental")
        account.schedule_created = NOW + year - 21 * DAY
        self.assertEqual(exports.kind_for(world.opened, account, NOW + year), "initial")
        world.opened.add(Export(account_id=account.id, kind="initial", google_id="c", created_at=NOW + year, status="imported"))
        world.opened.flush()
        self.assertEqual(exports.kind_for(world.opened, account, NOW + year + 30 * DAY), "incremental")


if __name__ == "__main__":
    unittest.main()
