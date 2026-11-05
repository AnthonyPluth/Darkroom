import json
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom import dates
from darkroom.domain import browser_jobs, dedupe, renewal
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, DedupeDecision, Export, GoogleAccount, MediaItem
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
        self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {"schedule_created": 100.0, "schedule_end": 4_000_000_000.0}})
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual(status, 409, reply)
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/connect")[0], 201)

    def test_a_schedule_that_has_ended_may_be_created_again_once(self):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {"schedule_created": 100.0, "schedule_end": 200.0}})
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 201)
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual(status, 409, reply)

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

    def unconfirmed_renewal(self, end):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account_id)
            account.schedule_created, account.schedule_end = end - 365 * 86400, end
            renewal.watch_schedules(opened, self.settings, dates.utc_now().timestamp())
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": "ScheduleNotConfirmed: check Takeout before trying again"})
        return job["id"]

    def test_a_renewal_takeout_has_is_recorded_through_the_check_and_shown_as_found(self):
        end = dates.utc_now().timestamp() + 20 * 86400
        job_id = self.unconfirmed_renewal(end)
        listed = {entry["id"]: entry for entry in self.call("GET", "/api/browser/jobs")[1]["jobs"]}
        self.assertEqual((listed[job_id]["needs_check"], listed[job_id]["checked"]), (True, None))
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": "yes"})[0], 400)
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})
        self.assertEqual((status, reply["job"]["id"], reply["job"]["needs_check"], reply["job"]["checked"]), (200, job_id, False, "found"))
        view = self.account_view()
        self.assertGreater(view["schedule_end"], end)
        self.assertEqual(view["schedule_end"], dates.one_year_after(view["schedule_created"]))
        with db.session(self.engine) as opened:
            kinds = {(alert.kind, alert.closed is None) for alert in opened.execute(select(Alert).where(Alert.account_id == self.account_id)).scalars() if alert.kind.startswith("schedule")}
        self.assertEqual(kinds, {("schedule_ending", False), ("schedule_renewal_unconfirmed", False), ("schedule_renewed", False)})
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})[0], 409)

    def test_a_renewal_takeout_does_not_have_is_reported_as_not_renewed(self):
        end = dates.utc_now().timestamp() + 20 * 86400
        job_id = self.unconfirmed_renewal(end)
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": False})
        self.assertEqual((status, reply["job"]["id"], reply["job"]["checked"]), (200, job_id, "missing"))
        self.assertEqual(self.account_view()["schedule_end"], end)
        with db.session(self.engine) as opened:
            open_kinds = {alert.kind for alert in opened.execute(select(Alert).where(Alert.account_id == self.account_id, Alert.closed.is_(None))).scalars()}
        self.assertIn("schedule_renewal_failed", open_kinds)
        self.assertNotIn("schedule_renewal_unconfirmed", open_kinds)
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 409)

    def test_a_found_renewal_that_would_not_extend_the_schedule_is_refused_with_409(self):
        job_id = self.unconfirmed_renewal(dates.utc_now().timestamp() + 20 * 86400)
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account_id).schedule_end = 9_000_000_000.0
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})
        self.assertEqual(status, 409)
        self.assertIn("must end after", reply["error"])
        self.assertEqual(self.account_view()["schedule_end"], 9_000_000_000.0)
        self.assertTrue(next(entry for entry in self.call("GET", "/api/browser/jobs")[1]["jobs"] if entry["id"] == job_id)["needs_check"])

    def unconfirmed_schedule(self, message="ScheduleNotConfirmed: check Takeout before trying again"):
        self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/failed", {"message": message})
        with db.session(self.engine) as opened:
            return job["id"], opened.get(BrowserJob, job["id"]).started

    def test_a_first_schedule_takeout_has_is_recorded_through_the_check_and_then_renewed_in_its_window(self):
        job_id, started = self.unconfirmed_schedule()
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})
        self.assertEqual((status, reply["job"]["id"], reply["job"]["needs_check"], reply["job"]["checked"]), (200, job_id, False, "found"))
        view = self.account_view()
        self.assertEqual((view["schedule_created"], view["schedule_end"]), (started, dates.one_year_after(started)))
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule")
        self.assertEqual(status, 409, reply)
        self.assertIn("already has an export schedule", reply["error"])
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})[0], 409)
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": False})[0], 409)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(Alert).where(Alert.kind.like("schedule%"))).scalars().all(), [])
            self.assertEqual(renewal.watch_schedules(opened, self.settings, view["schedule_end"] - 22 * 86400), 0)
            self.assertEqual(renewal.watch_schedules(opened, self.settings, view["schedule_end"] - 21 * 86400), 1)
            queued = opened.execute(select(BrowserJob).where(BrowserJob.kind == "renew")).scalar_one()
        self.assertEqual((queued.status, json.loads(queued.payload)["ending"]), ("queued", view["schedule_end"]))

    def test_a_first_schedule_found_after_a_later_end_is_refused_and_nothing_changes(self):
        job_id, _ = self.unconfirmed_schedule()
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account_id)
            account.schedule_created, account.schedule_end = 1.0, 9_000_000_000.0
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})
        self.assertEqual(status, 409)
        self.assertIn("must end after", reply["error"])
        view = self.account_view()
        self.assertEqual((view["schedule_created"], view["schedule_end"]), (1.0, 9_000_000_000.0))
        self.assertTrue(next(entry for entry in self.call("GET", "/api/browser/jobs")[1]["jobs"] if entry["id"] == job_id)["needs_check"])

    def test_a_plainly_failed_first_schedule_has_nothing_to_record(self):
        self.unconfirmed_schedule("UnexpectedPage: the page changed")
        status, reply = self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": True})
        self.assertEqual(status, 409, reply)
        view = self.account_view()
        self.assertEqual((view["schedule_created"], view["schedule_end"]), (None, None))
        self.assertEqual(self.call("POST", f"/api/accounts/{self.account_id}/schedule")[0], 201)

    def test_an_unconfirmed_first_schedule_keeps_the_email_until_it_is_answered(self):
        self.unconfirmed_schedule()
        status, reply = self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})
        self.assertEqual(status, 409)
        self.assertIn("Takeout check", reply["error"])
        self.call("POST", f"/api/accounts/{self.account_id}/schedule/checked", {"found": False})
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})[0], 200)

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

    def test_the_email_cannot_change_while_a_renew_job_is_open_and_can_afterwards(self):
        with db.session(self.engine) as opened:
            job_id = browser_jobs.queue(opened, self.account_id, browser_jobs.RENEW_KIND, {"email": "jo.photos@example.test"}, 1.0).id
        status, reply = self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})
        self.assertEqual(status, 409)
        self.assertIn("renewal", reply["error"])
        self.assertEqual(self.worker("/internal/browser/jobs/claim")[1]["job"]["id"], job_id)
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})[0], 409)
        self.worker(f"/internal/browser/jobs/{job_id}/failed", {"message": "UnexpectedPage"})
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})[0], 200)

    def test_a_rename_or_an_unchanged_default_person_does_not_scan_for_items_to_confirm(self):
        sam = self.person("Sam Example")["id"]
        with mock.patch.object(dedupe, "confirm_served_matches", wraps=dedupe.confirm_served_matches) as scan:
            self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"email": "renamed@example.test"})[0], 200)
            self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"default_person_id": self.person_id})[0], 200)
            self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {})[0], 200)
            scan.assert_not_called()
            self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"default_person_id": sam})[0], 200)
            scan.assert_called_once()

    def test_a_default_person_change_that_serves_a_matched_owner_confirms_their_items(self):
        sam = self.person("Sam Example")["id"]
        with db.session(self.engine) as opened:
            export = Export(account_id=self.account_id, kind="manual", owner_person_id=sam)
            phone = MediaItem(person_id=sam, source="phone", path="sam/a.jpg", sha1="a" * 40, bytes=1, indexed_at=1.0)
            opened.add_all([export, phone])
            opened.flush()
            opened.add(DedupeDecision(
                export_id=export.id, account_id=self.account_id, source_path="a.jpg", person_id=sam, matched_item_id=phone.id,
                tier=1, reason="same SHA-1", decided_by="auto", decided_at=1.0,
            ))
            phone_id = phone.id
        def confirmed():
            with db.session(self.engine) as opened:
                return opened.get(MediaItem, phone_id).confirmed_in_google

        self.assertFalse(confirmed())
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.account_id}", {"default_person_id": sam})[0], 200)
        self.assertTrue(confirmed())

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
