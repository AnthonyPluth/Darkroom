import dataclasses
import unittest

from sqlalchemy import select

from darkroom.domain import dedupe, exports, media, pixel_folder, review
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, Export, ExportPart, GoogleAccount, MediaItem, PixelCopy, PixelPerson
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

    def export(self, place=OLD_PLACE, description=CANAL, new=(), at=NOW, owner=None, form="json"):
        self.count += 1
        google_id = f"google-{self.count}"
        inbox = self.world.photos.inbox("jo") / google_id
        builder = TakeoutBuilder(inbox, seed=40 + self.count, parts=1)
        builder.photo("IMG_1.JPG", data=self.phone, gps=None)
        builder.photo("IMG_2.JPG", data=self.canal, gps=place, description=description, form=form)
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

    def summaries(self):
        return [message for title, message in self.world.recorder.sent if title == "Takeout import finished"]

    def test_repeated_phone_matches_are_counted_and_named_in_the_summary(self):
        first = self.imported()
        self.assertEqual(first.repeated, 0)
        self.assertNotIn("already imported earlier", self.summaries()[-1])
        repeats = len(self.phone_matches())
        self.assertEqual(repeats, len(self.people))
        again = self.imported(at=NOW + DAY)
        self.assertEqual((again.repeated, again.skipped_t1, again.skipped_t2), (repeats, 1, 0))
        self.assertIn(f"1 skipped as duplicates, {repeats} already imported earlier,", self.summaries()[-1])
        done = self.world.opened.execute(select(Event).where(Event.type == "import_done").order_by(Event.id.desc())).scalars().first()
        self.assertIn(f"{repeats} already imported earlier", done.detail)

    def test_a_reversed_earlier_match_is_decided_again_and_not_counted_as_a_repeat(self):
        self.imported()
        for confirming in [d for d in self.decisions() if d.matched_item_id is not None and d.tier == 1]:
            dedupe.reverse(self.world.opened, confirming.id, NOW + 1, "owner")
        self.world.opened.commit()
        again = self.imported(at=NOW + DAY)
        self.assertEqual(again.repeated, 0)
        self.assertNotIn("already imported earlier", self.summaries()[-1])

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

    def test_a_refresh_that_cannot_read_the_photo_keeps_the_stored_record_and_logs_nothing(self):
        self.imported()
        real = self.world.tools.read
        library = self.world.root / "jo/google-only"

        def failing(paths):
            found = real(paths)
            return {path: {"Error": "unreadable"} if library in path.parents else tags for path, tags in found.items()}

        self.world.tools = dataclasses.replace(self.world.tools, read=failing)
        before = self.canal_sidecar().read_bytes()
        again = self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        self.assertEqual(again.failed, 0)
        self.assertEqual(self.canal_sidecar().read_bytes(), before)
        self.assertEqual(self.refreshed(), [])
        stored = self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%IMG_2.JPG"))).scalar_one()
        self.assertIn(CANAL, stored.sidecar_json)

    def canal_item(self):
        return self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%IMG_2.JPG"))).scalar_one()

    def test_a_link_at_the_sidecar_is_skipped_and_never_written_through(self):
        self.imported()
        outside = self.world.base / "elsewhere-sidecar"
        outside.mkdir()
        sidecar = self.canal_sidecar()
        sidecar.rename(outside / "IMG_2.JPG.xmp")
        sidecar.symlink_to(outside / "IMG_2.JPG.xmp")
        self.assert_refresh_refused(outside)
        self.assertTrue(sidecar.is_symlink())

    def test_a_folder_at_the_sidecar_is_skipped_and_left_alone(self):
        self.imported()
        sidecar = self.canal_sidecar()
        sidecar.unlink()
        sidecar.mkdir()
        self.assert_refresh_refused(sidecar)
        self.assertTrue(sidecar.is_dir())

    def test_a_refresh_with_nothing_to_write_advances_the_stored_google_record_and_leaves_the_sidecar(self):
        self.imported()
        sidecar = self.canal_sidecar()
        sidecar.write_text(sidecar.read_text().replace(CANAL, "Our holiday"))
        edited = sidecar.read_bytes()
        again = self.imported(place=OLD_PLACE, description=DAWN, at=NOW + DAY)
        self.assertEqual(again.failed, 0)
        self.assertEqual(sidecar.read_bytes(), edited)
        self.assertEqual(self.refreshed(), [])
        self.assertIn(DAWN, self.canal_item().sidecar_json)
        self.assertNotIn(CANAL, self.canal_item().sidecar_json)

    def test_a_value_google_removed_and_later_set_again_still_reaches_the_sidecar(self):
        self.imported()
        self.imported(place=None, description="", at=NOW + DAY)
        self.assertIn(CANAL, self.canal_item().sidecar_json)
        self.assertIn(CANAL, self.canal_sidecar().read_text())
        self.assertEqual(self.refreshed(), [])
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 2 * DAY)
        text = self.canal_sidecar().read_text()
        self.assertIn(DAWN, text)
        self.assertNotIn(CANAL, text)
        self.assertEqual((self.canal_item().lat, self.canal_item().lon), NEW_PLACE)
        self.assertEqual(len(self.refreshed()), 1)

    def test_a_description_google_removed_while_moving_the_photo_is_refreshed_when_google_sets_it_again(self):
        self.imported()
        self.imported(place=NEW_PLACE, description="", at=NOW + DAY)
        self.assertEqual((self.canal_item().lat, self.canal_item().lon), NEW_PLACE)
        self.assertIn(CANAL, self.canal_item().sidecar_json)
        self.assertIn(CANAL, self.canal_sidecar().read_text())
        self.assertEqual(len(self.refreshed()), 1)
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 2 * DAY)
        text = self.canal_sidecar().read_text()
        self.assertIn(DAWN, text)
        self.assertNotIn(CANAL, text)
        self.assertEqual(len(self.refreshed()), 2)

    def test_a_location_google_removed_while_changing_the_description_is_refreshed_when_google_adds_a_new_one(self):
        self.imported()
        self.imported(place=None, description=DAWN, at=NOW + DAY)
        self.assertIn(DAWN, self.canal_sidecar().read_text())
        self.assertEqual(len(self.refreshed()), 1)
        self.assertEqual((self.canal_item().lat, self.canal_item().lon), OLD_PLACE)
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 2 * DAY)
        self.assertEqual(len(self.refreshed()), 2)
        stored = self.canal_item()
        self.assertEqual((stored.lat, stored.lon, stored.gps_source), (*NEW_PLACE, "google"))
        lat, lon, _ = media.location(self.world.tools.read([self.canal_sidecar()])[self.canal_sidecar()])
        self.assertAlmostEqual(lat, NEW_PLACE[0], places=5)
        self.assertAlmostEqual(lon, NEW_PLACE[1], places=5)
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 3 * DAY)
        self.assertEqual(len(self.refreshed()), 2)

    def test_an_owner_edited_description_beside_a_refreshed_location_keeps_the_edit_and_advances_both_fields(self):
        self.imported()
        sidecar = self.canal_sidecar()
        sidecar.write_text(sidecar.read_text().replace(CANAL, "Our holiday"))
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        self.assertIn("Our holiday", sidecar.read_text())
        self.assertNotIn(DAWN, sidecar.read_text())
        stored = self.canal_item()
        self.assertIn(DAWN, stored.sidecar_json)
        self.assertEqual((stored.lat, stored.lon), NEW_PLACE)
        self.assertEqual(len(self.refreshed()), 1)

    def test_an_export_without_the_photos_json_keeps_the_stored_record_for_the_next_re_add(self):
        self.imported()
        stored = self.canal_item().sidecar_json
        again = self.imported(form=None, at=NOW + DAY)
        self.assertEqual(again.failed, 0)
        self.assertEqual(self.canal_item().sidecar_json, stored)
        self.assertIn(CANAL, self.canal_sidecar().read_text())
        self.imported(place=NEW_PLACE, description=DAWN, at=NOW + 2 * DAY)
        text = self.canal_sidecar().read_text()
        self.assertIn(DAWN, text)
        self.assertNotIn(CANAL, text)
        self.assertEqual((self.canal_item().lat, self.canal_item().lon), NEW_PLACE)
        self.assertEqual(len(self.refreshed()), 1)

    def test_a_sidecar_write_that_fails_after_the_check_is_a_failed_item_not_a_skip(self):
        self.imported()
        tools = self.world.tools

        def vanishing(path, fields):
            raise paths.NotASidecar("no sidecar was written")

        self.world.tools = dataclasses.replace(tools, write=vanishing)
        before = self.canal_sidecar().read_bytes()
        again = self.imported(place=NEW_PLACE, description=DAWN, at=NOW + DAY)
        self.assertEqual(again.failed, 1)
        self.assertEqual(self.canal_sidecar().read_bytes(), before)
        self.assertEqual(self.refreshed(), [])
        self.assertIn(CANAL, self.canal_item().sidecar_json)

    def test_a_refresh_killed_after_the_sidecar_was_written_is_finished_by_the_retry(self):
        self.imported()
        tools = self.world.tools
        sidecar = self.canal_sidecar()

        def killed_after_writing(path, fields):
            written = tools.write(path, fields)
            if path == sidecar:
                raise KeyboardInterrupt
            return written

        self.world.tools = dataclasses.replace(tools, write=killed_after_writing)
        export_id = self.export(place=NEW_PLACE, description=DAWN, at=NOW + DAY).id
        with self.assertRaises(KeyboardInterrupt):
            self.world.run_import(export_id, NOW + DAY)
        self.world.reopen()
        self.world.tools = tools
        self.assertIn(DAWN, sidecar.read_text())
        self.assertIn(CANAL, self.canal_item().sidecar_json)
        scans = len(self.world.scanned())
        written = sidecar.read_bytes()
        done = self.world.run_import(export_id, NOW + DAY)
        self.assertEqual((done.status, done.failed), ("done", 0))
        self.assertEqual(sidecar.read_bytes(), written)
        self.assertIn(DAWN, self.canal_item().sidecar_json)
        self.assertEqual((self.canal_item().lat, self.canal_item().lon), NEW_PLACE)
        self.assertEqual(len(self.refreshed()), 1)
        self.assertIn("lib-jo", self.world.scanned()[scans:])

    def test_a_sidecar_the_owner_set_to_another_value_is_still_not_refreshed_on_a_retry(self):
        self.imported()
        sidecar = self.canal_sidecar()
        sidecar.write_text(sidecar.read_text().replace(CANAL, "Our holiday"))
        self.imported(place=OLD_PLACE, description=DAWN, at=NOW + DAY)
        self.imported(place=OLD_PLACE, description="Dusk on the canal", at=NOW + 2 * DAY)
        self.assertIn("Our holiday", sidecar.read_text())
        self.assertEqual(self.refreshed(), [])

    def missing_gps_ids(self):
        rows, _ = review.missing_gps(self.world.opened, self.world.ids["jo"], None, None, 0)
        return [row["id"] for row in rows]

    def test_a_location_google_adds_later_is_stored_on_the_item_and_leaves_missing_gps(self):
        self.imported(place=None)
        item = self.canal_item()
        self.assertEqual((item.lat, item.lon, item.gps_source), (None, None, "none"))
        self.assertIn(item.id, self.missing_gps_ids())
        self.imported(place=NEW_PLACE, at=NOW + DAY)
        item = self.canal_item()
        self.assertEqual((item.lat, item.lon, item.gps_source), (*NEW_PLACE, "google"))
        self.assertNotIn(item.id, self.missing_gps_ids())
        self.assertEqual(len(self.refreshed()), 1)

    def test_a_location_the_owner_already_set_keeps_the_item_missing_gps_when_google_adds_one(self):
        self.imported(place=None)
        sidecar = self.canal_sidecar()
        text = sidecar.read_text()
        self.world.tools.write(sidecar, {"XMP-exif:GPSLatitude": "10.0000000", "XMP-exif:GPSLongitude": "20.0000000"})
        self.assertNotEqual(sidecar.read_text(), text)
        self.imported(place=NEW_PLACE, at=NOW + DAY)
        item = self.canal_item()
        self.assertEqual((item.lat, item.lon, item.gps_source), (None, None, "none"))
        self.assertIn(item.id, self.missing_gps_ids())
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


class PixelFolderThroughImportsTests(IncrementalCase):
    people = ("jo",)
    pixel_people = ("jo",)

    def jo_phone_item(self):
        return self.world.opened.execute(select(MediaItem).where(MediaItem.source == "phone", MediaItem.person_id == self.world.ids["jo"])).scalar_one()

    def copies(self):
        return list(self.world.opened.execute(select(PixelCopy).order_by(PixelCopy.id)).scalars())

    def tend(self, at):
        tended = pixel_folder.tend(self.world.opened, self.world.photos, pixel_folder.DEFAULT_CAP_BYTES, at)
        self.world.opened.commit()
        return tended

    def copy_path(self):
        return self.world.root / f"_pixels/{self.world.pixel}/jo/IMG_1.JPG"

    def test_an_immich_photo_already_matched_is_pruned_once_a_later_takeout_contains_it_again(self):
        self.assertEqual(self.imported().failed, 0)
        item = self.jo_phone_item()
        self.assertEqual(pixel_folder.queue(self.world.opened, item, NOW + 10, always_send=True), 1)
        self.assertEqual(self.tend(NOW + 20).placed, 1)
        self.assertTrue(self.copy_path().is_file())
        self.assertEqual(self.tend(NOW + 30).pruned, 0)
        again = self.imported(at=NOW + 2 * DAY)
        self.assertEqual((again.failed, again.repeated), (0, 1))
        self.assertEqual(len(self.phone_matches()), 1)
        self.assertEqual(self.tend(NOW + 2 * DAY + 10).pruned, 1)
        self.assertFalse(self.copy_path().exists())
        self.assertEqual([c.state for c in self.copies()], ["pruned"])
        self.assertEqual((self.world.root / item.path).read_bytes(), self.phone)

    def test_a_takeout_created_before_the_copy_was_placed_does_not_prune_an_immich_photo(self):
        self.imported()
        item = self.jo_phone_item()
        pixel_folder.queue(self.world.opened, item, NOW + 10, always_send=True)
        self.tend(NOW + 3 * DAY)
        self.imported(at=NOW + 2 * DAY)
        self.assertEqual(self.tend(NOW + 3 * DAY + 10).pruned, 0)
        self.assertTrue(self.copy_path().is_file())

    def test_an_ordinary_copy_is_pruned_by_a_match_stamped_before_it_was_placed(self):
        item = self.jo_phone_item()
        self.assertEqual(pixel_folder.queue(self.world.opened, item, NOW - 100), 1)
        self.assertEqual(self.tend(NOW + 50).placed, 1)
        self.assertEqual(self.imported(at=NOW).failed, 0)
        self.assertEqual(self.tend(NOW + 60).pruned, 1)
        self.assertFalse(self.copy_path().exists())
        self.assertEqual((self.world.root / item.path).read_bytes(), self.phone)
