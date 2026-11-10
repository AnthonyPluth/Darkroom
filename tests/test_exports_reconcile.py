import json
import unittest
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from darkroom import dates
from darkroom.domain import browser_jobs, exports
from darkroom.storage import db
from darkroom.storage.models import BrowserJob, Event, Export, ExportPart, GoogleAccount
from tests.shared import add_person, another_writer_holds_the_lock, impatient_engine, own_database

DAY = 86400.0


def at(*when):
    return datetime(*when, tzinfo=UTC).timestamp()


SCHEDULED = at(2026, 11, 2, 3, 0)
NOW = at(2026, 11, 2, 12, 0)


def listed(google_id="g-1", state="ready", created=SCHEDULED + 600, expires=SCHEDULED + 7 * DAY, parts=((1, 100), (2, 50))):
    return exports.Listed(google_id, created, state, expires, tuple(exports.ListedPart(number, size) for number, size in parts) if state == "ready" else ())


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        with db.session(self.engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo.takeout@example.test", default_person_id=person, created=1.0, session_state="ok", schedule_created=SCHEDULED, schedule_end=at(2027, 11, 2, 3, 0))
            opened.add(account)
            opened.flush()
            self.account = account.id

    def reconcile(self, listing, now=NOW):
        with db.session(self.engine) as opened:
            return exports.reconcile(opened, opened.get(GoogleAccount, self.account), listing, now)

    def rows(self):
        with db.session(self.engine) as opened:
            return list(opened.execute(select(Export).order_by(Export.created_at)).scalars())

    def parts(self, export_id):
        with db.session(self.engine) as opened:
            return [(part.number, part.bytes, part.status, part.downloads_used) for part in exports.parts_of(opened, export_id)]

    def downloads(self):
        with db.session(self.engine) as opened:
            return [(job.kind, json.loads(job.payload), job.status) for job in opened.execute(select(BrowserJob).where(BrowserJob.kind == "download")).scalars()]

    def event_types(self):
        with db.session(self.engine) as opened:
            return [event.type for event in opened.execute(select(Event).order_by(Event.id)).scalars()]


class ReconcileTests(ReconcileCase):
    def test_a_new_ready_export_is_announced_with_its_parts_and_a_download_job(self):
        found = self.reconcile([listed()])
        self.assertEqual((found.announced, found.in_progress, found.expired), (1, 0, 0))
        (export,) = self.rows()
        self.assertEqual((export.status, export.kind, export.google_id, export.created_at, export.ready_at, export.expires_at, export.part_count),
                         ("announced", "initial", "g-1", SCHEDULED + 600, NOW, SCHEDULED + 7 * DAY, 2))
        self.assertEqual(self.parts(export.id), [(1, 100, "pending", 0), (2, 50, "pending", 0)])
        self.assertEqual(self.downloads(), [("download", {"email": "jo.takeout@example.test", "export_id": export.id}, "queued")])
        self.assertEqual(self.event_types().count("export_announced"), 1)

    def test_listing_the_same_export_again_changes_nothing(self):
        self.reconcile([listed()])
        with db.session(self.engine) as opened:
            before = (opened.execute(select(Export)).scalars().all()[0].ready_at, len(opened.execute(select(ExportPart)).scalars().all()), len(opened.execute(select(BrowserJob)).scalars().all()), len(opened.execute(select(Event)).scalars().all()))
        found = self.reconcile([listed()], NOW + 3 * DAY)
        self.assertEqual((found.announced, found.in_progress, found.expired), (0, 0, 0))
        with db.session(self.engine) as opened:
            after = (opened.execute(select(Export)).scalars().all()[0].ready_at, len(opened.execute(select(ExportPart)).scalars().all()), len(opened.execute(select(BrowserJob)).scalars().all()), len(opened.execute(select(Event)).scalars().all()))
        self.assertEqual(before, after)
        self.assertEqual(len(self.rows()), 1)

    def test_an_export_in_progress_is_recorded_without_a_ready_time_then_announced_once_when_ready(self):
        found = self.reconcile([listed(state="in_progress")])
        self.assertEqual((found.in_progress, found.announced), (1, 0))
        (export,) = self.rows()
        self.assertEqual((export.status, export.ready_at, export.part_count, export.kind), ("announced", None, None, "initial"))
        self.assertEqual(self.parts(export.id), [])
        self.assertEqual(self.downloads(), [])
        self.reconcile([listed(state="in_progress")], NOW + 600)
        self.assertEqual((len(self.rows()), self.downloads()), (1, []))
        found = self.reconcile([listed()], NOW + DAY)
        self.assertEqual(found.announced, 1)
        (export,) = self.rows()
        self.assertEqual((export.ready_at, export.part_count, export.kind), (NOW + DAY, 2, "initial"))
        self.assertEqual(len(self.parts(export.id)), 2)
        self.assertEqual(len(self.downloads()), 1)
        self.reconcile([listed()], NOW + 2 * DAY)
        self.assertEqual(len(self.downloads()), 1)

    def test_an_export_first_seen_after_it_expired_is_expired_and_never_enqueued(self):
        found = self.reconcile([listed(created=SCHEDULED, expires=SCHEDULED + 7 * DAY)], SCHEDULED + 8 * DAY)
        self.assertEqual((found.expired, found.announced), (1, 0))
        (export,) = self.rows()
        self.assertEqual((export.status, export.ready_at), ("expired", None))
        self.assertEqual((self.parts(export.id), self.downloads()), ([], []))
        self.assertEqual(self.event_types().count("export_expired"), 1)
        self.reconcile([listed(created=SCHEDULED, expires=SCHEDULED + 7 * DAY)], SCHEDULED + 9 * DAY)
        self.assertEqual((len(self.rows()), self.event_types().count("export_expired"), self.downloads()), (1, 1, []))

    def test_an_export_google_lists_as_expired_is_expired_and_never_enqueued(self):
        self.reconcile([listed(state="expired")])
        (export,) = self.rows()
        self.assertEqual((export.status, self.downloads()), ("expired", []))

    def test_a_ready_listing_after_the_expiry_time_never_downloads(self):
        self.reconcile([listed(state="in_progress", expires=None)], NOW)
        self.reconcile([listed(expires=NOW + DAY)], NOW + 2 * DAY)
        (export,) = self.rows()
        self.assertEqual((export.status, self.downloads()), ("expired", []))

    def test_a_waiting_export_that_expires_is_marked_expired_but_a_downloaded_one_is_kept(self):
        self.reconcile([listed("g-wait"), listed("g-done", created=SCHEDULED + 700), listed("g-fail", created=SCHEDULED + 800)])
        with db.session(self.engine) as opened:
            for export in opened.execute(select(Export)).scalars():
                export.status = {"g-wait": "downloading", "g-done": "downloaded", "g-fail": "failed"}[export.google_id]
        self.reconcile([listed("g-wait", "expired"), listed("g-done", "expired", created=SCHEDULED + 700), listed("g-fail", "expired", created=SCHEDULED + 800)], NOW + 8 * DAY)
        self.assertEqual({row.google_id: row.status for row in self.rows()}, {"g-wait": "expired", "g-done": "downloaded", "g-fail": "failed"})

    def test_the_first_export_of_a_schedule_is_initial_and_later_ones_incremental(self):
        self.reconcile([listed("g-2", created=SCHEDULED + 61 * DAY, expires=SCHEDULED + 68 * DAY), listed("g-1")], SCHEDULED + 62 * DAY)
        self.assertEqual([(row.google_id, row.kind) for row in self.rows()], [("g-1", "initial"), ("g-2", "incremental")])

    def test_an_export_from_before_the_schedule_is_manual_and_does_not_make_the_next_one_incremental(self):
        self.reconcile([listed("g-old", created=SCHEDULED - 30 * DAY, expires=SCHEDULED + 3 * DAY), listed("g-1")])
        self.assertEqual({row.google_id: row.kind for row in self.rows()}, {"g-old": "manual", "g-1": "initial"})

    def test_an_account_without_a_recorded_schedule_still_starts_with_an_initial_export(self):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            account.schedule_created = account.schedule_end = None
        self.reconcile([listed("g-1"), listed("g-2", created=SCHEDULED + 61 * DAY, expires=SCHEDULED + 68 * DAY)])
        self.assertEqual([(row.google_id, row.kind) for row in self.rows()], [("g-1", "initial"), ("g-2", "incremental")])

    def test_a_renewed_schedule_starts_with_an_initial_export_again(self):
        self.reconcile([listed("g-1")])
        renewed = at(2027, 11, 1, 3, 0)
        with db.session(self.engine) as opened:
            opened.get(GoogleAccount, self.account).schedule_created = renewed
        self.reconcile([listed("g-9", created=renewed + 300, expires=renewed + 7 * DAY)], renewed + 3600)
        self.assertEqual({row.google_id: row.kind for row in self.rows()}, {"g-1": "initial", "g-9": "initial"})

    def test_two_accounts_with_the_same_google_id_are_kept_apart(self):
        with db.session(self.engine) as opened:
            other = GoogleAccount(email="other@example.test", default_person_id=add_person(opened, "al"), created=1.0, session_state="ok")
            opened.add(other)
            opened.flush()
            exports.reconcile(opened, other, [listed()], NOW)
        self.reconcile([listed()])
        self.assertEqual(len(self.rows()), 2)


class ListingFromResultTests(unittest.TestCase):
    def test_a_good_result_is_read(self):
        result = {"exports": [{"google_id": "a", "created_at": 5, "state": "ready", "expires_at": 9.5, "parts": [{"number": 2, "bytes": 7}, {"number": 1, "bytes": 3}]},
                              {"google_id": "b", "created_at": 6.0, "state": "in_progress", "expires_at": None, "parts": []}]}
        self.assertEqual(exports.listing_from_result(result), [exports.Listed("a", 5.0, "ready", 9.5, (exports.ListedPart(1, 3), exports.ListedPart(2, 7))), exports.Listed("b", 6.0, "in_progress", None, ())])

    def test_anything_else_is_refused(self):
        good = {"google_id": "a", "created_at": 5, "state": "expired", "expires_at": 9, "parts": []}
        bad = [
            {}, {"exports": "x"}, {"exports": [1]}, {"exports": [{**good, "google_id": ""}]}, {"exports": [{**good, "google_id": 3}]},
            {"exports": [{**good, "google_id": "x" * 201}]}, {"exports": [{**good, "state": "done"}]}, {"exports": [{**good, "created_at": True}]},
            {"exports": [{**good, "created_at": 0}]}, {"exports": [{**good, "expires_at": "soon"}]}, {"exports": [{**good, "parts": "x"}]},
            {"exports": [{**good, "parts": [1]}]}, {"exports": [{**good, "parts": [{"number": 0, "bytes": 1}]}]}, {"exports": [{**good, "parts": [{"number": 1.5, "bytes": 1}]}]},
            {"exports": [{**good, "parts": [{"number": 1, "bytes": -1}]}]}, {"exports": [{**good, "parts": [{"number": 1, "bytes": 1}, {"number": 1, "bytes": 2}]}]},
            {"exports": [{**good, "state": "ready"}]}, {"exports": [good, good]}, {"exports": [{**good, "google_id": str(n)} for n in range(exports.MAX_EXPORTS + 1)]},
            {"exports": [{**good, "parts": [{"number": n + 1, "bytes": 1} for n in range(exports.MAX_PARTS + 1)]}]},
        ]
        for result in bad:
            with self.subTest(result=str(result)[:80]), self.assertRaises(exports.UnreadableListing):
                exports.listing_from_result(result)


    def test_times_that_are_not_finite_or_out_of_range_are_refused(self):
        good = {"google_id": "a", "created_at": 5, "state": "ready", "expires_at": 9, "parts": [{"number": 1, "bytes": 3}]}
        odd = (float("inf"), float("-inf"), float("nan"), 1e300, 10**400, dates.LATEST_INSTANT + 1)
        for value in odd:
            for name in ("created_at", "expires_at"):
                with self.subTest(name=name, value=value), self.assertRaises(exports.UnreadableListing):
                    exports.listing_from_result({"exports": [{**good, name: value}]})
            with self.subTest(part=value), self.assertRaises(exports.UnreadableListing):
                exports.listing_from_result({"exports": [{**good, "parts": [{"number": 1, "bytes": value}]}]})
        self.assertEqual(exports.listing_from_result({"exports": [{**good, "created_at": dates.LATEST_INSTANT}]})[0].created_at, dates.LATEST_INSTANT)


class NextExportTests(unittest.TestCase):
    def account(self, created, end):
        return GoogleAccount(email="a@example.test", default_person_id=1, created=1.0, schedule_created=created, schedule_end=end)

    def test_the_next_export_is_a_month_after_the_last_scheduled_one(self):
        account = self.account(at(2026, 11, 2, 3, 0), at(2027, 11, 2, 3, 0))
        self.assertEqual(exports.next_export_at(account, at(2026, 11, 2, 12, 0)), at(2026, 12, 2, 3, 0))
        self.assertEqual(exports.next_export_at(account, at(2026, 12, 2, 3, 0)), at(2027, 1, 2, 3, 0))
        self.assertEqual(exports.next_export_at(account, at(2027, 8, 1)), at(2027, 8, 2, 3, 0))
        self.assertEqual(exports.next_export_at(account, at(2027, 9, 3)), at(2027, 10, 2, 3, 0))

    def test_the_next_export_keeps_the_local_wall_clock_across_a_daylight_saving_change(self):
        account = self.account(at(2026, 3, 10, 4, 0), at(2027, 3, 10, 5, 0))
        self.assertEqual(exports.next_export_at(account, at(2026, 10, 11), ZoneInfo("America/New_York")), at(2026, 11, 10, 5, 0))
        self.assertEqual(exports.next_export_at(account, at(2026, 10, 11)), at(2026, 11, 10, 4, 0))

    def test_there_is_none_after_the_last_scheduled_export_or_without_a_schedule(self):
        account = self.account(at(2026, 11, 2, 3, 0), at(2027, 11, 2, 3, 0))
        self.assertIsNone(exports.next_export_at(account, at(2027, 10, 3)))
        self.assertIsNone(exports.next_export_at(self.account(None, None), 1.0))
        self.assertIsNone(exports.next_export_at(self.account(at(2026, 1, 1), None), 1.0))

    def test_a_stored_schedule_time_that_is_not_finite_means_no_next_export(self):
        for created, end in ((float("inf"), at(2027, 11, 2)), (at(2026, 11, 2), float("inf")), (float("-inf"), at(2027, 11, 2)), (1e300, 2e300)):
            with self.subTest(created=created, end=end):
                self.assertIsNone(exports.next_export_at(self.account(created, end), at(2026, 11, 3)))

    def test_a_month_that_is_too_short_uses_its_last_days(self):
        account = self.account(at(2026, 12, 31, 3, 0), at(2027, 12, 31, 3, 0))
        self.assertEqual(exports.next_export_at(account, at(2027, 2, 1)), at(2027, 2, 28, 3, 0))


    def test_a_short_month_ends_on_its_real_last_day(self):
        cases = [
            (at(2026, 12, 31, 3, 0), at(2027, 2, 1), at(2027, 2, 28, 3, 0)),
            (at(2026, 12, 31, 3, 0), at(2027, 4, 1), at(2027, 4, 30, 3, 0)),
            (at(2026, 12, 31, 3, 0), at(2027, 6, 1), at(2027, 6, 30, 3, 0)),
            (at(2027, 12, 31, 3, 0), at(2028, 2, 1), at(2028, 2, 29, 3, 0)),
            (at(2027, 12, 30, 3, 0), at(2028, 2, 1), at(2028, 2, 29, 3, 0)),
            (at(2026, 12, 30, 3, 0), at(2027, 2, 1), at(2027, 2, 28, 3, 0)),
            (at(2026, 12, 29, 3, 0), at(2027, 2, 1), at(2027, 2, 28, 3, 0)),
            (at(2027, 12, 29, 3, 0), at(2028, 2, 1), at(2028, 2, 29, 3, 0)),
            (at(2026, 8, 31, 3, 0), at(2026, 10, 1), at(2026, 10, 31, 3, 0)),
            (at(2026, 8, 31, 3, 0), at(2026, 12, 1), at(2026, 12, 31, 3, 0)),
        ]
        for created, now, expected in cases:
            with self.subTest(created=created, now=now):
                account = self.account(created, created + 400 * DAY)
                self.assertEqual(exports.next_export_at(account, now), expected)


class CheckingTests(ReconcileCase):
    zone = ZoneInfo("Europe/Berlin")

    def add_account(self, email, state="ok"):
        with db.session(self.engine) as opened:
            account = GoogleAccount(email=email, default_person_id=add_person(opened, email.split("@")[0]), created=1.0, session_state=state)
            opened.add(account)
            opened.flush()
            return account.id

    def status_jobs(self):
        with db.session(self.engine) as opened:
            return sorted((job.account_id, job.status, json.loads(job.payload)["email"]) for job in opened.execute(select(BrowserJob).where(BrowserJob.kind == "status")).scalars())

    def test_check_now_queues_one_job_for_each_connected_account_only(self):
        second = self.add_account("second@example.test")
        self.add_account("third@example.test", "needs_sign_in")
        self.add_account("fourth@example.test", "unknown")
        with db.session(self.engine) as opened:
            queued, skipped = exports.check_now(opened, NOW)
        self.assertEqual((len(queued), skipped), (2, 0))
        self.assertEqual(self.status_jobs(), [(self.account, "queued", "jo.takeout@example.test"), (second, "queued", "second@example.test")])

    def test_check_now_does_not_pile_up_jobs_for_an_account_that_has_one_open(self):
        with db.session(self.engine) as opened:
            exports.check_now(opened, NOW)
            queued, skipped = exports.check_now(opened, NOW + 5)
        self.assertEqual((len(queued), skipped), (0, 1))
        self.assertEqual(len(self.status_jobs()), 1)

    def test_a_locked_database_is_busy_and_not_an_account_that_has_a_job_open(self):
        impatient = impatient_engine(self, self.engine, 0.05)
        with another_writer_holds_the_lock(self.engine):
            with db.session(impatient) as opened, self.assertRaises(browser_jobs.Busy):
                exports.check_now(opened, NOW)
        self.assertEqual(self.status_jobs(), [])
        with db.session(self.engine) as opened:
            queued, skipped = exports.check_now(opened, NOW)
        self.assertEqual((len(queued), skipped), (1, 0))

    def test_the_daily_check_spreads_accounts_and_runs_each_once_a_day(self):
        ids = [self.account] + [self.add_account(f"a{n}@example.test") for n in range(5)]
        slots = [exports.check_slot(each, NOW, self.zone) for each in ids]
        self.assertGreater(len(set(slots)), 1)
        for slot in slots:
            local = datetime.fromtimestamp(slot, self.zone)
            self.assertTrue(3 <= local.hour < 7, local)
            self.assertEqual(local.date(), datetime.fromtimestamp(NOW, self.zone).date())
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, min(slots) - 1, self.zone), 0)
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, max(slots) + 1, self.zone), len(ids))
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, max(slots) + 600, self.zone), 0)
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, max(slots) + DAY + 1, self.zone), 0)
        self.assertEqual(len(self.status_jobs()), len(ids))

    def test_the_daily_check_runs_again_the_next_day_and_skips_accounts_that_need_a_sign_in(self):
        with db.session(self.engine) as opened:
            first = exports.check_slot(self.account, NOW, self.zone)
            exports.check_due(opened, first + 1, self.zone)
            job = opened.execute(select(BrowserJob)).scalars().one()
            job.status = "done"
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, first + DAY + 1, self.zone), 1)
            opened.get(GoogleAccount, self.account).session_state = "needs_sign_in"
            opened.execute(select(BrowserJob)).scalars().all()[-1].status = "done"
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, first + 2 * DAY + 1, self.zone), 0)

    def test_a_manual_check_counts_as_the_days_check(self):
        slot = exports.check_slot(self.account, NOW, self.zone)
        with db.session(self.engine) as opened:
            exports.check_now(opened, slot + 10)
            opened.execute(select(BrowserJob)).scalars().one().status = "done"
        with db.session(self.engine) as opened:
            self.assertEqual(exports.check_due(opened, slot + 600, self.zone), 0)

    def test_a_manual_check_before_the_slot_counts_as_the_days_check(self):
        slot = exports.check_slot(self.account, NOW, self.zone)
        start_of_day = datetime.fromtimestamp(slot, self.zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        manual = start_of_day + 3600
        with db.session(self.engine) as opened:
            queued, _ = exports.check_now(opened, manual)
            self.assertEqual(len(queued), 1)
            self.assertEqual(exports.check_due(opened, manual + 60, self.zone), 0)
        for tick in (slot - 1, slot + 1, slot + 600):
            with db.session(self.engine) as opened:
                self.assertEqual(exports.due_for_check(opened, tick, self.zone), [])
        with db.session(self.engine) as opened:
            opened.execute(select(BrowserJob)).scalars().one().status = "done"
        for tick in (slot + 1, slot + 3600):
            with db.session(self.engine) as opened:
                self.assertEqual(exports.check_due(opened, tick, self.zone), 0)
        self.assertEqual(len(self.status_jobs()), 1)
        with db.session(self.engine) as opened:
            self.assertEqual(exports.due_for_check(opened, slot + DAY + 1, self.zone), [self.account])

    def test_a_status_job_blocks_changing_the_accounts_email_while_open(self):
        with db.session(self.engine) as opened:
            exports.check_now(opened, NOW)
            self.assertTrue(browser_jobs.has_open_onboarding_job(opened, self.account))


class ViewTests(ReconcileCase):
    def test_exports_and_parts_are_shown_newest_first_with_progress_from_the_part_status(self):
        self.reconcile([listed("g-1")])
        self.reconcile([listed("g-2", created=SCHEDULED + 61 * DAY, expires=SCHEDULED + 68 * DAY)], SCHEDULED + 62 * DAY)
        with db.session(self.engine) as opened:
            first = opened.execute(select(ExportPart).where(ExportPart.number == 1)).scalars().first()
            first.status, first.downloads_used = "done", 1
            shown = [exports.as_dict(opened, each) for each in exports.listing(opened)]
            self.assertEqual([each["created_at"] for each in shown], sorted((each["created_at"] for each in shown), reverse=True))
            self.assertEqual(exports.one(opened, shown[1]["id"]).google_id, "g-1")
            self.assertEqual([(part["number"], part["progress"], part["downloads_used"], part["status"]) for part in shown[1]["parts"]], [(1, 100, 1, "done"), (2, 0, 0, "pending")])
            with self.assertRaises(exports.NoSuchExport):
                exports.one(opened, 9999)
            self.assertEqual(exports.schedule_dicts(opened, NOW)[0]["last_export_at"], SCHEDULED + 61 * DAY)


if __name__ == "__main__":
    unittest.main()
