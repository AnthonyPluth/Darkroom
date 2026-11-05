import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import alerts, browser_jobs, renewal
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
        self.assertEqual(self.tick(NOTICE_AT + DAY), 1)
        self.assertEqual(self.tick(NOTICE_AT + 4 * DAY), 1)
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
    def fail(self, now, files=("screenshot.png",)):
        job_id = self.claim_renewal(now)
        folder = self.photos / "_browser" / str(self.account) / "failures" / str(job_id)
        folder.mkdir(parents=True)
        for name in files:
            (folder / name).write_bytes(b"x")
        with db.session(self.engine) as opened:
            browser_jobs.failed(opened, self.settings, job_id, "ScheduleNotConfirmed: Takeout did not confirm a recurring export", browser_jobs.failure_folder(self.account, job_id), list(files), now)
        return job_id

    def test_a_failed_renewal_leaves_the_ending_alert_open_and_links_the_capture(self):
        self.watch(NOTICE_AT)
        job_id = self.fail(NOTICE_AT + 120)
        ((_, _, _, _, closed),) = self.alerts_of("schedule_ending")
        self.assertIsNone(closed)
        ((key, _, link, _, _),) = self.alerts_of("failure")
        self.assertEqual(key, f"browser:failed:{job_id}")
        self.assertEqual(link, f"https://darkroom.example/api/browser/failures/{job_id}/screenshot.png")
        self.assertEqual(self.alerts_of("schedule_renewed"), [])
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((account.schedule_created, account.schedule_end), (CREATED, END))

    def test_a_failed_renewal_is_not_queued_again_and_the_ending_alert_keeps_repeating(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        for days in range(1, 20):
            self.assertEqual(self.watch(NOTICE_AT + days * DAY), 0)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertEqual(self.tick(NOTICE_AT + DAY), 1)
        self.assertEqual(self.tick(NOTICE_AT + 4 * DAY), 1)
        self.assertEqual(self.alerts_of("schedule_ending")[0][3:], (3, None))

    def test_a_worker_that_vanished_mid_renewal_fails_the_job_the_same_way(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            browser_jobs.expire_leases(opened, self.settings, NOTICE_AT + 60 + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertIsNone(self.alerts_of("schedule_ending")[0][4])
        self.assertEqual(self.watch(NOTICE_AT + DAY), 0)


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
