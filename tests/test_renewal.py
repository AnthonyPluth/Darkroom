import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import accounts, alerts, browser_jobs, renewal
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Event, GoogleAccount
from tests.shared import add_person, own_database, settings_from

HOUR = 3600.0
DAY = 24 * HOUR
CREATED = 1_800_000_000.0
END = CREATED + 365 * DAY
NOTICE_AT = END - 21 * DAY


class RenewalCase(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = Path(folder.name)
        self.settings = settings_from(DARKROOM_PUBLIC_URL="https://darkroom.example", DARKROOM_PHOTOS_ROOT=folder.name, TZ="UTC")
        with db.session(self.engine) as opened:
            self.account = self.add_account(opened, "jo", "jo.takeout@example.test", CREATED, END)

    def add_account(self, opened, slug, email, created=None, end=None):
        person = add_person(opened, slug)
        account = GoogleAccount(email=email, default_person_id=person, created=1.0, session_state="ok", schedule_created=created, schedule_end=end)
        opened.add(account)
        opened.flush()
        return account.id

    def watch(self, now):
        with db.session(self.engine) as opened:
            return renewal.watch_schedules(opened, self.settings, now)

    def tick(self, now):
        with db.session(self.engine) as opened:
            return alerts.tick(opened, self.settings, now)

    def renew_jobs(self):
        with db.session(self.engine) as opened:
            return [(job.account_id, job.status, json.loads(job.payload)) for job in opened.execute(select(BrowserJob).where(BrowserJob.kind == "renew").order_by(BrowserJob.id)).scalars()]

    def alerts_of(self, kind):
        with db.session(self.engine) as opened:
            return [(alert.key, alert.message, alert.link, alert.sent_count, alert.closed) for alert in opened.execute(select(Alert).where(Alert.kind == kind).order_by(Alert.id)).scalars()]

    def event_types(self):
        with db.session(self.engine) as opened:
            return [event.type for event in opened.execute(select(Event).order_by(Event.id)).scalars()]

    def claim_renewal(self, now):
        with db.session(self.engine) as opened:
            job = browser_jobs.claim(opened, now)
            assert job is not None and job.kind == "renew"
            return job.id

    def finish(self, job_id, now, created=None, end=None):
        created = now if created is None else created
        with db.session(self.engine) as opened:
            job = browser_jobs.done(opened, job_id, {"schedule_created": created, "schedule_end": created + 365 * DAY if end is None else end}, now)
            renewal.renewed(opened, self.settings, job, now)


class RenewalTimingTests(RenewalCase):
    def test_nothing_happens_until_three_weeks_before_the_schedule_ends(self):
        self.assertEqual(self.watch(NOTICE_AT - 1), 0)
        self.assertEqual(self.alerts_of("schedule_ending"), [])
        self.assertEqual(self.renew_jobs(), [])

    def test_at_twenty_one_days_the_ending_alert_opens_and_one_renew_job_is_queued(self):
        self.assertEqual(self.watch(NOTICE_AT), 1)
        ((key, message, link, sent, closed),) = self.alerts_of("schedule_ending")
        self.assertEqual((key, sent, closed, link), (renewal.ending_key(self.account), 1, None, "https://darkroom.example/exports"))
        self.assertIn("jo.takeout@example.test", message)
        self.assertIn("15 Jan 2028", message)
        self.assertEqual(self.renew_jobs(), [(self.account, "queued", {"email": "jo.takeout@example.test", "ending": END})])
        self.assertEqual(self.event_types().count("schedule_renewal_queued"), 1)

    def test_every_later_daily_run_adds_neither_a_job_nor_a_second_alert(self):
        self.watch(NOTICE_AT)
        for days in range(1, 40):
            self.assertEqual(self.watch(NOTICE_AT + days * DAY), 0)
        self.assertEqual(len(self.renew_jobs()), 1)
        self.assertEqual(len(self.alerts_of("schedule_ending")), 1)

    def test_the_ending_alert_repeats_after_a_day_and_after_four_days(self):
        self.watch(NOTICE_AT)
        self.assertEqual(self.tick(NOTICE_AT + DAY - 1), 0)
        self.assertEqual(self.tick(NOTICE_AT + 120 + DAY), 1)
        self.assertEqual(self.tick(NOTICE_AT + 120 + 4 * DAY), 1)
        self.assertEqual(self.tick(NOTICE_AT + 30 * DAY), 0)
        self.assertEqual(self.alerts_of("schedule_ending")[0][3], 3)

    def test_an_account_without_a_schedule_is_left_alone_and_each_account_has_its_own_job(self):
        with db.session(self.engine) as opened:
            self.add_account(opened, "kim", "kim.takeout@example.test")
            other = self.add_account(opened, "lee", "lee.takeout@example.test", CREATED, END + 30 * DAY)
        self.assertEqual(self.watch(NOTICE_AT + DAY), 1)
        self.assertEqual([job[0] for job in self.renew_jobs()], [self.account])
        self.assertEqual(self.watch(NOTICE_AT + 31 * DAY), 1)
        self.assertEqual([job[0] for job in self.renew_jobs()], [self.account, other])

    def test_a_schedule_that_has_already_ended_is_still_renewed(self):
        self.assertEqual(self.watch(END + 5 * DAY), 1)
        self.assertEqual(len(self.renew_jobs()), 1)


class RenewalDoneTests(RenewalCase):
    def renewed_at(self, now):
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 60)
        self.finish(job_id, now)
        return job_id

    def test_a_done_renewal_records_the_new_schedule_and_confirms_it(self):
        self.renewed_at(NOTICE_AT + 300)
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.schedule_created, account.schedule_end), (NOTICE_AT + 300, NOTICE_AT + 300 + 365 * DAY))
        ((_, message, link, sent, closed),) = self.alerts_of("schedule_renewed")
        self.assertIn("24 Dec 2028", message)
        self.assertEqual((link, sent, closed), (None, 1, NOTICE_AT + 300))
        ((_, _, _, _, ending_closed),) = self.alerts_of("schedule_ending")
        self.assertEqual(ending_closed, NOTICE_AT + 300)
        self.assertIn("schedule_renewed", self.event_types())

    def test_after_the_renewal_nothing_more_is_queued_until_the_next_window(self):
        self.renewed_at(NOTICE_AT + 300)
        new_end = NOTICE_AT + 300 + 365 * DAY
        for days in range(1, 300):
            self.assertEqual(self.watch(NOTICE_AT + 300 + days * DAY), 0)
        self.assertEqual(len(self.renew_jobs()), 1)
        self.assertEqual(self.tick(NOTICE_AT + 400 * DAY), 0)
        self.assertEqual(self.watch(new_end - 21 * DAY), 1)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["done", "queued"])
        self.assertEqual(len(self.alerts_of("schedule_ending")), 2)

    def test_a_renewal_that_does_not_extend_the_schedule_is_refused_and_stays_open(self):
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            for end in (END, END - DAY):
                with self.subTest(end=end), self.assertRaises(ValueError):
                    browser_jobs.done(opened, job_id, {"schedule_created": NOTICE_AT, "schedule_end": end}, NOTICE_AT + 70)
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.schedule_created, account.schedule_end, opened.get(BrowserJob, job_id).status), (CREATED, END, "running"))
        self.assertEqual(self.alerts_of("schedule_renewed"), [])
        self.assertEqual(self.alerts_of("schedule_ending")[0][4], None)

    def test_a_renewal_result_needs_proper_dates(self):
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            for result in ({}, {"schedule_created": NOTICE_AT}, {"schedule_created": True, "schedule_end": END + DAY}):
                with self.subTest(result=result), self.assertRaises(ValueError):
                    browser_jobs.done(opened, job_id, result, NOTICE_AT + 70)


class RenewalFailureTests(RenewalCase):
    def fail(self, now, files=("screenshot.png",), message="UnexpectedPage: the page changed"):
        job_id = self.claim_renewal(now)
        folder = self.photos / "_browser" / str(self.account) / "failures" / str(job_id)
        folder.mkdir(parents=True)
        for name in files:
            (folder / name).write_bytes(b"x")
        with db.session(self.engine) as opened:
            job = browser_jobs.failed(opened, self.settings, job_id, message, browser_jobs.failure_folder(self.account, job_id), list(files), now)
            renewal.job_failed(opened, self.settings, job, now)
        return job_id

    def test_a_failed_renewal_closes_the_ending_alert_and_says_the_renewal_did_not_happen(self):
        self.watch(NOTICE_AT)
        job_id = self.fail(NOTICE_AT + 120)
        ((_, ending, _, _, closed),) = self.alerts_of("schedule_ending")
        self.assertIsNotNone(closed)
        self.assertIn("will be renewed shortly", ending)
        ((key, message, link, sent, still_open),) = self.alerts_of("schedule_renewal_failed")
        self.assertEqual((key, link, sent, still_open), (renewal.failed_key(self.account), "https://darkroom.example/exports", 1, None))
        self.assertIn("jo.takeout@example.test", message)
        self.assertIn("15 Jan 2028", message)
        self.assertIn("failed", message)
        self.assertIn("does not retry", message)
        self.assertIn("not available yet", message)
        self.assertNotIn("shortly", message)
        ((failure_key, _, failure_link, _, _),) = self.alerts_of("failure")
        self.assertEqual((failure_key, failure_link), (f"browser:failed:{job_id}", f"https://darkroom.example/api/browser/failures/{job_id}/screenshot.png"))
        self.assertEqual(self.alerts_of("schedule_renewed"), [])
        self.assertEqual(self.event_types().count("schedule_renewal_failed"), 1)
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.schedule_created, account.schedule_end), (CREATED, END))

    def test_an_unconfirmed_renewal_says_it_could_not_be_confirmed_not_that_it_failed(self):
        self.watch(NOTICE_AT)
        job_id = self.fail(NOTICE_AT + 120, message="ScheduleNotConfirmed: Takeout did not confirm a recurring export")
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])
        ((key, message, link, sent, closed),) = self.alerts_of("schedule_renewal_unconfirmed")
        self.assertEqual((key, sent, closed), (renewal.failed_key(self.account), 1, None))
        self.assertEqual(link, f"https://darkroom.example/api/browser/failures/{job_id}/screenshot.png")
        self.assertIn("could not confirm", message)
        self.assertIn("15 Jan 2028", message)
        self.assertIn("Takeout", message)
        self.assertIn("will not create a second", message)
        self.assertNotIn("did not happen", message)
        self.assertIsNotNone(self.alerts_of("schedule_ending")[0][4])
        self.assertEqual(self.event_types().count("schedule_renewal_unconfirmed"), 1)
        self.assertEqual(self.event_types().count("schedule_renewal_failed"), 0)

    def test_an_unconfirmed_renewal_without_a_capture_links_the_exports_page(self):
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            job = browser_jobs.failed(opened, self.settings, job_id, "ScheduleNotConfirmed: unsure", None, [], NOTICE_AT + 120)
            renewal.job_failed(opened, self.settings, job, NOTICE_AT + 120)
        self.assertEqual(self.alerts_of("schedule_renewal_unconfirmed")[0][2], "https://darkroom.example/exports")

    def test_the_daily_watch_keeps_an_unconfirmed_renewal_alert_and_never_queues_another_renewal(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120, message="ScheduleNotConfirmed: unsure")
        for days in range(1, 20):
            self.assertEqual(self.watch(NOTICE_AT + days * DAY), 0)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertEqual(len(self.alerts_of("schedule_renewal_unconfirmed")), 1)
        self.assertEqual(len(self.alerts_of("schedule_ending")), 1)
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])
        self.assertEqual(self.tick(NOTICE_AT + 120 + DAY), 1)

    def test_an_unconfirmed_renewal_that_was_in_fact_renewed_closes_the_alert(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120, message="ScheduleNotConfirmed: unsure")
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).schedule_end = END + 365 * DAY
            job = opened.execute(select(BrowserJob).where(BrowserJob.kind == "renew")).scalar_one()
            renewal.renewed(opened, self.settings, job, NOTICE_AT + 500)
        self.assertIsNotNone(self.alerts_of("schedule_renewal_unconfirmed")[0][4])

    def test_a_lost_worker_leaves_the_renewal_unconfirmed(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        jobs.build(self.settings, self.engine, NOTICE_AT + 60).run_one("browser.leases", NOTICE_AT + 60 + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])
        self.assertEqual(len(self.alerts_of("schedule_renewal_unconfirmed")), 1)

    def test_a_failed_renewal_is_not_queued_again_and_the_failed_alert_keeps_repeating_while_the_ending_alert_stays_closed(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        for days in range(1, 20):
            self.assertEqual(self.watch(NOTICE_AT + days * DAY), 0)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertEqual(self.tick(NOTICE_AT + 120 + DAY), 1)
        self.assertEqual(self.tick(NOTICE_AT + 120 + 4 * DAY), 1)
        self.assertEqual(self.alerts_of("schedule_renewal_failed")[0][3:], (3, None))
        self.assertEqual(len(self.alerts_of("schedule_renewal_failed")), 1)
        self.assertEqual(len(self.alerts_of("schedule_ending")), 1)
        self.assertIsNotNone(self.alerts_of("schedule_ending")[0][4])

    def test_a_failed_renewal_alert_is_opened_again_by_the_daily_watch_if_it_was_closed(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        with db.session(self.engine) as opened:
            alerts.close(opened, renewal.failed_key(self.account), NOTICE_AT + 200)
        self.watch(NOTICE_AT + DAY)
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_renewal_failed")], [False, True])
        self.assertIsNotNone(self.alerts_of("schedule_ending")[0][4])

    def test_a_worker_that_vanished_mid_renewal_fails_the_job_the_same_way(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        scheduler = jobs.build(self.settings, self.engine, NOTICE_AT + 60)
        scheduler.run_one("browser.leases", NOTICE_AT + 60 + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertIsNotNone(self.alerts_of("schedule_ending")[0][4])
        self.assertIsNone(self.alerts_of("schedule_renewal_unconfirmed")[0][4])
        self.assertEqual(self.watch(NOTICE_AT + DAY), 0)

    def test_another_kind_of_failure_leaves_the_ending_alert_alone(self):
        self.watch(NOTICE_AT)
        with db.session(self.engine) as opened:
            other = browser_jobs.queue(opened, self.account, "status", {}, NOTICE_AT + 10)
            other.status = "failed"
            renewal.job_failed(opened, self.settings, other, NOTICE_AT + 30)
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])
        self.assertIsNone(self.alerts_of("schedule_ending")[0][4])

    def test_a_renewal_that_is_still_queued_or_running_keeps_the_ending_alert_open(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        self.watch(NOTICE_AT + DAY)
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])
        self.assertIsNone(self.alerts_of("schedule_ending")[0][4])

    def test_a_renewal_that_fails_in_a_schedule_without_an_end_says_nothing(self):
        with db.session(self.engine) as opened:
            job = browser_jobs.queue(opened, self.account, browser_jobs.RENEW_KIND, {}, NOTICE_AT)
            opened.get(GoogleAccount, self.account).schedule_end = None
            renewal.job_failed(opened, self.settings, job, NOTICE_AT)
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])


class RenewalEmailGuardTests(RenewalCase):
    def open_job(self):
        with db.session(self.engine) as opened:
            return browser_jobs.has_open_onboarding_job(opened, self.account)

    def test_a_queued_or_running_renew_job_blocks_changing_the_accounts_email(self):
        self.assertFalse(self.open_job())
        self.watch(NOTICE_AT)
        self.assertTrue(self.open_job())
        self.claim_renewal(NOTICE_AT + 60)
        self.assertTrue(self.open_job())
        with db.session(self.engine) as opened:
            with self.assertRaises(accounts.JobOpen):
                accounts.update(opened, self.account, "other.takeout@example.test", None, NOTICE_AT + 200)
            self.assertEqual(opened.get(GoogleAccount, self.account).email, "jo.takeout@example.test")

    def test_a_renew_job_waiting_for_a_sign_in_blocks_it_too(self):
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            browser_jobs.waiting(opened, self.settings, job_id, "sign_in", NOTICE_AT + 70)
        self.assertTrue(self.open_job())

    def test_the_email_can_change_once_the_renew_job_is_done(self):
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 60)
        self.finish(job_id, NOTICE_AT + 120)
        self.assertFalse(self.open_job())
        with db.session(self.engine) as opened:
            accounts.update(opened, self.account, "other.takeout@example.test", None, NOTICE_AT + 200)
            self.assertEqual(opened.get(GoogleAccount, self.account).email, "other.takeout@example.test")

    def test_the_email_can_change_once_the_renew_job_has_failed(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            job = opened.execute(select(BrowserJob).where(BrowserJob.kind == "renew")).scalar_one()
            browser_jobs.failed(opened, self.settings, job.id, "UnexpectedPage", None, [], NOTICE_AT + 120)
        self.assertFalse(self.open_job())
        with db.session(self.engine) as opened:
            accounts.update(opened, self.account, "other.takeout@example.test", None, NOTICE_AT + 200)
            self.assertEqual(opened.get(GoogleAccount, self.account).email, "other.takeout@example.test")

    def test_a_renew_job_still_cannot_be_enqueued_by_hand(self):
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.UnknownKind):
                browser_jobs.enqueue(opened, self.account, browser_jobs.RENEW_KIND, None, NOTICE_AT)


class RenewalSchedulerTests(RenewalCase):
    def test_the_scheduler_runs_the_watch_once_a_day_at_five(self):
        scheduler = jobs.build(self.settings, self.engine, CREATED)
        registered = scheduler.jobs["schedules.watch"]
        self.assertEqual((registered.interval, registered.hour), (None, 5))

    def test_a_fake_clock_over_the_whole_window_queues_exactly_one_renew_job(self):
        scheduler = jobs.build(self.settings, self.engine, NOTICE_AT - 3 * DAY)
        ran = []
        for hour in range(0, 24 * 40, 6):
            ran += [name for name in scheduler.run_due(NOTICE_AT - 3 * DAY + hour * HOUR) if name == "schedules.watch"]
        self.assertGreaterEqual(len(ran), 38)
        self.assertEqual(len(self.renew_jobs()), 1)
        self.assertEqual(len(self.alerts_of("schedule_ending")), 1)
        with db.session(self.engine) as opened:
            queued = opened.execute(select(BrowserJob.created).where(BrowserJob.kind == "renew")).scalar_one()
        self.assertGreaterEqual(queued, NOTICE_AT)
        self.assertLess(queued, NOTICE_AT + DAY)


if __name__ == "__main__":
    unittest.main()
