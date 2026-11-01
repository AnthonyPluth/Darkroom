import json
import tempfile
import threading
import unittest
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import alerts, browser_jobs
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Event, GoogleAccount
from tests.shared import add_person, own_database, settings_from


def add_account(opened, email="jo.takeout@example.test", slug="jo"):
    person = add_person(opened, slug)
    account = GoogleAccount(email=email, default_person_id=person, created=1.0)
    opened.add(account)
    opened.flush()
    return account.id


class BrowserJobTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        self.photos = Path(photos.name)
        self.settings = settings_from(DARKROOM_PUBLIC_URL="https://darkroom.example", VNC_URL="https://browser.example/vnc.html", DARKROOM_PHOTOS_ROOT=photos.name)
        with db.session(self.engine) as opened:
            self.account = add_account(opened)

    def enqueue(self, kind="noop", now=10.0):
        with db.session(self.engine) as opened:
            return browser_jobs.enqueue(opened, self.account, kind, {"n": 1}, now).id

    def write_capture(self, job_id, *names):
        folder = self.photos / "_browser" / str(self.account) / "failures" / str(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"x")

    def job(self, job_id):
        with db.session(self.engine) as opened:
            return opened.get(BrowserJob, job_id)

    def open_alerts(self):
        with db.session(self.engine) as opened:
            return [(alert.kind, alert.link, alert.closed) for alert in opened.execute(select(Alert)).scalars()]

    def test_enqueue_refuses_unknown_kinds_and_accounts(self):
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.UnknownKind):
                browser_jobs.enqueue(opened, self.account, "rm -rf", None, 1.0)
            with self.assertRaises(browser_jobs.UnknownKind):
                browser_jobs.enqueue(opened, self.account, "download", None, 1.0)
            with self.assertRaises(browser_jobs.UnknownAccount):
                browser_jobs.enqueue(opened, self.account + 7, "noop", None, 1.0)

    def test_claim_takes_the_oldest_queued_job_and_leases_it(self):
        first, second = self.enqueue(now=1.0), self.enqueue(now=2.0)
        with db.session(self.engine) as opened:
            claimed = browser_jobs.claim(opened, 5.0)
            self.assertEqual((claimed.id, claimed.status, claimed.started, claimed.heartbeat), (first, "running", 5.0, 5.0))
            self.assertEqual(browser_jobs.for_worker(claimed), {"id": first, "kind": "noop", "account": str(self.account), "payload": {"n": 1}})
            self.assertEqual(browser_jobs.claim(opened, 6.0).id, second)
            self.assertIsNone(browser_jobs.claim(opened, 7.0))

    def test_two_workers_never_hold_the_same_job(self):
        wanted = 6
        for _ in range(wanted):
            self.enqueue()
        claimed, errors = [], []
        start = threading.Barrier(8)

        def worker():
            start.wait()
            for _ in range(4):
                try:
                    with db.session(self.engine) as opened:
                        job = browser_jobs.claim(opened, 1.0)
                    if job is not None:
                        claimed.append(job.id)
                except Exception as error:
                    errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(claimed), wanted)
        self.assertEqual(len(set(claimed)), wanted)

    def test_heartbeat_progress_and_done(self):
        job_id, queued = self.enqueue(), self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.heartbeat(opened, job_id, 20.0)
            browser_jobs.progress(opened, job_id, "opening Takeout", 30.0)
            done = browser_jobs.done(opened, job_id, {"ok": True}, 40.0)
            self.assertEqual((done.status, done.finished, done.message), ("done", 40.0, "opening Takeout"))
            self.assertEqual(json.loads(done.result), {"ok": True})
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "ok")
            for action in (lambda: browser_jobs.heartbeat(opened, job_id, 41.0), lambda: browser_jobs.done(opened, job_id, {}, 41.0),
                           lambda: browser_jobs.failed(opened, self.settings, job_id, "x", None, [], 41.0)):
                with self.assertRaises(browser_jobs.NotOpen):
                    action()
            with self.assertRaises(browser_jobs.NoSuchJob):
                browser_jobs.heartbeat(opened, job_id + 50, 41.0)
            with self.assertRaises(browser_jobs.NotOpen):
                browser_jobs.heartbeat(opened, queued, 41.0)

    def test_waiting_opens_the_sign_in_alert_with_the_vnc_link_and_progress_closes_it(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            with self.assertRaises(browser_jobs.BadReason):
                browser_jobs.waiting(opened, self.settings, job_id, "lunch", 6.0)
            waiting = browser_jobs.waiting(opened, self.settings, job_id, "sign_in", 6.0)
            self.assertEqual((waiting.status, waiting.message, waiting.heartbeat), ("waiting_for_user", "sign_in", 6.0))
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "needs_sign_in")
        self.assertEqual(self.open_alerts(), [("sign_in_needed", "https://browser.example/vnc.html", None)])
        with db.session(self.engine) as opened:
            resumed = browser_jobs.progress(opened, job_id, "signed in", 60.0)
            self.assertEqual(resumed.status, "running")
        self.assertEqual(self.open_alerts(), [("sign_in_needed", "https://browser.example/vnc.html", 60.0)])

    def test_verify_waits_without_touching_the_session_state(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "verify", 6.0)
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "unknown")
            browser_jobs.done(opened, job_id, {}, 7.0)
        self.assertEqual(self.open_alerts()[0][2], 7.0)

    def test_failed_records_the_capture_and_alerts_with_a_link_to_it(self):
        job_id = self.enqueue()
        self.write_capture(job_id, "screenshot.png", "page.html")
        folder = browser_jobs.failure_folder(self.account, job_id)
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            with self.assertRaises(ValueError):
                browser_jobs.failed(opened, self.settings, job_id, "boom", "../../etc", ["page.html"], 6.0)
            with self.assertRaises(ValueError):
                browser_jobs.failed(opened, self.settings, job_id, "boom", folder, ["../page.html"], 6.0)
            with self.assertRaises(ValueError):
                browser_jobs.failed(opened, self.settings, job_id, "boom", None, ["page.html"], 6.0)
            failed = browser_jobs.failed(opened, self.settings, job_id, "TimeoutError", folder, ["page.html", "screenshot.png"], 6.0)
            self.assertEqual((failed.status, failed.message, failed.failure_dir), ("failed", "TimeoutError", f"_browser/{self.account}/failures/{job_id}"))
        self.assertEqual(self.open_alerts(), [("failure", f"https://darkroom.example/api/browser/failures/{job_id}/screenshot.png", 6.0)])
        with db.session(self.engine) as opened:
            alert = opened.execute(select(Alert)).scalar_one()
            self.assertEqual(alert.message, f"Browser job {job_id} (noop) for jo.takeout@example.test failed. Open Darkroom for details.")

    def test_a_capture_without_a_screenshot_links_to_the_page_snapshot(self):
        job_id = self.enqueue()
        self.write_capture(job_id, "page.html")
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            failed = browser_jobs.failed(opened, self.settings, job_id, "TimeoutError", browser_jobs.failure_folder(self.account, job_id), ["page.html"], 6.0)
            self.assertIsNotNone(failed.failure_dir)
        self.assertEqual(self.open_alerts(), [("failure", f"https://darkroom.example/api/browser/failures/{job_id}/page.html", 6.0)])

    def test_files_the_worker_names_but_did_not_write_are_not_linked(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            failed = browser_jobs.failed(opened, self.settings, job_id, "TimeoutError", browser_jobs.failure_folder(self.account, job_id), ["screenshot.png"], 6.0)
            self.assertIsNone(failed.failure_dir)
        self.assertEqual(self.open_alerts(), [("failure", None, 6.0)])

    def test_failed_without_a_capture_has_no_link(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            failed = browser_jobs.failed(opened, self.settings, job_id, "Crashed", None, [], 6.0)
            self.assertIsNone(failed.failure_dir)
        self.assertEqual(self.open_alerts(), [("failure", None, 6.0)])

    def test_lease_expiry_fails_the_job_and_alerts(self):
        job_id = self.enqueue()
        kept = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 100.0)
            browser_jobs.claim(opened, 100.0)
            browser_jobs.heartbeat(opened, kept, 250.0)
            self.assertEqual(browser_jobs.expire_leases(opened, self.settings, 100.0 + browser_jobs.LEASE_SECONDS), [])
            expired = browser_jobs.expire_leases(opened, self.settings, 101.0 + browser_jobs.LEASE_SECONDS)
            self.assertEqual([job.id for job in expired], [job_id])
        self.assertEqual(self.job(job_id).status, "failed")
        self.assertEqual(self.job(job_id).message, "the worker stopped reporting")
        self.assertEqual(self.job(kept).status, "running")
        self.assertEqual(self.open_alerts(), [("failure", None, 101.0 + browser_jobs.LEASE_SECONDS)])

    def test_a_waiting_job_whose_worker_died_expires_too(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 100.0)
            browser_jobs.waiting(opened, self.settings, job_id, "sign_in", 100.0)
            self.assertEqual(len(browser_jobs.expire_leases(opened, self.settings, 1000.0)), 1)
        self.assertEqual(self.job(job_id).status, "failed")

    def test_the_scheduler_expires_leases(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 100.0)
        scheduler = jobs.build(self.settings, self.engine, now=0.0)
        self.assertIn("browser.leases", scheduler.jobs)
        scheduler.run_one("browser.leases", 100.0 + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual(self.job(job_id).status, "failed")
        scheduler.run_one("browser.leases", 100.0 + browser_jobs.LEASE_SECONDS + 2)

    def test_list_jobs_newest_first_and_per_account(self):
        with db.session(self.engine) as opened:
            other = add_account(opened, "sam.takeout@example.test", "sam")
        mine = [self.enqueue(now=float(n)) for n in range(3)]
        with db.session(self.engine) as opened:
            theirs = browser_jobs.enqueue(opened, other, "noop", None, 9.0).id
            self.assertEqual([job.id for job in browser_jobs.list_jobs(opened, self.account)], mine[::-1])
            self.assertEqual([job.id for job in browser_jobs.list_jobs(opened, None)], [theirs, *mine[::-1]])
            self.assertEqual([job.id for job in browser_jobs.list_jobs(opened, None, limit=1)], [theirs])

    def test_worker_last_seen_round_trips(self):
        with db.session(self.engine) as opened:
            self.assertIsNone(browser_jobs.worker_last_seen(opened))
            browser_jobs.worker_seen(opened, 12.5)
            browser_jobs.worker_seen(opened, 13.5)
            self.assertEqual(browser_jobs.worker_last_seen(opened), 13.5)

    def test_events_follow_the_job(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.done(opened, job_id, {}, 6.0)
            types = [event.type for event in opened.execute(select(Event).order_by(Event.id)).scalars()]
        self.assertEqual(types, ["browser_job_queued", "browser_job_claimed", "browser_job_done"])
        self.assertEqual(alerts.failure("x").title, "Darkroom needs a look")


if __name__ == "__main__":
    unittest.main()
