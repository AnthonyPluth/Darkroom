import unittest
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from darkroom_browser.pages import takeout
from tests.browser.takeout_pages import (COMPLETED_ID, EXPIRED_ID, IN_PROGRESS_ID, LIVE_BASE, MADE_UP_USER, MANAGE_PAGE, NNBSP, SIZES, archive, archive_url,
                                         completed_row, download_href, expired_row, in_progress_row, items_linked, part_item, part_names, row, summary)

NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 17, 14, 55, tzinfo=NEW_YORK).timestamp()
CANARY = "CANARY-PAGE-TEXT"
ARCHIVE_PAGE = archive_url(COMPLETED_ID)


def at(*when, zone=NEW_YORK):
    return datetime(*when, tzinfo=zone).timestamp()


def listed(page, now=NOW, zone=NEW_YORK, address=MANAGE_PAGE):
    return takeout.parse_exports(page, address, now, zone)


class SummaryPageTests(unittest.TestCase):
    def test_the_summary_page_lists_a_completed_and_an_expired_export(self):
        completed, gone = listed(summary(completed_row(), expired_row()))
        self.assertEqual((completed.google_id, completed.state, completed.parts), (COMPLETED_ID, "ready", ()))
        self.assertEqual(completed.created_at, NOW - 3600)
        self.assertEqual(completed.expires_at, at(2026, 11, 24, 13, 55))
        self.assertEqual(completed.archive_url, archive_url(COMPLETED_ID))
        self.assertEqual((gone.google_id, gone.state, gone.expires_at, gone.parts), (EXPIRED_ID, "expired", None, ()))
        self.assertEqual(gone.created_at, at(2026, 11, 9, 14, 45))

    def test_an_export_in_progress(self):
        export = listed(summary(in_progress_row()))[0]
        self.assertEqual((export.google_id, export.state, export.expires_at, export.created_at), (IN_PROGRESS_ID, "in_progress", None, NOW - 300))

    def test_relative_creation_times_count_back_from_the_jobs_clock(self):
        for text, seconds in (("Created just now", 0), ("Created 1 minute ago", 60), ("Created an hour ago", 3600), ("Created 3 hours ago", 3 * 3600),
                              ("Created a day ago", 86400), ("Created 2 days ago", 2 * 86400)):
            with self.subTest(text=text):
                self.assertEqual(listed(summary(row(created=text)))[0].created_at, NOW - seconds)

    def test_a_date_without_a_year_is_the_nearest_one_not_in_the_future(self):
        january = at(2027, 1, 3, 9, 0)
        self.assertEqual(listed(summary(row(created=f"Created December 28, 9:05{NNBSP}AM")), now=january)[0].created_at, at(2026, 12, 28, 9, 5))
        self.assertEqual(listed(summary(row(created=f"Created January 2, 11:40{NNBSP}PM")), now=january)[0].created_at, at(2027, 1, 2, 23, 40))
        self.assertEqual(listed(summary(row(created=f"Created November 17, 3:10{NNBSP}PM")))[0].created_at, at(2026, 11, 17, 15, 10))

    def test_a_date_with_a_year_or_without_a_time_is_read(self):
        self.assertEqual(listed(summary(row(created="Created October 30, 2026, 8:00 AM")))[0].created_at, at(2026, 10, 30, 8, 0))
        self.assertEqual(listed(summary(row(created="Created October 30")))[0].created_at, at(2026, 10, 30, 0, 0))

    def test_wall_clock_times_are_read_in_the_workers_time_zone(self):
        export = listed(summary(expired_row()), zone=UTC)[0]
        self.assertEqual(export.created_at, at(2026, 11, 9, 14, 45, zone=UTC))
        self.assertEqual(listed(summary(completed_row()), zone=UTC)[0].expires_at, at(2026, 11, 24, 13, 55, zone=UTC))

    def test_several_exports_come_back_in_page_order(self):
        found = listed(summary(in_progress_row(), completed_row(), expired_row()))
        self.assertEqual([(each.google_id, each.state) for each in found], [(IN_PROGRESS_ID, "in_progress"), (COMPLETED_ID, "ready"), (EXPIRED_ID, "expired")])

    def test_links_to_other_places_are_not_exports(self):
        others = (row(href="https://example.test/manage/archive/x"), row(href="https://takeout.google.com/settings"),
                  row(href="https://takeout.google.com/manage/archive/../../x"))
        self.assertEqual(listed(summary(completed_row(), *others)), listed(summary(completed_row())))

    def test_the_script_trap_is_ignored(self):
        self.assertEqual([each.google_id for each in listed(summary(expired_row()))], [EXPIRED_ID])

    def test_anything_unreadable_is_refused_with_a_fixed_message_naming_no_page_text(self):
        broken = {
            "unknown state": row(state=CANARY),
            "failed state": row(state="Failed"),
            "two states": row(state="Completed</p><p>Expired"),
            "no creation date": row(created=CANARY),
            "a creation date in the future": row(created="Created in 3 days"),
            "an impossible date": row(created="Created February 30, 2026"),
            "an unreadable expiry": row(available=f"Available until {CANARY}"),
            "repeated export": completed_row() + completed_row(),
        }
        for name, item in broken.items():
            with self.subTest(name), self.assertRaises(takeout.UnreadableExports) as raised:
                listed(summary(item))
            self.assertNotIn(CANARY, str(raised.exception))
            self.assertNotIn(COMPLETED_ID, str(raised.exception))

    def test_too_many_exports_are_refused(self):
        rows = [in_progress_row(google_id=f"id-{number}") for number in range(takeout.MAX_EXPORTS + 1)]
        with self.assertRaises(takeout.UnreadableExports):
            listed(summary(*rows))

    def test_the_page_must_be_the_summary_page(self):
        old = "<html><body><h1>Your Google data</h1><h2>Manage your exports</h2><ul role='list'><li data-export-id='x'>in progress</li></ul></body></html>"
        for text in ("", "<html><body><h1>Create a new export</h1></body></html>", old):
            with self.subTest(text=text[:30]), self.assertRaises(takeout.UnreadableExports):
                listed(text)
            self.assertFalse(takeout.exports_page_ready(text, MANAGE_PAGE))


class SummaryReadyTests(unittest.TestCase):
    def test_a_summary_with_exports_is_loaded(self):
        self.assertTrue(takeout.exports_page_ready(summary(completed_row(), expired_row()), MANAGE_PAGE))

    def test_the_heading_alone_is_not_a_loaded_list(self):
        self.assertFalse(takeout.exports_page_ready(summary(), MANAGE_PAGE))
        self.assertFalse(takeout.exports_page_ready(summary(body=""), MANAGE_PAGE))
        self.assertFalse(takeout.exports_page_ready(summary(row(href="https://example.test/manage/archive/x")), MANAGE_PAGE))

    def test_an_explicit_empty_summary_is_loaded_and_empty(self):
        for sentence in ("You haven’t made any requests in the last 30 days", "You have no requests", "No requests"):
            with self.subTest(sentence=sentence):
                page = summary(body=f"<div>{sentence}</div>", scheduled=False)
                self.assertTrue(takeout.exports_page_ready(page, MANAGE_PAGE))
                self.assertEqual(listed(page), [])


class ArchivePageTests(unittest.TestCase):
    def test_a_completed_archive_lists_its_parts_with_exact_sizes_and_links(self):
        detail = takeout.parse_archive(archive(), ARCHIVE_PAGE, COMPLETED_ID)
        self.assertEqual((detail.state, detail.created_on, detail.expires_on), ("ready", date(2026, 11, 17), date(2026, 11, 24)))
        self.assertEqual([part.number for part in detail.parts], [1, 2, 3])
        self.assertEqual([part.bytes for part in detail.parts], list(SIZES))
        self.assertEqual([part.href for part in detail.parts], [download_href(COMPLETED_ID, index).replace("&amp;", "&") for index in range(3)])
        self.assertTrue(takeout.archive_page_ready(archive()))

    def test_the_part_number_comes_from_the_label_not_the_file_name_or_the_order(self):
        names = part_names(3)
        items = [part_item(COMPLETED_ID, 2, 3, SIZES[2], names[0]), part_item(COMPLETED_ID, 0, 3, SIZES[0], names[2]), part_item(COMPLETED_ID, 1, 3, SIZES[1], names[1])]
        self.assertEqual([(part.number, part.bytes) for part in takeout.parse_archive(archive(items=items), ARCHIVE_PAGE, COMPLETED_ID).parts], list(zip((1, 2, 3), SIZES, strict=True)))

    def test_the_size_falls_back_to_the_shown_size(self):
        items = [part_item(COMPLETED_ID, index, 3, size, "x.zip", size_attribute=False) for index, size in enumerate(SIZES)]
        parts = takeout.parse_archive(archive(items=items), ARCHIVE_PAGE, COMPLETED_ID).parts
        self.assertEqual([part.bytes for part in parts], [round(round(size / 1024**3, 2) * 1024**3) for size in SIZES])

    def test_the_latest_export_card_and_the_report_link_are_not_parts(self):
        self.assertEqual(len(takeout.parse_archive(archive(stacked=True), ARCHIVE_PAGE, COMPLETED_ID).parts), 3)

    def test_an_expired_or_running_archive_has_no_parts(self):
        for status, state in (("Expired", "expired"), ("In progress", "in_progress")):
            with self.subTest(status=status):
                detail = takeout.parse_archive(archive(status=status, items=[], expires=None), ARCHIVE_PAGE, COMPLETED_ID)
                self.assertEqual((detail.state, detail.parts, detail.expires_on), (state, (), None))

    def test_a_loading_archive_is_not_ready(self):
        self.assertFalse(takeout.archive_page_ready(archive(loading=True)))
        self.assertFalse(takeout.archive_page_ready(archive(items=[])))
        self.assertFalse(takeout.archive_page_ready(summary(completed_row())))

    def test_anything_unreadable_on_an_archive_is_refused_with_a_fixed_message(self):
        names = part_names(3)
        good = [part_item(COMPLETED_ID, index, 3, size, names[index]) for index, size in enumerate(SIZES)]
        broken = {
            "another export's links": archive(google_id=EXPIRED_ID),
            "a link to another host": archive(items=[*good[:2], part_item(COMPLETED_ID, 2, 3, SIZES[2], "x", href=f"https://example.test/takeout/download?j={COMPLETED_ID}&amp;i=2")]),
            "a link to another path": archive(items=[*good[:2], part_item(COMPLETED_ID, 2, 3, SIZES[2], "x", href=f"https://takeout.google.com/elsewhere?j={COMPLETED_ID}&amp;i=2")]),
            "a missing part": archive(items=good[:2]),
            "a repeated part": archive(items=[*good, good[0]]),
            "an unnumbered part": archive(items=[*good[:2], part_item(COMPLETED_ID, 2, 3, SIZES[2], "x", label=CANARY)]),
            "no size": archive(items=[*good[:2], part_item(COMPLETED_ID, 2, 3, SIZES[2], "x", size_attribute=False).replace("GB</div>", f"{CANARY}</div>")]),
            "an unknown status": archive(status=CANARY),
            "a completed archive with no parts": archive(items=[]),
            "an unreadable creation date": archive(created=CANARY),
        }
        for name, page in broken.items():
            with self.subTest(name), self.assertRaises(takeout.UnreadableExports) as raised:
                takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID)
            self.assertNotIn(CANARY, str(raised.exception))
            self.assertNotIn("user=", str(raised.exception))

    def test_an_archive_url_is_only_built_for_a_plain_id(self):
        self.assertEqual(takeout.archive_url(COMPLETED_ID), archive_url(COMPLETED_ID))
        for bad in ("", "../settings", "a/b", "a?b", "x" * 200):
            with self.subTest(bad=bad), self.assertRaises(takeout.UnreadableExports):
                takeout.archive_url(bad)


ROW_FORMS = {
    "absolute": ("https://takeout.google.com/manage/archive/{}", None, MANAGE_PAGE),
    "the live form with the live base": ("./manage/archive/{}", LIVE_BASE, MANAGE_PAGE),
    "the live form with the live base on /manage/": ("./manage/archive/{}", LIVE_BASE, MANAGE_PAGE + "/"),
    "the live form on /manage with no base": ("./manage/archive/{}", None, MANAGE_PAGE),
    "the live form without the dot": ("manage/archive/{}", LIVE_BASE, MANAGE_PAGE),
    "root-relative": ("/manage/archive/{}", None, MANAGE_PAGE),
    "path-relative on /manage/": ("archive/{}", None, MANAGE_PAGE + "/"),
    "path-relative under a relative base": ("archive/{}", "/manage/", MANAGE_PAGE),
}
ROWS_ELSEWHERE = {
    "the live form on /manage/ with no base": ("./manage/archive/{}", None, MANAGE_PAGE + "/"),
    "path-relative on /manage with no base": ("archive/{}", None, MANAGE_PAGE),
    "another host": ("https://other.example/manage/archive/{}", None, MANAGE_PAGE),
    "scheme-relative to another host": ("//other.example/manage/archive/{}", None, MANAGE_PAGE),
    "an uppercase host": ("https://TAKEOUT.GOOGLE.COM/manage/archive/{}", None, MANAGE_PAGE),
    "a mixed-case host": ("https://Takeout.google.com/manage/archive/{}", None, MANAGE_PAGE),
    "a host that only starts with takeout's": ("https://takeout.google.com.other.example/manage/archive/{}", None, MANAGE_PAGE),
    "takeout's name as user info": ("https://takeout.google.com@other.example/manage/archive/{}", None, MANAGE_PAGE),
    "user info before takeout's host": ("https://jo@takeout.google.com/manage/archive/{}", None, MANAGE_PAGE),
    "a port": ("https://takeout.google.com:8443/manage/archive/{}", None, MANAGE_PAGE),
    "plain http": ("http://takeout.google.com/manage/archive/{}", None, MANAGE_PAGE),
    "javascript": ("javascript:alert(1)//takeout.google.com/manage/archive/{}", None, MANAGE_PAGE),
    "data": ("data:text/html,https://takeout.google.com/manage/archive/{}", None, MANAGE_PAGE),
    "a backslash that browsers read as a slash": ("/\\other.example/manage/archive/{}", None, MANAGE_PAGE),
    "two backslashes": ("\\\\other.example/manage/archive/{}", None, MANAGE_PAGE),
    "a tab that browsers drop": ("/\t/other.example/manage/archive/{}", None, MANAGE_PAGE),
    "dot segments out of the archive path": ("./manage/archive/../../settings/{}", LIVE_BASE, MANAGE_PAGE),
    "dot segments above the root": ("../../other/manage/archive/{}", None, MANAGE_PAGE + "/x/"),
    "a base on another host": ("./manage/archive/{}", "https://other.example/", MANAGE_PAGE),
    "a scheme-relative base": ("./manage/archive/{}", "//other.example/", MANAGE_PAGE),
    "a base with a backslash that browsers read as a slash": ("./manage/archive/{}", "/\\other.example/", MANAGE_PAGE),
    "a page that is not on takeout": ("./manage/archive/{}", None, "https://accounts.google.com/manage"),
    "a malformed bracketed host": ("https://[x]/manage/archive/{}", None, MANAGE_PAGE),
    "an unclosed bracketed host": ("//[/manage/archive/{}", None, MANAGE_PAGE),
    "a malformed bracketed base": ("./manage/archive/{}", "http://[::1", MANAGE_PAGE),
    "a malformed bracketed page address": ("./manage/archive/{}", None, "https://[::1/manage"),
}


class RelativeLinkTests(unittest.TestCase):
    def test_an_export_link_is_read_the_way_the_browser_resolves_it(self):
        for name, (form, base, address) in ROW_FORMS.items():
            with self.subTest(name):
                page = summary(row(href=form.format(COMPLETED_ID)), expired_row(), base=base)
                self.assertTrue(takeout.exports_page_ready(page, address))
                found = listed(page, address=address)
                self.assertEqual([(each.google_id, each.archive_url) for each in found], [(COMPLETED_ID, ARCHIVE_PAGE), (EXPIRED_ID, archive_url(EXPIRED_ID))])

    def test_a_link_that_resolves_anywhere_else_is_not_an_export(self):
        for name, (form, base, address) in ROWS_ELSEWHERE.items():
            with self.subTest(name):
                page = summary(row(href=form.format(COMPLETED_ID)), base=base)
                self.assertFalse(takeout.exports_page_ready(page, address))
                self.assertEqual(listed(page, address=address), [])

    def test_only_the_first_base_counts(self):
        page = summary(row(href=f"./manage/archive/{COMPLETED_ID}"), base=LIVE_BASE).replace("<title>", '<base href="https://other.example/"><title>', 1)
        self.assertEqual([each.google_id for each in listed(page, address=MANAGE_PAGE + "/")], [COMPLETED_ID])
        hostile_first = summary(row(href=f"./manage/archive/{COMPLETED_ID}"), base="https://other.example/").replace("<title>", f'<base href="{LIVE_BASE}"><title>', 1)
        self.assertEqual(listed(hostile_first), [])

    def test_a_malformed_link_elsewhere_on_the_page_is_only_counted(self):
        page = summary(row(href=f"./manage/archive/{COMPLETED_ID}"), base=LIVE_BASE).replace('<a href="#main">', '<a href="http://[::1">x</a><a href="//[">y</a><a href="#main">')
        self.assertEqual([each.google_id for each in listed(page)], [COMPLETED_ID])
        self.assertTrue(takeout.exports_page_ready(page, MANAGE_PAGE))
        self.assertEqual(takeout.summary_findings(page, MANAGE_PAGE),
                         "summary text found; export links 1 of 8 links (2 relative, 1 naming an export's page); no empty-list sentence; a base address; page address on Takeout")
        archived = archive(base=LIVE_BASE).replace("<header>", '<header><a href="http://[::1" aria-label="Download part 1 of 3">x</a>')
        self.assertEqual(len(takeout.parse_archive(archived, ARCHIVE_PAGE, COMPLETED_ID).parts), 3)
        self.assertEqual(takeout.archive_findings(archived, ARCHIVE_PAGE), "overall status found; numbered download links 3 (3 usable, 0 relative); a base address; page address on Takeout")

    def test_a_base_inside_a_script_is_not_the_pages_base(self):
        page = summary(row(href=f"./manage/archive/{COMPLETED_ID}")).replace('var trap = "', 'var trap = "<base href=\\"https://other.example/\\">')
        self.assertEqual([each.google_id for each in listed(page)], [COMPLETED_ID])


PART_FORMS = {
    "absolute": ("https://takeout.google.com/takeout/download?{query}", None, ARCHIVE_PAGE),
    "relative to the live base": ("takeout/download?{query}", LIVE_BASE, ARCHIVE_PAGE),
    "dot-relative to the live base": ("./takeout/download?{query}", LIVE_BASE, ARCHIVE_PAGE),
    "root-relative": ("/takeout/download?{query}", None, ARCHIVE_PAGE),
    "up from the archive's own address": ("../../takeout/download?{query}", None, ARCHIVE_PAGE),
}
PARTS_ELSEWHERE = {
    "relative to the archive's own address": ("takeout/download?{query}", None, ARCHIVE_PAGE),
    "another host": ("https://other.example/takeout/download?{query}", None, ARCHIVE_PAGE),
    "scheme-relative to another host": ("//other.example/takeout/download?{query}", None, ARCHIVE_PAGE),
    "an uppercase host": ("https://TAKEOUT.google.com/takeout/download?{query}", None, ARCHIVE_PAGE),
    "user info": ("https://takeout.google.com@other.example/takeout/download?{query}", None, ARCHIVE_PAGE),
    "a backslash": ("/\\other.example/takeout/download?{query}", None, ARCHIVE_PAGE),
    "dot segments to another path": ("/takeout/download/../../elsewhere?{query}", None, ARCHIVE_PAGE),
    "dot segments in an absolute link": ("https://takeout.google.com/takeout/download/../../elsewhere?{query}", None, ARCHIVE_PAGE),
    "up to another path": ("../../../elsewhere/download?{query}", None, ARCHIVE_PAGE),
    "a base on another host": ("takeout/download?{query}", "https://other.example/", ARCHIVE_PAGE),
    "a javascript link": ("javascript:void(0)//takeout/download?{query}", None, ARCHIVE_PAGE),
    "a malformed bracketed host": ("https://[x]/takeout/download?{query}", None, ARCHIVE_PAGE),
    "an unclosed bracketed host": ("//[/takeout/download?{query}", None, ARCHIVE_PAGE),
    "a malformed bracketed base": ("takeout/download?{query}", "http://[::1", ARCHIVE_PAGE),
}


class RelativePartLinkTests(unittest.TestCase):
    def test_a_part_link_is_read_the_way_the_browser_resolves_it_and_clicked_as_written(self):
        for name, (form, base, address) in PART_FORMS.items():
            with self.subTest(name):
                page = archive(items=items_linked(form), base=base)
                self.assertTrue(takeout.archive_page_ready(page))
                parts = takeout.parse_archive(page, address, COMPLETED_ID).parts
                self.assertEqual([part.href for part in parts], [f"https://takeout.google.com/takeout/download?j={COMPLETED_ID}&i={index}&user={MADE_UP_USER}" for index in range(3)])
                self.assertEqual([part.link for part in parts], [download_href(COMPLETED_ID, index, form=form).replace("&amp;", "&") for index in range(3)])

    def test_a_part_link_that_resolves_anywhere_else_is_refused(self):
        for name, (form, base, address) in PARTS_ELSEWHERE.items():
            with self.subTest(name), self.assertRaises(takeout.UnreadableExports) as raised:
                takeout.parse_archive(archive(items=items_linked(form), base=base), address, COMPLETED_ID)
            self.assertNotIn("user=", str(raised.exception))
            self.assertNotIn(COMPLETED_ID, str(raised.exception))


class FindingsTests(unittest.TestCase):
    def test_the_summary_findings_count_what_was_found_in_fixed_words(self):
        rejected = summary(row(href=f"./manage/archive/{COMPLETED_ID}"), row(href=f"https://{CANARY}.example/manage/archive/{EXPIRED_ID}"))
        self.assertEqual(takeout.summary_findings(rejected, MANAGE_PAGE + "/"),
                         "summary text found; export links 0 of 7 links (1 relative, 2 naming an export's page); no empty-list sentence; no base address; page address on Takeout")
        self.assertEqual(takeout.summary_findings(summary(completed_row(), base=LIVE_BASE), "https://accounts.google.com/x"),
                         "summary text found; export links 1 of 6 links (0 relative, 1 naming an export's page); no empty-list sentence; a base address; page address not on Takeout")
        self.assertEqual(takeout.summary_findings("", MANAGE_PAGE), "summary text not found; export links 0 of 0 links (0 relative, 0 naming an export's page); no empty-list sentence; no base address; page address on Takeout")
        self.assertEqual(takeout.summary_findings(summary(body="<div>No requests</div>"), MANAGE_PAGE),
                         "summary text found; export links 0 of 5 links (0 relative, 0 naming an export's page); an empty-list sentence; no base address; page address on Takeout")

    def test_the_archive_findings_count_what_was_found_in_fixed_words(self):
        self.assertEqual(takeout.archive_findings(archive(items=items_linked("takeout/download?{query}")), ARCHIVE_PAGE),
                         "overall status found; numbered download links 3 (0 usable, 3 relative); no base address; page address on Takeout")
        self.assertEqual(takeout.archive_findings(archive(loading=True), ARCHIVE_PAGE),
                         "overall status not found; numbered download links 0 (0 usable, 0 relative); no base address; page address on Takeout")
        self.assertEqual(takeout.archive_findings(archive(base=LIVE_BASE), ARCHIVE_PAGE),
                         "overall status found; numbered download links 3 (3 usable, 0 relative); a base address; page address on Takeout")

    def test_findings_name_no_page_text_ids_or_addresses(self):
        page = summary(row(href=f"./{CANARY}/archive/{COMPLETED_ID}"), base=f"https://{CANARY}.example/")
        for said in (takeout.summary_findings(page, f"https://{CANARY}.example/"), takeout.archive_findings(page, f"https://{CANARY}.example/")):
            self.assertNotIn(CANARY.lower(), said.lower())
            self.assertNotIn(COMPLETED_ID, said)
            self.assertNotIn("example", said)


class MergeTests(unittest.TestCase):
    def test_the_archive_adds_its_parts_and_keeps_the_lists_precise_times(self):
        export = takeout.with_archive(listed(summary(completed_row()))[0], takeout.parse_archive(archive(), ARCHIVE_PAGE, COMPLETED_ID), NEW_YORK)
        self.assertEqual((export.state, export.created_at, export.expires_at), ("ready", NOW - 3600, at(2026, 11, 24, 13, 55)))
        self.assertEqual([part.number for part in export.parts], [1, 2, 3])

    def test_the_archives_expiry_date_is_used_when_the_list_has_none(self):
        listing = listed(summary(completed_row(available=None)))[0]
        export = takeout.with_archive(listing, takeout.parse_archive(archive(), ARCHIVE_PAGE, COMPLETED_ID), NEW_YORK)
        self.assertEqual(export.expires_at, at(2026, 11, 24, 0, 0))

    def test_disagreements_are_refused(self):
        listing = listed(summary(completed_row()))[0]
        for name, page in {"state": archive(status="Expired", items=[]), "creation date": archive(created="November 3, 2026")}.items():
            with self.subTest(name), self.assertRaises(takeout.UnreadableExports):
                takeout.with_archive(listing, takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID), NEW_YORK)


class LandmarkTests(unittest.TestCase):
    def test_known_headings_are_named_and_others_only_counted(self):
        page = "<html><head><title>Google Takeout</title></head><body><h1>Summary</h1><h2>Status</h2><h2>Hello Jo Example</h2><h3>jo@example.test</h3></body></html>"
        seen = takeout.landmarks(page)
        self.assertEqual(seen, "title 'google takeout'; headings 'summary', 'status' and 1 other")
        self.assertNotIn("jo", seen)

    def test_a_page_with_nothing_known(self):
        self.assertEqual(takeout.landmarks("<html><head><title>Jo's page</title></head><body><h1>Jo</h1></body></html>"), "an unrecognised title; 1 other heading")
        self.assertEqual(takeout.landmarks(""), "no title or headings")


if __name__ == "__main__":
    unittest.main()
