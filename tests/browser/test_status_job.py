import os
import unittest
from unittest import mock

from darkroom_browser import selectors
from darkroom_browser.jobs import REGISTRY, session, status
from darkroom_browser.pages import takeout
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser.takeout_pages import (COMPLETED_ID, EXPIRED_ID, IN_PROGRESS_ID, LIVE_BASE, SIZES, archive, archive_url, completed_row, expired_row, in_progress_row,
                                         items_linked, row, summary)
from tests.browser.test_classify import SIGN_IN_NEEDED, SIGNED_IN, UNKNOWN, VERIFY
from tests.browser.test_parse_exports import NOW, at

EXPORTS = summary(completed_row(), in_progress_row(), expired_row())
ARCHIVE = archive_url(COMPLETED_ID)


class SiteBrowser(ScriptedBrowser):
    def __init__(self, site, landing=None):
        super().__init__([SIGNED_IN])
        self.site = {url: list(pages) for url, pages in site.items()}
        self.landing = landing or {}
        self.at = None

    def goto(self, url):
        super().goto(url)
        self.at = url
        self.address = self.landing.get(url, url)
        self.pages, self.reads = list(self.site.get(url, [SIGNED_IN])), 0


def site(listing=EXPORTS, detail=None):
    return {selectors.TAKEOUT_URL: [SIGNED_IN], selectors.TAKEOUT_MANAGE_URL: listing if isinstance(listing, list) else [listing],
            ARCHIVE: detail if isinstance(detail, list) else [detail or archive()]}


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
        self.assertEqual(str(raised.exception), "Takeout's list of exports did not load; summary text not found; export links 0 of 0 links (0 relative, 0 naming an export's page); "
                                                "no empty-list sentence; no base address; page address on Takeout; the page showed an unrecognised title; 1 other heading")

    def test_the_old_manage_page_or_a_bare_summary_is_named_in_the_failure(self):
        old = "<html><head><title>Google Takeout</title></head><body><h1>Your Google data</h1><h2>Manage your exports</h2><ul></ul></body></html>"
        bare = summary()
        old_found = "summary text not found; export links 0 of 0 links (0 relative, 0 naming an export's page); no empty-list sentence; no base address; page address on Takeout"
        bare_found = "summary text found; export links 0 of 5 links (0 relative, 0 naming an export's page); no empty-list sentence; no base address; page address on Takeout"
        for page, found, seen in ((old, old_found, "title 'google takeout'; headings 'your google data', 'manage your exports'"),
                                  (bare, bare_found, "title 'google takeout'; headings 'summary', 'status'")):
            with self.subTest(seen=seen), self.assertRaises(status.ExportsUnreadable) as raised:
                self.run_job(SiteBrowser(site(page)))
            self.assertEqual(str(raised.exception), f"Takeout's list of exports did not load; {found}; the page showed {seen}")

    def test_the_live_pages_relative_links_are_read_against_the_browsers_address(self):
        listing = summary(*(row(google_id, state, created, None, href=f"./manage/archive/{google_id}") for google_id, state, created in
                            ((COMPLETED_ID, "Completed", "Created 1 hour ago"), (IN_PROGRESS_ID, "In progress", "Created 5 minutes ago"),
                             (EXPIRED_ID, "Expired", "Created November 9, 2:45\u202fPM"))), base=LIVE_BASE)
        detail = archive(items=items_linked("takeout/download?{query}"), base=LIVE_BASE)
        for landed in (selectors.TAKEOUT_MANAGE_URL, selectors.TAKEOUT_MANAGE_URL + "/"):
            with self.subTest(landed=landed):
                result, _, _ = self.run_job(SiteBrowser(site(listing, detail), {selectors.TAKEOUT_MANAGE_URL: landed}))
                self.assertEqual([(each["google_id"], len(each["parts"])) for each in result["exports"]], [(COMPLETED_ID, 3), (IN_PROGRESS_ID, 0), (EXPIRED_ID, 0)])

    def test_without_a_base_the_links_follow_the_address_the_browser_landed_on(self):
        listing = summary(row(href=f"./manage/archive/{COMPLETED_ID}"))
        result, _, _ = self.run_job(SiteBrowser(site(listing)))
        self.assertEqual([each["google_id"] for each in result["exports"]], [COMPLETED_ID])
        with self.assertRaises(status.ExportsUnreadable) as raised:
            self.run_job(SiteBrowser(site(listing), {selectors.TAKEOUT_MANAGE_URL: selectors.TAKEOUT_MANAGE_URL + "/"}))
        self.assertIn("summary text found; export links 0 of 6 links (1 relative, 1 naming an export's page); no empty-list sentence; no base address; page address on Takeout",
                      str(raised.exception))
        self.assertNotIn(COMPLETED_ID, str(raised.exception))

    def test_part_links_relative_to_the_archives_own_address_are_refused(self):
        with self.assertRaises(status.ExportsUnreadable) as raised:
            self.run_job(SiteBrowser(site(detail=archive(items=items_linked("takeout/download?{query}")))))
        self.assertIn("not one Darkroom can use", str(raised.exception))
        self.assertNotIn("user=", str(raised.exception))

    def test_the_longest_failure_message_fits_what_darkroom_records(self):
        from darkroom.server.api.browser_internal import MAX_MESSAGE
        from darkroom_browser import runner

        headings = "".join(f"<h2>{heading}</h2>" for heading in sorted(takeout.KNOWN_HEADINGS, key=len, reverse=True)) + "<h2>other</h2>" * 99_999
        links = '<a href="./archive/x">archive/</a>' * 99_999
        page = f"<html><head><title>{max(takeout.KNOWN_HEADINGS, key=len)}</title></head><body>{headings}{links}</body></html>"
        for broken, landing in (({selectors.TAKEOUT_MANAGE_URL: [page]}, {selectors.TAKEOUT_MANAGE_URL: "https://other.example/"}), ({ARCHIVE: [page]}, {})):
            with self.subTest(broken=list(broken)), self.assertRaises(status.ExportsUnreadable) as raised:
                self.run_job(SiteBrowser({**site(), **broken}, landing))
            said = runner.failure_message(raised.exception)
            self.assertLessEqual(len(said), MAX_MESSAGE)
            self.assertTrue(said.startswith("ExportsUnreadable: "))

    def test_waiting_for_the_list_takes_the_poll_count(self):
        _, clock, _ = self.run_job(SiteBrowser(site([UNKNOWN] * 3 + [EXPORTS])))
        self.assertEqual(clock.slept, [status.LIST_POLL_SECONDS] * 3)
        with self.assertRaises(status.ExportsUnreadable):
            self.run_job(SiteBrowser(site([UNKNOWN] * (status.LIST_POLLS + 1) + [EXPORTS])))

    def test_an_archive_page_that_never_loads_fails_saying_what_was_seen(self):
        with self.assertRaises(status.ExportsUnreadable) as raised:
            self.run_job(SiteBrowser(site(detail=archive(loading=True))))
        self.assertEqual(str(raised.exception), "an export's own page on Takeout did not load; overall status not found; numbered download links 0 (0 usable, 0 relative); "
                                                "no base address; page address on Takeout; the page showed title 'google takeout'; headings 'google photos', 'summary'")

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
