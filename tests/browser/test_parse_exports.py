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


def created(text, now=NOW, zone=NEW_YORK):
    return listed(summary(row(created=text, available=None)), now=now, zone=zone)[0].created_at


def until(text):
    return listed(summary(row(available=text)))[0].expires_at


def refusal(page, now=NOW):
    try:
        listed(page, now=now)
    except takeout.UnreadableExports as error:
        return str(error)
    raise AssertionError("the page was read")


class DateShapeTests(unittest.TestCase):
    def test_yesterday_and_today_are_days_of_the_workers_zone(self):
        for text, expected in (("Created yesterday", at(2026, 11, 16, 0, 0)), (f"Created yesterday at 8:12{NNBSP}PM", at(2026, 11, 16, 20, 12)),
                               ("Created Yesterday, 20:12", at(2026, 11, 16, 20, 12)), ("Created today", at(2026, 11, 17, 0, 0)),
                               ("Created today at 9:05 AM", at(2026, 11, 17, 9, 5)), ("Created Today, 14:45", at(2026, 11, 17, 14, 45))):
            with self.subTest(text=text):
                self.assertEqual(created(text), expected)
        tokyo = ZoneInfo("Asia/Tokyo")
        self.assertEqual(created("Created yesterday at 10:00 PM", zone=tokyo), at(2026, 11, 17, 22, 0, zone=tokyo))

    def test_months_and_years_ago_count_as_thirty_and_three_hundred_sixty_five_days(self):
        for text, days in (("Created a month ago", 30), ("Created 1 month ago", 30), ("Created 2 months ago", 60), ("Created a year ago", 365),
                           ("Created 2 years ago", 730), ("Created 3 weeks ago", 21)):
            with self.subTest(text=text):
                self.assertEqual(created(text), NOW - days * 86400)

    def test_abbreviated_months_and_on_are_read(self):
        for text in (f"Created Nov 9, 2:45{NNBSP}PM", f"Created Nov. 9, 2:45{NNBSP}PM", "Created on November 9, 2:45 PM", "Created on Nov 9 at 2:45 p.m.",
                     "Created November 9, 2:45PM", "Created November 9, 2026, 2:45 PM", "Created Mon, Nov 9, 2026, 2:45 PM"):
            with self.subTest(text=text):
                self.assertEqual(created(text), at(2026, 11, 9, 14, 45))
        self.assertEqual(created("Created Monday, November 9"), at(2026, 11, 9, 0, 0))
        self.assertEqual(created("Created Sept 30"), at(2026, 9, 30, 0, 0))
        self.assertEqual(created("Created sep 30, 2026"), at(2026, 9, 30, 0, 0))

    def test_a_twenty_four_hour_clock_and_the_day_before_the_month_are_read(self):
        for text in ("Created November 9, 14:45", "Created 9 November, 14:45", "Created 9 Nov 2026, 14:45", "Created 9 November 2026 at 14:45"):
            with self.subTest(text=text):
                self.assertEqual(created(text), at(2026, 11, 9, 14, 45))
        self.assertEqual(created("Created 9 November, 00:05"), at(2026, 11, 9, 0, 5))

    def test_a_weekday_is_the_latest_one_not_after_now(self):
        for text, expected in (("Created Monday at 8:12 PM", at(2026, 11, 16, 20, 12)), ("Created Tuesday at 9:00 AM", at(2026, 11, 17, 9, 0)),
                               ("Created Tuesday at 11:00 PM", at(2026, 11, 10, 23, 0)), ("Created Wed", at(2026, 11, 11, 0, 0)),
                               ("Created Wednesday, 18:30", at(2026, 11, 11, 18, 30)), ("Created Thurs. at 1:01 AM", at(2026, 11, 12, 1, 1))):
            with self.subTest(text=text):
                self.assertEqual(created(text), expected)

    def test_more_forms_of_available_until_are_read(self):
        for text in ("Available until Nov 24, 2026 at 1:55 PM", "Available until November 24, 2026, 1:55 PM", "Available until November 24, 2026 1:55 PM",
                     f"Available until Tuesday, November 24, 2026 at 1:55{NNBSP}PM", "Available until Tue, Nov 24, 2026, 13:55", "Available until 24 November 2026, 13:55",
                     "Available until 24 Nov. 2026 at 13:55"):
            with self.subTest(text=text):
                self.assertEqual(until(text), at(2026, 11, 24, 13, 55))
        self.assertEqual(until("Available until Nov 24, 2026"), at(2026, 11, 24, 0, 0))

    def test_shapes_that_prove_nothing_are_still_refused(self):
        for text in ("Created Novembr 9", "Created November 9, 24:00", "Created November 9, 13:00 PM", "Created November 9, 2:60 PM", "Created November 9, 0:30 AM",
                     "Created November 9, 2:45", "Created yesterday at 9:05",                      "Created Funday at 3:00 PM", "Created 9 9", "Created 11/9", "Created 2 fortnights ago", "Created in 3 days", "Created today at 4:30 PM",
                     "Created November 9, 2:45 PM extra", "Created Wednesday, November 9, 2026", "Created Wednesday, November 9", "Created Wed Nov 9 at 2:45 PM", "Created December 1, 2026", "Created tomorrow",
                     "Created yesterday 2:45 PM ago", "Created Tuesday November", "Created 32 November", "Created 1 Juno 2026"):
            with self.subTest(text=text):
                refusal(summary(row(created=text)))
        for text in ("Available until November 24", "Available until Wednesday, November 24, 2026", "Available until Nov 31, 2026", "Available until tomorrow"):
            with self.subTest(text=text):
                refusal(summary(row(available=text)))

    def test_a_clock_whose_am_or_pm_is_shown_apart_is_refused(self):
        self.assertEqual(created("Created November 9, 10:45"), at(2026, 11, 9, 10, 45))
        for split in ("PM", "p.m.", "AM"):
            with self.subTest(split=split):
                refusal(summary(row(created=f"Created November 9, 10:45</div><div>{split}")))


class DateShapeMessageTests(unittest.TestCase):
    def test_the_failure_names_the_shape_of_each_unreadable_date(self):
        said = refusal(summary(row(created=f"Created Novembre 9, 2:45{NNBSP}PM"), expired_row()))
        self.assertEqual(said, "an export on Takeout's summary has a date that could not be read (1 of 2 exports): 'created ? 9, 9:99 pm'")
        said = refusal(summary(row(created="Created yesterday at 2:45 PM ago", available="Available until 24/11/2026")))
        self.assertEqual(said, "an export on Takeout's summary has a date that could not be read (1 of 1 exports): 'created yesterday at 9:99 pm ago', 'available until 99/99/9999'")

    def test_only_the_first_two_unreadable_exports_are_shaped(self):
        rows = [row(google_id=f"id-{number}", created=f"Created {number} lunes ago", available=None) for number in range(1, 5)]
        said = refusal(summary(*rows, expired_row()))
        self.assertEqual(said, "an export on Takeout's summary has a date that could not be read (4 of 5 exports): 'created 9 ? ago'; 'created 9 ? ago'")

    def test_a_row_with_no_creation_date_shows_the_shape_of_its_text(self):
        said = refusal(summary(row(created="Created</div><div>2 hours ago")))
        self.assertEqual(said, "an export on Takeout's summary has no readable creation date: '? ?', 'completed', '? ? ? a ? ?', 'created', '9 hours ago', "
                               "'available until november 99, 9999 at 9:99 pm'")

    def test_digits_long_runs_and_long_text_are_bounded(self):
        said = refusal(summary(row(created="Created " + "a " * 200 + "12345678901234567890")))
        self.assertIn("'" + ("created " + "a " * 200)[:takeout.SHAPE_LIMIT - 3] + "...'", said)
        self.assertLess(len(said), len(takeout.DATE_UNREADABLE) + takeout.SHAPE_LIMIT + 30)
        self.assertEqual(takeout.shape("user 100000000000000000001 at 0a1b"), "? 9+ at 9a9?")

    def test_the_shapes_keep_no_ids_addresses_or_free_text(self):
        canaries = (f"Created {CANARY} 9, 2:45 PM", f"Created {COMPLETED_ID}", "Created https://takeout.google.com/manage/archive/x9", "Created jo@example.test",
                    "Created Jo Example's photos", "Created 東京 9", "Created Ｎｏｖ ９")
        for text in canaries:
            with self.subTest(text=text):
                said = refusal(summary(row(created=text)))
                for leaked in (CANARY.lower(), COMPLETED_ID, "takeout.google", "example", "jo", "photos", "manage", "東京", "Ｎｏｖ", "９", "x9"):
                    self.assertNotIn(leaked, said.lower().removeprefix("an export on takeout's summary has a date that could not be read"))
        self.assertEqual(takeout.shape("created jo@example.test"), "created ?.?")
        self.assertEqual(takeout.shape(f"created {COMPLETED_ID}"), "created 9a9?9?9?-9999-9?-9?-9+")


class ArchiveDateTests(unittest.TestCase):
    def test_the_archives_dates_take_the_same_month_forms(self):
        for text in ("November 17, 2026", "Nov 17, 2026", "Nov. 17, 2026", "17 November 2026", "17 Nov 2026", "Tuesday, November 17, 2026", "Tue, Nov 17, 2026",
                     "November 17, 2026, 12:55 PM", "November 17, 2026 at 12:55 PM", "17 November 2026, 12:55"):
            with self.subTest(text=text):
                detail = takeout.parse_archive(archive(created=text, expires=text.replace("17", "24")), ARCHIVE_PAGE, COMPLETED_ID)
                self.assertEqual((detail.created_on, detail.expires_on), (date(2026, 11, 17), date(2026, 11, 24)))

    def test_an_archive_date_that_proves_nothing_is_refused(self):
        for text in ("November 17", "Monday, November 17, 2026", "Novembre 17, 2026", "17/11/2026", "yesterday", "November 17, 2026, 13:00 PM"):
            with self.subTest(text=text), self.assertRaises(takeout.UnreadableExports):
                takeout.parse_archive(archive(created=text), ARCHIVE_PAGE, COMPLETED_ID)

    def test_the_failure_names_the_shape_of_each_unreadable_archive_date(self):
        failed = "an export's page on Takeout has a date that could not be read: "
        for page, said in ((archive(created="Novembre 17, 2026"), "creation date '? 99, 9999'"),
                           (archive(created="Tue 17 Novembre", expires="24/11/2026"), "creation date 'tue 99 ?', expiration date '99/99/9999'"),
                           (archive().replace("<li>Creation date: November 17, 2026</li>", ""), "no creation date"),
                           (archive(created=f"{CANARY} 17, 2026 {COMPLETED_ID}"), "creation date '?-?-? 99, 9999 9a9?9?9?-9999-9?-9?-9+'")):
            with self.subTest(said=said), self.assertRaises(takeout.UnreadableExports) as raised:
                takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID)
            self.assertEqual(str(raised.exception), failed + said)


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
    "an encoded dot segment": ("/manage/%2e/archive/{}", None, MANAGE_PAGE),
    "an uppercase encoded dot segment": ("/manage/%2E/archive/{}", None, MANAGE_PAGE),
    "encoded double dots": ("/manage/archive/x/%2e%2e/{}", None, MANAGE_PAGE),
    "a dot and an encoded dot": ("/manage/archive/x/.%2E/{}", None, MANAGE_PAGE),
    "an encoded dot and a dot": ("/manage/archive/x/%2e./{}", None, MANAGE_PAGE),
    "half-encoded double dots up from a segment": ("/manage/.%2e/manage/archive/{}", None, MANAGE_PAGE),
    "an encoded dot relative to /manage/": ("%2e/archive/{}", None, MANAGE_PAGE + "/"),
    "a base with encoded double dots": ("./manage/archive/{}", "https://takeout.google.com/x/%2E%2e/", MANAGE_PAGE),
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
    "a trailing dot segment": ("/manage/archive/{}/.", None, MANAGE_PAGE),
    "a trailing double-dot segment": ("/manage/archive/{}/x/..", None, MANAGE_PAGE),
    "a trailing encoded dot segment": ("/manage/archive/{}/%2e", None, MANAGE_PAGE),
    "a trailing encoded double-dot segment": ("/manage/archive/{}/x/%2E%2e", None, MANAGE_PAGE),
    "an encoded dot segment that moves the path": ("/manage/%2e/../archive/{}", None, MANAGE_PAGE),
    "an uppercase encoded dot segment that moves the path": ("/manage/%2E/../archive/{}", None, MANAGE_PAGE),
    "a dot and an encoded dot that move the path": ("/manage/.%2e/../archive/{}", None, MANAGE_PAGE),
    "an encoded dot and a dot that move the path": ("/manage/%2E./../archive/{}", None, MANAGE_PAGE),
    "encoded double dots that move the path": ("/manage/%2e%2e/../archive/{}", None, MANAGE_PAGE),
    "an encoded slash": ("/manage/archive/x%2f/../{}", None, MANAGE_PAGE),
    "an uppercase encoded slash": ("/manage/archive/x%2F/../{}", None, MANAGE_PAGE),
    "an encoded backslash": ("/manage/archive/x%5c/../{}", None, MANAGE_PAGE),
    "an uppercase encoded backslash": ("/manage/archive/x%5C/../{}", None, MANAGE_PAGE),
    "a double-encoded dot": ("/manage/archive/%252e/../{}", None, MANAGE_PAGE),
    "a base with an encoded slash": ("./manage/archive/{}", "https://takeout.google.com/x%2f/../", MANAGE_PAGE),
    "a base with an encoded dot that moves it": ("./archive/{}", "https://takeout.google.com/manage/%2e/../", MANAGE_PAGE),
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
    "an encoded dot segment": ("/takeout/%2e/download?{query}", None, ARCHIVE_PAGE),
    "encoded double dots": ("/takeout/x/%2E%2E/download?{query}", None, ARCHIVE_PAGE),
    "encoded double dots up from the archive's own address": ("%2e%2e/%2e%2e/takeout/download?{query}", None, ARCHIVE_PAGE),
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
    "a trailing dot segment": ("/takeout/download/.?{query}", None, ARCHIVE_PAGE),
    "a trailing double-dot segment": ("/takeout/download/x/..?{query}", None, ARCHIVE_PAGE),
    "a trailing encoded dot segment": ("/takeout/download/%2E?{query}", None, ARCHIVE_PAGE),
    "an encoded dot segment that moves the path": ("/takeout/%2e/../download?{query}", None, ARCHIVE_PAGE),
    "an encoded dot segment in an absolute link": ("https://takeout.google.com/takeout/%2e/../download?{query}", None, ARCHIVE_PAGE),
    "a dot and an encoded dot that move the path": ("/takeout/.%2E/../download?{query}", None, ARCHIVE_PAGE),
    "an encoded dot and a dot that move the path": ("/takeout/%2E./../download?{query}", None, ARCHIVE_PAGE),
    "an encoded slash": ("/takeout/x%2F/../download?{query}", None, ARCHIVE_PAGE),
    "an encoded backslash": ("/takeout/x%5c/../download?{query}", None, ARCHIVE_PAGE),
    "a double-encoded dot": ("/takeout/%252e/../download?{query}", None, ARCHIVE_PAGE),
    "the export id only in the fragment": ("/takeout/download?i=0#&{query}", None, ARCHIVE_PAGE),
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

    def test_encoded_characters_in_the_query_are_left_as_written(self):
        form = "/takeout/download?{query}&amp;next=%2F%5C%252e"
        parts = takeout.parse_archive(archive(items=items_linked(form)), ARCHIVE_PAGE, COMPLETED_ID).parts
        self.assertEqual(parts[0].href, f"https://takeout.google.com/takeout/download?j={COMPLETED_ID}&i=0&user={MADE_UP_USER}&next=%2F%5C%252e")

    def test_a_part_link_that_resolves_anywhere_else_is_refused(self):
        for name, (form, base, address) in PARTS_ELSEWHERE.items():
            with self.subTest(name):
                with self.assertRaises(takeout.UnreadableExports) as raised:
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
