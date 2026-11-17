import os
import unittest
from unittest import mock

from darkroom_browser import selectors
from darkroom_browser.jobs import REGISTRY, session, status
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.takeout_pages import COMPLETED_ID, EXPIRED_ID, IN_PROGRESS_ID, SIZES, archive, archive_url, completed_row, expired_row, in_progress_row, summary
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN, UNKNOWN, VERIFY
from tests.browser.test_parse_exports import NOW, at
from tests.snapshots import snapshot_pending

EXPORTS = summary(completed_row(), in_progress_row(), expired_row())
ARCHIVE = archive_url(COMPLETED_ID)


class SiteBrowser(ScriptedBrowser):
    def __init__(self, site):
        super().__init__([SIGNED_IN])
        self.site = {url: list(pages) for url, pages in site.items()}
        self.at = None

    def goto(self, url):
        super().goto(url)
        self.at = url
        self.pages, self.reads = list(self.site.get(url, [SIGNED_IN])), 0


def site(listing=EXPORTS, detail=None):
    return {selectors.TAKEOUT_URL: [SIGNED_IN], selectors.TAKEOUT_MANAGE_URL: listing if isinstance(listing, list) else [listing],
            ARCHIVE: detail if isinstance(detail, list) else [detail or archive()]}


@snapshot_pending
class StatusJobTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TZ": "America/New_York"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_job(self, browser, **options):
        ctx, clock, reporter = context_for(browser, wall=NOW, **options)
        return status.check_exports(ctx), clock, reporter

    def test_it_is_registered_as_the_status_job(self):
        self.assertIs(REGISTRY["status"], status.check_exports)

    def test_it_reads_the_summary_then_each_completed_exports_own_page(self):
        browser = SiteBrowser(site())
        result, clock, reporter = self.run_job(browser)
        self.assertEqual(browser.visited, [selectors.TAKEOUT_URL, selectors.TAKEOUT_MANAGE_URL, ARCHIVE])
        self.assertEqual((browser.clicks, browser.typed, clock.slept, reporter.kinds("waiting")), ([], [], [], []))
        self.assertEqual(reporter.kinds("progress"), ["read 3 exports"])
        self.assertEqual(result, {"exports": [
            {"google_id": COMPLETED_ID, "created_at": NOW - 3600, "state": "ready", "expires_at": at(2026, 11, 24, 13, 55),
             "parts": [{"number": number, "bytes": size} for number, size in enumerate(SIZES, start=1)]},
            {"google_id": IN_PROGRESS_ID, "created_at": NOW - 300, "state": "in_progress", "expires_at": None, "parts": []},
            {"google_id": EXPIRED_ID, "created_at": at(2026, 11, 9, 14, 45), "state": "expired", "expires_at": None, "parts": []},
        ]})

    def test_no_download_url_or_user_id_is_reported(self):
        result, _, _ = self.run_job(SiteBrowser(site()))
        self.assertNotIn("takeout/download", repr(result))
        self.assertNotIn("user=", repr(result))

    def test_only_completed_exports_have_their_page_opened(self):
        browser = SiteBrowser(site(summary(in_progress_row(), expired_row())))
        result, _, _ = self.run_job(browser)
        self.assertEqual(len(result["exports"]), 2)
        self.assertNotIn(ARCHIVE, browser.visited)

    def test_a_list_that_loads_slowly_is_waited_for(self):
        result, clock, _ = self.run_job(SiteBrowser(site([UNKNOWN, summary(), EXPORTS])))
        self.assertEqual(len(result["exports"]), 3)
        self.assertEqual(clock.slept, [5.0, 5.0])

    def test_an_archive_page_that_loads_slowly_is_waited_for(self):
        result, clock, _ = self.run_job(SiteBrowser(site(detail=[archive(loading=True), archive(items=[]), archive()])))
        self.assertEqual(len(result["exports"][0]["parts"]), 3)
        self.assertEqual(clock.slept, [5.0, 5.0])

    def test_an_account_with_no_requests_reports_an_empty_listing_once_the_page_says_so(self):
        empty = summary(body="<div>You haven’t made any requests in the last 30 days</div>", scheduled=False)
        result, clock, _ = self.run_job(SiteBrowser(site([summary(), empty])))
        self.assertEqual(result["exports"], [])
        self.assertEqual(clock.slept, [5.0])

    def test_a_list_that_never_loads_fails_saying_what_was_seen(self):
        with self.assertRaises(status.ExportsUnreadable) as raised:
            self.run_job(SiteBrowser(site(UNKNOWN)))
        self.assertEqual(str(raised.exception), "Takeout's list of exports did not load; the page showed an unrecognised title; 1 other heading")

    def test_the_old_manage_page_or_a_bare_summary_is_named_in_the_failure(self):
        old = "<html><head><title>Google Takeout</title></head><body><h1>Your Google data</h1><h2>Manage your exports</h2><ul></ul></body></html>"
        for page, seen in ((old, "title 'google takeout'; headings 'your google data', 'manage your exports'"), (summary(), "title 'google takeout'; headings 'summary', 'status'")):
            with self.subTest(seen=seen), self.assertRaises(status.ExportsUnreadable) as raised:
                self.run_job(SiteBrowser(site(page)))
            self.assertEqual(str(raised.exception), f"Takeout's list of exports did not load; the page showed {seen}")

    def test_waiting_for_the_list_takes_the_poll_count(self):
        _, clock, _ = self.run_job(SiteBrowser(site([UNKNOWN] * 3 + [EXPORTS])))
        self.assertEqual(clock.slept, [status.LIST_POLL_SECONDS] * 3)
        with self.assertRaises(status.ExportsUnreadable):
            self.run_job(SiteBrowser(site([UNKNOWN] * (status.LIST_POLLS + 1) + [EXPORTS])))

    def test_an_archive_page_that_never_loads_fails_saying_what_was_seen(self):
        with self.assertRaises(status.ExportsUnreadable) as raised:
            self.run_job(SiteBrowser(site(detail=archive(loading=True))))
        self.assertEqual(str(raised.exception), "an export's own page on Takeout did not load; the page showed title 'google takeout'; headings 'google photos', 'summary'")

    def test_an_unreadable_export_fails_without_page_text(self):
        for page, detail in ((summary(completed_row(state="CANARY-PAGE-TEXT")), None), (EXPORTS, archive(created="CANARY-PAGE-TEXT"))):
            with self.subTest(), self.assertRaises(status.ExportsUnreadable) as raised:
                self.run_job(SiteBrowser(site(page, detail)))
            self.assertNotIn("CANARY", str(raised.exception))
            self.assertNotIn(COMPLETED_ID, str(raised.exception))

    def test_a_job_with_no_account_email_does_not_read_anything(self):
        browser = SiteBrowser(site())
        with self.assertRaises(session.WrongAccount):
            self.run_job(browser, email=None)
        self.assertEqual(browser.visited, [selectors.TAKEOUT_URL])

    def test_a_sign_in_page_waits_for_a_person_once_and_then_reads(self):
        browser = SiteBrowser(site())
        browser.site[selectors.TAKEOUT_URL] = [SIGN_IN_NEEDED, VERIFY, SIGNED_IN]
        result, clock, reporter = self.run_job(browser)
        self.assertEqual(reporter.kinds("waiting"), ["sign_in"])
        self.assertEqual(len(result["exports"]), 3)
        self.assertEqual(clock.slept, [30.0, 30.0])

    def test_a_session_that_ends_on_a_takeout_page_signs_in_again_then_reads(self):
        for url in (selectors.TAKEOUT_MANAGE_URL, ARCHIVE):
            with self.subTest(url=url):
                browser = SiteBrowser(site())
                visits = {"count": 0}
                original = browser.goto

                def goto(target, url=url, browser=browser, visits=visits, original=original):
                    original(target)
                    if target == url:
                        visits["count"] += 1
                        if visits["count"] == 1:
                            browser.pages = [SIGN_IN_NEEDED]

                browser.goto = goto
                result, _, reporter = self.run_job(browser)
                self.assertEqual(len(result["exports"]), 3)
                self.assertEqual(visits["count"], 2)
                self.assertEqual(browser.visited.count(selectors.TAKEOUT_URL), 2)
                self.assertEqual(reporter.kinds("waiting"), [])

    def test_a_session_that_keeps_ending_gives_up(self):
        browser = SiteBrowser(site(SIGN_IN_NEEDED))
        with self.assertRaises(status.ExportsUnreadable):
            self.run_job(browser)
        self.assertEqual(browser.visited.count(selectors.TAKEOUT_MANAGE_URL), status.MAX_ROUNDS)


if __name__ == "__main__":
    unittest.main()
