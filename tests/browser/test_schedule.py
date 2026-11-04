import unittest
from datetime import UTC, datetime

from darkroom_browser import selectors
from darkroom_browser.jobs import REGISTRY, schedule, session
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN, UNKNOWN
from tests.browser.test_parse_schedule import CONFIRMED, ONE_OFF
from tests.snapshots import snapshot_pending

RECORDED_CLICKS = [
    "button:has-text('Deselect all')",
    "input[type='checkbox'][aria-label='Google Photos']",
    "button:has-text('Next step')",
    "[aria-label='Delivery method']",
    "[role='option']:has-text('Send download link via email')",
    "input[type='radio'][aria-label*='Export every 2 months for 1 year']",
    "[aria-label='File type']",
    "[role='option']:has-text('.zip')",
    "[aria-label='File size']",
    "[role='option']:has-text('50 GB')",
    "button:has-text('Create export')",
]
CREATED = datetime(2026, 11, 4, 9, 30, tzinfo=UTC).timestamp()


def export_page(confirmed=CONFIRMED):
    return ScriptedBrowser([SIGNED_IN], after_click={selectors.EXPORT_CREATE: [confirmed]})


@snapshot_pending
class ScheduleJobTests(unittest.TestCase):
    def test_happy_path_sends_the_recorded_click_sequence_and_returns_the_dates(self):
        browser = export_page()
        ctx, _, reporter = context_for(browser, wall=CREATED)
        result = schedule.create_schedule(ctx)
        self.assertEqual(browser.clicks, RECORDED_CLICKS)
        self.assertEqual(browser.waited, RECORDED_CLICKS)
        self.assertEqual(result, {"schedule_created": CREATED, "schedule_end": datetime(2027, 11, 4, 9, 30, tzinfo=UTC).timestamp()})
        self.assertEqual(len(reporter.kinds("progress")), 11)
        self.assertEqual(reporter.kinds("waiting"), [])
        self.assertEqual(browser.typed, [])

    def test_a_page_missing_a_control_stops_before_clicking_anything_further(self):
        browser = export_page()
        browser.missing.add(selectors.EXPORT_SIZE_50GB)
        ctx, _, _ = context_for(browser, wall=CREATED)
        with self.assertRaises(TimeoutError):
            schedule.create_schedule(ctx)
        self.assertEqual(browser.clicks, RECORDED_CLICKS[:9])
        self.assertNotIn(selectors.EXPORT_CREATE, browser.clicks)

    def test_a_one_off_confirmation_is_not_a_schedule(self):
        ctx, _, _ = context_for(export_page(ONE_OFF), wall=CREATED)
        with self.assertRaises(schedule.ScheduleNotConfirmed):
            schedule.create_schedule(ctx)

    def test_a_confirmation_that_arrives_after_a_few_seconds_is_waited_for(self):
        browser = ScriptedBrowser([SIGNED_IN], after_click={selectors.EXPORT_CREATE: [SIGNED_IN, UNKNOWN, CONFIRMED]})
        ctx, clock, _ = context_for(browser, wall=CREATED)
        result = schedule.create_schedule(ctx)
        self.assertEqual(result["schedule_created"], CREATED)
        self.assertEqual(clock.slept, [5.0, 5.0])
        self.assertEqual(browser.clicks, RECORDED_CLICKS)

    def test_a_sign_in_page_after_create_is_a_definitive_failure(self):
        browser = ScriptedBrowser([SIGNED_IN], after_click={selectors.EXPORT_CREATE: [SIGN_IN_NEEDED]})
        ctx, clock, _ = context_for(browser, wall=CREATED)
        with self.assertRaises(schedule.UnexpectedPage):
            schedule.create_schedule(ctx)
        self.assertEqual(clock.slept, [])

    def test_a_confirmation_that_appears_after_the_last_wait_is_still_read(self):
        pages = [UNKNOWN] * 12 + [CONFIRMED]
        browser = ScriptedBrowser([SIGNED_IN], after_click={selectors.EXPORT_CREATE: pages})
        ctx, clock, _ = context_for(browser, wall=CREATED)
        self.assertEqual(schedule.create_schedule(ctx)["schedule_created"], CREATED)
        self.assertEqual(sum(clock.slept), 60.0)

    def test_no_confirmation_within_a_minute_fails_without_clicking_create_twice(self):
        browser = export_page(UNKNOWN)
        ctx, clock, _ = context_for(browser, wall=CREATED)
        with self.assertRaises(schedule.ScheduleNotConfirmed):
            schedule.create_schedule(ctx)
        self.assertEqual(sum(clock.slept), 60.0)
        self.assertEqual(browser.clicks.count(selectors.EXPORT_CREATE), 1)

    def test_an_unexpected_page_after_create_stops(self):
        ctx, _, _ = context_for(export_page(UNKNOWN), wall=CREATED)
        with self.assertRaises(schedule.UnexpectedPage):
            schedule.create_schedule(ctx)

    def test_an_unrecognised_start_page_is_never_clicked(self):
        browser = ScriptedBrowser([UNKNOWN])
        ctx, _, _ = context_for(browser)
        with self.assertRaises(session.UnexpectedPage):
            schedule.create_schedule(ctx)
        self.assertEqual(browser.clicks, [])

    def test_a_signed_out_session_waits_for_the_person_and_then_carries_on(self):
        browser = ScriptedBrowser([SIGN_IN_NEEDED, SIGNED_IN], after_click={selectors.EXPORT_CREATE: [CONFIRMED]})
        ctx, _, reporter = context_for(browser, wall=CREATED)
        schedule.create_schedule(ctx)
        self.assertEqual(reporter.kinds("waiting"), ["sign_in"])
        self.assertEqual(browser.clicks, RECORDED_CLICKS)

    def test_the_end_is_a_year_after_even_from_a_leap_day(self):
        leap = datetime(2028, 2, 29, 12, 0, tzinfo=UTC).timestamp()
        self.assertEqual(schedule.one_year_after(leap), datetime(2029, 2, 28, 12, 0, tzinfo=UTC).timestamp())
        self.assertEqual(schedule.one_year_after(CREATED), datetime(2027, 11, 4, 9, 30, tzinfo=UTC).timestamp())

    def test_a_schedule_is_never_created_in_a_different_account(self):
        browser = export_page()
        ctx, _, _ = context_for(browser, wall=CREATED, email="someone.else@example.test")
        with self.assertRaises(session.WrongAccount):
            schedule.create_schedule(ctx)
        self.assertEqual(browser.clicks, [])

    def test_the_job_is_registered(self):
        self.assertIs(REGISTRY["schedule"], schedule.create_schedule)


if __name__ == "__main__":
    unittest.main()
