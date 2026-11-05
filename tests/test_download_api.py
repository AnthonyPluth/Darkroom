import io
import json
import logging
import unittest
import zipfile

from sqlalchemy import select

from darkroom.domain import exports
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Export, GoogleAccount
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


class DownloadApiTests(DownloadApiServer):
    def test_a_download_runs_through_the_worker_routes_and_queues_the_import(self):
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
            self.assertEqual((export.status, export.stage), ("downloaded", "queued"))
            self.assertEqual(opened.get(BrowserJob, self.job).status, "done")

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
