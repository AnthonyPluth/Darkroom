import json
import unittest

from sqlalchemy import select

from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, GoogleAccount
from tests.api_world import ApiCase
from tests.shared import fetch

TOKEN = "accounts-jobs-worker-token-canary-0123456789abcdef"


class AccountJobsApiTests(ApiCase):
    extra_env = {"DARKROOM_BROWSER_TOKEN": TOKEN, "VNC_URL": "https://browser.example/vnc.html"}

    def setUp(self):
        super().setUp()
        self.person_id = self.person()["id"]
        self.account_id = self.account(self.person_id)["id"]

    def worker(self, path, body=None):
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        status, _, reply = fetch(self.base, "POST", path, headers, json.dumps(body or {}).encode())
        return status, json.loads(reply)

    def account_view(self):
        _, reply = self.call("GET", "/api/accounts")
        return next(account for account in reply["accounts"] if account["id"] == self.account_id)

    def test_connect_queues_a_job_for_the_account(self):
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/connect")
        self.assertEqual(status, 201)
        self.assertEqual((reply["job"]["kind"], reply["job"]["status"], reply["job"]["account_id"]), ("connect", "queued", self.account_id))

    def test_schedule_queues_a_job_for_the_account(self):
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual((status, reply["job"]["kind"]), (201, "schedule"))

    def test_a_second_request_while_one_is_open_is_a_conflict(self):
        self.call("POST", f"/api/accounts/{self.account_id}/connect")
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/connect")
        self.assertEqual(status, 409)
        self.assertIn("already", reply["error"])
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 201)
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(BrowserJob)).scalars().all()), 2)

    def test_only_one_schedule_per_account_even_after_it_is_done(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual(status, 409, reply)
        self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {"schedule_created": 100.0, "schedule_end": 200.0}})
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual(status, 409, reply)
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/connect")[0], 201)

    def test_after_an_unconfirmed_schedule_the_person_must_say_they_checked_takeout(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "ScheduleNotConfirmed: check Takeout before trying again"})
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual(status, 409)
        self.assertIn("check Takeout", reply["error"])
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked")
        self.assertEqual((status, reply["job"]["message"], reply["job"]["needs_check"]), (200, "ScheduleNotConfirmed: check Takeout before trying again", False))
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked")[0], 409)
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 201)

    def test_checking_an_unknown_account_is_404(self):
        for path in (f"/api/accounts/{self.account_id + 50}/schedule/checked", "/api/accounts/abc/schedule/checked"):
            self.assertEqual(self.call("POST", path)[0], 404)

    def test_a_failed_schedule_job_may_be_retried(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "UnexpectedPage"})
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 201)

    def test_the_generic_job_route_cannot_queue_the_guarded_kinds(self):
        for kind in ("connect", "schedule"):
            status, reply = self.call("POST", "/api/browser/jobs", {"account_id": self.account_id, "kind": kind})
            self.assertEqual(status, 400, reply)
            self.assertNotIn("connect", reply["error"])
        self.assertEqual(self.call("POST", "/api/browser/jobs", {"account_id": self.account_id, "kind": "noop"})[0], 201)
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 201)
        self.assertEqual(self.call("POST", "/api/browser/jobs", {"account_id": self.account_id, "kind": "schedule"})[0], 400)
        with db.session(self.engine) as opened:
            self.assertEqual(sorted(job.kind for job in opened.execute(select(BrowserJob)).scalars()), ["noop", "schedule"])

    def test_the_claimed_job_carries_the_account_email(self):
        self.call("POST", f"/api/accounts/{self.account_id}/connect")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.assertEqual(job["payload"], {"email": "jo.photos@example.test"})

    def test_the_email_cannot_change_while_a_connect_or_schedule_job_is_open(self):
        self.call("POST", f"/api/accounts/{self.account_id}/connect")
        status, reply = self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})
        self.assertEqual(status, 409)
        self.assertIn("still open", reply["error"])
        self.assertEqual(self.account_view()["email"], "jo.photos@example.test")
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "jo.photos@example.test"})[0], 200)
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})[0], 409)
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "UnexpectedPage"})
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})[0], 200)

    def test_an_unresolved_schedule_attempt_stays_in_the_job_list_beyond_the_history_cap(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "ScheduleNotConfirmed: check Takeout"})
        with db.session(self.engine) as opened:
            for number in range(60):
                opened.add(BrowserJob(account_id=self.account_id, kind="noop", status="done", payload="{}", created=float(number)))
        for path in ("/api/browser/jobs", f"/api/browser/jobs?account={self.account_id}"):
            listed = self.call("GET", path)[1]["jobs"]
            self.assertEqual(len(listed), 51, path)
            self.assertIn(job["id"], [entry["id"] for entry in listed])
            self.assertEqual([entry["id"] for entry in listed], sorted((entry["id"] for entry in listed), reverse=True))
        self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked")
        self.assertNotIn(job["id"], [entry["id"] for entry in self.call("GET", "/api/browser/jobs")[1]["jobs"]])

    def test_the_job_list_marks_the_attempt_that_needs_checking(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "ScheduleNotConfirmed: check Takeout"})
        self.assertEqual([entry["needs_check"] for entry in self.call("GET", "/api/browser/jobs")[1]["jobs"]], [True])
        self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked")
        self.assertEqual([entry["needs_check"] for entry in self.call("GET", "/api/browser/jobs")[1]["jobs"]], [False])

    def test_a_failed_job_lists_only_the_capture_files_that_exist(self):
        self.call("POST", f"/api/accounts/{self.account_id}/connect")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        folder = self.photos / "_browser" / str(self.account_id) / "failures" / str(job["id"])
        folder.mkdir(parents=True)
        (folder / "page.html").write_bytes(b"x")
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "UnexpectedPage", "failure_dir": f"_browser/{self.account_id}/failures/{job['id']}", "files": ["screenshot.png", "page.html"]})
        listed = self.call("GET", "/api/browser/jobs")[1]["jobs"][0]
        self.assertEqual((listed["has_capture"], listed["capture_files"]), (True, ["page.html"]))

    def test_unknown_and_malformed_accounts_are_404(self):
        for path in (f"/api/accounts/{self.account_id + 50}/connect", "/api/accounts/abc/schedule", f"/api/accounts/{self.account_id + 50}/schedule"):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path)[0], 404)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(BrowserJob)).scalars().all(), [])

    def test_the_connect_round_trip_flips_the_session_state_and_the_alert(self):
        self.call("POST", f"/api/accounts/{self.account_id}/connect")
        _, claimed = self.worker("/internal/browser/jobs/claim")
        job = claimed["job"]
        self.assertEqual((job["kind"], job["account"]), ("connect", str(self.account_id)))
        self.assertEqual(self.worker(f"/internal/browser/jobs/{job['id']}/waiting", {"reason": "sign_in"})[0], 200)
        self.assertEqual(self.account_view()["session_state"], "needs_sign_in")
        with db.session(self.engine) as opened:
            (alert,) = opened.execute(select(Alert)).scalars().all()
            self.assertEqual((alert.kind, alert.link, alert.closed), ("sign_in_needed", "https://browser.example/vnc.html", None))
        self.assertEqual(self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {"session_state": "ok"}})[0], 200)
        self.assertEqual(self.account_view()["session_state"], "ok")
        with db.session(self.engine) as opened:
            self.assertIsNotNone(opened.execute(select(Alert)).scalars().one().closed)

    def test_the_schedule_round_trip_shows_on_the_account(self):
        self.assertEqual((self.account_view()["schedule_created"], self.account_view()["schedule_end"]), (None, None))
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        status, reply = self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {"schedule_created": 1_800_000_000.0, "schedule_end": 1_831_536_000.0}})
        self.assertEqual(status, 200, reply)
        view = self.account_view()
        self.assertEqual((view["schedule_created"], view["schedule_end"]), (1_800_000_000.0, 1_831_536_000.0))

    def test_a_schedule_result_without_dates_is_a_400_and_the_job_stays_open(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.assertEqual(self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {}})[0], 400)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job["id"]).status, "running")
            self.assertIsNone(opened.get(GoogleAccount, self.account_id).schedule_end)

    def test_the_view_gives_the_viewer_address_and_when_the_worker_was_last_seen(self):
        status, reply = self.call("GET", "/api/browser/view")
        self.assertEqual((status, reply), (200, {"vnc_url": "https://browser.example/vnc.html", "worker_last_seen": None}))
        self.worker("/internal/browser/jobs/claim")
        self.assertIsInstance(self.call("GET", "/api/browser/view")[1]["worker_last_seen"], float)


class ViewWithoutAViewerTests(ApiCase):
    extra_env = {"VNC_URL": "javascript:alert(1)"}

    def test_an_address_that_is_not_http_is_not_offered(self):
        self.assertEqual(self.call("GET", "/api/browser/view")[1]["vnc_url"], None)


class ViewMalformedTests(ApiCase):
    extra_env = {"VNC_URL": "https://[::1"}

    def test_a_malformed_address_is_unavailable_not_an_error(self):
        self.assertEqual(self.call("GET", "/api/browser/view"), (200, {"vnc_url": None, "worker_last_seen": None}))
        status, headers, _ = fetch(self.base, "GET", "/")
        self.assertNotIn(status, (500,))
        self.assertNotIn("frame-src", headers.get("Content-Security-Policy", ""))


class ViewUnsetTests(ApiCase):
    def test_no_address_is_null(self):
        self.assertEqual(self.call("GET", "/api/browser/view")[1], {"vnc_url": None, "worker_last_seen": None})


if __name__ == "__main__":
    unittest.main()
