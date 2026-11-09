import unittest
from datetime import UTC, datetime

from darkroom_browser import selectors
from darkroom_browser.context import Cancelled
from darkroom_browser.jobs import REGISTRY, schedule, session
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN, UNKNOWN
from tests.browser.test_parse_schedule import CONFIRMED, ONE_OFF
from tests.snapshots import snapshot_pending

RECORDED_CLICKS = [
    "button:has-text('Deselect all')",
    "input[type='checkbox'][name='Google Photos']",
    "button:has-text('Next step')",
    "[role='combobox'][aria-labelledby]",
    "[role='option'][data-value='TEMP']",
    "label:text-is('Export every month for 1 year')",
    "[role='combobox'][aria-label='File type select']",
    "[role='option'][data-value='ZIP']",
    "[role='combobox'][aria-label='File size select']",
    "[role='option'][data-value='53687091200']",
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
        with self.assertRaises(schedule.ScheduleNotConfirmed):
            schedule.create_schedule(ctx)
        self.assertEqual(clock.slept, [])

    def test_any_failure_at_or_after_the_create_click_is_a_maybe_created_failure(self):
        class Crashing(ScriptedBrowser):
            def __init__(self, on_click=False, on_read=False):
                super().__init__([SIGNED_IN], after_click={selectors.EXPORT_CREATE: [CONFIRMED]})
                self.on_click, self.on_read, self.created = on_click, on_read, False

            def click(self, selector):
                if selector == selectors.EXPORT_CREATE and self.on_click:
                    raise RuntimeError("page crashed")
                super().click(selector)
                self.created = self.created or selector == selectors.EXPORT_CREATE

            def content(self):
                if self.created and self.on_read:
                    raise RuntimeError("page crashed")
                return super().content()

        for browser in (Crashing(on_click=True), Crashing(on_read=True)):
            ctx, _, _ = context_for(browser, wall=CREATED)
            with self.assertRaises(schedule.ScheduleNotConfirmed) as caught:
                schedule.create_schedule(ctx)
            self.assertIn("check Takeout before trying again", str(caught.exception))
            self.assertIn("RuntimeError", str(caught.exception))

    def test_a_failure_before_the_create_click_stays_an_ordinary_failure(self):
        browser = export_page()
        browser.missing.add(selectors.EXPORT_SIZE_50GB)
        ctx, _, _ = context_for(browser, wall=CREATED)
        with self.assertRaises(TimeoutError):
            schedule.create_schedule(ctx)
        self.assertNotIn(selectors.EXPORT_CREATE, browser.clicks)

    def test_a_lost_lease_after_the_create_click_is_still_a_cancellation(self):
        browser = export_page(UNKNOWN)
        ctx, _, _ = context_for(browser, wall=CREATED)
        original = ctx.sleep

        def sleep(seconds):
            original(seconds)
            ctx.cancelled.set()

        ctx.sleep = sleep
        with self.assertRaises(Cancelled):
            schedule.create_schedule(ctx)

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
        ctx, _, reporter = context_for(browser, wall=CREATED, email="someone.else@example.test")
        with self.assertRaises(session.SignInTimedOut):
            schedule.create_schedule(ctx)
        self.assertEqual((browser.clicks, reporter.kinds("waiting")), ([], ["wrong_account"]))

    def test_after_the_person_switches_to_the_right_account_the_schedule_is_created(self):
        wrong = SIGNED_IN.replace("jo@example.test", "someone.else@example.test")
        browser = ScriptedBrowser([wrong, SIGNED_IN], after_click={selectors.EXPORT_CREATE: [CONFIRMED]})
        ctx, _, reporter = context_for(browser, wall=CREATED)
        schedule.create_schedule(ctx)
        self.assertEqual((browser.clicks, reporter.kinds("waiting")), (RECORDED_CLICKS, ["wrong_account"]))

    def test_the_job_is_registered(self):
        self.assertIs(REGISTRY["schedule"], schedule.create_schedule)


if __name__ == "__main__":
    unittest.main()
