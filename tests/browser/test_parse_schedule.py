import unittest

from darkroom_browser.pages import takeout
from tests.snapshots import snapshot_pending

CONFIRMED = """<!doctype html><html><head><title>Google Takeout</title><script>var s = "export created";</script></head>
<body><h1>Export created</h1><p>We&#8217;ve started creating a copy of your Google Photos data. Export every 2 months for 1 year.</p>
<p>You&#8217;ll get an email with a download link when each export is ready.</p></body></html>"""

CONFIRMED_SCHEDULED = "<html><body><h2>Your export has been scheduled</h2><p>Frequency: Export every 2 months for 12 months</p></body></html>"

ONE_OFF = "<html><body><h1>Export created</h1><p>We've started creating a copy of your Google Photos data. This can take a long time.</p></body></html>"


@snapshot_pending
class ParseScheduleConfirmationTests(unittest.TestCase):
    def test_a_recurring_confirmation_names_its_frequency(self):
        self.assertEqual(takeout.parse_schedule_confirmation(CONFIRMED), takeout.ScheduleConfirmation("every 2 months for 1 year"))
        self.assertEqual(takeout.parse_schedule_confirmation(CONFIRMED_SCHEDULED), takeout.ScheduleConfirmation("every 2 months for 12 months"))

    def test_a_one_off_export_is_not_a_schedule(self):
        self.assertIsNone(takeout.parse_schedule_confirmation(ONE_OFF))

    def test_other_pages_and_hidden_script_text_are_not_confirmations(self):
        self.assertIsNone(takeout.parse_schedule_confirmation(""))
        self.assertIsNone(takeout.parse_schedule_confirmation("<html><body><h1>Create a new export</h1></body></html>"))
        hidden = "<html><body><script>var s = 'export created every 2 months for 1 year';</script><p>Choose data</p></body></html>"
        self.assertIsNone(takeout.parse_schedule_confirmation(hidden))

    def test_a_different_frequency_is_not_accepted(self):
        page = "<html><body><h1>Export created</h1><p>Export every 3 months for 1 year</p></body></html>"
        self.assertIsNone(takeout.parse_schedule_confirmation(page))


if __name__ == "__main__":
    unittest.main()
