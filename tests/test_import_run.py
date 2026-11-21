import dataclasses
import json
import shutil
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from darkroom.domain import alerts, dedupe, media
from darkroom.domain.ingest import cleanup, finish_export, metadata, pairing, part_ingest, place, run, sidecar, unpack
from darkroom.storage import locks, paths
from darkroom.storage.models import Alert, DedupeDecision, Event, Export, ExportPart, ImportRun, MediaItem, Person, ReviewItem
from tests.import_world import DAY, NOW, World, sha1_of, tree
from tests.privacy import no_leaks
from tests.takeout_fixtures import TakeoutBuilder

SUMMARY = "Takeout import finished"
FAILURE = "Darkroom needs a look"


def kill_on_call(target, number, after=False):
    calls = {"n": 0}

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == number and not after:
            raise KeyboardInterrupt
        result = target(*args, **kwargs)
        if calls["n"] == number:
            raise KeyboardInterrupt
        return result

    return wrapped


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.hand_dropped()

    def test_counts_per_tier_and_the_summary(self):
        done = self.world.run_import(self.export)
        self.assertEqual((done.status, done.stage), ("done", "done"))
        self.assertEqual((done.added, done.skipped_t1, done.skipped_t2, done.skipped_t3, done.review, done.missing_gps, done.failed), (2, 3, 1, 1, 1, 1, 0))
        export = self.world.opened.get(Export, self.export.id)
        self.assertEqual((export.status, export.stage), ("imported", "done"))
        self.assertEqual(self.world.recorder.sent.count((SUMMARY, "Export 1: 2 added, 5 skipped as duplicates, 1 in review, 1 missing GPS")), 1)

    def test_google_only_files_land_in_the_default_persons_folder_with_sidecars(self):
        self.world.run_import(self.export)
        google_only = {key for key in tree(self.world.root) if "/google-only/" in f"/{key}"}
        self.assertEqual(google_only, {"jo/google-only/2017/07/IMG_2001.JPG", "jo/google-only/2017/07/IMG_2001.JPG.xmp",
                                       "jo/google-only/2017/07/IMG_2002.JPG", "jo/google-only/2017/07/IMG_2002.JPG.xmp"})
        self.assertEqual(sorted(p.name for p in (self.world.root / "sam/google-only").rglob("*")), [])

    def test_sidecars_are_written_next_to_matched_originals_and_originals_keep_their_bytes(self):
        before = {key: value for key, value in tree(self.world.root).items() if "/originals/" in f"/{key}"}
        self.world.run_import(self.export)
        after = {key: value for key, value in tree(self.world.root).items() if "/originals/" in f"/{key}"}
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(set(after) - set(before), {"sam/originals/2019/IMG_1002.JPG.xmp", "sam/originals/IMG_1004.JPG.xmp"})

    def test_immich_scans_each_changed_library_once(self):
        self.world.run_import(self.export)
        self.assertEqual(sorted(self.world.scanned()), ["lib-jo", "lib-sam"])
        self.assertEqual(json.loads(self.world.opened.execute(select(ImportRun)).scalar_one().changed_people), [self.world.ids["jo"], self.world.ids["sam"]])

    def test_only_tier_one_and_two_phone_matches_are_confirmed(self):
        self.world.run_import(self.export)
        confirmed = set(self.world.opened.execute(select(MediaItem.path).where(MediaItem.confirmed_in_google.is_(True))).scalars())
        self.assertEqual(confirmed, {"jo/originals/IMG_1001.JPG", "sam/originals/2019/IMG_1002.JPG", "jo/originals/IMG_1003.JPG",
                                     "sam/originals/IMG_1004.JPG"})

    def test_the_review_item_is_held_and_pending(self):
        self.world.run_import(self.export)
        review = self.world.opened.execute(select(ReviewItem)).scalar_one()
        self.assertEqual((review.status, review.hold_path), ("pending", "_inbox/jo/review/export-1/IMG_1005.JPG"))

    def test_importing_again_is_refused_and_changes_nothing(self):
        self.world.run_import(self.export)
        before = self.world.state()
        with self.assertRaises(run.AlreadyImported):
            self.world.run_import(self.export)
        self.assertEqual(self.world.state(), before)

    def test_events_logs_and_the_database_carry_no_file_names_or_the_immich_key(self):
        with mock.patch.object(metadata, "candidate", side_effect=ValueError("IMG_2002.JPG-is-private")):
            with no_leaks(self, "IMG_2002.JPG-is-private", "fake-immich-admin-key-canary", database=self.world.engine):
                self.world.run_import(self.export)
        details = [e.detail or "" for e in self.world.opened.execute(select(Event)).scalars()]
        self.assertFalse(any("IMG_" in detail for detail in details))


class LeftOutTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def import_with(self, *adding):
        self.world.extra_items = lambda builder: [add(builder) for add in adding]
        return self.world.run_import(self.world.hand_dropped())

    def import_done(self):
        return self.world.opened.execute(select(Event).where(Event.type == "import_done")).scalar_one().detail

    def test_raw_avif_mts_and_a_pixel_motion_clip_are_placed_with_sidecars_and_nothing_is_left_out(self):
        done = self.import_with(lambda b: b.photo("shot.CR2", gps=(45.4, 12.3)), lambda b: b.photo("pic.avif", gps=(45.4, 12.3)),
                                lambda b: b.video("cam.MTS"), lambda b: b.video("PXL_20200101_101010101.MP"))
        self.assertEqual((done.status, done.failed, json.loads(done.not_imported), done.orphan_sidecars, done.unpaired_media), ("done", 0, {}, 0, 0))
        self.assertEqual(done.added, 6)
        placed = {key.rsplit("/", 1)[-1] for key in tree(self.world.root) if "/google-only/" in f"/{key}"}
        self.assertLessEqual({"shot.CR2", "shot.CR2.xmp", "pic.avif", "pic.avif.xmp", "cam.MTS", "PXL_20200101_101010101.MP"}, placed)
        self.assertNotIn("not imported", self.import_done())

    def test_a_text_file_and_a_file_without_an_extension_are_counted_and_named_nowhere(self):
        canaries = ("notes-canary-jo.txt", "IMG_NOEXT_canary_jo")
        with no_leaks(self, *canaries, database=self.world.engine):
            done = self.import_with(lambda b: b.loose_file(canaries[0]), lambda b: b.loose_file(canaries[1]),
                                    lambda b: b.loose_file(f"{canaries[1]}.json", b"{}"), lambda b: b.photo("IMG_7001.JPG", form=None))
        self.assertEqual((done.status, json.loads(done.not_imported), done.orphan_sidecars, done.unpaired_media), ("done", {".txt": 1, "none": 1}, 1, 1))
        detail = self.import_done()
        self.assertTrue(detail.endswith(", 2 not imported (1 .txt, 1 with no extension), 1 sidecar matched no file, 1 without a sidecar"), detail)
        self.assertFalse(any(canary in detail for canary in canaries))
        self.assertEqual(self.world.recorder.sent.count((SUMMARY, "Export 1: 3 added, 5 skipped as duplicates, 1 in review, 2 missing GPS")), 1)


class SpecialFolderTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def folder_photo(self, name, *folders):
        def add(builder):
            data = builder.image_bytes(".JPG")
            for folder in folders:
                builder.add(name, data, folder, 0, "json", None, (45.4, 12.3), "")
        return add

    def import_with(self, *adding):
        self.world.extra_items = lambda builder: [add(builder) for add in adding]
        return self.world.run_import(self.world.hand_dropped())

    def placed_names(self):
        return {key.rsplit("/", 1)[-1] for key in tree(self.world.root) if "/google-only/" in f"/{key}"}

    def test_photos_only_in_trash_or_bin_are_not_placed_given_no_decision_and_counted_without_names(self):
        canaries = ("only_in_bin_canary_jo.jpg", "only_in_trash_canary_jo.jpg")
        with no_leaks(self, *canaries, database=self.world.engine):
            done = self.import_with(self.folder_photo(canaries[0], "Bin"), self.folder_photo(canaries[1], "Trash"))
        self.assertEqual((done.status, done.failed, done.added, json.loads(done.not_imported)), ("done", 0, 2, {pairing.TRASHED: 2}))
        self.assertEqual(self.placed_names(), {"IMG_2001.JPG", "IMG_2001.JPG.xmp", "IMG_2002.JPG", "IMG_2002.JPG.xmp"})
        self.assertEqual([d for d in self.world.opened.execute(select(DedupeDecision)).scalars() if "canary" in (d.source_path or "")], [])
        self.assertEqual(self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%canary%"))).scalars().all(), [])
        detail = self.world.opened.execute(select(Event).where(Event.type == "import_done")).scalar_one().detail
        self.assertTrue(detail.endswith(", 2 not imported (2 in Trash or Bin)"), detail)
        self.assertFalse(any(canary in detail for canary in canaries))

    def test_a_photo_in_a_year_folder_and_in_trash_is_placed_once(self):
        done = self.import_with(self.folder_photo("kept_in_year.jpg", "Photos from 2019", "Trash"))
        self.assertEqual((done.added, json.loads(done.not_imported)), (3, {}))
        self.assertEqual({"kept_in_year.jpg", "kept_in_year.jpg.xmp"} - self.placed_names(), set())

    def test_a_photo_only_in_archive_is_placed_as_before(self):
        done = self.import_with(self.folder_photo("archived_only.jpg", "Archive"))
        self.assertEqual((done.added, json.loads(done.not_imported)), (3, {}))
        self.assertLessEqual({"archived_only.jpg", "archived_only.jpg.xmp"}, self.placed_names())

    def test_a_photo_in_trash_and_an_album_is_placed(self):
        done = self.import_with(self.folder_photo("album_and_trash.jpg", "Trash", "Family trip"))
        self.assertEqual((done.added, json.loads(done.not_imported)), (3, {}))

    def test_trashed_files_stay_in_staging(self):
        self.import_with(self.folder_photo("left_alone.jpg", "Bin"))
        staged = [key for key in tree(self.world.root) if key.endswith("/Bin/left_alone.jpg")]
        self.assertEqual(len(staged), 1)


class CrashTests(unittest.TestCase):
    def clean_state(self):
        world = World(self)
        world.run_import(world.hand_dropped())
        return world.state()

    def crashed_state(self, patch_target, attribute, replacement):
        world = World(self)
        export_id = world.hand_dropped().id
        with mock.patch.object(patch_target, attribute, replacement), self.assertRaises(KeyboardInterrupt):
            world.run_import(export_id)
        world.reopen()
        stopped = world.opened.get(Export, export_id)
        self.assertNotEqual(stopped.status, "imported")
        done = world.run_import(export_id)
        self.assertEqual(done.status, "done")
        return world.state()

    def test_killing_the_run_before_each_stage_and_restarting_ends_the_same(self):
        clean = self.clean_state()
        for stage in ("stage_unpack", "stage_dedupe", "stage_place", "stage_count", "stage_rescan", "stage_report"):
            with self.subTest(stage=stage):
                crashed = self.crashed_state(run, stage, mock.Mock(side_effect=KeyboardInterrupt))
                self.assertEqual(crashed, clean)

    def test_killing_the_run_inside_dedupe_and_restarting_ends_the_same(self):
        clean = self.clean_state()
        self.assertEqual(self.crashed_state(dedupe, "apply", kill_on_call(dedupe.apply, 3)), clean)
        self.assertEqual(self.crashed_state(dedupe, "apply", kill_on_call(dedupe.apply, 4, after=True)), clean)

    def test_killing_the_run_inside_place_and_restarting_ends_the_same(self):
        clean = self.clean_state()
        for number in (2, 6):
            with self.subTest(call=number):
                self.assertEqual(self.crashed_state(place, "place", kill_on_call(place.place, number, after=True)), clean)
                self.assertEqual(self.crashed_state(place, "place", kill_on_call(place.place, number)), clean)

    def killed_after_sending(self, title):
        world = World(self)
        export_id = world.hand_dropped().id

        def notifier(settings, sent_title, message, link=None):
            outcome = world.recorder(settings, sent_title, message, link)
            if sent_title == title:
                raise KeyboardInterrupt
            return outcome

        world.tools = run.Tools(notifier=notifier)
        with self.assertRaises(KeyboardInterrupt):
            world.run_import(export_id)
        self.assertEqual(world.sent(title), 1)
        world.reopen()
        world.tools = run.Tools(notifier=world.recorder)
        self.assertEqual(world.run_import(export_id).status, "done")
        alerts.tick(world.opened, world.settings, NOW + 60, world.recorder)
        self.assertEqual(world.sent(title), 1)
        return world

    def test_killing_the_run_between_sending_the_summary_and_saving_it_sends_it_once(self):
        world = self.killed_after_sending(SUMMARY)
        self.assertEqual(world.opened.execute(select(Alert).where(Alert.kind == "export_imported")).scalar_one().closed, NOW)

    def test_killing_the_run_between_sending_an_unmatched_alert_and_saving_it_sends_it_once(self):
        world = self.killed_after_sending("Some originals are not in Google Photos")
        alert = world.opened.execute(select(Alert).where(Alert.kind == "originals_unmatched")).scalar_one()
        self.assertEqual((alert.closed, alert.sent_count), (None, 1))
        self.assertEqual(world.sent(SUMMARY), 1)

    def test_a_failed_save_after_the_summary_is_sent_leaves_the_run_to_resume_not_failed(self):
        world = World(self)
        export_id = world.hand_dropped().id
        real = run.mark
        calls = {"reported": 0}

        def failing(opened, export, run_row, stage):
            if stage == "reported" and not calls["reported"]:
                calls["reported"] += 1
                raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
            return real(opened, export, run_row, stage)

        with mock.patch.object(run, "mark", failing):
            done = world.run_import(export_id)
        self.assertEqual((done.status, done.error), ("running", None))
        self.assertEqual(world.opened.get(Export, export_id).status, "importing")
        self.assertEqual((world.sent(SUMMARY), world.sent(FAILURE)), (1, 0))
        self.assertEqual(run.waiting_exports(world.opened), [export_id])
        run.run_waiting(world.opened, world.settings, NOW, world.tools)
        self.assertEqual(world.opened.get(Export, export_id).status, "imported")
        self.assertEqual(world.sent(SUMMARY), 1)

    def test_the_stage_is_recorded_on_the_export_as_the_run_goes(self):
        world = World(self)
        export_id = world.hand_dropped().id
        with mock.patch.object(run, "stage_count", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            world.run_import(export_id)
        world.reopen()
        stopped = world.opened.get(Export, export_id)
        self.assertEqual((stopped.status, stopped.stage), ("importing", "placed"))
        self.assertEqual(world.opened.execute(select(ImportRun)).scalar_one().status, "running")


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def test_an_item_that_fails_its_metadata_becomes_an_event_and_the_run_goes_on(self):
        export = self.world.hand_dropped()
        real = metadata.candidate

        def failing(item, *args, **kwargs):
            if item.path.name == "IMG_2002.JPG":
                raise ValueError("unreadable")
            return real(item, *args, **kwargs)

        with mock.patch.object(metadata, "candidate", failing):
            done = self.world.run_import(export)
        self.assertEqual((done.status, done.added, done.failed), ("done", 1, 1))
        failed = list(self.world.opened.execute(select(Event).where(Event.type == "import_item_failed")).scalars())
        self.assertEqual([(e.subject, e.detail) for e in failed], [("export 1", "metadata: ValueError")])
        self.assertIn("1 could not be imported", self.world.recorder.sent[-1][1])

    def test_an_item_that_fails_to_place_leaves_no_row_and_the_run_goes_on(self):
        export = self.world.hand_dropped()
        real = place.copy_into

        def failing(folder, source, sha1, **options):
            if source.name == "IMG_2001.JPG":
                raise place.PlaceFailed("both names are taken by other files")
            return real(folder, source, sha1, **options)

        with mock.patch.object(place, "copy_into", failing):
            done = self.world.run_import(export)
        self.assertEqual((done.status, done.added, done.failed), ("done", 1, 1))
        decision = self.world.opened.execute(select(DedupeDecision).where(DedupeDecision.source_path.endswith("IMG_2001.JPG"))).scalar_one()
        self.assertIsNone(decision.takeout_item_id)
        self.assertFalse((self.world.root / "jo/google-only/2017/07/IMG_2001.JPG").exists())
        self.assertEqual([e.detail for e in self.world.opened.execute(select(Event).where(Event.type == "import_item_failed")).scalars()],
                         ["place: PlaceFailed"])

    def test_an_item_whose_decision_is_refused_becomes_an_event(self):
        export = self.world.hand_dropped()
        real = dedupe.apply

        def refusing(opened, decision, *args):
            if decision.source_path.endswith("IMG_2002.JPG"):
                raise ValueError("refused")
            return real(opened, decision, *args)

        with mock.patch.object(dedupe, "apply", refusing):
            done = self.world.run_import(export)
        self.assertEqual((done.status, done.failed, done.added), ("done", 1, 1))
        self.assertEqual(len(list(self.world.opened.execute(select(DedupeDecision)).scalars())), 7)

    def test_a_bad_part_fails_the_run_with_an_alert_and_a_retry_after_fixing_it_finishes(self):
        export = self.world.hand_dropped()
        part = sorted(self.world.photos.manual_inbox("jo").glob("*.zip"))[0]
        good = part.read_bytes()
        part.write_bytes(b"not a zip")
        done = self.world.run_import(export)
        self.assertEqual(done.status, "failed")
        self.assertIn("not a valid zip", done.error)
        self.assertNotIn(part.name, done.error)
        self.assertEqual(self.world.opened.get(Export, export.id).status, "failed")
        self.assertEqual(self.world.sent(FAILURE), 1)
        self.assertEqual((self.world.sent(SUMMARY), self.world.scanned()), (0, []))
        self.assertEqual([e.type for e in self.world.opened.execute(select(Event).where(Event.type == "import_failed")).scalars()], ["import_failed"])
        part.write_bytes(good)
        done = self.world.run_import(export)
        self.assertEqual((done.status, done.added), ("done", 2))
        self.assertEqual(len(list(self.world.opened.execute(select(ImportRun)).scalars())), 1)

    def test_an_unexpected_error_stops_at_the_stage_and_a_retry_resumes_there(self):
        export = self.world.hand_dropped()
        with mock.patch.object(run, "stage_count", side_effect=RuntimeError("boom")):
            done = self.world.run_import(export)
        self.assertEqual((done.status, done.error), ("failed", "RuntimeError during the import"))
        self.assertEqual((self.world.opened.get(Export, export.id).status, self.world.opened.get(Export, export.id).stage), ("failed", "placed"))
        with mock.patch.object(run, "prepare", side_effect=AssertionError("placing is not redone")):
            done = self.world.run_import(export)
        self.assertEqual((done.status, done.added), ("done", 2))

    def test_without_immich_configured_the_import_still_finishes(self):
        world = World(self, immich_configured=False)
        done = world.run_import(world.hand_dropped())
        self.assertEqual(done.status, "done")
        self.assertEqual(world.scanned(), [])
        skipped = world.opened.execute(select(Event).where(Event.type == "immich_not_configured")).scalar_one()
        self.assertEqual(skipped.detail, "2 libraries not rescanned")


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.hand_dropped()
        self.export_id = self.export.id

    def one_item_failing(self):
        real = place.copy_into

        def failing(folder, source, sha1, **options):
            if source.name == "IMG_2001.JPG":
                raise place.PlaceFailed("both names are taken by other files")
            return real(folder, source, sha1, **options)

        return mock.patch.object(place, "copy_into", failing)

    def import_with_one_item_failing(self):
        with self.one_item_failing():
            done = self.world.run_import(self.export_id)
        self.assertEqual((done.status, done.added, done.failed), ("done", 1, 1))
        return done

    def originals(self):
        return {key: value for key, value in tree(self.world.root).items() if "/originals/" in key and not key.endswith(".xmp")}

    def staging(self):
        return self.world.root / "_inbox/jo" / f"export-{self.export_id}"

    def clean(self, at):
        return cleanup.clean(self.world.opened, self.world.photos, at)

    def retry(self, at=NOW + DAY):
        run.request_import(self.world.opened, self.export_id, self.world.photos, at)
        self.world.opened.commit()
        return run.run_waiting(self.world.opened, self.world.settings, at, self.world.tools)

    def test_a_run_with_failed_items_holds_the_staging_and_a_retry_places_them_and_releases_it(self):
        self.import_with_one_item_failing()
        before = self.originals()
        self.clean(NOW + 15 * DAY)
        self.assertTrue(self.staging().is_dir())
        self.assertEqual(self.retry(NOW + 15 * DAY), [self.export_id])
        runs = list(self.world.opened.execute(select(ImportRun).order_by(ImportRun.id)).scalars())
        self.assertEqual([(r.status, r.stage, r.failed, r.added) for r in runs], [("done", "done", 1, 1), ("done", "done", 0, 2)])
        self.assertEqual(self.world.opened.get(Export, self.export_id).status, "imported")
        self.assertTrue((self.world.root / "jo/google-only/2017/07/IMG_2001.JPG").is_file())
        self.assertEqual(self.originals(), before)
        self.assertEqual(self.world.sent(SUMMARY), 1)
        self.clean(NOW + 30 * DAY)
        self.assertFalse(self.staging().exists())

    def test_a_retry_that_fails_again_keeps_the_staging(self):
        self.import_with_one_item_failing()
        with mock.patch.object(place, "copy_into", side_effect=place.PlaceFailed("taken")):
            self.assertEqual(self.retry(), [self.export_id])
        last = run.latest_run(self.world.opened, self.export_id)
        self.assertEqual(last.status, "done")
        self.assertGreater(last.failed, 0)
        self.clean(NOW + 40 * DAY)
        self.assertTrue(self.staging().is_dir())
        self.assertEqual(self.world.opened.get(Export, self.export_id).status, "imported")

    def test_an_export_without_failed_items_still_cannot_be_requested_again(self):
        self.world.run_import(self.export_id)
        with self.assertRaises(run.AlreadyImported):
            run.request_import(self.world.opened, self.export_id, self.world.photos, NOW)
        self.assertEqual(self.world.opened.get(Export, self.export_id).status, "imported")
        self.assertEqual(run.waiting_exports(self.world.opened), [])

    def test_a_retry_is_refused_and_changes_nothing_when_the_staging_is_gone(self):
        self.import_with_one_item_failing()
        shutil.rmtree(self.staging())
        for photos in (self.world.photos, None):
            with self.assertRaises(run.NotReady):
                run.request_import(self.world.opened, self.export_id, photos, NOW)
        export = self.world.opened.get(Export, self.export_id)
        self.assertEqual((export.status, export.stage), ("imported", "done"))
        self.assertEqual(self.world.count(ImportRun), 1)
        self.assertEqual(run.waiting_exports(self.world.opened), [])

    def test_a_retry_finds_the_staging_after_the_accounts_default_person_changed(self):
        from darkroom.storage.models import GoogleAccount

        self.import_with_one_item_failing()
        placed = self.world.opened.execute(select(DedupeDecision).where(DedupeDecision.source_path.endswith("IMG_2002.JPG"))).scalar_one()
        kept = (placed.takeout_item_id, placed.person_id)
        self.assertIsNotNone(kept[0])
        in_jo = sorted(p.name for p in (self.world.root / "jo/google-only").rglob("*.JPG"))
        self.assertEqual(in_jo, ["IMG_2002.JPG"])
        self.world.opened.get(GoogleAccount, self.world.account).default_person_id = self.world.ids["sam"]
        self.world.opened.commit()
        self.assertTrue(self.staging().is_dir())
        before = self.originals()
        self.assertEqual(self.retry(), [self.export_id])
        self.world.opened.refresh(placed)
        self.assertEqual((placed.takeout_item_id, placed.person_id), kept)
        self.assertEqual(sorted(p.name for p in (self.world.root / "jo/google-only").rglob("*.JPG")), ["IMG_2001.JPG", "IMG_2002.JPG"])
        self.assertEqual(sorted(p.name for p in (self.world.root / "sam/google-only").rglob("*.JPG")), [])
        last = run.latest_run(self.world.opened, self.export_id)
        self.assertEqual((last.status, last.failed), ("done", 0))
        self.assertEqual(self.originals(), before)
        self.clean(NOW + 30 * DAY)
        self.assertFalse(self.staging().exists())

    def test_requesting_twice_queues_one_retry_and_keeps_its_stage(self):
        self.import_with_one_item_failing()
        for _ in range(2):
            run.request_import(self.world.opened, self.export_id, self.world.photos, NOW + DAY)
            export = self.world.opened.get(Export, self.export_id)
            self.assertEqual((export.status, export.stage), ("importing", "unpacked"))
        self.assertEqual(self.world.count(ImportRun), 2)
        self.assertEqual(run.waiting_exports(self.world.opened), [self.export_id])

    def test_killing_a_retry_and_restarting_ends_the_same_as_an_uninterrupted_retry(self):
        self.import_with_one_item_failing()
        self.retry()
        clean = self.world.state()
        for number in (1, 4):
            with self.subTest(call=number):
                world = World(self)
                export_id = world.hand_dropped().id
                with self.one_item_failing():
                    world.run_import(export_id)
                run.request_import(world.opened, export_id, world.photos, NOW + DAY)
                world.opened.commit()
                with mock.patch.object(place, "place", kill_on_call(place.place, number)), self.assertRaises(KeyboardInterrupt):
                    run.run_waiting(world.opened, world.settings, NOW + DAY, world.tools)
                world.reopen()
                stopped = world.opened.get(Export, export_id)
                self.assertEqual(stopped.status, "importing")
                self.clean_never_touches_staging(world)
                self.assertEqual(run.run_waiting(world.opened, world.settings, NOW + DAY, world.tools), [export_id])
                self.assertEqual(world.state(), clean)

    def clean_never_touches_staging(self, world):
        cleanup.clean(world.opened, world.photos, NOW + 60 * DAY)
        self.assertTrue((world.root / "_inbox/jo" / "export-1").is_dir())


class OneAtATimeTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.hand_dropped()

    def test_an_import_already_running_in_another_process_is_refused(self):
        with locks.exclusive(run.lock_file(self.world.settings, self.world.account)), self.assertRaises(run.Busy):
            self.world.run_import(self.export)
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "downloaded")
        self.assertEqual(self.world.run_import(self.export).status, "done")

    def test_an_import_already_running_in_this_process_is_refused(self):
        with run.account_lock(self.world.settings, self.world.account), self.assertRaises(run.Busy):
            self.world.run_import(self.export)

    def test_another_account_is_not_held_up(self):
        with run.account_lock(self.world.settings, self.world.account + 1):
            self.assertEqual(self.world.run_import(self.export).status, "done")

    def test_an_export_not_downloaded_yet_is_refused(self):
        for status in ("announced", "downloading", "expired"):
            with self.subTest(status=status):
                self.export.status = status
                with self.assertRaises(run.NotReady):
                    self.world.run_import(self.export)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.hand_dropped()

    def test_a_downloaded_export_waits_until_requested(self):
        self.assertEqual(run.waiting_exports(self.world.opened), [])
        run.request_import(self.world.opened, self.export.id)
        self.assertEqual(run.waiting_exports(self.world.opened), [self.export.id])
        self.assertEqual(run.run_waiting(self.world.opened, self.world.settings, NOW, self.world.tools), [self.export.id])
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "imported")
        self.assertEqual(run.waiting_exports(self.world.opened), [])

    def test_a_crashed_run_is_resumed_by_the_scheduler(self):
        export_id = self.export.id
        with mock.patch.object(run, "stage_rescan", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.world.run_import(export_id)
        self.world.reopen()
        self.assertEqual(run.waiting_exports(self.world.opened), [export_id])
        run.run_waiting(self.world.opened, self.world.settings, NOW, self.world.tools)
        self.assertEqual(self.world.opened.get(Export, export_id).status, "imported")
        self.assertEqual(self.world.sent(SUMMARY), 1)

    def test_a_failed_run_waits_for_a_request_then_resumes(self):
        with mock.patch.object(run, "stage_report", side_effect=RuntimeError("boom")):
            self.world.run_import(self.export)
        self.assertEqual(run.waiting_exports(self.world.opened), [])
        run.request_import(self.world.opened, self.export.id)
        self.assertEqual(run.waiting_exports(self.world.opened), [self.export.id])
        run.run_waiting(self.world.opened, self.world.settings, NOW, self.world.tools)
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "imported")

    def test_a_failed_unpack_is_requested_again_from_the_start(self):
        self.export.status, self.export.stage = "failed", "unpack"
        self.world.opened.commit()
        run.request_import(self.world.opened, self.export.id)
        export = self.world.opened.get(Export, self.export.id)
        self.assertEqual((export.status, export.stage), ("downloaded", "queued"))
        self.assertEqual(run.waiting_exports(self.world.opened), [self.export.id])

    def test_the_scheduler_skips_an_account_that_is_busy(self):
        run.request_import(self.world.opened, self.export.id)
        self.world.opened.commit()
        with run.account_lock(self.world.settings, self.world.account):
            self.assertEqual(run.run_waiting(self.world.opened, self.world.settings, NOW, self.world.tools), [])
        self.assertEqual(run.waiting_exports(self.world.opened), [self.export.id])

    def test_requests_for_missing_imported_or_unready_exports_are_refused(self):
        with self.assertRaises(LookupError):
            run.request_import(self.world.opened, 999)
        self.export.status = "announced"
        with self.assertRaises(run.NotReady):
            run.request_import(self.world.opened, self.export.id)
        self.export.status = "imported"
        with self.assertRaises(run.AlreadyImported):
            run.request_import(self.world.opened, self.export.id)


class HandDroppedAndDownloadedTests(unittest.TestCase):
    def test_a_hand_dropped_export_goes_through_like_a_downloaded_one(self):
        dropped = World(self)
        dropped.run_import(dropped.hand_dropped())
        downloaded = World(self)
        export = downloaded.downloaded()
        self.assertEqual((export.kind, dropped.opened.get(Export, 1).kind), ("initial", "manual"))
        downloaded.run_import(export)
        self.assertEqual(downloaded.state(), dropped.state())
        for world in (dropped, downloaded):
            held = world.root / "_inbox/jo/review/export-1/IMG_1005.JPG"
            self.assertEqual(sha1_of(held), sha1_of(world.root / "_inbox/jo/export-1/staging/Takeout/Google Photos/Photos from 2019/IMG_1005.JPG"))


class PartQueueingTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def parts(self, export):
        self.world.opened.expire_all()
        return [(part.number, part.ingest_state, part.ingest_queued_at) for part in
                self.world.opened.execute(select(ExportPart).where(ExportPart.export_id == export.id).order_by(ExportPart.number)).scalars()]

    def test_registered_zips_of_a_new_export_queue_each_part_and_record_the_route(self):
        export = self.world.hand_dropped(route=None)
        self.assertEqual(self.parts(export), [(1, "queued", NOW), (2, "queued", NOW)])
        self.assertEqual(export.ingest_route, run.PER_PART)

    def test_a_whole_export_import_still_works_on_an_export_whose_route_is_whole(self):
        export = self.world.hand_dropped()
        self.assertEqual(self.parts(export), [(1, "not_queued", None), (2, "not_queued", None)])
        self.assertEqual(part_ingest.run_queued(self.world.opened, self.world.settings, NOW), [])
        self.assertEqual(self.world.run_import(export).status, "done")


class InFlightRouteTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.hand_dropped()
        self.export_id = self.export.id

    def undecided(self, status="downloaded", stage=None, last_run=None):
        export = self.world.opened.get(Export, self.export_id)
        export.ingest_route, export.status, export.stage = None, status, stage
        if last_run is not None:
            self.world.opened.add(ImportRun(export_id=export.id, started=NOW, status=last_run, error="boom" if last_run == "failed" else None))
        self.world.opened.commit()

    def states(self):
        self.world.opened.expire_all()
        parts = self.world.opened.execute(select(ExportPart).where(ExportPart.export_id == self.export_id).order_by(ExportPart.number)).scalars()
        return [part.ingest_state for part in parts]

    def export_row(self):
        self.world.opened.expire_all()
        export = self.world.opened.get(Export, self.export_id)
        return export.status, export.stage, export.ingest_route

    def staging(self):
        folder = self.world.root / "_inbox/jo" / f"export-{self.export_id}" / "staging"
        folder.mkdir(parents=True)

    def tick(self, at=NOW):
        done = run.run_waiting(self.world.opened, self.world.settings, at, self.world.tools)
        part_ingest.run_queued(self.world.opened, self.world.settings, at, self.world.tools)
        finish_export.finish_ready(self.world.opened, self.world.settings, at, self.world.tools)
        return done

    def test_a_downloaded_export_that_has_not_started_unpacking_switches_and_ends_imported_part_by_part(self):
        self.undecided(stage="queued")
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.export_row(), ("imported", "done", "per_part"))
        self.assertEqual(self.states(), ["ingested", "ingested"])
        self.assertEqual(self.world.sent(SUMMARY), 1)

    def test_an_export_with_no_stage_at_all_switches_too(self):
        self.undecided(stage=None)
        run.run_waiting(self.world.opened, self.world.settings, NOW, self.world.tools)
        self.assertEqual((self.export_row()[2], self.states()), ("per_part", ["queued", "queued"]))

    def test_an_export_still_downloading_queues_only_the_parts_already_done(self):
        self.undecided(status="downloading")
        part = self.world.opened.execute(select(ExportPart).where(ExportPart.export_id == self.export_id, ExportPart.number == 2)).scalar_one()
        part.status = "pending"
        self.world.opened.commit()
        run.run_waiting(self.world.opened, self.world.settings, NOW, self.world.tools)
        self.assertEqual((self.export_row(), self.states()), (("downloading", None, "per_part"), ["queued", "not_queued"]))

    def test_an_export_whose_stage_is_unpacked_or_later_finishes_the_old_way(self):
        export = self.export
        with mock.patch.object(run, "stage_rescan", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.world.run_import(export)
        self.world.reopen()
        status, stage, _ = self.export_row()
        self.assertEqual(status, "importing")
        self.assertIn(stage, run.STARTED_STAGES)
        self.undecided(status=status, stage=stage)
        self.assertEqual(self.tick(), [self.export_id])
        self.assertEqual((self.export_row(), self.states()), (("imported", "done", "whole"), ["not_queued", "not_queued"]))
        self.assertEqual(self.world.sent(SUMMARY), 1)

    def test_a_stage_of_unpacked_alone_is_not_switched(self):
        self.undecided(status="importing", stage="unpacked")
        self.assertEqual(run.route_pending(self.world.opened, self.world.settings, NOW), [])
        self.assertEqual((self.export_row(), self.states()), (("importing", "unpacked", "whole"), ["not_queued", "not_queued"]))

    def test_an_export_with_a_staging_folder_finishes_the_old_way(self):
        self.undecided(stage="queued")
        self.staging()
        self.assertEqual(run.route_pending(self.world.opened, self.world.settings, NOW), [])
        self.assertEqual((self.export_row(), self.states()), (("downloaded", "queued", "whole"), ["not_queued", "not_queued"]))
        self.assertEqual(run.waiting_exports(self.world.opened), [self.export_id])

    def test_an_export_marked_failed_before_unpacking_is_retried_part_by_part(self):
        self.undecided(status="failed", stage="unpack", last_run="failed")
        self.assertEqual(run.route_pending(self.world.opened, self.world.settings, NOW), [])
        self.assertEqual(self.states(), ["not_queued", "not_queued"])
        run.request_import(self.world.opened, self.export_id, self.world.photos, NOW)
        self.world.opened.commit()
        self.assertEqual((self.export_row()[2], self.states()), ("per_part", ["queued", "queued"]))
        self.assertEqual(run.latest_run(self.world.opened, self.export_id).status, "queued")
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.export_row()[:2], self.states()), (("imported", "done"), ["ingested", "ingested"]))

    def test_an_export_marked_failed_before_unpacking_with_no_run_is_retried_part_by_part(self):
        self.undecided(status="failed", stage="unpack")
        run.request_import(self.world.opened, self.export_id, self.world.photos, NOW)
        self.world.opened.commit()
        self.assertEqual((self.export_row(), self.states()), (("downloaded", "unpack", "per_part"), ["queued", "queued"]))
        self.tick()
        self.assertEqual(self.export_row()[:2], ("imported", "done"))

    def test_an_export_marked_failed_after_unpacking_started_is_retried_the_old_way(self):
        self.undecided(status="failed", stage="unpack")
        self.staging()
        run.request_import(self.world.opened, self.export_id, self.world.photos, NOW)
        self.world.opened.commit()
        self.assertEqual((self.export_row(), self.states()), (("downloaded", "queued", "whole"), ["not_queued", "not_queued"]))

    def test_the_route_is_decided_once_and_never_flipped(self):
        self.undecided(stage="queued")
        self.assertEqual(run.route_of(self.world.opened, self.world.photos, self.world.opened.get(Export, self.export_id)), run.PER_PART)
        export = self.world.opened.get(Export, self.export_id)
        export.stage = "unpacked"
        self.staging()
        self.assertEqual(run.route_of(self.world.opened, self.world.photos, export), run.PER_PART)
        export.ingest_route = run.WHOLE
        export.stage = None
        self.assertEqual(run.route_of(self.world.opened, self.world.photos, export), run.WHOLE)

    def test_a_per_part_export_with_failed_items_is_not_retried_through_a_staging_folder(self):
        self.undecided(stage="queued")
        self.tick()
        found = run.latest_run(self.world.opened, self.export_id)
        found.failed = 1
        self.world.opened.commit()
        with self.assertRaises(run.NotReady):
            run.request_import(self.world.opened, self.export_id, self.world.photos, NOW + DAY)


TAKEN = 1_600_000_000
BRIDGE = (48.85, 2.35)


class BadSidecarTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def export_with(self, sidecars):
        builder = TakeoutBuilder(self.world.photos.manual_inbox("jo"), seed=11, parts=1)
        for number, (name, change) in enumerate(sidecars.items()):
            item = builder.photo(name, gps=BRIDGE, description="CAPTION", taken=TAKEN + number * 3600)
            item.sidecar_data = change(item.sidecar_data)
        builder.build()
        person = self.world.opened.get(Person, self.world.ids["jo"])
        exports = unpack.inspect_manual_inbox(self.world.opened, self.world.photos, person, NOW)
        self.world.opened.commit()
        return exports[0]

    def takeout_items(self):
        rows = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars()
        return {row.path.rsplit("/", 1)[-1]: row for row in rows}

    def test_a_latin1_caption_keeps_the_photo_with_its_date_and_place_and_is_counted_as_repaired(self):
        latin1 = "Café made-up".encode("latin-1")
        done = self.world.run_import(self.export_with({"IMG_4001.JPG": lambda raw: raw.replace(b"CAPTION", latin1)}))
        self.assertEqual((done.status, done.added, done.failed, done.sidecars_repaired, done.sidecars_unreadable), ("done", 1, 0, 1, 0))
        placed = self.takeout_items()["IMG_4001.JPG"]
        self.assertEqual((placed.captured_utc, placed.time_source, placed.lat, placed.lon, placed.gps_source), (TAKEN, "sidecar", *BRIDGE, "google"))
        self.assertIn("Caf� made-up", placed.sidecar_json)

    def test_a_byte_order_mark_or_utf16_keeps_the_sidecars_metadata(self):
        done = self.world.run_import(self.export_with({"IMG_4101.JPG": lambda raw: b"\xef\xbb\xbf" + raw,
                                                       "IMG_4102.JPG": lambda raw: raw.decode().encode("utf-16")}))
        self.assertEqual((done.added, done.failed, done.sidecars_repaired, done.sidecars_unreadable), (2, 0, 0, 0))
        placed = self.takeout_items()
        self.assertEqual({name: (row.time_source, row.lat) for name, row in placed.items()},
                         {"IMG_4101.JPG": ("sidecar", BRIDGE[0]), "IMG_4102.JPG": ("sidecar", BRIDGE[0])})
        self.assertFalse(placed["IMG_4101.JPG"].sidecar_json.startswith("﻿"))

    def test_an_unreadable_sidecar_costs_only_its_metadata_and_is_counted(self):
        cases = {"IMG_4201.JPG": lambda raw: raw[:len(raw) // 2], "IMG_4202.JPG": lambda raw: b"",
                 "IMG_4203.JPG": lambda raw: raw + b" " * sidecar.LIMIT, "IMG_4204.JPG": lambda raw: b"[" * 200_000,
                 "IMG_4205.JPG": lambda raw: b"\x00\x00\x00{" + bytes(range(256)) * 8, "IMG_4206.JPG": lambda raw: b"[1, 2]"}
        done = self.world.run_import(self.export_with(cases))
        self.assertEqual((done.status, done.added, done.failed, done.sidecars_repaired, done.sidecars_unreadable), ("done", 6, 0, 0, 6))
        placed = self.takeout_items()
        self.assertEqual(set(placed), set(cases))
        self.assertEqual([name for name, row in placed.items() if row.time_source == "sidecar" or row.lat is not None or row.sidecar_json is not None], [])
        self.assertFalse(self.world.opened.execute(select(Event).where(Event.type == "import_item_failed")).scalars().all())
        self.assertTrue(self.import_done().endswith(", 6 sidecars could not be read, so imported without Google metadata"), self.import_done())

    def import_done(self):
        return self.world.opened.execute(select(Event).where(Event.type == "import_done")).scalar_one().detail

    def test_the_import_done_event_names_both_counts_in_fixed_words_and_the_summary_alert_is_unchanged(self):
        done = self.world.run_import(self.export_with({"IMG_4801.JPG": lambda raw: raw[:10], "IMG_4802.JPG": lambda raw: raw.replace(b"CAPTION", b"\xe9"),
                                                       "IMG_4803.JPG": lambda raw: raw.replace(b"CAPTION", b"\xe9")}))
        self.assertEqual((done.sidecars_unreadable, done.sidecars_repaired), (1, 2))
        self.assertTrue(self.import_done().endswith(", 1 sidecar could not be read, so imported without Google metadata, 2 sidecars read with characters replaced"),
                        self.import_done())
        summaries = [message for title, message in self.world.recorder.sent if title == SUMMARY]
        self.assertEqual(len(summaries), 1)
        self.assertNotIn("sidecar", summaries[0])

    def test_a_clean_run_adds_no_sidecar_words_to_the_event(self):
        self.world.run_import(self.export_with({"IMG_4901.JPG": lambda raw: raw}))
        self.assertNotIn("sidecar", self.import_done())

    def test_an_error_reading_the_sidecar_is_a_failed_item_not_a_photo_without_metadata(self):
        export = self.export_with({"IMG_5001.JPG": lambda raw: raw, "IMG_5002.JPG": lambda raw: raw})
        real = paths.open_inbox_file

        def failing(photos, path):
            if path.name == "IMG_5001.JPG.json":
                raise OSError(5, "made-up I/O error")
            return real(photos, path)

        with mock.patch.object(paths, "open_inbox_file", side_effect=failing):
            done = self.world.run_import(export)
        self.assertEqual((done.added, done.failed, done.sidecars_unreadable), (1, 1, 0))
        failures = self.world.opened.execute(select(Event.detail).where(Event.type == "import_item_failed")).scalars().all()
        self.assertEqual(failures, ["metadata: OSError"])

    def test_a_sidecar_up_to_the_limit_is_still_read(self):
        def padded(raw):
            return raw + b" " * (sidecar.LIMIT - len(raw))

        done = self.world.run_import(self.export_with({"IMG_4301.JPG": padded}))
        self.assertEqual((done.added, done.sidecars_unreadable), (1, 0))
        self.assertEqual(self.takeout_items()["IMG_4301.JPG"].time_source, "sidecar")

    def test_a_sidecar_swapped_for_a_link_after_pairing_is_not_followed_and_its_photo_is_kept(self):
        export = self.export_with({"IMG_4401.JPG": lambda raw: raw})
        secret = self.world.base / "outside.json"
        secret.write_text('{"photoTakenTime": {"timestamp": "1234567890"}, "description": "SECRET-CAPTION-CANARY"}')
        real = pairing.pair

        def pairing_then_swapping(root, *rest):
            report = real(root, *rest)
            for path in (self.world.root / "_inbox").rglob("IMG_4401.JPG.json"):
                if not path.is_symlink():
                    path.unlink()
                    path.symlink_to(secret)
            return report

        with mock.patch.object(pairing, "pair", side_effect=pairing_then_swapping):
            done = self.world.run_import(export)
        self.assertEqual((done.added, done.failed, done.sidecars_unreadable), (1, 0, 1))
        self.assertIsNone(self.takeout_items()["IMG_4401.JPG"].sidecar_json)

    def test_a_photo_that_cannot_be_read_still_fails_even_with_a_bad_sidecar(self):
        export = self.export_with({"IMG_4501.JPG": lambda raw: raw[:10], "IMG_4502.JPG": lambda raw: raw})
        real = metadata.candidate

        def failing(item, *args, **kwargs):
            if item.path.name == "IMG_4501.JPG":
                raise ValueError("unreadable")
            return real(item, *args, **kwargs)

        with mock.patch.object(metadata, "candidate", failing):
            done = self.world.run_import(export)
        self.assertEqual((done.added, done.failed, done.sidecars_unreadable), (1, 1, 0))

    def test_resuming_at_place_counts_the_same(self):
        export = self.export_with({"IMG_4601.JPG": lambda raw: raw[:10], "IMG_4602.JPG": lambda raw: raw.replace(b"CAPTION", b"\xe9")})
        with mock.patch.object(run, "stage_place", mock.Mock(side_effect=KeyboardInterrupt)), self.assertRaises(KeyboardInterrupt):
            self.world.run_import(export)
        self.world.reopen()
        done = self.world.run_import(export.id)
        self.assertEqual((done.status, done.added, done.sidecars_unreadable, done.sidecars_repaired), ("done", 2, 1, 1))

    def test_no_event_or_log_names_the_sidecar_or_carries_its_content(self):
        name = "IMG_CANARY-name-4701.JPG"
        export = self.export_with({name: lambda raw: raw.replace(b"CAPTION", b"CANARY-caption-secret \xe9")[:-3],
                                   "IMG_4702.JPG": lambda raw: raw.replace(b"CAPTION", b"CANARY-caption-secret")[:-3]})
        with no_leaks(self, "CANARY-name-4701", "CANARY-caption-secret"):
            done = self.world.run_import(export)
        self.assertEqual((done.added, done.sidecars_unreadable), (2, 2))
        details = " ".join(f"{e.subject} {e.detail or ''}" for e in self.world.opened.execute(select(Event)).scalars())
        self.assertNotIn("CANARY", details)
        self.assertNotIn("CANARY", " ".join(message for _, message in self.world.recorder.sent))



class PinnedMetadataTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.secret = self.world.original("jo", "secret.jpg", self.world.scratch_jpeg("secret.jpg", make="SecretMake", size=(37, 23)))
        self.export = self.world.hand_dropped()
        self.before = tree(self.world.root / "jo/originals")
        self.seen = []
        self.found = {}
        real = self.world.tools.read

        def reading(names):
            self.seen.extend(names)
            self.while_reading()
            answer = real(names)
            self.found.update(answer)
            return answer

        self.world.tools = dataclasses.replace(self.world.tools, read=reading)

    def while_reading(self):
        pass

    def swap(self):
        for path in (self.world.root / "_inbox").rglob("IMG_2001.JPG"):
            if "staging" in path.parts and not path.is_symlink():
                path.unlink()
                path.symlink_to(self.secret)

    def assert_nothing_outside_the_inbox_was_read(self):
        inbox = self.world.root / "_inbox"
        self.assertFalse([name for name in self.seen if name.is_relative_to(inbox)])
        self.assertTrue(self.seen)
        self.assertFalse([tags for tags in self.found.values() if media.tag(tags, "Make") == "SecretMake"])
        self.assertEqual(tree(self.world.root / "jo/originals"), self.before)
        secret = sha1_of(self.secret)
        self.assertFalse(self.world.opened.execute(select(MediaItem).where(MediaItem.sha1 == secret, MediaItem.source == "takeout")).scalars().all())

    def test_a_staged_file_swapped_for_a_link_while_exiftool_reads_is_never_read(self):
        self.while_reading = self.swap
        self.world.run_import(self.export)
        self.assert_nothing_outside_the_inbox_was_read()

    def test_a_staged_file_swapped_for_a_link_after_it_was_hashed_is_never_read(self):
        real = pairing.pair

        def pairing_then_swapping(root, *rest):
            report = real(root, *rest)
            self.swap()
            return report

        with mock.patch.object(pairing, "pair", side_effect=pairing_then_swapping):
            self.world.run_import(self.export)
        self.assert_nothing_outside_the_inbox_was_read()
        failures = self.world.opened.execute(select(Event.detail).where(Event.type == "import_item_failed")).scalars().all()
        self.assertTrue([detail for detail in failures if detail.startswith("metadata:")])


if __name__ == "__main__":
    unittest.main()
