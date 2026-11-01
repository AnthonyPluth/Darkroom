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


if __name__ == "__main__":
    unittest.main()
