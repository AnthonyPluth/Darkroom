import unittest
from datetime import UTC, datetime

from darkroom_browser import selectors
from darkroom_browser.jobs import REGISTRY, renew, schedule
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN
from tests.browser.test_parse_schedule import CONFIRMED, ONE_OFF
from tests.browser.test_schedule import RECORDED_CLICKS
from tests.snapshots import snapshot_pending

RENEWED = datetime(2027, 10, 15, 6, 0, tzinfo=UTC).timestamp()


def export_page(confirmed=CONFIRMED):
    return ScriptedBrowser([SIGNED_IN], after_click={selectors.EXPORT_CREATE: [confirmed]})


@snapshot_pending
class RenewJobTests(unittest.TestCase):
    def test_it_is_registered_as_the_renew_job(self):
        self.assertIs(REGISTRY["renew"], renew.renew_schedule)

    def test_it_creates_next_years_schedule_with_the_recorded_clicks_and_returns_the_new_end(self):
        browser = export_page()
        ctx, _, reporter = context_for(browser, wall=RENEWED)
        result = renew.renew_schedule(ctx)
        self.assertEqual(browser.clicks, RECORDED_CLICKS)
        self.assertEqual(result, {"schedule_created": RENEWED, "schedule_end": datetime(2028, 10, 15, 6, 0, tzinfo=UTC).timestamp()})
        self.assertEqual(reporter.kinds("progress")[0], "creating next year's export schedule")
        self.assertEqual(reporter.kinds("waiting"), [])
        self.assertEqual(browser.typed, [])

    def test_it_gives_the_same_result_as_creating_a_schedule(self):
        ctx, _, _ = context_for(export_page(), wall=RENEWED)
        renewed = renew.renew_schedule(ctx)
        ctx, _, _ = context_for(export_page(), wall=RENEWED)
        self.assertEqual(renewed, schedule.create_schedule(ctx))

    def test_a_one_off_confirmation_is_not_a_renewal(self):
        ctx, _, _ = context_for(export_page(ONE_OFF), wall=RENEWED)
        with self.assertRaises(schedule.ScheduleNotConfirmed):
            renew.renew_schedule(ctx)

    def test_a_sign_in_page_after_create_fails_without_a_result(self):
        browser = ScriptedBrowser([SIGNED_IN], after_click={selectors.EXPORT_CREATE: [SIGN_IN_NEEDED]})
        ctx, _, _ = context_for(browser, wall=RENEWED)
        with self.assertRaises(schedule.ScheduleNotConfirmed):
            renew.renew_schedule(ctx)

    def test_a_page_missing_a_control_stops_before_create_is_clicked(self):
        browser = export_page()
        browser.missing.add(selectors.EXPORT_SIZE_50GB)
        ctx, _, _ = context_for(browser, wall=RENEWED)
        with self.assertRaises(TimeoutError):
            renew.renew_schedule(ctx)
        self.assertNotIn(selectors.EXPORT_CREATE, browser.clicks)


if __name__ == "__main__":
    unittest.main()
