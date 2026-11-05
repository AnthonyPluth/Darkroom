import json
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.dates import utc_now
from darkroom.domain import browser_jobs
from darkroom.storage import db
from darkroom.storage.models import BrowserJob, Export, GoogleAccount
from tests.api_world import ApiCase
from tests.shared import fetch

TOKEN = "exports-api-worker-token-canary-0123456789abcdef"
DAY = 86400.0


class ExportsApiTests(ApiCase):
    extra_env = {"DARKROOM_BROWSER_TOKEN": TOKEN}

    def setUp(self):
        super().setUp()
        self.person_id = self.person()["id"]
        self.account_id = self.account(self.person_id)["id"]
        self.now = utc_now().timestamp()
        self.set_account(self.account_id, session_state="ok", schedule_created=self.now - DAY, schedule_end=self.now + 364 * DAY)

    def set_account(self, account_id, **values):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, account_id)
            for name, value in values.items():
                setattr(account, name, value)

    def worker(self, path, body=None):
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        status, _, reply = fetch(self.base, "POST", path, headers, json.dumps(body or {}).encode())
        return status, json.loads(reply)

    def listing(self, *exports):
        return {"exports": list(exports)}

    def ready(self, google_id="g-1", parts=2):
        return {"google_id": google_id, "created_at": self.now - 3600, "state": "ready", "expires_at": self.now + 6 * DAY, "parts": [{"number": n + 1, "bytes": 1000 * (n + 1)} for n in range(parts)]}

    def check_and_finish(self, listing):
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertIn(status, (200, 201), reply)
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        while job["kind"] != "status":
            job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        return self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": listing})

    def test_nothing_is_listed_before_a_check(self):
        status, reply = self.call("GET", "/api/exports")
        self.assertEqual((status, reply["exports"]), (200, []))
        self.assertEqual([(each["account_id"], each["last_export_at"]) for each in reply["schedules"]], [(self.account_id, None)])
        self.assertGreater(reply["schedules"][0]["next_export_at"], self.now)

    def test_check_now_queues_a_status_job_for_each_connected_account_and_not_twice(self):
        other = self.account(self.person_id, "other@example.test")["id"]
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual(status, 201)
        self.assertEqual([(job["account_id"], job["kind"], job["status"]) for job in reply["jobs"]], [(self.account_id, "status", "queued")])
        self.assertEqual(reply["skipped"], 0)
        self.set_account(other, session_state="ok")
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, [job["account_id"] for job in reply["jobs"]], reply["skipped"]), (201, [other], 1))
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, reply["jobs"], reply["skipped"]), (200, [], 2))
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(BrowserJob)).scalars().all()), 2)

    def test_check_now_says_when_darkroom_is_busy_and_queues_nothing(self):
        with mock.patch.object(browser_jobs, "enqueue_once", side_effect=browser_jobs.Busy("Darkroom is busy with another change; try again in a moment")):
            status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, reply), (503, {"error": "Darkroom is busy with another change; try again in a moment"}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(BrowserJob)).scalars().all(), [])

    def test_check_now_with_no_connected_account_queues_nothing(self):
        self.set_account(self.account_id, session_state="needs_sign_in")
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, reply), (200, {"jobs": [], "skipped": 0}))

    def test_a_finished_status_job_announces_exports_and_the_page_can_list_them(self):
        status, reply = self.check_and_finish(self.listing(self.ready(), {"google_id": "g-2", "created_at": self.now - 60, "state": "in_progress", "expires_at": None, "parts": []}))
        self.assertEqual(status, 200, reply)
        status, reply = self.call("GET", "/api/exports")
        self.assertEqual(status, 200)
        by_id = {"ready" if each["parts"] else "waiting": each for each in reply["exports"]}
        ready = by_id["ready"]
        self.assertEqual((ready["kind"], ready["status"], ready["part_count"], ready["account_id"]), ("initial", "announced", 2, self.account_id))
        self.assertEqual([(part["number"], part["bytes"], part["progress"], part["downloads_used"], part["status"]) for part in ready["parts"]], [(1, 1000, 0, 0, "pending"), (2, 2000, 0, 0, "pending")])
        self.assertEqual((by_id["waiting"]["ready_at"], by_id["waiting"]["parts"]), (None, []))
        self.assertEqual(reply["schedules"][0]["last_export_at"], by_id["waiting"]["created_at"])
        status, one = self.call("GET", f"/api/exports/{ready['id']}")
        self.assertEqual((status, one["export"]), (200, ready))
        with db.session(self.engine) as opened:
            kinds = sorted(job.kind for job in opened.execute(select(BrowserJob)).scalars())
        self.assertEqual(kinds, ["download", "status"])

    def test_a_second_check_with_the_same_listing_changes_nothing(self):
        listing = self.listing(self.ready())
        self.check_and_finish(listing)
        first = self.call("GET", "/api/exports")[1]["exports"]
        self.check_and_finish(listing)
        self.assertEqual(self.call("GET", "/api/exports")[1]["exports"], first)
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(BrowserJob).where(BrowserJob.kind == "download")).scalars().all()), 1)

    def test_a_listing_that_is_not_valid_is_refused_and_the_job_stays_open(self):
        self.call("POST", "/api/exports/check-now")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        for result in ({}, {"exports": [{"google_id": "x"}]}, {"exports": [self.ready(parts=0)]}):
            status, reply = self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": result})
            self.assertEqual(status, 400, reply)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job["id"]).status, "running")
            self.assertEqual(opened.execute(select(Export)).scalars().all(), [])
        self.assertEqual(self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": self.listing()})[0], 200)

    def test_the_first_export_after_the_schedule_is_initial_and_later_ones_incremental(self):
        self.check_and_finish(self.listing(self.ready("g-1")))
        later = {**self.ready("g-2"), "created_at": self.now + 61 * DAY, "expires_at": self.now + 68 * DAY}
        self.check_and_finish(self.listing(later))
        kinds = [row["kind"] for row in self.call("GET", "/api/exports")[1]["exports"]]
        self.assertEqual(sorted(kinds), ["incremental", "initial"])

    def test_one_export_is_returned_by_id_and_unknown_ids_are_404(self):
        self.assertEqual(self.call("GET", "/api/exports/99")[0], 404)
        self.assertEqual(self.call("GET", "/api/exports/abc")[0], 404)
        self.assertEqual(self.call("GET", "/api/exports/" + "9" * 13)[0], 404)

    def test_the_session_is_marked_ok_by_a_finished_status_job(self):
        self.call("POST", "/api/exports/check-now")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/waiting", {"reason": "sign_in"})
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account_id).session_state, "needs_sign_in")
        self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": self.listing()})
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account_id).session_state, "ok")

    def test_a_status_job_cannot_be_queued_through_the_generic_endpoint(self):
        status, reply = self.call("POST", "/api/browser/jobs", {"account_id": self.account_id, "kind": "status"})
        self.assertEqual(status, 400, reply)

    def test_an_export_that_expired_before_it_was_seen_is_listed_as_expired_with_no_parts(self):
        old = {"google_id": "g-old", "created_at": self.now - 10 * DAY, "state": "ready", "expires_at": self.now - 3 * DAY, "parts": [{"number": 1, "bytes": 5}]}
        self.check_and_finish(self.listing(old))
        (row,) = self.call("GET", "/api/exports")[1]["exports"]
        self.assertEqual((row["status"], row["parts"], row["ready_at"]), ("expired", [], None))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(BrowserJob).where(BrowserJob.kind == "download")).scalars().all(), [])


if __name__ == "__main__":
    unittest.main()
