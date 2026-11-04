import json
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from darkroom.domain import alerts, dedupe
from darkroom.domain.ingest import metadata, place, run
from darkroom.storage import locks
from darkroom.storage.models import Alert, DedupeDecision, Event, Export, ImportRun, MediaItem, ReviewItem
from tests.import_world import NOW, World, sha1_of, tree
from tests.privacy import no_leaks

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

        def failing(folder, source, sha1):
            if source.name == "IMG_2001.JPG":
                raise place.PlaceFailed("both names are taken by other files")
            return real(folder, source, sha1)

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


if __name__ == "__main__":
    unittest.main()
