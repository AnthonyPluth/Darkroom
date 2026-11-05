import tempfile
import unittest

from sqlalchemy import select

from darkroom.domain import alerts, browser_jobs, downloads, exports
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, Export, GoogleAccount
from tests.shared import add_person, own_database, settings_from

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
EXPIRES = NOW + 7 * DAY


class ExpiryCase(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = settings_from(DARKROOM_PUBLIC_URL="https://darkroom.example", DARKROOM_PHOTOS_ROOT=folder.name, TZ="UTC")
        self.sent = []
        with db.session(self.engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo.takeout@example.test", default_person_id=person, created=1.0, session_state="ok")
            opened.add(account)
            opened.flush()
            listing = [exports.Listed("g-1", NOW - 600, "ready", EXPIRES, (exports.ListedPart(1, 100),))]
            exports.reconcile(opened, account, listing, NOW)
            self.export = opened.execute(select(Export)).scalar_one().id

    def notifier(self, settings, title, message, link=None):
        self.sent.append((title, message, link))
        return alerts.SENT

    def watch(self, now):
        with db.session(self.engine) as opened:
            return downloads.watch_expiry(opened, self.settings, now)

    def tick(self, now):
        with db.session(self.engine) as opened:
            return alerts.tick(opened, self.settings, now, self.notifier)

    def expiring(self):
        with db.session(self.engine) as opened:
            return [(alert.key, alert.sent_count, alert.closed) for alert in opened.execute(select(Alert).where(Alert.kind == "export_expiring")).scalars()]

    def set_status(self, status):
        with db.session(self.engine) as opened:
            opened.get(Export, self.export).status = status


class ExpiryAlertTests(ExpiryCase):
    def test_the_alert_opens_four_days_before_expiry_and_repeats_after_a_day(self):
        self.assertEqual(self.watch(EXPIRES - 4 * DAY - 1), 0)
        self.assertEqual(self.watch(EXPIRES - 4 * DAY), 1)
        self.assertEqual(self.watch(EXPIRES - 4 * DAY + HOUR), 0)
        self.assertEqual(self.expiring(), [(downloads.expiry_key(self.export), 1, None)])
        with db.session(self.engine) as opened:
            alert = opened.execute(select(Alert).where(Alert.kind == "export_expiring")).scalar_one()
            self.assertEqual(alert.link, "https://darkroom.example/exports")
            self.assertIn(f"Export {self.export} for jo.takeout@example.test expires on 22 Jan 2027 at 08:00", alert.message)
        self.assertEqual(self.tick(EXPIRES - 3 * DAY - 1), 0)
        self.assertEqual(self.tick(EXPIRES - 3 * DAY), 1)
        self.assertEqual(self.sent[0][0], "A Takeout export is about to expire")
        self.assertEqual(self.expiring()[0][1], 2)

    def test_the_alert_closes_once_the_export_is_downloaded_and_does_not_come_back(self):
        self.watch(EXPIRES - 2 * DAY)
        self.set_status("downloaded")
        self.watch(EXPIRES - 2 * DAY + HOUR)
        ((_, _, closed),) = self.expiring()
        self.assertEqual(closed, EXPIRES - 2 * DAY + HOUR)
        for hours in range(1, 60):
            self.watch(EXPIRES - 2 * DAY + hours * HOUR)
            self.tick(EXPIRES - 2 * DAY + hours * HOUR)
        self.assertEqual(len(self.expiring()), 1)
        self.assertEqual(self.sent, [])

    def test_finishing_the_download_closes_the_alert_at_once(self):
        self.watch(EXPIRES - DAY)
        with db.session(self.engine) as opened:
            job = browser_jobs.claim(opened, EXPIRES - DAY)
            for part in exports.parts_of(opened, self.export):
                part.status = "done"
        with db.session(self.engine) as opened:
            downloads.finish(opened, self.settings, job.id, EXPIRES - DAY + 60)
        self.assertEqual(self.expiring()[0][2], EXPIRES - DAY + 60)

    def test_an_alert_whose_repeat_falls_after_the_expiry_is_closed_before_it_is_sent_again(self):
        opened_at = EXPIRES - 2 * DAY
        self.watch(opened_at)
        scheduler = jobs.build(self.settings, self.engine, NOW)
        scheduler.run_one("alerts.tick", opened_at + 4 * DAY)
        ((_, sent, closed),) = self.expiring()
        self.assertEqual((sent, closed), (1, opened_at + 4 * DAY))

    def test_the_alert_closes_when_the_export_expires(self):
        self.watch(EXPIRES - DAY)
        self.watch(EXPIRES)
        self.assertEqual(self.expiring()[0][2], EXPIRES)
        self.assertEqual(self.watch(EXPIRES + HOUR), 0)

    def test_an_export_that_failed_or_expired_gets_no_alert(self):
        for status in ("failed", "expired", "imported"):
            self.set_status(status)
            self.assertEqual(self.watch(EXPIRES - DAY), 0)
        self.assertEqual(self.expiring(), [])

    def test_an_export_with_no_expiry_or_not_ready_gets_no_alert(self):
        with db.session(self.engine) as opened:
            row = opened.get(Export, self.export)
            row.expires_at = None
        self.assertEqual(self.watch(EXPIRES - DAY), 0)
        with db.session(self.engine) as opened:
            row = opened.get(Export, self.export)
            row.expires_at, row.ready_at = EXPIRES, None
        self.assertEqual(self.watch(EXPIRES - DAY), 0)

    def test_the_scheduler_watches_expiry_every_hour(self):
        scheduler = jobs.build(self.settings, self.engine, NOW)
        self.assertEqual(scheduler.jobs["exports.expiry"].interval, HOUR)
        scheduler.run_one("exports.expiry", EXPIRES - DAY)
        self.assertEqual(len(self.expiring()), 1)


if __name__ == "__main__":
    unittest.main()
