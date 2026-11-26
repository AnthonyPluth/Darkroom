import io
import json
import logging
import unittest
import zipfile
from unittest import mock

from sqlalchemy import select

from darkroom.domain import downloads, events, exports, managed_switch
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Export, ExportPart, GoogleAccount, ImportRun, Person
from darkroom.storage.paths import Photos
from tests.privacy import no_leaks
from tests.test_browser_internal_api import TOKEN, WorkerApiServer

NOW = 1_800_000_000.0
FILE_CANARY = "takeout-canaryfile0451-001.zip"


class DownloadApiServer(WorkerApiServer):
    def setUp(self):
        super().setUp()
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            account.session_state = "ok"
            listing = [exports.Listed("g-1", NOW - 600, "ready", NOW + 7 * 86400, (exports.ListedPart(1, 2048),))]
            exports.reconcile(opened, account, listing, NOW)
            self.export = opened.execute(select(Export)).scalar_one().id
        self.job = self.claim()["id"]

    def part(self, body, number=1):
        return self.worker("POST", f"/internal/browser/jobs/{self.job}/parts/{number}", body)

    def write_part(self, name):
        folder = Photos(self.photos).export_tree("jo", f"export-{self.export}")
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(folder / name, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("Takeout/x.bin", b"x" * 1900)
        return (folder / name).stat().st_size


GB = 10**9


class GoogleCountTests(DownloadApiServer):
    def part_row(self):
        with db.session(self.engine) as opened:
            part = exports.parts_of(opened, self.export)[0]
            return part.status, part.attempts, part.downloads_used

    def test_the_count_googles_page_showed_reaches_the_budget_before_the_attempt(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].downloads_used = 5
        status, reply = self.part({"event": "attempt", "google_downloads": 4})
        self.assertEqual((status, reply["allowed"], reply["part"]["downloads_used"]), (200, True, 5))
        self.assertEqual(self.part_row(), ("downloading", 1, 5))

    def test_without_a_count_the_attempt_counts_on_its_own(self):
        status, reply = self.part({"event": "attempt"})
        self.assertEqual((status, reply["part"]["downloads_used"]), (200, 1))

    def test_a_count_that_is_not_a_whole_number_in_range_is_refused_and_changes_nothing(self):
        for bad in (-1, 1001, "4", 4.5, True, None):
            with self.subTest(bad=bad):
                status, reply = self.part({"event": "attempt", "google_downloads": bad})
                self.assertEqual(status, 400 if bad is not None else 200, reply)
                if status == 400:
                    self.assertEqual(self.part_row(), ("pending", 0, 0))
        self.assertEqual(self.part_row()[0], "downloading")


class SpaceHelpers(DownloadApiServer):
    def free(self, size):
        return mock.patch.object(Photos, "free_bytes", return_value=size)

    def part_row(self):
        with db.session(self.engine) as opened:
            part = exports.parts_of(opened, self.export)[0]
            return part.status, part.attempts, part.downloads_used


class FreeSpaceTests(SpaceHelpers):

    def test_the_threshold_is_300_decimal_gigabytes(self):
        self.assertEqual(downloads.MIN_FREE_BYTES, 300 * 10**9)

    def test_below_the_threshold_nothing_starts_and_nothing_is_used(self):
        with self.free(299 * GB):
            status, reply = self.part({"event": "attempt"})
        self.assertEqual((status, reply["allowed"], reply["gave_up"], reply["paused_for_space"]), (200, False, False, True))
        self.assertEqual((reply["part"]["attempts"], reply["part"]["downloads_used"], reply["part"]["status"]), (0, 0, "pending"))
        self.assertEqual(self.part_row(), ("pending", 0, 0))
        with db.session(self.engine) as opened:
            self.assertNotEqual(opened.get(Export, self.export).status, "downloading")
            self.assertIsNone(opened.get(BrowserJob, self.job).result)
            self.assertEqual([entry for entry in events.recent(opened, type="export_part_attempt")], [])

    def test_one_byte_below_pauses_and_exactly_at_the_threshold_starts(self):
        with self.free(300 * GB - 1):
            self.assertTrue(self.part({"event": "attempt"})[1]["paused_for_space"])
        with self.free(300 * GB):
            status, reply = self.part({"event": "attempt"})
        self.assertEqual((status, reply["allowed"], reply["paused_for_space"], reply["part"]["downloads_used"]), (200, True, False, 1))

    def test_a_pause_that_ends_lets_the_same_part_start_with_all_its_attempts(self):
        with self.free(0):
            self.part({"event": "attempt"})
            self.part({"event": "attempt"})
        with self.free(400 * GB):
            reply = self.part({"event": "attempt"})[1]
        self.assertEqual((reply["allowed"], reply["part"]["attempts"], reply["part"]["downloads_used"]), (True, 1, 1))

    def test_a_part_that_is_downloading_when_space_falls_finishes(self):
        with self.free(400 * GB):
            self.assertTrue(self.part({"event": "attempt"})[1]["allowed"])
        with self.free(1 * GB):
            self.assertEqual(self.part({"event": "progress", "bytes": 1024})[0], 200)
            size = self.write_part(FILE_CANARY)
            status, reply = self.part({"event": "done", "filename": FILE_CANARY, "bytes": size})
        self.assertEqual((status, reply["part"]["status"], reply["paused_for_space"]), (200, "done", False))

    def test_a_share_whose_space_cannot_be_read_stops_nothing_and_is_logged(self):
        logged = io.StringIO()
        with mock.patch.object(Photos, "free_bytes", side_effect=OSError("canary-path-0451")), mock.patch("sys.stderr", logged):
            status, reply = self.part({"event": "attempt"})
        self.assertEqual((status, reply["allowed"], reply["paused_for_space"]), (200, True, False))
        self.assertIn("free space on the share could not be read", logged.getvalue())
        self.assertNotIn("canary-path-0451", logged.getvalue())

    def test_an_exhausted_part_still_gives_up_while_space_is_low(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].attempts = downloads.MAX_ATTEMPTS
        with self.free(0):
            reply = self.part({"event": "attempt"})[1]
        self.assertEqual((reply["gave_up"], reply["paused_for_space"]), (True, False))

    def test_the_expiry_alert_opens_during_a_pause(self):
        with self.free(0):
            self.part({"event": "attempt"})
        with db.session(self.engine) as opened:
            export = opened.get(Export, self.export)
            export.expires_at = NOW + 2 * 86400
            opened.flush()
            self.assertEqual(downloads.watch_expiry(opened, self.settings, NOW), 1)
            self.assertEqual(opened.execute(select(Alert).where(Alert.kind == downloads.EXPIRING, Alert.closed.is_(None))).scalars().all()[0].key, downloads.expiry_key(self.export))


class SpaceCheckTests(SpaceHelpers):
    def unchanged(self):
        self.assertEqual(self.part_row(), ("pending", 0, 0))
        with db.session(self.engine) as opened:
            self.assertNotEqual(opened.get(Export, self.export).status, "downloading")
            self.assertIsNone(opened.get(BrowserJob, self.job).result)
            self.assertEqual(events.recent(opened, type="export_part_attempt"), [])
            self.assertEqual(events.recent(opened, type="export_part_failed"), [])

    def test_below_the_threshold_the_space_check_pauses_and_uses_nothing(self):
        with self.free(300 * GB - 1):
            status, reply = self.part({"event": "space"})
        self.assertEqual((status, reply["allowed"], reply["gave_up"], reply["paused_for_space"]), (200, False, False, True))
        self.assertEqual((reply["part"]["attempts"], reply["part"]["downloads_used"]), (0, 0))
        self.unchanged()

    def test_with_room_the_space_check_allows_and_still_uses_nothing(self):
        with self.free(300 * GB):
            status, reply = self.part({"event": "space"})
        self.assertEqual((status, reply["allowed"], reply["gave_up"], reply["paused_for_space"]), (200, True, False, False))
        self.unchanged()

    def test_the_space_check_counts_what_other_parts_still_have_to_come(self):
        with self.free(400 * GB):
            self.assertFalse(self.part({"event": "space"})[1]["paused_for_space"])
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].bytes = 400 * GB
        with self.free(400 * GB):
            self.assertTrue(self.part({"event": "space"})[1]["paused_for_space"])

    def test_a_share_whose_space_cannot_be_read_pauses_nothing_in_the_space_check(self):
        logged = io.StringIO()
        with mock.patch.object(Photos, "free_bytes", side_effect=OSError("canary-path-0452")), mock.patch("sys.stderr", logged):
            status, reply = self.part({"event": "space"})
        self.assertEqual((status, reply["allowed"], reply["paused_for_space"]), (200, True, False))
        self.assertIn("free space on the share could not be read", logged.getvalue())
        self.assertNotIn("canary-path-0452", logged.getvalue())
        self.unchanged()

    def test_a_part_out_of_attempts_gives_up_at_the_space_check_while_space_is_low(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].attempts = downloads.MAX_ATTEMPTS
        with self.free(0):
            status, reply = self.part({"event": "space"})
        self.assertEqual((status, reply["allowed"], reply["gave_up"], reply["paused_for_space"]), (200, False, True, False))
        self.assertEqual(self.part_row(), ("failed", downloads.MAX_ATTEMPTS, 0))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Export, self.export).status, "failed")

    def test_a_part_with_attempts_left_is_not_given_up_by_the_space_check(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].attempts = downloads.MAX_ATTEMPTS - 1
        with self.free(0):
            reply = self.part({"event": "space"})[1]
        self.assertEqual((reply["gave_up"], reply["paused_for_space"]), (False, True))
        self.assertEqual(self.part_row(), ("pending", downloads.MAX_ATTEMPTS - 1, 0))

    def test_a_part_out_of_downloads_by_darkrooms_count_is_left_for_the_attempt_to_read_googles_count(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].downloads_used = downloads.MAX_DOWNLOADS
        with self.free(0):
            status, reply = self.part({"event": "space"})
        self.assertEqual((status, reply["allowed"], reply["gave_up"], reply["paused_for_space"]), (200, False, False, False))
        self.assertEqual(self.part_row(), ("pending", 0, downloads.MAX_DOWNLOADS), "the space check gives up nothing Google's count could still allow")
        with self.free(400 * GB):
            reply = self.part({"event": "attempt", "google_downloads": downloads.MAX_DOWNLOADS - 1})[1]
        self.assertEqual((reply["allowed"], reply["gave_up"], reply["part"]["downloads_used"]), (True, False, downloads.MAX_DOWNLOADS))

    def test_a_part_out_of_downloads_by_both_counts_still_gives_up_at_the_attempt(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].downloads_used = downloads.MAX_DOWNLOADS
        with self.free(0):
            self.assertFalse(self.part({"event": "space"})[1]["paused_for_space"])
            reply = self.part({"event": "attempt", "google_downloads": downloads.MAX_DOWNLOADS})[1]
        self.assertEqual((reply["gave_up"], reply["paused_for_space"]), (True, False))

    def test_a_downloaded_part_is_refused(self):
        with self.free(400 * GB):
            self.part({"event": "attempt"})
            size = self.write_part(FILE_CANARY)
            self.part({"event": "done", "filename": FILE_CANARY, "bytes": size})
            status, _ = self.part({"event": "space"})
        self.assertEqual(status, 400)

    def test_the_expiry_alert_opens_while_the_space_check_keeps_pausing(self):
        with self.free(0):
            for _ in range(3):
                self.assertTrue(self.part({"event": "space"})[1]["paused_for_space"])
        with db.session(self.engine) as opened:
            opened.get(Export, self.export).expires_at = NOW + 2 * 86400
            opened.flush()
            self.assertEqual(downloads.watch_expiry(opened, self.settings, NOW), 1)
            self.assertEqual([alert.key for alert in opened.execute(select(Alert).where(Alert.kind == downloads.EXPIRING, Alert.closed.is_(None))).scalars()], [downloads.expiry_key(self.export)])


class DownloadApiTests(DownloadApiServer):
    def test_a_completed_whole_download_is_saved_without_queuing_a_refused_import(self):
        with db.session(self.engine, writing=True) as opened:
            opened.query(Person).one().managed_imports = "on"
            opened.get(Export, self.export).ingest_route = "whole"
        self.assertEqual(self.ingest_state_after_done(), ("not_queued", None))
        status, reply = self.worker("POST", f"/internal/browser/jobs/{self.job}/done", {"result": {"parts": 1}})
        self.assertEqual((status, reply["job"]["id"]), (200, self.job))
        with db.session(self.engine) as opened:
            export = opened.get(Export, self.export)
            part = opened.query(ExportPart).one()
            self.assertEqual((export.status, export.stage, part.status, part.ingest_state), ("failed", None, "done", "not_queued"))
            self.assertEqual(opened.get(BrowserJob, self.job).status, "done")
            self.assertEqual(opened.query(ImportRun).count(), 0)
            self.assertEqual([row.detail for row in events.recent(opened, type="export_import_refused")], [managed_switch.REFUSED])
            self.assertTrue((Photos(self.photos).export_tree("jo", f"export-{self.export}") / FILE_CANARY).is_file())

    def test_refusing_a_retry_keeps_existing_export_and_part_states(self):
        with db.session(self.engine, writing=True) as opened:
            opened.query(Person).one().managed_imports = "stopped"
            export = opened.get(Export, self.export)
            export.status, export.stage, export.ingest_route = "failed", "unpacked", "per_part"
            part = opened.query(ExportPart).one()
            part.status, part.ingest_state = "done", "failed"
            opened.add(ImportRun(export_id=export.id, started=NOW, status="failed", error="earlier failure"))
        status, _, body = self.owner("POST", f"/api/exports/{self.export}/import")
        self.assertEqual((status, json.loads(body)), (409, {"error": managed_switch.REFUSED}))
        with db.session(self.engine) as opened:
            export = opened.get(Export, self.export)
            part = opened.query(ExportPart).one()
            prior = opened.query(ImportRun).one()
            self.assertEqual((export.status, export.stage, part.ingest_state, prior.status, prior.error), ("failed", "unpacked", "failed", "failed", "earlier failure"))

    def test_a_download_runs_through_the_worker_routes_and_leaves_its_parts_to_the_per_part_ingest(self):
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        with no_leaks(self, TOKEN, database=self.engine, replies=self.replies):
            logging.getLogger().addHandler(handler)
            try:
                status, plan = self.worker("POST", f"/internal/browser/jobs/{self.job}/download")
                self.assertEqual(status, 200)
                self.assertEqual((plan["person"], plan["folder"], plan["part_count"]), ("jo", f"export-{self.export}", 1))
                status, reply = self.part({"event": "attempt"})
                self.assertEqual((status, reply["allowed"], reply["part"]["downloads_used"]), (200, True, 1))
                self.assertEqual((reply["part"]["max_attempts"], reply["part"]["max_downloads"]), (3, 5))
                self.assertEqual(self.part({"event": "progress", "bytes": 1024})[0], 200)
                status, _, body = self.owner("GET", f"/api/exports/{self.export}")
                self.assertEqual((status, json.loads(body)["export"]["parts"][0]["progress"]), (200, 50))
                size = self.write_part(FILE_CANARY)
                status, reply = self.part({"event": "done", "filename": FILE_CANARY, "bytes": size})
                self.assertEqual((status, reply["part"]["status"]), (200, "done"))
                self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{self.job}/done", {"result": {"parts": 1}})[0], 200)
            finally:
                logging.getLogger().removeHandler(handler)
        self.assertNotIn(FILE_CANARY, logged.getvalue())
        with db.session(self.engine) as opened:
            export = opened.get(Export, self.export)
            self.assertEqual((export.status, export.stage), ("downloaded", None))
            self.assertEqual(opened.get(BrowserJob, self.job).status, "done")

    def ingest_state_after_done(self):
        self.part({"event": "attempt"})
        size = self.write_part(FILE_CANARY)
        self.assertEqual(self.part({"event": "done", "filename": FILE_CANARY, "bytes": size})[0], 200)
        with db.session(self.engine) as opened:
            found = opened.execute(select(ExportPart)).scalar_one()
            return found.ingest_state, found.ingest_queued_at

    def test_a_finished_part_is_queued_for_ingest(self):
        state, queued_at = self.ingest_state_after_done()
        self.assertEqual(state, "queued")
        self.assertIsNotNone(queued_at)

    def test_a_finished_part_of_an_export_that_started_unpacking_is_not_queued(self):
        with db.session(self.engine, writing=True) as opened:
            export = opened.get(Export, self.export)
            export.stage = "unpacked"
        self.assertEqual(self.ingest_state_after_done(), ("not_queued", None))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Export, self.export).ingest_route, "whole")

    def test_done_before_every_part_is_downloaded_is_refused_and_the_job_stays_open(self):
        status, reply = self.worker("POST", f"/internal/browser/jobs/{self.job}/done", {"result": {}})
        self.assertEqual((status, reply), (400, {"error": f"not every part of export {self.export} is downloaded"}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, self.job).status, "running")

    def test_a_part_that_fails_three_times_gives_up(self):
        for tries in range(3):
            self.assertEqual(self.part({"event": "attempt"})[1]["allowed"], True)
            status, reply = self.part({"event": "failed", "reason": "corrupt"})
            self.assertEqual((status, reply["gave_up"]), (200, tries == 2))
        status, reply = self.worker("POST", f"/internal/browser/jobs/{self.job}/download")
        self.assertEqual((status, reply), (400, {"error": f"export {self.export} is failed, not waiting to be downloaded"}))

    def test_googles_prompt_after_a_click_does_not_count_as_an_attempt_but_still_counts_the_download(self):
        for tries in range(5):
            status, reply = self.part({"event": "attempt"})
            self.assertEqual((status, reply["allowed"], reply["part"]["attempts"], reply["part"]["downloads_used"]), (200, True, 1, tries + 1))
            status, reply = self.part({"event": "failed", "reason": "asked_to_verify"})
            self.assertEqual((status, reply["gave_up"], reply["part"]["attempts"], reply["part"]["status"]), (200, tries == 4, 0, "failed" if tries == 4 else "pending"))
        self.assertEqual(self.part({"event": "attempt"})[0], 400)

    def test_a_prompt_after_a_counted_failure_keeps_the_earlier_attempt(self):
        self.part({"event": "attempt"})
        self.part({"event": "failed", "reason": "not_started"})
        self.part({"event": "attempt"})
        status, reply = self.part({"event": "failed", "reason": "asked_to_verify"})
        self.assertEqual((status, reply["part"]["attempts"], reply["part"]["downloads_used"]), (200, 1, 2))

    def test_a_prompt_reported_with_no_attempt_in_flight_takes_nothing_back(self):
        self.part({"event": "attempt"})
        self.part({"event": "failed", "reason": "not_started"})
        status, reply = self.part({"event": "failed", "reason": "asked_to_verify"})
        self.assertEqual((status, reply["part"]["attempts"], reply["part"]["downloads_used"]), (200, 1, 1))
        self.part({"event": "attempt"})
        self.part({"event": "failed", "reason": "corrupt"})
        status, reply = self.part({"event": "failed", "reason": "asked_to_verify"})
        self.assertEqual((status, reply["part"]["attempts"]), (200, 2))

    def recorded(self, kind):
        with db.session(self.engine) as opened:
            return [entry.detail for entry in reversed(events.recent(opened, type=kind))]

    def test_clicks_and_prompts_are_recorded_in_fixed_words_and_change_no_part(self):
        self.part({"event": "attempt"})
        before = self.part({"event": "progress", "bytes": 0})[1]["part"]
        cases = [
            ({"event": "clicked", "downloading": 0}, "part 1: clicked with 0 other parts downloading, the first click of this job, no prompt answered yet in this job"),
            ({"event": "clicked", "downloading": 1, "since_click": 840, "since_answer": 1890},
             "part 1: clicked with 1 other part downloading, 14 minutes after the previous click, 31 minutes after the last answered prompt"),
            ({"event": "clicked", "downloading": 3, "since_click": 59, "since_answer": 60},
             "part 1: clicked with 3 other parts downloading, 0 minutes after the previous click, 1 minute after the last answered prompt"),
        ]
        for body, words in cases:
            status, reply = self.part(body)
            self.assertEqual((status, reply["allowed"], reply["gave_up"], reply["part"]), (200, True, False, before))
            self.assertEqual(self.recorded("export_part_clicked")[-1], words)
        status, reply = self.part({"event": "prompted", "downloading": 1, "clicks": 4, "since_click": 600, "since_answer": 7200})
        self.assertEqual((status, reply["part"]), (200, before))
        self.assertEqual(self.recorded("export_part_prompt"), ["part 1: Google asked to verify after 4 clicks since the last answered prompt, with 1 other part downloading, "
                                                               "10 minutes after the previous click, 120 minutes after the last answered prompt"])
        self.part({"event": "prompted", "downloading": 0, "clicks": 1})
        self.assertEqual(self.recorded("export_part_prompt")[-1], "part 1: Google asked to verify after 1 click since the last answered prompt, with 0 other parts downloading, "
                                                                  "the first click of this job, no prompt answered yet in this job")

    def test_bad_telemetry_is_refused_and_records_nothing(self):
        for body in ({"event": "clicked"}, {"event": "clicked", "downloading": -1}, {"event": "clicked", "downloading": 11}, {"event": "clicked", "downloading": "1"},
                     {"event": "clicked", "downloading": 1, "since_click": -5}, {"event": "clicked", "downloading": 1, "since_answer": 10**9},
                     {"event": "prompted", "downloading": 1}, {"event": "prompted", "downloading": 1, "clicks": True}):
            self.assertEqual(self.part(body)[0], 400, body)
        self.assertEqual(self.part({"event": "clicked", "downloading": 0}, 9)[0], 400)
        self.assertEqual(self.recorded("export_part_clicked") + self.recorded("export_part_prompt"), [])

    def test_a_failed_download_job_resets_its_part_and_alerts(self):
        self.part({"event": "attempt"})
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{self.job}/failed", {"message": "UnexpectedPage: gone"})[0], 200)
        with db.session(self.engine) as opened:
            self.assertEqual([part.status for part in exports.parts_of(opened, self.export)], ["pending"])
            self.assertEqual(opened.execute(select(Alert.kind)).scalars().all(), ["failure"])

    def test_bad_part_requests_are_refused(self):
        cases = [
            ({"event": "attempt"}, "x", 400),
            ({"event": "attempt"}, "%C2%B2", 400),
            ({"event": "attempt"}, "12345", 400),
            ({"event": "jump"}, 1, 400),
            ({"event": "progress", "bytes": -1}, 1, 400),
            ({"event": "done", "filename": "../x.zip", "bytes": 10}, 1, 400),
            ({"event": "failed", "reason": "bored"}, 1, 400),
            ({"event": "attempt"}, 9, 400),
        ]
        for body, number, expected in cases:
            self.assertEqual(self.part(body, number)[0], expected, (body, number))
        self.assertEqual(self.worker("POST", "/internal/browser/jobs/999/parts/1", {"event": "attempt"})[0], 404)
        self.assertEqual(self.worker("POST", "/internal/browser/jobs/999/download")[0], 404)

    def test_a_job_that_is_not_a_download_cannot_report_parts(self):
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{self.job}/done", {"result": {}})[0], 400)
        with db.session(self.engine) as opened:
            job = opened.get(BrowserJob, self.job)
            job.kind = "noop"
        status, reply = self.part({"event": "attempt"})
        self.assertEqual((status, reply), (400, {"error": f"browser job {self.job} is not a download"}))

    def test_a_finished_job_cannot_report_parts(self):
        self.worker("POST", f"/internal/browser/jobs/{self.job}/failed", {"message": "stopped"})
        self.assertEqual(self.part({"event": "attempt"}), (409, {"error": "this job is no longer running"}))


if __name__ == "__main__":
    unittest.main()
