import unittest

from darkroom_browser.pages import takeout
from tests.snapshots import snapshot_pending

CONFIRMED = """<!doctype html><html><head><title>Google Takeout</title><script>var s = "export created";</script></head>
<body><h1>Export created</h1><p>We&#8217;ve started creating a copy of your Google Photos data. Export every month for 1 year.</p>
<p>You&#8217;ll get an email with a download link when each export is ready.</p></body></html>"""

CONFIRMED_SCHEDULED = "<html><body><h2>Your export has been scheduled</h2><p>Frequency: Export every month for 12 months</p></body></html>"

ONE_OFF = "<html><body><h1>Export created</h1><p>We've started creating a copy of your Google Photos data. This can take a long time.</p></body></html>"


@snapshot_pending
class SignedInEmailTests(unittest.TestCase):
    def test_reads_the_account_the_menu_names(self):
        page = """<nav><a aria-label="Google Account: Jo Example  (Jo.Photos@Example.test)">J</a></nav>"""
        self.assertEqual(takeout.signed_in_email(page), "jo.photos@example.test")

    def test_unreadable_pages_give_none(self):
        for page in ("", "<a aria-label='Google Account'>J</a>", "<a aria-label='Settings (a@b.test)'>x</a>", "<p>Google Account: Jo (jo@example.test)</p>"):
            with self.subTest(page=page):
                self.assertIsNone(takeout.signed_in_email(page))


@snapshot_pending
class ParseScheduleConfirmationTests(unittest.TestCase):
    def test_a_recurring_confirmation_names_its_frequency(self):
        self.assertEqual(takeout.parse_schedule_confirmation(CONFIRMED), takeout.ScheduleConfirmation("every month for 1 year"))
        self.assertEqual(takeout.parse_schedule_confirmation(CONFIRMED_SCHEDULED), takeout.ScheduleConfirmation("every month for 12 months"))

    def test_a_one_off_export_is_not_a_schedule(self):
        self.assertIsNone(takeout.parse_schedule_confirmation(ONE_OFF))

    def test_other_pages_and_hidden_script_text_are_not_confirmations(self):
        self.assertIsNone(takeout.parse_schedule_confirmation(""))
        self.assertIsNone(takeout.parse_schedule_confirmation("<html><body><h1>Create a new export</h1></body></html>"))
        hidden = "<html><body><script>var s = 'export created every month for 1 year';</script><p>Choose data</p></body></html>"
        self.assertIsNone(takeout.parse_schedule_confirmation(hidden))

    def test_a_different_frequency_is_not_accepted(self):
        for frequency in ("every 2 months for 1 year", "every 3 months for 1 year"):
            with self.subTest(frequency=frequency):
                page = f"<html><body><h1>Export created</h1><p>Export {frequency}</p></body></html>"
                self.assertIsNone(takeout.parse_schedule_confirmation(page))


if __name__ == "__main__":
    unittest.main()
