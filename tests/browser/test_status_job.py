import unittest

from darkroom_browser import selectors
from darkroom_browser.jobs import REGISTRY, session, status
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN, UNKNOWN, VERIFY
from tests.browser.test_parse_exports import expired, in_progress, page, ready, stamp
from tests.snapshots import snapshot_pending

EXPORTS = page(ready(), in_progress(), expired())


@snapshot_pending
class StatusJobTests(unittest.TestCase):
    def test_it_is_registered_as_the_status_job(self):
        self.assertIs(REGISTRY["status"], status.check_exports)

    def test_it_opens_manage_exports_and_reports_the_listing(self):
        browser = ScriptedBrowser([SIGNED_IN, EXPORTS])
        ctx, clock, reporter = context_for(browser)
        result = status.check_exports(ctx)
        self.assertEqual(browser.visited, [selectors.TAKEOUT_URL, selectors.TAKEOUT_MANAGE_URL])
        self.assertEqual(browser.clicks, [])
        self.assertEqual(browser.typed, [])
        self.assertEqual(clock.slept, [])
        self.assertEqual(reporter.kinds("waiting"), [])
        self.assertEqual(reporter.kinds("progress"), ["read 3 exports"])
        self.assertEqual(result, {"exports": [
            {"google_id": "aaaa-1111", "created_at": stamp(2026, 11, 2, 3, 10), "state": "ready", "expires_at": stamp(2026, 11, 9, 3, 10),
             "parts": [{"number": 1, "bytes": round(49.8 * 1024**3)}, {"number": 2, "bytes": round(49.9 * 1024**3)}, {"number": 3, "bytes": round(12.4 * 1024**3)}]},
            {"google_id": "bbbb-2222", "created_at": stamp(2026, 12, 2, 3, 10), "state": "in_progress", "expires_at": None, "parts": []},
            {"google_id": "cccc-3333", "created_at": stamp(2026, 9, 2, 3, 10), "state": "expired", "expires_at": stamp(2026, 9, 9, 3, 10), "parts": []},
        ]})

    def test_no_download_url_is_reported(self):
        browser = ScriptedBrowser([SIGNED_IN, EXPORTS])
        ctx, _, _ = context_for(browser)
        self.assertNotIn("takeout.google.com/takeout/download", repr(status.check_exports(ctx)))

    def test_a_list_that_loads_slowly_is_waited_for(self):
        browser = ScriptedBrowser([SIGNED_IN, UNKNOWN, UNKNOWN, EXPORTS])
        ctx, clock, _ = context_for(browser)
        result = status.check_exports(ctx)
        self.assertEqual(len(result["exports"]), 3)
        self.assertEqual(clock.slept, [5.0, 5.0])

    def test_a_list_that_never_loads_fails_with_a_fixed_message(self):
        browser = ScriptedBrowser([SIGNED_IN, UNKNOWN])
        ctx, clock, _ = context_for(browser)
        with self.assertRaises(status.ExportsUnreadable) as raised:
            status.check_exports(ctx)
        self.assertEqual(str(raised.exception), "Takeout's list of exports did not load")
        self.assertEqual(clock.slept, [status.LIST_POLL_SECONDS] * status.LIST_POLLS)

    def test_an_unreadable_export_fails_without_page_text(self):
        bad = page('<li data-export-id="x"><time data-time="created" datetime="2026-11-02T03:10:00Z"></time><p>CANARY-PAGE-TEXT</p></li>')
        ctx, _, _ = context_for(ScriptedBrowser([SIGNED_IN, bad]))
        with self.assertRaises(status.ExportsUnreadable) as raised:
            status.check_exports(ctx)
        self.assertNotIn("CANARY", str(raised.exception))

    def test_a_job_with_no_account_email_does_not_read_anything(self):
        browser = ScriptedBrowser([SIGNED_IN, EXPORTS])
        ctx, _, _ = context_for(browser, email=None)
        with self.assertRaises(session.WrongAccount):
            status.check_exports(ctx)
        self.assertEqual(browser.visited, [selectors.TAKEOUT_URL])

    def test_a_sign_in_page_waits_for_a_person_once_and_then_reads(self):
        browser = ScriptedBrowser([SIGN_IN_NEEDED, VERIFY, SIGNED_IN], after_click={})
        pages = {selectors.TAKEOUT_MANAGE_URL: EXPORTS}
        original_goto = browser.goto

        def goto(url):
            original_goto(url)
            if url in pages:
                browser.pages, browser.reads = [pages[url]], 0

        browser.goto = goto
        ctx, clock, reporter = context_for(browser)
        result = status.check_exports(ctx)
        self.assertEqual(reporter.kinds("waiting"), ["sign_in"])
        self.assertEqual(len(result["exports"]), 3)
        self.assertEqual(clock.slept, [30.0, 30.0])

    def test_a_session_that_ends_on_the_exports_page_signs_in_again_then_reads(self):
        visits = {"manage": 0}
        browser = ScriptedBrowser([SIGNED_IN])
        original_goto = browser.goto

        def goto(url):
            original_goto(url)
            if url == selectors.TAKEOUT_MANAGE_URL:
                visits["manage"] += 1
                browser.pages, browser.reads = ([SIGN_IN_NEEDED] if visits["manage"] == 1 else [EXPORTS]), 0
            else:
                browser.pages, browser.reads = [SIGNED_IN], 0

        browser.goto = goto
        ctx, _, reporter = context_for(browser)
        self.assertEqual(len(status.check_exports(ctx)["exports"]), 3)
        self.assertEqual(visits["manage"], 2)
        self.assertEqual(browser.visited.count(selectors.TAKEOUT_URL), 2)
        self.assertEqual(reporter.kinds("waiting"), [])

    def test_a_session_that_keeps_ending_gives_up(self):
        browser = ScriptedBrowser([SIGNED_IN])
        original_goto = browser.goto

        def goto(url):
            original_goto(url)
            browser.pages, browser.reads = ([SIGN_IN_NEEDED] if url == selectors.TAKEOUT_MANAGE_URL else [SIGNED_IN]), 0

        browser.goto = goto
        ctx, _, _ = context_for(browser)
        with self.assertRaises(status.ExportsUnreadable):
            status.check_exports(ctx)
        self.assertEqual(browser.visited.count(selectors.TAKEOUT_MANAGE_URL), status.MAX_ROUNDS)


if __name__ == "__main__":
    unittest.main()
