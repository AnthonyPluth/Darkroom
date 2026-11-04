import unittest

from darkroom_browser import selectors
from darkroom_browser.context import Cancelled
from darkroom_browser.jobs import REGISTRY, session
from darkroom_browser.jobs.connect import connect
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN, UNKNOWN, VERIFY
from tests.snapshots import snapshot_pending


OTHER_ACCOUNT = SIGNED_IN.replace("jo@example.test", "someone.else@example.test")
UNNAMED = SIGNED_IN.replace("Google Account: Jo (jo@example.test)", "Account menu")


@snapshot_pending
class ConnectStateMachineTests(unittest.TestCase):
    def test_sign_in_then_verify_then_signed_in_reports_waiting_once_and_done_ok(self):
        browser = ScriptedBrowser([SIGN_IN_NEEDED, SIGN_IN_NEEDED, VERIFY, VERIFY, SIGNED_IN])
        ctx, clock, reporter = context_for(browser)
        self.assertEqual(connect(ctx), {"session_state": "ok"})
        self.assertEqual(reporter.kinds("waiting"), ["sign_in"])
        self.assertEqual(reporter.kinds("progress"), ["signed in"])
        self.assertEqual(clock.slept, [30.0] * 4)
        self.assertEqual(browser.visited, [selectors.TAKEOUT_URL])
        self.assertEqual(browser.clicks, [])
        self.assertEqual(browser.typed, [])

    def test_already_signed_in_reports_nothing_and_never_waits(self):
        ctx, clock, reporter = context_for(ScriptedBrowser([SIGNED_IN]))
        self.assertEqual(connect(ctx), {"session_state": "ok"})
        self.assertEqual(reporter.events, [])
        self.assertEqual(clock.slept, [])

    def test_verify_first_is_one_waiting_report_too(self):
        ctx, _, reporter = context_for(ScriptedBrowser([VERIFY, SIGN_IN_NEEDED, SIGNED_IN]))
        connect(ctx)
        self.assertEqual(reporter.kinds("waiting"), ["verify"])

    def test_an_unrecognised_page_for_more_than_five_minutes_fails(self):
        browser = ScriptedBrowser([UNKNOWN])
        ctx, clock, reporter = context_for(browser)
        with self.assertRaises(session.UnexpectedPage):
            connect(ctx)
        self.assertEqual(reporter.events, [])
        self.assertEqual(sum(clock.slept), 330.0)

    def test_a_short_unrecognised_page_is_tolerated_and_resets_the_timer(self):
        pages = [UNKNOWN] * 5 + [SIGN_IN_NEEDED] + [UNKNOWN] * 5 + [SIGNED_IN]
        ctx, _, reporter = context_for(ScriptedBrowser(pages))
        self.assertEqual(connect(ctx), {"session_state": "ok"})
        self.assertEqual(reporter.kinds("waiting"), ["sign_in"])

    def test_waiting_gives_up_after_48_hours(self):
        ctx, clock, reporter = context_for(ScriptedBrowser([SIGN_IN_NEEDED]))
        with self.assertRaises(session.SignInTimedOut):
            connect(ctx)
        self.assertEqual(reporter.kinds("waiting"), ["sign_in"])
        self.assertGreater(sum(clock.slept), 48 * 3600)
        self.assertLess(sum(clock.slept), 48 * 3600 + 60)

    def test_a_lost_lease_stops_the_polling(self):
        ctx, clock, _ = context_for(ScriptedBrowser([SIGN_IN_NEEDED]))
        original = ctx.sleep

        def sleep(seconds):
            original(seconds)
            if len(clock.slept) == 3:
                ctx.cancelled.set()

        ctx.sleep = sleep
        with self.assertRaises(Cancelled):
            connect(ctx)
        self.assertEqual(len(clock.slept), 3)

    def test_a_different_account_waits_for_the_person_to_switch_and_then_succeeds(self):
        ctx, clock, reporter = context_for(ScriptedBrowser([OTHER_ACCOUNT, OTHER_ACCOUNT, SIGNED_IN]))
        self.assertEqual(connect(ctx), {"session_state": "ok"})
        self.assertEqual(reporter.kinds("waiting"), ["wrong_account"])
        self.assertEqual(reporter.kinds("progress"), ["signed in"])
        self.assertEqual(clock.slept, [30.0, 30.0])

    def test_sign_in_then_the_wrong_account_then_the_right_one_reports_each_change_once(self):
        ctx, _, reporter = context_for(ScriptedBrowser([SIGN_IN_NEEDED, OTHER_ACCOUNT, OTHER_ACCOUNT, SIGN_IN_NEEDED, SIGNED_IN]))
        connect(ctx)
        self.assertEqual(reporter.kinds("waiting"), ["sign_in", "wrong_account", "sign_in"])

    def test_never_the_right_account_gives_up_after_48_hours_with_the_browser_waiting_all_along(self):
        browser = ScriptedBrowser([OTHER_ACCOUNT])
        ctx, clock, reporter = context_for(browser)
        with self.assertRaises(session.SignInTimedOut):
            connect(ctx)
        self.assertEqual(reporter.kinds("waiting"), ["wrong_account"])
        self.assertEqual(reporter.kinds("progress"), [])
        self.assertGreater(sum(clock.slept), 48 * 3600)
        self.assertEqual((browser.clicks, browser.typed, browser.closed), ([], [], False))

    def test_the_account_is_compared_without_regard_to_case(self):
        ctx, _, _ = context_for(ScriptedBrowser([SIGNED_IN]), email="Jo@Example.Test")
        self.assertEqual(connect(ctx), {"session_state": "ok"})

    def test_a_signed_in_page_that_does_not_name_the_account_fails_after_five_minutes(self):
        ctx, clock, reporter = context_for(ScriptedBrowser([UNNAMED]))
        with self.assertRaises(session.WrongAccount) as caught:
            connect(ctx)
        self.assertIn("could not read", str(caught.exception))
        self.assertEqual((reporter.events, sum(clock.slept)), ([], 330.0))

    def test_a_page_that_names_the_account_a_moment_later_is_fine(self):
        ctx, _, reporter = context_for(ScriptedBrowser([UNNAMED, UNNAMED, SIGNED_IN]))
        self.assertEqual(connect(ctx), {"session_state": "ok"})
        self.assertEqual(reporter.events, [])

    def test_a_job_without_an_expected_account_fails_at_once(self):
        ctx, clock, _ = context_for(ScriptedBrowser([SIGNED_IN]), email=None)
        with self.assertRaises(session.WrongAccount) as caught:
            connect(ctx)
        self.assertIn("does not say", str(caught.exception))
        self.assertEqual(clock.slept, [])

    def test_the_job_is_registered(self):
        self.assertIs(REGISTRY["connect"], connect)


if __name__ == "__main__":
    unittest.main()
