import unittest
from datetime import UTC, datetime

from darkroom import config
from darkroom.domain import alerts, events
from darkroom.server import jobs
from darkroom.storage import db
from tests.shared import own_database


def at(day, hour, minute=0):
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC).timestamp()


class SchedulerTests(unittest.TestCase):
    def test_an_every_job_runs_once_per_interval_under_a_fake_clock(self):
        scheduler = jobs.Scheduler()
        runs = []
        scheduler.every("a", 600, runs.append, now=0)
        self.assertEqual(scheduler.run_due(0), ["a"])
        self.assertEqual(scheduler.run_due(599), [])
        self.assertEqual(scheduler.run_due(600), ["a"])
        self.assertEqual(runs, [0, 600])

    def test_first_after_delays_the_first_run(self):
        scheduler = jobs.Scheduler()
        scheduler.every("a", 600, lambda now: None, first_after=100, now=0)
        self.assertEqual(scheduler.run_due(99), [])
        self.assertEqual(scheduler.run_due(100), ["a"])

    def test_one_failing_job_never_stops_the_others(self):
        scheduler = jobs.Scheduler()
        ran = []

        def broken(now):
            raise RuntimeError("boom")

        scheduler.every("broken", 10, broken)
        scheduler.every("fine", 10, ran.append)
        self.assertEqual(scheduler.run_due(0), ["broken", "fine"])
        self.assertEqual(ran, [0])
        self.assertEqual(scheduler.run_due(10), ["broken", "fine"])

    def test_a_job_with_its_own_thread_does_not_hold_up_the_others(self):
        import threading

        scheduler = jobs.Scheduler()
        release, started, ran = threading.Event(), threading.Event(), []

        def slow(now):
            started.set()
            release.wait(5)
            ran.append(("slow", now))

        scheduler.every("slow", 10, slow, own_thread=True)
        scheduler.every("quick", 10, lambda now: ran.append(("quick", now)))
        self.addCleanup(release.set)
        self.assertEqual(scheduler.run_due(0), ["slow", "quick"])
        self.assertTrue(started.wait(5))
        self.assertEqual(ran, [("quick", 0)])
        self.assertEqual(scheduler.run_due(10), ["quick"])
        self.assertEqual(scheduler.run_due(20), ["quick"])
        self.assertEqual(ran, [("quick", 0), ("quick", 10), ("quick", 20)])
        release.set()
        scheduler.jobs["slow"].worker.join(5)
        self.assertEqual(scheduler.run_due(30), ["slow", "quick"])
        scheduler.jobs["slow"].worker.join(5)
        self.assertEqual([entry for entry in ran if entry[0] == "slow"], [("slow", 0), ("slow", 30)])

    def test_a_job_with_its_own_thread_that_fails_is_logged_and_runs_again(self):
        from unittest import mock

        scheduler = jobs.Scheduler()
        calls = []

        def broken(now):
            calls.append(now)
            raise RuntimeError("boom")

        scheduler.every("broken", 10, broken, own_thread=True)
        with mock.patch.object(jobs.monitoring, "log") as logged:
            scheduler.run_due(0)
            scheduler.jobs["broken"].worker.join(5)
            scheduler.run_due(10)
            scheduler.jobs["broken"].worker.join(5)
        self.assertEqual(calls, [0, 10])
        logged.assert_called_with("job failed", job="broken", error="RuntimeError")

    def test_daily_at_runs_at_the_local_hour_each_day(self):
        scheduler = jobs.Scheduler("UTC")
        ran = []
        scheduler.daily_at("nightly", 3, ran.append, now=at(1, 1))
        self.assertEqual(scheduler.run_due(at(1, 2, 59)), [])
        self.assertEqual(scheduler.run_due(at(1, 3)), ["nightly"])
        self.assertEqual(scheduler.run_due(at(1, 23)), [])
        self.assertEqual(scheduler.run_due(at(2, 3)), ["nightly"])
        self.assertEqual(len(ran), 2)

    def test_daily_at_after_the_hour_waits_for_tomorrow(self):
        scheduler = jobs.Scheduler("UTC")
        scheduler.daily_at("nightly", 3, lambda now: None, now=at(1, 5))
        self.assertEqual(scheduler.run_due(at(1, 6)), [])
        self.assertEqual(scheduler.run_due(at(2, 3)), ["nightly"])

    def test_daily_at_uses_the_configured_zone(self):
        scheduler = jobs.Scheduler("America/New_York")
        scheduler.daily_at("nightly", 3, lambda now: None, now=at(1, 0))
        self.assertEqual(scheduler.run_due(at(1, 7)), [])
        self.assertEqual(scheduler.run_due(at(1, 8)), ["nightly"])

    def test_run_one_and_the_loop(self):
        scheduler = jobs.Scheduler()
        ran = []
        scheduler.every("a", 10, ran.append)
        scheduler.run_one("a", 5.0)
        self.assertEqual(ran, [5.0])
        thread = scheduler.start(lambda: 0.0, poll=0.01)
        for _ in range(200):
            if len(ran) > 1:
                break
            thread.join(0.01)
        scheduler.stop()
        thread.join(2)
        self.assertGreater(len(ran), 1)


class BuildTests(unittest.TestCase):
    def test_the_alert_tick_is_registered_every_ten_minutes_and_sends_due_repeats(self):
        engine = own_database(self)
        settings = config.load()
        scheduler = jobs.build(settings, engine)
        self.assertEqual(scheduler.jobs["alerts.tick"].interval, 600.0)
        with db.session(engine) as opened:
            alerts.open_alert(opened, settings, "k", "sign_in_needed", "x", alerts.sign_in_needed("x"), None, 0.0, notifier=lambda *a: "sent")
        scheduler.run_one("alerts.tick", 24 * 3600.0)
        with db.session(engine) as opened:
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_disabled", "alert_sent"])

    def test_the_session_recheck_runs_hourly_and_records_only_notable_outcomes(self):
        from unittest import mock

        from darkroom import oidc

        engine = own_database(self)
        scheduler = jobs.build(config.load(), engine)
        self.assertEqual(scheduler.jobs["sessions.recheck"].interval, 3600.0)
        with mock.patch.object(oidc, "recheck_due", return_value=oidc.RecheckSummary(checked=2, kept=2)):
            scheduler.run_one("sessions.recheck", 10.0)
        with mock.patch.object(oidc, "recheck_due", return_value=oidc.RecheckSummary(checked=3, kept=1, ended=1, unreachable=1)):
            scheduler.run_one("sessions.recheck", 20.0)
        with db.session(engine) as opened:
            found = events.recent(opened)
            self.assertEqual([(e.type, e.detail) for e in found], [("sessions_rechecked", "checked=3 kept=1 ended=1 capped=0 unreachable=1")])

    def test_the_index_job_indexes_each_person(self):
        import tempfile
        from pathlib import Path

        from darkroom.storage.models import MediaItem
        from darkroom.storage.paths import Photos
        from tests.fixtures import media as fixtures
        from tests.shared import add_person

        engine = own_database(self)
        with tempfile.TemporaryDirectory() as folder:
            settings = config.Settings(**{**config.load().__dict__, "photos_root": Path(folder)})
            with db.session(engine) as opened:
                add_person(opened, "jo")
            originals = Photos(Path(folder)).ensure_person_tree("jo")[0]
            fixtures.make_jpeg(originals / "a.jpg", taken="2019:07:04 12:30:15")
            scheduler = jobs.build(settings, engine)
            self.assertEqual(scheduler.jobs["index"].interval, 3600.0)
            scheduler.run_one("index", 5.0)
            with db.session(engine) as opened:
                self.assertEqual(opened.query(MediaItem).count(), 1)


    def test_a_long_import_does_not_delay_the_session_recheck(self):
        import threading
        from unittest import mock

        from darkroom import oidc
        from darkroom.domain.ingest import run
        from darkroom.storage.models import Export
        from tests.import_world import NOW, World

        world = World(self, immich_configured=False)
        export_id = world.hand_dropped().id
        run.request_import(world.opened, export_id)
        world.opened.commit()
        scheduler = jobs.build(world.settings, world.engine, NOW)
        scheduler.jobs["imports"].next_due = scheduler.jobs["sessions.recheck"].next_due = NOW
        release, started, rechecked = threading.Event(), threading.Event(), []
        real = run.run_waiting

        def long_import(opened, settings, at, tools=None):
            started.set()
            release.wait(10)
            return real(opened, settings, at, tools)

        def recheck(opened, settings, at):
            rechecked.append(at)
            return oidc.RecheckSummary()

        self.addCleanup(release.set)
        with mock.patch.object(run, "run_waiting", long_import), mock.patch.object(oidc, "recheck_due", recheck):
            ran = scheduler.run_due(NOW)
            self.assertTrue(started.wait(5))
            self.assertIn("sessions.recheck", ran)
            self.assertIn("imports", ran)
            self.assertEqual(rechecked, [NOW])
            self.assertTrue(scheduler.jobs["imports"].worker.is_alive())
            self.assertNotEqual(world.reopen().get(Export, export_id).status, "imported")
            release.set()
            scheduler.jobs["imports"].worker.join(30)
        self.assertEqual(world.reopen().get(Export, export_id).status, "imported")

    def test_the_import_job_runs_requested_imports_in_its_own_thread(self):
        from darkroom.domain.ingest import run
        from darkroom.storage.models import Export
        from tests.import_world import NOW, World

        world = World(self, immich_configured=False)
        export_id = world.hand_dropped().id
        scheduler = jobs.build(world.settings, world.engine)
        self.assertEqual((scheduler.jobs["imports"].interval, scheduler.jobs["imports"].own_thread), (60.0, True))
        self.assertFalse(any(job.own_thread for name, job in scheduler.jobs.items() if name != "imports"))
        scheduler.run_one("imports", NOW)
        self.assertEqual(world.reopen().get(Export, export_id).status, "downloaded")
        run.request_import(world.opened, export_id)
        world.opened.commit()
        scheduler.run_one("imports", NOW)
        self.assertEqual(world.reopen().get(Export, export_id).status, "imported")

    def test_the_cleanup_job_runs_daily_and_removes_old_staging(self):
        from darkroom.storage.models import Export
        from tests.import_world import DAY, NOW, World

        world = World(self, immich_configured=False)
        export_id = world.hand_dropped().id
        world.run_import(export_id)
        scheduler = jobs.build(world.settings, world.engine)
        self.assertEqual((scheduler.jobs["cleanup"].interval, scheduler.jobs["cleanup"].hour), (None, 4))
        scheduler.run_one("cleanup", NOW + 15 * DAY)
        self.assertFalse((world.root / "_inbox/jo/export-1").exists())
        self.assertEqual(world.reopen().get(Export, export_id).status, "imported")

    def test_the_export_check_runs_every_ten_minutes_and_queues_a_status_job_once_a_day_per_connected_account(self):
        from zoneinfo import ZoneInfo

        from sqlalchemy import select

        from darkroom.domain import exports
        from darkroom.storage.models import BrowserJob, GoogleAccount
        from tests.shared import add_person, settings_from

        engine = own_database(self)
        settings = settings_from()
        with db.session(engine) as opened:
            person = add_person(opened, "jo")
            opened.add(GoogleAccount(email="jo@example.test", default_person_id=person, created=1.0, session_state="ok"))
            opened.add(GoogleAccount(email="signed-out@example.test", default_person_id=person, created=1.0, session_state="needs_sign_in"))
        scheduler = jobs.build(settings, engine, 0)
        self.assertEqual(scheduler.jobs["exports.check"].interval, 600.0)
        morning = exports.check_slot(1, at(5, 12), ZoneInfo("UTC"))
        scheduler.run_one("exports.check", morning - 1)
        scheduler.run_one("exports.check", morning + 1)
        scheduler.run_one("exports.check", morning + 600)
        with db.session(engine) as opened:
            queued = [(job.account_id, job.kind) for job in opened.execute(select(BrowserJob)).scalars()]
        self.assertEqual(queued, [(1, "status")])

    def test_the_daily_check_is_postponed_when_the_database_is_busy_and_runs_on_the_next_tick(self):
        from unittest import mock
        from zoneinfo import ZoneInfo

        from sqlalchemy import select

        from darkroom.domain import browser_jobs, exports
        from darkroom.storage.models import BrowserJob, GoogleAccount
        from tests.shared import add_person, settings_from

        engine = own_database(self)
        with db.session(engine) as opened:
            person = add_person(opened, "jo")
            opened.add(GoogleAccount(email="jo@example.test", default_person_id=person, created=1.0, session_state="ok"))
        scheduler = jobs.build(settings_from(), engine, 0)
        morning = exports.check_slot(1, at(5, 12), ZoneInfo("UTC"))
        with mock.patch.object(browser_jobs, "enqueue_once", side_effect=browser_jobs.Busy("busy")):
            scheduler.run_one("exports.check", morning + 1)
        with db.session(engine) as opened:
            self.assertEqual(opened.execute(select(BrowserJob)).scalars().all(), [])
        scheduler.run_one("exports.check", morning + 601)
        with db.session(engine) as opened:
            self.assertEqual([(job.account_id, job.kind) for job in opened.execute(select(BrowserJob)).scalars()], [(1, "status")])


if __name__ == "__main__":
    unittest.main()
