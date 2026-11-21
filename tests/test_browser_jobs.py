import json
import tempfile
import threading
import unittest
from pathlib import Path

from sqlalchemy import event, select

from darkroom import config
from darkroom.domain import alerts, browser_jobs
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Event, GoogleAccount
from tests.browser import one_container
from tests.shared import VIEWER_SECRET, add_person, another_writer_holds_the_lock, impatient_engine, own_database, settings_from

RELAYED = config.relay_address("https://darkroom.example")


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
        self.settings = settings_from(DARKROOM_PUBLIC_URL="https://darkroom.example", viewer_secret=VIEWER_SECRET, DARKROOM_PHOTOS_ROOT=photos.name)
        with db.session(self.engine) as opened:
            self.account = add_account(opened)

    def enqueue(self, kind="noop", now=10.0, account=None):
        with db.session(self.engine) as opened:
            queued = browser_jobs.queue if kind in (*browser_jobs.GUARDED_KINDS, *browser_jobs.LONG_KINDS) else browser_jobs.enqueue
            return queued(opened, self.account if account is None else account, kind, {"n": 1}, now).id

    def another_account(self, name):
        with db.session(self.engine) as opened:
            return add_account(opened, f"{name}.takeout@example.test", name)

    def claimed(self, now=5.0, lane=browser_jobs.ANY_LANE):
        with db.session(self.engine) as opened:
            job = browser_jobs.claim(opened, now, lane)
            return None if job is None else job.id

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
            for guarded in ("connect", "schedule"):
                with self.assertRaises(browser_jobs.UnknownKind):
                    browser_jobs.enqueue(opened, self.account, guarded, None, 1.0)
            with self.assertRaises(browser_jobs.UnknownAccount):
                browser_jobs.enqueue(opened, self.account + 7, "noop", None, 1.0)

    def test_claim_takes_the_oldest_queued_job_and_leases_it(self):
        first, second = self.enqueue(now=1.0), self.enqueue(now=2.0, account=self.another_account("sam"))
        with db.session(self.engine) as opened:
            claimed = browser_jobs.claim(opened, 5.0)
            self.assertEqual((claimed.id, claimed.status, claimed.started, claimed.heartbeat), (first, "running", 5.0, 5.0))
            self.assertEqual(browser_jobs.for_worker(claimed), {"id": first, "kind": "noop", "account": str(self.account), "payload": {"n": 1}})
            self.assertEqual(browser_jobs.claim(opened, 6.0).id, second)
            self.assertIsNone(browser_jobs.claim(opened, 7.0))

    def test_two_workers_never_hold_the_same_job(self):
        wanted = 6
        for number in range(wanted):
            self.enqueue(account=self.another_account(f"p{number}"))
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

    def race(self, kind):
        made, refused, errors = [], [], []
        start = threading.Barrier(8)

        def request():
            start.wait()
            try:
                with db.session(self.engine) as opened:
                    made.append(browser_jobs.enqueue_once(opened, self.account, kind, 1.0).id)
            except browser_jobs.AlreadyOpen:
                refused.append(kind)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=request) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return errors, len(made), len(refused)

    def test_simultaneous_requests_queue_one_job_per_kind(self):
        for kind in ("connect", "schedule"):
            self.assertEqual(self.race(kind), ([], 1, 7), kind)

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
        self.assertEqual(self.open_alerts(), [("sign_in_needed", RELAYED, None)])
        with db.session(self.engine) as opened:
            resumed = browser_jobs.progress(opened, job_id, "signed in", 60.0)
            self.assertEqual(resumed.status, "running")
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.session_state, account.session_checked), ("ok", 60.0))
        self.assertEqual(self.open_alerts(), [("sign_in_needed", RELAYED, 60.0)])

    def test_verify_needs_sign_in_attention_and_done_sets_the_session_ok(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).session_state = "ok"
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "verify", 6.0)
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.session_state, account.session_checked), ("needs_sign_in", 6.0))
            browser_jobs.done(opened, job_id, {}, 7.0)
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "ok")
        self.assertEqual(self.open_alerts()[0][2], 7.0)

    def test_a_resume_after_verification_sets_the_session_ok(self):
        job_id = self.enqueue("connect")
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).session_state = "ok"
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "verify", 6.0)
            browser_jobs.progress(opened, job_id, "signed in", 8.0)
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.session_state, account.session_checked), ("ok", 8.0))

    def test_a_verification_that_times_out_leaves_needs_sign_in_with_connect_available(self):
        job_id = self.enqueue("connect")
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).session_state = "ok"
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "verify", 6.0)
        with db.session(self.engine) as opened:
            browser_jobs.failed(opened, self.settings, job_id, "SignInTimedOut: nobody signed in", None, [], 9.0)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "needs_sign_in")
            self.assertEqual(browser_jobs.enqueue_once(opened, self.account, "connect", 10.0).status, "queued")
        self.assertEqual([closed for kind, _, closed in self.open_alerts() if kind == "sign_in_needed"], [9.0])

    def test_progress_after_verifying_marks_the_session_ok(self):
        job_id = self.enqueue()
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "verify", 6.0)
            browser_jobs.progress(opened, job_id, "verified", 7.0)
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "ok")

    def test_a_schedule_job_records_when_it_was_created_and_when_it_ends(self):
        job_id = self.enqueue("schedule")
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.done(opened, job_id, {"schedule_created": 100.0, "schedule_end": 31_536_100}, 8.0)
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.schedule_created, account.schedule_end, account.session_state), (100.0, 31_536_100.0, "ok"))

    def test_a_schedule_job_without_proper_dates_is_refused_and_stays_open(self):
        job_id = self.enqueue("schedule")
        bad = ({}, {"schedule_created": 100.0}, {"schedule_created": "x", "schedule_end": 200.0}, {"schedule_created": True, "schedule_end": 200.0},
               {"schedule_created": 300.0, "schedule_end": 200.0}, {"schedule_created": 100.0, "schedule_end": float("inf")},
               {"schedule_created": float("nan"), "schedule_end": 200.0}, {"schedule_created": float("-inf"), "schedule_end": 200.0},
               {"schedule_created": 100.0, "schedule_end": 1e300})
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            for result in bad:
                with self.subTest(result=result), self.assertRaises(ValueError):
                    browser_jobs.done(opened, job_id, result, 8.0)
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.schedule_created, account.schedule_end, opened.get(BrowserJob, job_id).status), (None, None, "running"))

    def test_a_connect_job_leaves_the_schedule_alone(self):
        job_id = self.enqueue("connect")
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).schedule_end = 500.0
            browser_jobs.claim(opened, 5.0)
            browser_jobs.done(opened, job_id, {"session_state": "ok"}, 8.0)
            self.assertEqual(opened.get(GoogleAccount, self.account).schedule_end, 500.0)

    def test_enqueue_once_tells_the_worker_which_google_account_the_job_is_for(self):
        with db.session(self.engine) as opened:
            job = browser_jobs.enqueue_once(opened, self.account, "connect", 1.0)
            self.assertEqual(json.loads(job.payload), {"email": "jo.takeout@example.test"})
            self.assertEqual(browser_jobs.for_worker(job)["payload"], {"email": "jo.takeout@example.test"})

    def test_enqueue_once_refuses_a_second_open_job_of_the_same_kind_only(self):
        with db.session(self.engine) as opened:
            first = browser_jobs.enqueue_once(opened, self.account, "connect", 1.0)
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "connect", 2.0)
            browser_jobs.enqueue_once(opened, self.account, "schedule", 2.0)
            with self.assertRaises(browser_jobs.UnknownAccount):
                browser_jobs.enqueue_once(opened, self.account + 9, "connect", 2.0)
            first.status = "failed"
            opened.flush()
            browser_jobs.enqueue_once(opened, self.account, "connect", 3.0)

    def test_enqueue_once_refuses_a_second_schedule_once_one_exists_or_was_done(self):
        with db.session(self.engine) as opened:
            first = browser_jobs.enqueue_once(opened, self.account, "schedule", 1.0)
            first.status = "done"
            opened.flush()
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", 2.0)
            first.status = "failed"
            opened.flush()
            browser_jobs.enqueue_once(opened, self.account, "schedule", 3.0)
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).schedule_created = 5.0
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", 4.0)
            browser_jobs.enqueue_once(opened, self.account, "connect", 4.0)

    def test_a_wrong_account_wait_replaces_the_sign_in_alert_and_a_resume_closes_it(self):
        job_id = self.enqueue("connect")
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "sign_in", 6.0)
            browser_jobs.waiting(opened, self.settings, job_id, "wrong_account", 7.0)
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.session_state, opened.get(BrowserJob, job_id).message), ("needs_sign_in", "wrong_account"))
        self.assertEqual(self.open_alerts(), [("sign_in_needed", RELAYED, 7.0), ("wrong_account", RELAYED, None)])
        with db.session(self.engine) as opened:
            self.assertIn("jo.takeout@example.test", opened.execute(select(Alert).where(Alert.kind == "wrong_account")).scalar_one().message)
            browser_jobs.waiting(opened, self.settings, job_id, "wrong_account", 8.0)
            browser_jobs.progress(opened, job_id, "signed in", 9.0)
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "ok")
        self.assertEqual([closed for _, _, closed in self.open_alerts()], [7.0, 9.0])

    def fail_schedule(self, message, now=20.0):
        job_id = self.enqueue("schedule")
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, now)
            browser_jobs.failed(opened, self.settings, job_id, message, None, [], now + 1)
        return job_id

    def test_an_unconfirmed_schedule_blocks_a_retry_until_the_person_says_they_checked(self):
        self.fail_schedule("ScheduleNotConfirmed: Takeout did not confirm a recurring export; check Takeout before trying again")
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.AlreadyOpen) as caught:
                browser_jobs.enqueue_once(opened, self.account, "schedule", 30.0)
            self.assertIn("check Takeout", str(caught.exception))
            browser_jobs.enqueue_once(opened, self.account, "connect", 30.0)
            checked = browser_jobs.mark_schedule_checked(opened, self.account, 31.0)
            self.assertTrue(checked.message.startswith("ScheduleNotConfirmed"))
            self.assertEqual(json.loads(checked.result), {"checked": True})
            with self.assertRaises(browser_jobs.NothingToCheck):
                browser_jobs.mark_schedule_checked(opened, self.account, 32.0)
            self.assertEqual(browser_jobs.enqueue_once(opened, self.account, "schedule", 33.0).status, "queued")

    def test_other_schedule_failures_may_be_retried_at_once(self):
        self.fail_schedule("UnexpectedPage: the page stayed unrecognised for more than 5 minutes")
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.NothingToCheck):
                browser_jobs.mark_schedule_checked(opened, self.account, 31.0)
            self.assertEqual(browser_jobs.enqueue_once(opened, self.account, "schedule", 33.0).status, "queued")

    def test_an_unconfirmed_schedule_of_one_account_does_not_block_another(self):
        self.fail_schedule("ScheduleNotConfirmed: check Takeout")
        with db.session(self.engine) as opened:
            other = add_account(opened, "other.takeout@example.test", "sam")
            self.assertEqual(browser_jobs.enqueue_once(opened, other, "schedule", 33.0).status, "queued")
            with self.assertRaises(browser_jobs.UnknownAccount):
                browser_jobs.mark_schedule_checked(opened, other + 9, 1.0)

    def test_a_schedule_job_whose_worker_vanished_may_have_created_the_export(self):
        job_id = self.enqueue("schedule", now=1.0)
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.expire_leases(opened, self.settings, 5.0 + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual(self.job(job_id).message, browser_jobs.EXPIRED_MESSAGE)
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", 500.0)

    def test_a_waiting_job_that_fails_closes_the_sign_in_alert_and_frees_the_account_to_reconnect(self):
        job_id = self.enqueue("connect")
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.waiting(opened, self.settings, job_id, "sign_in", 6.0)
        with db.session(self.engine) as opened:
            browser_jobs.failed(opened, self.settings, job_id, "SignInTimedOut", None, [], 7.0)
        kinds = {(kind, closed is not None) for kind, _, closed in self.open_alerts()}
        self.assertEqual(kinds, {("sign_in_needed", True), ("failure", True)})
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account).session_state, "needs_sign_in")
            self.assertEqual(browser_jobs.enqueue_once(opened, self.account, "connect", 8.0).status, "queued")

    def test_a_running_job_that_fails_leaves_other_sign_in_alerts_alone(self):
        job_id = self.enqueue("connect")
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, 5.0)
            browser_jobs.failed(opened, self.settings, job_id, "UnexpectedPage", None, [], 7.0)
        self.assertEqual([kind for kind, _, _ in self.open_alerts()], ["failure"])

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
        kept = self.enqueue(account=self.another_account("sam"))
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

    def test_claim_skips_an_account_with_a_running_or_waiting_job_and_takes_another_accounts(self):
        for status in browser_jobs.OPEN_STATUSES:
            with self.subTest(status=status):
                with db.session(self.engine) as opened:
                    opened.query(BrowserJob).delete()
                    mine = add_account(opened, f"a{status.replace('_', '')}@example.test", f"a{status.replace('_', '')}")
                    theirs = add_account(opened, f"b{status.replace('_', '')}@example.test", f"b{status.replace('_', '')}")
                open_job = self.enqueue("download", now=1.0, account=mine)
                self.assertEqual(self.claimed(), open_job)
                if status == "waiting_for_user":
                    with db.session(self.engine) as opened:
                        browser_jobs.waiting(opened, self.settings, open_job, "sign_in", 6.0)
                behind = self.enqueue("status", now=2.0, account=mine)
                self.assertIsNone(self.claimed(7.0), "never two jobs for one account at once")
                other = self.enqueue("status", now=3.0, account=theirs)
                self.assertEqual(self.claimed(8.0), other, "another account's job starts at once")
                self.assertIsNone(self.claimed(9.0))
                with db.session(self.engine) as opened:
                    browser_jobs.done(opened, open_job, {}, 10.0)
                self.assertEqual(self.claimed(11.0), behind, "the account's queued job starts once its open job has ended")

    def test_a_job_waiting_days_for_a_person_does_not_hold_other_accounts(self):
        waiting = self.enqueue("download", now=1.0)
        self.assertEqual(self.claimed(2.0), waiting)
        with db.session(self.engine) as opened:
            browser_jobs.waiting(opened, self.settings, waiting, "verify", 3.0)
        later = 3.0 + 47 * 3600.0
        others = [self.enqueue(kind, now=4.0, account=self.another_account(f"w{number}")) for number, kind in enumerate(("connect", "download"))]
        with db.session(self.engine) as opened:
            browser_jobs.heartbeat(opened, waiting, later)
        self.assertEqual([self.claimed(later), self.claimed(later)], others)
        self.assertEqual(self.job(waiting).status, "waiting_for_user")

    def test_claim_skips_an_account_whose_lease_just_expired_until_the_lease_window_has_passed(self):
        lost = self.enqueue(now=1.0)
        self.assertEqual(self.claimed(100.0), lost)
        with db.session(self.engine) as opened:
            expired_at = 100.0 + browser_jobs.LEASE_SECONDS + 1
            self.assertEqual([job.id for job in browser_jobs.expire_leases(opened, self.settings, expired_at)], [lost])
            self.assertEqual(browser_jobs.holds_profile(opened, self.account, expired_at + 1), "lease_expired")
        behind = self.enqueue(now=2.0)
        other = self.enqueue(now=3.0, account=self.another_account("sam"))
        self.assertEqual(self.claimed(expired_at + 1), other)
        self.assertIsNone(self.claimed(expired_at + browser_jobs.LEASE_SECONDS - 1), "its Chrome may still be open until the worker's watchdog fires")
        self.assertEqual(self.claimed(expired_at + browser_jobs.LEASE_SECONDS + 1), behind)

    def test_after_a_worker_is_killed_its_account_resumes_inside_the_one_container_checks_wait_and_others_at_once(self):
        budget = one_container.resume_seconds()
        poll = jobs.SCHEDULER_POLL_SECONDS
        for started in (0.0, 1.0, poll / 2, poll - 1):
            with self.subTest(scheduler_started=started):
                account = self.another_account(f"held{int(started)}")
                scheduler = jobs.Scheduler()

                def expire(at):
                    with db.session(self.engine) as opened:
                        browser_jobs.expire_leases(opened, self.settings, at)

                scheduler.every("browser.leases", jobs.BROWSER_LEASE_TICK_SECONDS, expire, now=started)
                killed_at = 1000.0
                killed = self.enqueue("connect", now=killed_at - 5, account=account)
                self.assertEqual(self.claimed(killed_at), killed, "the worker claims the job and is killed under its browser right after")
                held = self.enqueue(now=killed_at + 1, account=account)
                other = self.enqueue(now=killed_at + 1, account=self.another_account(f"other{int(started)}"))
                self.assertEqual(self.claimed(killed_at + 2), other, "another account's job is not held by the killed worker's")
                resumed = None
                for second in range(int(killed_at) + 2, int(killed_at + budget) + 1):
                    if (second - started) % poll == 0:
                        scheduler.run_due(float(second))
                    if self.claimed(float(second)) == held:
                        resumed = float(second)
                        break
                self.assertEqual((self.job(killed).status, self.job(killed).message), ("failed", browser_jobs.EXPIRED_MESSAGE))
                self.assertIsNotNone(resumed, f"the account's next job did not start within {budget} s of the kill")
                self.assertGreaterEqual(resumed, killed_at + 2 * browser_jobs.LEASE_SECONDS, "the lease and the hold after it both ran out first")

    def test_finished_jobs_and_other_failures_do_not_hold_the_account(self):
        for status, message in (("done", ""), ("failed", "TimeoutError")):
            with self.subTest(status=status):
                with db.session(self.engine) as opened:
                    opened.query(BrowserJob).delete()
                ended = self.enqueue(now=1.0)
                with db.session(self.engine) as opened:
                    job = opened.get(BrowserJob, ended)
                    job.status, job.message, job.finished = status, message, 100.0
                queued = self.enqueue(now=2.0)
                self.assertEqual(self.claimed(101.0), queued)

    def test_claim_puts_interactive_kinds_before_long_ones_then_takes_the_oldest(self):
        self.assertEqual(browser_jobs.LONG_KINDS, ("download",))
        accounts = [self.another_account(f"k{number}") for number in range(5)]
        download = self.enqueue("download", now=1.0, account=accounts[0])
        later_download = self.enqueue("download", now=2.0, account=accounts[1])
        status = self.enqueue("status", now=3.0, account=accounts[2])
        connect = self.enqueue("connect", now=4.0, account=accounts[3])
        noop = self.enqueue("noop", now=5.0, account=accounts[4])
        self.assertEqual([self.claimed() for _ in range(6)], [status, connect, noop, download, later_download, None])

    def test_every_interactive_kind_comes_before_a_download(self):
        for kind in ("connect", "status", "schedule", "renew", "noop"):
            with self.subTest(kind=kind):
                with db.session(self.engine) as opened:
                    opened.query(BrowserJob).delete()
                download = self.enqueue("download", now=1.0, account=self.another_account(f"d{kind}"))
                interactive = self.enqueue(kind, now=2.0, account=self.another_account(f"i{kind}"))
                self.assertEqual([self.claimed(), self.claimed()], [interactive, download])

    def test_the_interactive_lane_takes_no_download_and_the_other_lane_does(self):
        download = self.enqueue("download", now=1.0)
        self.assertIsNone(self.claimed(lane=browser_jobs.INTERACTIVE_LANE))
        connect = self.enqueue("connect", now=2.0, account=self.another_account("sam"))
        self.assertEqual(self.claimed(lane=browser_jobs.INTERACTIVE_LANE), connect)
        self.assertIsNone(self.claimed(lane=browser_jobs.INTERACTIVE_LANE))
        self.assertEqual(self.job(download).status, "queued")
        self.assertEqual(self.claimed(lane=browser_jobs.ANY_LANE), download)
        with db.session(self.engine) as opened:
            self.assertIsNone(browser_jobs.claim(opened, 6.0))

    def test_an_unknown_lane_is_refused_and_claims_nothing(self):
        queued = self.enqueue()
        with db.session(self.engine) as opened:
            with self.assertRaisesRegex(browser_jobs.UnknownLane, "not a claim lane"):
                browser_jobs.claim(opened, 5.0, "downloads-only")
        self.assertEqual(self.job(queued).status, "queued")

    def test_a_download_waits_only_for_the_interactive_jobs_ahead_of_it(self):
        statuses = [self.enqueue("status", now=float(number), account=self.another_account(f"s{number}")) for number in range(3)]
        download = self.enqueue("download", now=0.5, account=self.another_account("big"))
        self.assertEqual([self.claimed(lane=browser_jobs.INTERACTIVE_LANE), self.claimed(), self.claimed()], statuses)
        self.assertEqual(self.job(download).status, "queued")
        self.assertEqual(self.claimed(), download, "the next free lane that may take a download takes it")

    def test_two_claimers_racing_for_one_account_get_one_job_between_them(self):
        accounts = [self.another_account(f"r{number}") for number in range(3)]
        for account in accounts:
            self.enqueue("download", account=account)
            self.enqueue("status", account=account)
        claimed, errors = [], []
        start = threading.Barrier(8)

        def worker(lane):
            start.wait()
            for _ in range(4):
                try:
                    with db.session(self.engine) as opened:
                        job = browser_jobs.claim(opened, 1.0, lane)
                        if job is not None:
                            claimed.append((job.id, job.account_id))
                except browser_jobs.Busy:
                    pass
                except Exception as error:
                    errors.append(error)

        threads = [threading.Thread(target=worker, args=(browser_jobs.INTERACTIVE_LANE if number % 2 else browser_jobs.ANY_LANE,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(account for _, account in claimed), sorted(accounts), "one job per account, each handed out once")
        self.assertEqual(len({job for job, _ in claimed}), len(claimed))

    def test_a_claim_holds_the_lock_from_its_first_statement_so_a_second_claimer_waits(self):
        self.enqueue("download", now=1.0)
        status = self.enqueue("status", now=2.0)
        seen = []

        def claim_elsewhere():
            try:
                with db.session(impatient_engine(self, self.engine, 0.1)) as opened:
                    return "claimed" if browser_jobs.claim(opened, 5.0, browser_jobs.INTERACTIVE_LANE) is not None else "nothing"
            except browser_jobs.Busy:
                return "locked"

        def claim_meanwhile(conn, cursor, statement, parameters, context, executemany):
            if not seen:
                seen.append(claim_elsewhere())

        event.listen(self.engine, "after_cursor_execute", claim_meanwhile)
        try:
            with db.session(self.engine) as opened:
                self.assertEqual(browser_jobs.claim(opened, 5.0).id, status)
        finally:
            event.remove(self.engine, "after_cursor_execute", claim_meanwhile)
        self.assertEqual(seen, ["locked"])
        self.assertIsNone(self.claimed(6.0, browser_jobs.INTERACTIVE_LANE))

    def test_the_job_on_screen_is_the_last_one_the_worker_raised_while_it_still_waits(self):
        sam = self.another_account("sam")
        first, second = self.enqueue("connect", now=1.0), self.enqueue("connect", now=2.0, account=sam)
        self.assertEqual([self.claimed(), self.claimed()], [first, second])
        with db.session(self.engine) as opened:
            self.assertIsNone(browser_jobs.job_on_screen(opened))
            for job_id in (first, second):
                browser_jobs.waiting(opened, self.settings, job_id, "sign_in", 6.0)
            browser_jobs.on_screen(opened, first, 7.0)
            self.assertEqual(browser_jobs.job_on_screen(opened), first)
            self.assertEqual(opened.get(BrowserJob, first).heartbeat, 7.0)
            browser_jobs.progress(opened, first, "signed in", 8.0)
            self.assertIsNone(browser_jobs.job_on_screen(opened), "a job that resumed is not on screen for anyone")
            browser_jobs.on_screen(opened, second, 9.0)
            self.assertEqual(browser_jobs.job_on_screen(opened), second)
            opened.delete(opened.get(BrowserJob, second))
            opened.flush()
            self.assertIsNone(browser_jobs.job_on_screen(opened))
            browser_jobs.done(opened, first, {}, 10.0)
            with self.assertRaises(browser_jobs.NotOpen):
                browser_jobs.on_screen(opened, first, 11.0)

    def test_a_claim_that_cannot_get_the_write_lock_is_busy_and_claims_nothing(self):
        queued = self.enqueue()
        impatient = impatient_engine(self, self.engine, 0.05)
        with another_writer_holds_the_lock(self.engine):
            with db.session(impatient) as opened:
                with self.assertRaisesRegex(browser_jobs.Busy, "try again in a moment"):
                    browser_jobs.claim(opened, 5.0)
        self.assertEqual(self.job(queued).status, "queued")


if __name__ == "__main__":
    unittest.main()
