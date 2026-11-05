import unittest

from sqlalchemy import select

from darkroom import config
from darkroom.domain import alerts, events
from darkroom.storage import db
from darkroom.storage.models import Alert
from tests.shared import own_database

HOUR = 3600.0
DAY = 24 * HOUR


class Notifier:
    def __init__(self, outcome=alerts.SENT):
        self.sent = []
        self.outcome = outcome

    def __call__(self, settings, title, message, url):
        self.sent.append((title, message, url))
        return self.outcome


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.settings = config.load()
        self.notifier = Notifier()

    def open_sign_in(self, opened, now):
        return alerts.open_alert(opened, self.settings, "sign-in:a", "sign_in_needed", "a@x.test", alerts.sign_in_needed("a@x.test"),
                                 "https://d.example/browser", now, notifier=self.notifier, about=alerts.NOBODY)

    def tick(self, now):
        with db.session(self.engine) as opened:
            return alerts.tick(opened, self.settings, now, self.notifier)

    def test_a_kept_open_alert_is_sent_at_0_24h_and_4d_and_never_again(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0][2], "https://d.example/browser")
        self.assertEqual(self.tick(DAY - 1), 0)
        self.assertEqual(self.tick(DAY), 1)
        self.assertEqual(self.tick(DAY + HOUR), 0)
        self.assertEqual(self.tick(4 * DAY - 1), 0)
        self.assertEqual(self.tick(4 * DAY), 1)
        self.assertEqual(self.tick(40 * DAY), 0)
        self.assertEqual(len(self.notifier.sent), 3)

    def test_opening_the_same_alert_twice_sends_once(self):
        with db.session(self.engine) as opened:
            first = self.open_sign_in(opened, 0.0)
            second = self.open_sign_in(opened, 5.0)
            self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_close_stops_repeats_and_reopening_starts_over(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        with db.session(self.engine) as opened:
            self.assertEqual(alerts.close(opened, "sign-in:a", 10.0), 1)
            self.assertEqual(alerts.close(opened, "sign-in:a", 11.0), 0)
        self.assertEqual(self.tick(2 * DAY), 0)
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 3 * DAY)
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.tick(4 * DAY), 1)

    def test_one_shot_alerts_are_closed_at_once(self):
        with db.session(self.engine) as opened:
            alert = alerts.open_alert(opened, self.settings, "imported:1", "export_imported", "jo", alerts.export_imported("3 added"), None, 0.0,
                                      repeats=alerts.ONCE, notifier=self.notifier, about=alerts.NOBODY)
            self.assertEqual(alert.closed, 0.0)
        self.assertEqual(self.tick(10 * DAY), 0)

    def test_one_shot_kinds_close_at_once_without_asking(self):
        with db.session(self.engine) as opened:
            for kind in ("export_imported", "schedule_renewed", "failure"):
                alert = alerts.open_alert(opened, self.settings, f"k:{kind}", kind, "x", alerts.failure("x"), None, 0.0, notifier=self.notifier, about=alerts.NOBODY)
                self.assertEqual(alert.closed, 0.0)
        self.assertEqual(self.tick(10 * DAY), 0)

    def test_repeats_keep_the_original_title(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        self.tick(DAY)
        self.assertEqual({title for title, _, _ in self.notifier.sent}, {"Google sign-in needed"})

    def test_every_send_records_an_event(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        self.tick(DAY)
        with db.session(self.engine) as opened:
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_sent", "alert_sent"])

    def test_a_failed_send_is_recorded_and_the_caller_goes_on(self):
        self.notifier = Notifier(alerts.FAILED)
        with db.session(self.engine) as opened:
            alert = self.open_sign_in(opened, 0.0)
            self.assertEqual(alert.sent_count, 1)
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_failed"])

    def test_disabled_home_assistant_is_recorded_too(self):
        self.notifier = Notifier(alerts.DISABLED)
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_disabled"])
            self.assertEqual(opened.execute(select(Alert)).scalars().one().sent_count, 1)

    def test_wording(self):
        self.assertIn("BACK", alerts.originals_unmatched("Jo", "BACK", 4).message.upper())
        without_pixel = alerts.originals_unmatched("Jo", None, 4).message
        self.assertIn("Jo's", without_pixel)
        self.assertNotIn("None", without_pixel)
        self.assertNotIn("delet", without_pixel.lower())
        self.assertIn("2026", alerts.schedule_ending("a@x", "2026-12-01").message)
        self.assertIn("2027", alerts.schedule_renewed("a@x", "2027-12-01").message)
        self.assertIn("2026", alerts.schedule_renewal_failed("a@x", "2026-12-01").message)
        self.assertIn("2026", alerts.schedule_renewal_unconfirmed("a@x", "2026-12-01").message)
        self.assertIn("E1", alerts.export_expiring("E1", "soon").message)
        self.assertIn("Import", alerts.failure("Import").message)


if __name__ == "__main__":
    unittest.main()
