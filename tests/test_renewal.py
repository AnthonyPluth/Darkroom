import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from sqlalchemy import select

from darkroom import dates
from darkroom.domain import accounts, alerts, browser_jobs, downloads, renewal
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Event, Export, GoogleAccount
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

    def open_alert_kinds(self):
        with db.session(self.engine) as opened:
            return {alert.kind for alert in opened.execute(select(Alert).where(Alert.closed.is_(None))).scalars() if alert.kind.startswith("schedule")}

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


BAD_TIMES = (float("inf"), float("-inf"), float("nan"), 1e300, 5e9, dates.LATEST_INSTANT + 1, 0.0, -5.0)


class StoredBadScheduleTests(RenewalCase):
    def set_schedule(self, created, end):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            account.schedule_created, account.schedule_end = created, end

    def test_a_schedule_with_a_time_that_is_not_an_instant_is_no_schedule(self):
        good = GoogleAccount(email="a@example.test", default_person_id=1, created=1.0, schedule_created=CREATED, schedule_end=END)
        self.assertTrue(browser_jobs.schedule_live(good, CREATED + DAY))
        self.assertEqual(browser_jobs.schedule_of(good), (CREATED, END))
        for bad in BAD_TIMES:
            for created, end in ((CREATED, bad), (bad, END), (bad, None)):
                account = GoogleAccount(email="a@example.test", default_person_id=1, created=1.0, schedule_created=created, schedule_end=end)
                with self.subTest(created=created, end=end):
                    self.assertIsNone(browser_jobs.schedule_of(account))
                    self.assertFalse(browser_jobs.schedule_live(account, CREATED + DAY))
                    self.assertTrue(browser_jobs.schedule_unusable(account))

    def test_a_stored_infinite_end_lets_a_replacement_schedule_be_created_and_shows_as_no_schedule(self):
        for end in (float("inf"), float("-inf"), 1e300):
            with self.subTest(end=end):
                self.set_schedule(CREATED, end)
                with db.session(self.engine) as opened:
                    for job in opened.execute(select(BrowserJob)).scalars():
                        opened.delete(job)
                    done = browser_jobs.queue(opened, self.account, "schedule", {}, CREATED)
                    done.status = "done"
                with db.session(self.engine) as opened:
                    job = browser_jobs.enqueue_once(opened, self.account, "schedule", CREATED + DAY)
                    self.assertEqual(job.kind, "schedule")
                    shown = accounts.as_dict(opened, opened.get(GoogleAccount, self.account))
                    self.assertEqual((shown["schedule_created"], shown["schedule_end"], shown["renew_from"]), (None, None, None))
                    with self.assertRaises(renewal.NoSchedule):
                        renewal.renew_now(opened, self.settings, self.account, CREATED + DAY)

    def test_renewal_watching_neither_crashes_nor_waits_on_a_bad_stored_end(self):
        for end in (float("inf"), float("-inf"), 1e300, 0.0):
            with self.subTest(end=end):
                self.set_schedule(CREATED, end)
                for now in (NOTICE_AT, END + DAY):
                    self.assertEqual(self.watch(now), 0)
                    with db.session(self.engine) as opened:
                        self.assertEqual(renewal.reword_ended(opened, self.settings, now), 0)
                self.assertEqual(self.alerts_of("schedule_ending"), [])
                self.assertEqual(self.renew_jobs(), [])

    def test_the_export_expiry_watch_skips_a_stored_expiry_that_is_not_an_instant(self):
        with db.session(self.engine) as opened:
            for n, expires in enumerate((float("nan"), float("inf"), float("-inf"), 1e300)):
                opened.add(Export(account_id=self.account, kind="manual", google_id=f"g-{n}", created_at=CREATED, ready_at=CREATED, expires_at=expires, status="announced"))
        with db.session(self.engine) as opened:
            self.assertEqual(downloads.watch_expiry(opened, self.settings, CREATED + DAY), 0)


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


class FailingCase(RenewalCase):
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


class RenewalFailureTests(FailingCase):
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
        self.assertIn("was not renewed", message)
        self.assertIn("does not retry", message)
        self.assertIn("Renew now", message)
        self.assertNotIn("later addition", message)
        self.assertIn("Press Renew now on the Exports page to ask for another.", message)
        self.assertNotIn("Create schedule", message)
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

    def test_a_renewal_that_fails_under_a_day_before_the_end_repeats_in_the_past_tense_once_it_has_ended(self):
        self.watch(NOTICE_AT)
        self.fail(END - 3600)
        ((_, before, _, _, _),) = self.alerts_of("schedule_renewal_failed")
        self.assertIn("and it ends on", before)
        sent = []

        def notifier(settings, title, message, link):
            sent.append(message)
            return alerts.SENT

        with db.session(self.engine) as opened:
            self.assertEqual(renewal.reword_ended(opened, self.settings, END - 3600 + DAY), 1)
            self.assertEqual(alerts.tick(opened, self.settings, END - 3600 + DAY, notifier), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("and it ended on 15 Jan 2028", sent[0])
        self.assertNotIn("and it ends on", sent[0])
        self.assertIn("so scheduled exports have stopped", sent[0])
        self.assertIn("Press Renew now on the Exports page to create a new schedule.", sent[0])
        self.assertNotIn("Create schedule", sent[0])

    def test_rewording_leaves_a_schedule_that_has_not_ended_and_rewords_an_ended_one_once(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        with db.session(self.engine) as opened:
            self.assertEqual(renewal.reword_ended(opened, self.settings, END - 1), 0)
        ((_, message, _, _, _),) = self.alerts_of("schedule_renewal_failed")
        self.assertIn("and it ends on", message)
        with db.session(self.engine) as opened:
            self.assertEqual(renewal.reword_ended(opened, self.settings, END + 1), 1)
            self.assertEqual(renewal.reword_ended(opened, self.settings, END + 2), 0)
        ((_, message, _, _, _),) = self.alerts_of("schedule_renewal_failed")
        self.assertIn("and it ended on", message)

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


class CheckingCase(FailingCase):
    UNSURE = "ScheduleNotConfirmed: Takeout did not confirm a recurring export"

    def unconfirmed(self):
        self.watch(NOTICE_AT)
        return self.fail(NOTICE_AT + 120, message=self.UNSURE)

    def check(self, found, now):
        with db.session(self.engine) as opened:
            return renewal.checked(opened, self.settings, self.account, found, now).id

    def schedule(self):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            return account.schedule_created, account.schedule_end

    def needs_check(self, job_id):
        with db.session(self.engine) as opened:
            return browser_jobs.is_unchecked(opened, opened.get(BrowserJob, job_id))


class RenewalCheckTests(CheckingCase):
    def test_a_renewal_found_in_takeout_records_the_new_schedule_from_when_the_job_started_and_confirms_it(self):
        job_id = self.unconfirmed()
        self.assertTrue(self.needs_check(job_id))
        self.assertEqual(self.check(True, NOTICE_AT + DAY), job_id)
        self.assertEqual(self.schedule(), (NOTICE_AT + 120, dates.one_year_after(NOTICE_AT + 120)))
        self.assertGreater(self.schedule()[1], END)
        self.assertEqual(self.alerts_of("schedule_renewal_unconfirmed")[0][4], NOTICE_AT + DAY)
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])
        ((_, message, _, sent, _),) = self.alerts_of("schedule_renewed")
        self.assertIn("25 Dec 2028", message)
        self.assertEqual(sent, 1)
        self.assertFalse(self.needs_check(job_id))
        self.assertIn("schedule_checked", self.event_types())
        self.assertIn("schedule_renewed", self.event_types())
        with db.session(self.engine) as opened:
            self.assertEqual(browser_jobs.as_dict(opened.get(BrowserJob, job_id))["checked"], "found")

    def test_a_renewal_not_found_in_takeout_is_reported_as_not_renewed_and_stays_so(self):
        job_id = self.unconfirmed()
        self.check(False, NOTICE_AT + DAY)
        self.assertEqual(self.schedule(), (CREATED, END))
        ((_, _, _, _, unconfirmed_closed),) = self.alerts_of("schedule_renewal_unconfirmed")
        self.assertEqual(unconfirmed_closed, NOTICE_AT + DAY)
        ((key, message, _, _, closed),) = self.alerts_of("schedule_renewal_failed")
        self.assertEqual((key, closed), (renewal.failed_key(self.account), None))
        self.assertIn("was not renewed", message)
        self.assertEqual(self.alerts_of("schedule_renewed"), [])
        for days in range(2, 60):
            self.assertEqual(self.watch(NOTICE_AT + days * DAY), 0)
        self.assertEqual(len(self.alerts_of("schedule_renewal_unconfirmed")), 1)
        self.assertEqual(len(self.alerts_of("schedule_renewal_failed")), 1)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertFalse(self.needs_check(job_id))
        with db.session(self.engine) as opened:
            self.assertEqual(browser_jobs.as_dict(opened.get(BrowserJob, job_id))["checked"], "missing")

    def test_a_found_renewal_that_would_not_extend_the_schedule_is_refused_and_nothing_changes(self):
        job_id = self.unconfirmed()
        later = dates.one_year_after(NOTICE_AT + 120)
        for end in (later, later + DAY):
            with db.session(self.engine) as opened:
                opened.get(GoogleAccount, self.account).schedule_end = end
            with self.subTest(end=end), self.assertRaises(browser_jobs.NotExtended):
                self.check(True, NOTICE_AT + DAY)
            self.assertEqual(self.schedule(), (CREATED, end))
        self.assertTrue(self.needs_check(job_id))
        self.assertIsNone(self.alerts_of("schedule_renewal_unconfirmed")[0][4])
        self.assertEqual(self.alerts_of("schedule_renewed"), [])

    def test_a_check_is_answered_once_and_only_for_an_unconfirmed_attempt(self):
        for first in (True, False):
            with self.subTest(first=first):
                self.setUp()
                self.unconfirmed()
                self.check(first, NOTICE_AT + DAY)
                for again in (True, False):
                    with self.assertRaises(browser_jobs.NothingToCheck):
                        self.check(again, NOTICE_AT + 2 * DAY)
        self.setUp()
        with self.assertRaises(browser_jobs.NothingToCheck):
            self.check(True, NOTICE_AT)
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        with self.assertRaises(browser_jobs.NothingToCheck):
            self.check(True, NOTICE_AT + DAY)
        self.assertEqual(self.schedule(), (CREATED, END))

    def test_after_a_found_renewal_the_next_years_window_queues_the_next_renewal(self):
        self.unconfirmed()
        self.check(True, NOTICE_AT + DAY)
        new_end = dates.one_year_after(NOTICE_AT + 120)
        day = NOTICE_AT + 2 * DAY
        while day < new_end - 21 * DAY:
            self.assertEqual(self.watch(day), 0, day)
            day += DAY
        self.assertEqual(self.watch(new_end - 21 * DAY), 1)
        self.assertEqual(self.renew_jobs()[-1], (self.account, "queued", {"email": "jo.takeout@example.test", "ending": new_end}))
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_ending")], [False, True])
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_renewal_unconfirmed")], [False])

    def test_an_unchecked_renewal_blocks_creating_a_schedule_even_after_it_has_ended(self):
        self.unconfirmed()
        with db.session(self.engine) as opened:
            for now in (NOTICE_AT + DAY, END + DAY):
                with self.subTest(now=now), self.assertRaises(browser_jobs.AlreadyOpen):
                    browser_jobs.enqueue_once(opened, self.account, "schedule", now)

    def test_a_renewal_that_did_not_happen_leaves_create_schedule_closed_until_the_schedule_ends_then_open_once(self):
        self.unconfirmed()
        self.check(False, NOTICE_AT + DAY)
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", END - 1)
            job = browser_jobs.enqueue_once(opened, self.account, "schedule", END + DAY)
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", END + DAY + 1)
            job_id = job.id
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, END + DAY + 10)
            done = browser_jobs.done(opened, job_id, {"schedule_created": END + DAY + 60, "schedule_end": dates.one_year_after(END + DAY + 60)}, END + DAY + 70)
            renewal.schedule_recorded(opened, self.settings, done, END + DAY + 70)
        self.assertEqual(self.alerts_of("schedule_renewal_failed")[0][4], END + DAY + 70)
        self.assertEqual(self.alerts_of("schedule_renewed"), [])
        new_end = dates.one_year_after(END + DAY + 60)
        self.assertEqual(self.watch(END + 2 * DAY), 0)
        self.assertEqual(self.watch(new_end - 21 * DAY), 1)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed", "queued"])

    def test_a_renewal_still_queued_when_the_schedule_ends_blocks_creating_another(self):
        self.watch(NOTICE_AT)
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", END + DAY)

    def test_a_genuinely_failed_renewal_stays_reported_past_the_end_and_is_never_queued_again(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        for days in range(1, 120):
            self.assertEqual(self.watch(NOTICE_AT + days * DAY), 0)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertEqual([alert[4] for alert in self.alerts_of("schedule_renewal_failed")], [None])
        self.assertEqual(self.alerts_of("schedule_renewal_unconfirmed"), [])


class RenewalOverlapTests(CheckingCase):
    def all_alert_kinds(self):
        with db.session(self.engine) as opened:
            return sorted(alert.kind for alert in opened.execute(select(Alert)).scalars())

    def test_an_unconfirmed_renewal_sends_no_generic_failure_alert(self):
        self.unconfirmed()
        self.assertEqual(self.all_alert_kinds(), ["schedule_ending", "schedule_renewal_unconfirmed"])
        self.assertIn("browser_job_failed", self.event_types())

    def test_a_lost_renewal_sends_no_generic_failure_alert_either(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        jobs.build(self.settings, self.engine, NOTICE_AT + 60).run_one("browser.leases", NOTICE_AT + 60 + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual(self.all_alert_kinds(), ["schedule_ending", "schedule_renewal_unconfirmed"])

    def test_a_genuinely_failed_renewal_still_sends_the_failure_alert_with_its_capture(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        self.assertEqual(self.all_alert_kinds(), ["failure", "schedule_ending", "schedule_renewal_failed"])

    def create_schedule(self, now):
        with db.session(self.engine) as opened:
            return browser_jobs.enqueue_once(opened, self.account, "schedule", now).id

    def test_the_watch_queues_no_renewal_while_a_create_schedule_job_is_open_or_unanswered(self):
        job_id = self.create_schedule(END + DAY)
        self.assertEqual(self.watch(END + DAY + 60), 0)
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, END + DAY + 100)
        self.assertEqual(self.watch(END + 2 * DAY), 0)
        with db.session(self.engine) as opened:
            browser_jobs.failed(opened, self.settings, job_id, "ScheduleNotConfirmed: unsure", None, [], END + 2 * DAY + 10)
        self.assertEqual(self.watch(END + 3 * DAY), 0)
        self.assertEqual(self.renew_jobs(), [])
        self.assertEqual(self.alerts_of("schedule_ending"), [])
        self.check(False, END + 3 * DAY + 10)
        self.assertEqual(self.watch(END + 4 * DAY), 1)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["queued"])
        with db.session(self.engine) as opened, self.assertRaises(browser_jobs.AlreadyOpen):
            browser_jobs.enqueue_once(opened, self.account, "schedule", END + 4 * DAY + 10)

    def test_a_create_schedule_job_that_records_a_schedule_leaves_the_watch_nothing_to_do(self):
        job_id = self.create_schedule(END + DAY)
        with db.session(self.engine) as opened:
            browser_jobs.claim(opened, END + DAY + 10)
            done = browser_jobs.done(opened, job_id, {"schedule_created": END + DAY + 60, "schedule_end": dates.one_year_after(END + DAY + 60)}, END + DAY + 70)
            renewal.schedule_recorded(opened, self.settings, done, END + DAY + 70)
        self.assertEqual(self.watch(END + 2 * DAY), 0)
        self.assertEqual(self.renew_jobs(), [])

    def test_a_not_renewed_alert_opened_after_the_end_says_the_schedule_ended(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        self.assertIn("it ends on 15 Jan 2028", self.alerts_of("schedule_renewal_failed")[0][1])
        with db.session(self.engine) as opened:
            alerts.close(opened, renewal.failed_key(self.account), END + DAY)
        self.watch(END + 2 * DAY)
        message = self.alerts_of("schedule_renewal_failed")[1][1]
        self.assertIn("it ended on 15 Jan 2028", message)
        self.assertNotIn("Once it has ended", message)

    def test_a_not_renewed_alert_still_open_at_the_end_is_reworded_and_repeats_with_the_new_text(self):
        failed_at = END - 2 * DAY
        self.watch(NOTICE_AT)
        self.fail(failed_at)
        self.watch(END - 1)
        ((_, before, _, _, _),) = self.alerts_of("schedule_renewal_failed")
        self.assertIn("Press Renew now on the Exports page to ask for another.", before)
        self.assertNotIn("Create schedule", before)
        self.watch(END + HOUR)
        ((key, message, _, sent, closed),) = self.alerts_of("schedule_renewal_failed")
        self.assertEqual((key, sent, closed), (renewal.failed_key(self.account), 1, None))
        self.assertIn("it ended on 15 Jan 2028", message)
        self.assertNotIn("Once it has ended", message)
        sent_texts = []
        with db.session(self.engine) as opened:
            alerts.tick(opened, self.settings, failed_at + DAY, lambda settings, title, text, link: sent_texts.append(text) or alerts.SENT)
        self.assertEqual(len(sent_texts), 1)
        self.assertIn("it ended on 15 Jan 2028", sent_texts[0])
        self.assertNotIn("Once it has ended", sent_texts[0])

    def test_an_unconfirmed_renewal_alert_keeps_its_text_after_the_end(self):
        self.watch(NOTICE_AT)
        self.fail(END - 2 * DAY, message="ScheduleNotConfirmed: unsure")
        ((_, before, _, _, _),) = self.alerts_of("schedule_renewal_unconfirmed")
        self.watch(END + HOUR)
        ((_, after, _, sent, closed),) = self.alerts_of("schedule_renewal_unconfirmed")
        self.assertEqual((after, sent, closed), (before, 1, None))
        self.assertEqual(self.alerts_of("schedule_renewal_failed"), [])


class RenewalLifecycleTests(RenewalCase):
    def claim_renewal(self, now):
        with db.session(self.engine) as opened:
            while (job := browser_jobs.claim(opened, now)) is not None:
                if job.kind == "renew":
                    return job.id
        raise AssertionError("no renew job was queued")

    def test_over_two_years_an_unconfirmed_renewal_found_in_takeout_leads_to_next_years_renewal(self):
        start = NOTICE_AT - DAY
        scheduler = jobs.build(self.settings, self.engine, start)
        clock = start
        while clock < NOTICE_AT + DAY:
            clock += 12 * HOUR
            scheduler.run_due(clock)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["queued"])
        self.claim_renewal(clock)
        clock += browser_jobs.LEASE_SECONDS + 1
        scheduler.run_due(clock)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed"])
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_renewal_unconfirmed")], [True])
        with db.session(self.engine) as opened:
            renewal.checked(opened, self.settings, self.account, True, clock + HOUR)
        with db.session(self.engine) as opened:
            new_end = opened.get(GoogleAccount, self.account).schedule_end
        self.assertGreater(new_end, END)
        while clock < new_end - 22 * DAY:
            clock += 12 * HOUR
            scheduler.run_due(clock)
        self.assertEqual(len(self.renew_jobs()), 1)
        while clock < new_end - 20 * DAY:
            clock += 12 * HOUR
            scheduler.run_due(clock)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed", "queued"])
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_ending")], [False, True])
        job_id = self.claim_renewal(clock)
        self.finish(job_id, clock + 60)
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_ending")], [False, False])
        self.assertEqual(len(self.alerts_of("schedule_renewed")), 2)
        self.assertEqual([alert[4] is None for alert in self.alerts_of("schedule_renewal_unconfirmed")], [False])


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

    def change_email(self, now):
        with db.session(self.engine) as opened:
            accounts.update(opened, self.account, "other.takeout@example.test", None, now)

    def test_an_unconfirmed_renewal_keeps_the_email_until_the_takeout_check_is_answered(self):
        for found in (True, False):
            with self.subTest(found=found):
                self.setUp()
                self.watch(NOTICE_AT)
                self.claim_renewal(NOTICE_AT + 60)
                with db.session(self.engine) as opened:
                    job = opened.execute(select(BrowserJob).where(BrowserJob.kind == "renew")).scalar_one()
                    browser_jobs.failed(opened, self.settings, job.id, "ScheduleNotConfirmed: unsure", None, [], NOTICE_AT + 120)
                self.assertFalse(self.open_job())
                with self.assertRaises(accounts.JobOpen) as caught:
                    self.change_email(NOTICE_AT + 200)
                self.assertIn("Takeout check", str(caught.exception))
                with db.session(self.engine) as opened:
                    self.assertEqual(opened.get(GoogleAccount, self.account).email, "jo.takeout@example.test")
                    renewal.checked(opened, self.settings, self.account, found, NOTICE_AT + 300)
                self.change_email(NOTICE_AT + 400)
                with db.session(self.engine) as opened:
                    self.assertEqual(opened.get(GoogleAccount, self.account).email, "other.takeout@example.test")

    def test_a_lost_renewal_keeps_the_email_too(self):
        self.watch(NOTICE_AT)
        self.claim_renewal(NOTICE_AT + 60)
        with db.session(self.engine) as opened:
            browser_jobs.expire_leases(opened, self.settings, NOTICE_AT + 60 + browser_jobs.LEASE_SECONDS + 1)
        with self.assertRaises(accounts.JobOpen):
            self.change_email(NOTICE_AT + 400)

    def test_a_renew_job_still_cannot_be_enqueued_by_hand(self):
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.UnknownKind):
                browser_jobs.enqueue(opened, self.account, browser_jobs.RENEW_KIND, None, NOTICE_AT)


class RenewNowTests(FailingCase):
    def request(self, now, account=None):
        with db.session(self.engine) as opened:
            return renewal.renew_now(opened, self.settings, self.account if account is None else account, now).id

    def test_it_queues_one_renew_job_with_the_watchs_payload_and_records_the_request(self):
        job_id = self.request(NOTICE_AT)
        self.assertEqual(self.renew_jobs(), [(self.account, "queued", {"email": "jo.takeout@example.test", "ending": END})])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job_id).kind, "renew")
        self.assertEqual(self.event_types().count("schedule_renewal_requested"), 1)
        self.assertEqual(self.event_types().count("schedule_renewal_queued"), 0)

    def test_a_second_request_while_the_job_is_queued_or_running_is_refused(self):
        self.request(NOTICE_AT)
        with self.assertRaises(browser_jobs.AlreadyOpen):
            self.request(NOTICE_AT + 5)
        self.claim_renewal(NOTICE_AT + 10)
        with self.assertRaises(browser_jobs.AlreadyOpen):
            self.request(NOTICE_AT + 30)
        self.assertEqual(len(self.renew_jobs()), 1)

    def test_a_second_request_while_the_job_waits_for_a_sign_in_is_refused(self):
        self.request(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 10)
        with db.session(self.engine) as opened:
            browser_jobs.waiting(opened, self.settings, job_id, "sign_in", NOTICE_AT + 20)
        with self.assertRaises(renewal.NeedsSignIn):
            self.request(NOTICE_AT + 30)
        self.assertEqual(len(self.renew_jobs()), 1)

    def test_it_is_refused_while_a_create_schedule_job_is_open(self):
        with db.session(self.engine) as opened:
            browser_jobs.queue(opened, self.account, "schedule", {"email": "jo.takeout@example.test"}, NOTICE_AT)
        with self.assertRaises(browser_jobs.AlreadyOpen):
            self.request(NOTICE_AT + 1)
        self.assertEqual(self.renew_jobs(), [])

    def test_it_is_refused_while_an_attempt_is_unanswered_and_allowed_once_answered(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120, message="ScheduleNotConfirmed: unsure")
        with self.assertRaises(browser_jobs.AlreadyOpen) as caught:
            self.request(NOTICE_AT + 200)
        self.assertIn("check Takeout", str(caught.exception))
        with db.session(self.engine) as opened:
            renewal.checked(opened, self.settings, self.account, False, NOTICE_AT + 300)
        self.request(NOTICE_AT + 400)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed", "queued"])

    def test_it_is_allowed_after_a_failed_renewal_inside_the_window_and_the_watch_queues_nothing_more(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        self.assertEqual(self.watch(NOTICE_AT + DAY), 0)
        self.request(NOTICE_AT + 2 * DAY)
        self.assertEqual([job[1] for job in self.renew_jobs()], ["failed", "queued"])
        self.assertEqual(self.watch(NOTICE_AT + 3 * DAY), 0)
        self.assertEqual(len(self.renew_jobs()), 2)

    def test_a_requested_renewal_that_succeeds_renews_and_closes_the_failed_alert(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        self.assertIsNone(self.alerts_of("schedule_renewal_failed")[0][4])
        self.request(NOTICE_AT + DAY)
        job_id = self.claim_renewal(NOTICE_AT + DAY + 10)
        self.finish(job_id, NOTICE_AT + DAY + 100)
        self.assertIsNotNone(self.alerts_of("schedule_renewal_failed")[0][4])
        self.assertEqual(len(self.alerts_of("schedule_renewed")), 1)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account).schedule_end, NOTICE_AT + DAY + 100 + 365 * DAY)

    def test_a_requested_renewal_that_fails_reports_it_like_the_watchs(self):
        self.request(NOTICE_AT + DAY)
        self.fail(NOTICE_AT + DAY + 100)
        self.assertEqual(len(self.alerts_of("schedule_renewal_failed")), 1)
        self.assertEqual(self.event_types().count("schedule_renewal_failed"), 1)

    def test_it_is_allowed_after_the_schedule_has_ended(self):
        self.request(END + DAY)
        self.assertEqual(len(self.renew_jobs()), 1)

    def test_it_is_refused_before_the_window_opens_and_on_a_schedule_just_renewed(self):
        with self.assertRaises(renewal.NotDue) as caught:
            self.request(NOTICE_AT - 1)
        self.assertIn("not due for renewal", str(caught.exception))
        self.watch(NOTICE_AT)
        job_id = self.claim_renewal(NOTICE_AT + 10)
        self.finish(job_id, NOTICE_AT + 100)
        for now in (NOTICE_AT + 200, NOTICE_AT + 100 + 300 * DAY):
            with self.subTest(now=now), self.assertRaises(renewal.NotDue):
                self.request(now)
        self.assertEqual(len(self.renew_jobs()), 1)

    def test_a_requested_renewal_leaves_one_open_alert_while_queued_and_after_the_next_watch(self):
        self.watch(NOTICE_AT)
        self.fail(NOTICE_AT + 120)
        self.assertEqual(self.open_alert_kinds(), {"schedule_renewal_failed"})
        self.request(NOTICE_AT + DAY)
        self.assertEqual(self.open_alert_kinds(), set())
        self.watch(NOTICE_AT + 2 * DAY)
        self.assertEqual(self.open_alert_kinds(), {"schedule_ending"})
        self.claim_renewal(NOTICE_AT + 2 * DAY + 10)
        with db.session(self.engine) as opened:
            job = opened.execute(select(BrowserJob).where(BrowserJob.status == "running")).scalar_one()
            browser_jobs.failed(opened, self.settings, job.id, "UnexpectedPage", None, [], NOTICE_AT + 3 * DAY)
            renewal.job_failed(opened, self.settings, job, NOTICE_AT + 3 * DAY)
        self.assertEqual(self.open_alert_kinds(), {"schedule_renewal_failed"})

    def test_after_the_end_the_alerts_use_the_past_tense_and_name_renew_now(self):
        self.watch(END + DAY)
        self.fail(END + DAY + 100)
        ((_, failed, _, _, _),) = self.alerts_of("schedule_renewal_failed")
        self.assertIn("ended on", failed)
        self.assertIn("Press Renew now on the Exports page to create a new schedule.", failed)
        self.assertNotIn("Create schedule", failed)
        self.request(END + 2 * DAY)
        self.watch(END + 3 * DAY)
        ending = [alert[1] for alert in self.alerts_of("schedule_ending")]
        self.assertTrue(all("ended on" in text and "ends on" not in text for text in ending))
        self.assertEqual(self.open_alert_kinds(), {"schedule_ending"})

    def test_a_request_that_arrives_while_the_watch_is_deciding_queues_no_second_job(self):
        outcomes = []

        def press():
            try:
                with db.session(self.engine) as opened:
                    outcomes.append(renewal.renew_now(opened, self.settings, self.account, NOTICE_AT).id)
            except browser_jobs.AlreadyOpen as error:
                outcomes.append(error)

        presser = threading.Thread(target=press)
        started = []
        original = renewal.latest_renewal

        def latest_renewal_with_a_request_arriving(opened, account, since):
            if not started:
                started.append(True)
                presser.start()
                time.sleep(0.5)
            return original(opened, account, since)

        with mock.patch.object(renewal, "latest_renewal", latest_renewal_with_a_request_arriving):
            queued = self.watch(NOTICE_AT)
        presser.join()
        self.assertEqual((queued, len(self.renew_jobs())), (1, 1))
        self.assertEqual([isinstance(outcome, browser_jobs.AlreadyOpen) for outcome in outcomes], [True])

    def test_a_request_committed_just_before_the_watch_locks_leaves_it_nothing_to_queue_and_the_other_accounts_are_still_watched(self):
        with db.session(self.engine) as opened:
            other = self.add_account(opened, "al", "al.takeout@example.test", CREATED, END)
        original = browser_jobs.lock_scheduled_accounts

        def lock_after_a_request_commits(opened):
            self.request(NOTICE_AT)
            original(opened)

        with mock.patch.object(browser_jobs, "lock_scheduled_accounts", lock_after_a_request_commits):
            queued = self.watch(NOTICE_AT + 5)
        self.assertEqual(queued, 1)
        self.assertEqual([job[0] for job in self.renew_jobs()], [self.account, other])

    def test_the_watch_queues_with_the_email_the_account_has_when_it_locks(self):
        original = browser_jobs.lock_scheduled_accounts

        def lock_after_an_email_change(opened):
            with db.session(self.engine) as changing:
                accounts.update(changing, self.account, "other.takeout@example.test", None, NOTICE_AT)
            original(opened)

        with mock.patch.object(browser_jobs, "lock_scheduled_accounts", lock_after_an_email_change):
            self.assertEqual(self.watch(NOTICE_AT + 5), 1)
        self.assertEqual(self.renew_jobs(), [(self.account, "queued", {"email": "other.takeout@example.test", "ending": END})])

    def test_a_request_just_before_the_watch_leaves_the_watch_nothing_to_queue(self):
        self.request(NOTICE_AT)
        self.assertEqual(self.watch(NOTICE_AT + 5), 0)
        self.assertEqual(len(self.renew_jobs()), 1)

    def test_an_ending_alert_open_across_the_end_is_reworded_in_place_and_repeats_with_the_new_text(self):
        self.watch(NOTICE_AT)
        ((key, before, _, _, _),) = self.alerts_of("schedule_ending")
        self.assertIn("ends on", before)
        self.watch(END + HOUR)
        ((same_key, message, _, sent, closed),) = self.alerts_of("schedule_ending")
        self.assertEqual((same_key, sent, closed), (key, 1, None))
        self.assertIn("ended on", message)
        self.assertNotIn("ends on", message)
        sent_texts = []
        with db.session(self.engine) as opened:
            alerts.tick(opened, self.settings, NOTICE_AT + DAY + 120, lambda settings, title, text, link: sent_texts.append(text) or alerts.SENT)
        self.assertEqual(len(sent_texts), 1)
        self.assertIn("ended on", sent_texts[0])

    def test_two_requests_at_once_queue_one_job(self):
        outcomes = []
        gate = threading.Barrier(2)

        def press():
            gate.wait()
            try:
                with db.session(self.engine) as opened:
                    outcomes.append(renewal.renew_now(opened, self.settings, self.account, NOTICE_AT).id)
            except browser_jobs.AlreadyOpen as error:
                outcomes.append(error)

        threads = [threading.Thread(target=press) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.renew_jobs()), 1)
        self.assertEqual(sum(isinstance(outcome, int) for outcome in outcomes), 1)
        self.assertEqual(sum(isinstance(outcome, browser_jobs.AlreadyOpen) for outcome in outcomes), 1)

    def test_it_is_refused_for_an_account_that_needs_sign_in(self):
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).session_state = "needs_sign_in"
        with self.assertRaises(renewal.NeedsSignIn):
            self.request(NOTICE_AT)
        self.assertEqual(self.renew_jobs(), [])

    def test_it_is_refused_for_an_account_with_no_schedule(self):
        with db.session(self.engine) as opened:
            bare = self.add_account(opened, "al", "al.takeout@example.test")
            half = self.add_account(opened, "bo", "bo.takeout@example.test", CREATED, None)
        for account in (bare, half):
            with self.subTest(account=account), self.assertRaises(renewal.NoSchedule):
                self.request(NOTICE_AT, account)
        self.assertEqual(self.renew_jobs(), [])

    def test_it_is_refused_for_an_unknown_account(self):
        with self.assertRaises(browser_jobs.UnknownAccount):
            self.request(NOTICE_AT, self.account + 50)

    def test_the_email_stays_pinned_while_the_requested_job_is_open(self):
        self.request(NOTICE_AT)
        with db.session(self.engine) as opened:
            with self.assertRaises(accounts.JobOpen):
                accounts.update(opened, self.account, "other.takeout@example.test", None, NOTICE_AT + 5)
            self.assertEqual(opened.get(GoogleAccount, self.account).email, "jo.takeout@example.test")

    def test_create_schedule_is_refused_while_the_requested_job_is_open(self):
        self.request(END + DAY)
        with db.session(self.engine) as opened:
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, self.account, "schedule", END + DAY + 5)


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
