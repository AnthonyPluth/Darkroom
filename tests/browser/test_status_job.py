import os
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from darkroom_browser import selectors
from darkroom_browser.jobs import REGISTRY, session, status
from darkroom_browser.pages import takeout
from tests.browser.scripted import ScriptedBrowser, context_for
from tests.browser import test_takeout_data as data
from tests.browser.takeout_pages import (COMPLETED_ID, EXPIRED_ID, IN_PROGRESS_ID, LIVE_BASE, SIZES, archive, archive_url, completed_row, downloaded_page_parts,
                                         embedded_parts, expired_row, in_progress_row, items_linked, list_blocks, row, summary, with_blocks)
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
        self.assertEqual(reporter.kinds("progress"), ["read 3 exports; Takeout's embedded data was not used on the list of exports (none on the page); "
                                                      "Takeout's embedded data was not used on an export's page (none on the page)"])
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

    def test_the_longest_date_shapes_fit_what_darkroom_records_without_being_cut(self):
        from darkroom.server.api.browser_internal import MAX_MESSAGE

        words = " ".join(sorted(takeout.SHAPE_WORDS, key=len, reverse=True)) * 50
        rows = [row(google_id=f"id-{number}", created=f"Created {words}", available=f"Available until {words}") for number in range(takeout.MAX_EXPORTS)]
        no_date = [row(created="Created</div>" + f"<div>{words}</div>" * 50)]
        for broken, failure in (({selectors.TAKEOUT_MANAGE_URL: [summary(*rows)]}, "a date that could not be read"),
                                ({selectors.TAKEOUT_MANAGE_URL: [summary(*no_date)]}, "no readable creation date"),
                                ({ARCHIVE: [archive(created=words, expires=words)]}, "a date that could not be read")):
            with self.subTest(failure=failure), self.assertRaises(status.ExportsUnreadable) as raised:
                self.run_job(SiteBrowser({**site(), **broken}))
            self.assertIn(failure, str(raised.exception))
            self.assertLessEqual(len(f"ExportsUnreadable: {raised.exception}"), MAX_MESSAGE)

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


class EmbeddedDataStatusTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TZ": "America/New_York"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_job(self, listing, detail):
        ctx, _, reporter = context_for(SiteBrowser(site(listing, detail)), wall=NOW)
        return status.check_exports(ctx), reporter

    def listing(self):
        return with_blocks(EXPORTS, list_blocks([data.completed(), data.in_progress(), data.expired()]))

    def test_googles_own_download_counts_and_the_embedded_times_are_reported(self):
        detail = data.detail_page(data.completed(parts=embedded_parts(downloaded=(2,))), page=archive(items=downloaded_page_parts(count=3, downloaded=(2,), sizes=SIZES)))
        result, reporter = self.run_job(self.listing(), detail)
        self.assertEqual(reporter.kinds("progress"), ["read 3 exports"])
        self.assertEqual(result, {"exports": [
            {"google_id": COMPLETED_ID, "created_at": data.CREATED_MS / 1000, "state": "ready", "expires_at": data.EXPIRES_MS / 1000,
             "parts": [{"number": number, "bytes": size, "google_downloads": 2 if number == 2 else 0} for number, size in enumerate(SIZES, start=1)]},
            {"google_id": IN_PROGRESS_ID, "created_at": data.IN_PROGRESS_CREATED_MS / 1000, "state": "in_progress", "expires_at": None, "parts": []},
            {"google_id": EXPIRED_ID, "created_at": data.EXPIRED_CREATED_MS / 1000, "state": "expired", "expires_at": data.EXPIRED_EXPIRES_MS / 1000, "parts": []},
        ]})
        for leak in ("takeout-2026", ".zip", "made-up"):
            self.assertNotIn(leak, repr(result))

    def test_a_disagreement_on_an_exports_page_reports_the_page_alone_and_says_so(self):
        detail = data.detail_page(data.completed(parts=embedded_parts(sizes=(SIZES[0], SIZES[1] + 7, SIZES[2]))))
        result, reporter = self.run_job(self.listing(), detail)
        self.assertEqual(reporter.kinds("progress"), ["read 3 exports; Takeout's embedded data was not used on an export's page (it and the page disagree about the sizes)"])
        self.assertEqual(result["exports"][0]["parts"], [{"number": number, "bytes": size} for number, size in enumerate(SIZES, start=1)])

    def test_a_failure_of_the_embedded_data_never_fails_a_job_the_page_alone_passes(self):
        broken = with_blocks(EXPORTS, [data.raw_block("[" * 40 + "]" * 40)])
        result, reporter = self.run_job(broken, with_blocks(archive(), [data.raw_block("[1,]", key="ds:0")]))
        self.assertEqual(len(result["exports"]), 3)
        self.assertEqual(reporter.kinds("progress"), ["read 3 exports; Takeout's embedded data was not used on the list of exports (it was nested too deeply); "
                                                      "Takeout's embedded data was not used on an export's page (it was not plain data)"])

    def test_an_export_pages_embedded_expiry_is_reported_as_its_instant_in_a_zone_ahead_of_utc(self):
        expires = datetime(2026, 11, 24, 8, 0, tzinfo=ZoneInfo("Australia/Brisbane")).timestamp()
        with mock.patch.dict(os.environ, {"TZ": "Australia/Brisbane"}):
            ctx, _, _ = context_for(SiteBrowser(site(summary(completed_row(available=None)), data.detail_page(data.completed(expires_ms=int(expires * 1000))))), wall=NOW)
            result = status.check_exports(ctx)
        self.assertEqual(result["exports"][0]["expires_at"], expires)

    def test_the_same_reason_on_many_pages_is_said_once(self):
        self.assertEqual(status.read_message(2, ["a", "b", "a", "a"]), "read 2 exports; a; b")

    def test_a_date_the_page_shows_unreadably_is_named_as_not_checked(self):
        listing = with_blocks(summary(completed_row(created="Created Novembre 17, 1:55 PM"), in_progress_row(), expired_row()),
                              list_blocks([data.completed(), data.in_progress(), data.expired()]))
        result, reporter = self.run_job(listing, data.detail_page(page=archive(expires="24/11/2026")))
        self.assertEqual(result["exports"][0]["created_at"], data.CREATED_MS / 1000)
        self.assertEqual(reporter.kinds("progress"), [
            "read 3 exports; Takeout's embedded data on the list of exports was used without checking its creation date, which the page shows in a form that could not be read; "
            "Takeout's embedded data on an export's page was used without checking its expiry date, which the page shows in a form that could not be read"])

    def test_a_readable_date_that_disagrees_beside_an_unreadable_one_fails_the_job_in_fixed_words(self):
        listing = with_blocks(summary(completed_row(created="Created Novembre 17, 1:55 PM"), in_progress_row(), expired_row()),
                              list_blocks([data.completed(expires_ms=data.EARLY_EXPIRES_MS), data.in_progress(), data.expired()]))
        with self.assertRaises(status.ExportsUnreadable) as raised:
            self.run_job(listing, data.detail_page())
        self.assertEqual(str(raised.exception), "Takeout's embedded data and the page disagree about the expiry date")

    def test_the_message_stays_within_what_darkroom_records(self):
        from darkroom.server.api.browser_internal import MAX_MESSAGE

        notes = [takeout.unused_note("an export's page", f"it and the page disagree about the {about}") for about in
                 (takeout.ABOUT_ID, takeout.ABOUT_COUNT, takeout.ABOUT_NAMES, takeout.ABOUT_INDEXES, takeout.ABOUT_SIZES, takeout.ABOUT_DOWNLOADED)]
        notes += [takeout.unchecked_note(where, about) for where in ("the list of exports", "an export's page") for about in (takeout.ABOUT_CREATED, takeout.ABOUT_EXPIRY)]
        said = status.read_message(takeout.MAX_EXPORTS, notes)
        self.assertLessEqual(len(said), status.MAX_MESSAGE_CHARS)
        self.assertLessEqual(len(said), MAX_MESSAGE)
        self.assertTrue(said.endswith("more notes about Takeout's embedded data"), said)
        self.assertEqual(status.read_message(1, notes[:1]), f"read 1 exports; {notes[0]}")


if __name__ == "__main__":
    unittest.main()
