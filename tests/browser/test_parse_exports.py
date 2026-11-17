import unittest
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from darkroom_browser.pages import takeout
from tests.browser.takeout_pages import (COMPLETED_ID, EXPIRED_ID, IN_PROGRESS_ID, NNBSP, SIZES, archive, archive_url, completed_row, download_href,
                                         expired_row, in_progress_row, part_item, part_names, row, summary)
from tests.snapshots import snapshot_pending

NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 17, 14, 55, tzinfo=NEW_YORK).timestamp()
CANARY = "CANARY-PAGE-TEXT"


def at(*when, zone=NEW_YORK):
    return datetime(*when, tzinfo=zone).timestamp()


def listed(page, now=NOW, zone=NEW_YORK):
    return takeout.parse_exports(page, now, zone)


@snapshot_pending
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
            self.assertFalse(takeout.exports_page_ready(text))


@snapshot_pending
class SummaryReadyTests(unittest.TestCase):
    def test_a_summary_with_exports_is_loaded(self):
        self.assertTrue(takeout.exports_page_ready(summary(completed_row(), expired_row())))

    def test_the_heading_alone_is_not_a_loaded_list(self):
        self.assertFalse(takeout.exports_page_ready(summary()))
        self.assertFalse(takeout.exports_page_ready(summary(body="")))
        self.assertFalse(takeout.exports_page_ready(summary(row(href="https://example.test/manage/archive/x"))))

    def test_an_explicit_empty_summary_is_loaded_and_empty(self):
        for sentence in ("You haven’t made any requests in the last 30 days", "You have no requests", "No requests"):
            with self.subTest(sentence=sentence):
                page = summary(body=f"<div>{sentence}</div>", scheduled=False)
                self.assertTrue(takeout.exports_page_ready(page))
                self.assertEqual(listed(page), [])


@snapshot_pending
class ArchivePageTests(unittest.TestCase):
    def test_a_completed_archive_lists_its_parts_with_exact_sizes_and_links(self):
        detail = takeout.parse_archive(archive(), COMPLETED_ID)
        self.assertEqual((detail.state, detail.created_on, detail.expires_on), ("ready", date(2026, 11, 17), date(2026, 11, 24)))
        self.assertEqual([part.number for part in detail.parts], [1, 2, 3])
        self.assertEqual([part.bytes for part in detail.parts], list(SIZES))
        self.assertEqual([part.href for part in detail.parts], [download_href(COMPLETED_ID, index).replace("&amp;", "&") for index in range(3)])
        self.assertTrue(takeout.archive_page_ready(archive()))

    def test_the_part_number_comes_from_the_label_not_the_file_name_or_the_order(self):
        names = part_names(3)
        items = [part_item(COMPLETED_ID, 2, 3, SIZES[2], names[0]), part_item(COMPLETED_ID, 0, 3, SIZES[0], names[2]), part_item(COMPLETED_ID, 1, 3, SIZES[1], names[1])]
        self.assertEqual([(part.number, part.bytes) for part in takeout.parse_archive(archive(items=items), COMPLETED_ID).parts], list(zip((1, 2, 3), SIZES, strict=True)))

    def test_the_size_falls_back_to_the_shown_size(self):
        items = [part_item(COMPLETED_ID, index, 3, size, "x.zip", size_attribute=False) for index, size in enumerate(SIZES)]
        parts = takeout.parse_archive(archive(items=items), COMPLETED_ID).parts
        self.assertEqual([part.bytes for part in parts], [round(round(size / 1024**3, 2) * 1024**3) for size in SIZES])

    def test_the_latest_export_card_and_the_report_link_are_not_parts(self):
        self.assertEqual(len(takeout.parse_archive(archive(stacked=True), COMPLETED_ID).parts), 3)

    def test_an_expired_or_running_archive_has_no_parts(self):
        for status, state in (("Expired", "expired"), ("In progress", "in_progress")):
            with self.subTest(status=status):
                detail = takeout.parse_archive(archive(status=status, items=[], expires=None), COMPLETED_ID)
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
                takeout.parse_archive(page, COMPLETED_ID)
            self.assertNotIn(CANARY, str(raised.exception))
            self.assertNotIn("user=", str(raised.exception))

    def test_an_archive_url_is_only_built_for_a_plain_id(self):
        self.assertEqual(takeout.archive_url(COMPLETED_ID), archive_url(COMPLETED_ID))
        for bad in ("", "../settings", "a/b", "a?b", "x" * 200):
            with self.subTest(bad=bad), self.assertRaises(takeout.UnreadableExports):
                takeout.archive_url(bad)


@snapshot_pending
class MergeTests(unittest.TestCase):
    def test_the_archive_adds_its_parts_and_keeps_the_lists_precise_times(self):
        export = takeout.with_archive(listed(summary(completed_row()))[0], takeout.parse_archive(archive(), COMPLETED_ID), NEW_YORK)
        self.assertEqual((export.state, export.created_at, export.expires_at), ("ready", NOW - 3600, at(2026, 11, 24, 13, 55)))
        self.assertEqual([part.number for part in export.parts], [1, 2, 3])

    def test_the_archives_expiry_date_is_used_when_the_list_has_none(self):
        listing = listed(summary(completed_row(available=None)))[0]
        export = takeout.with_archive(listing, takeout.parse_archive(archive(), COMPLETED_ID), NEW_YORK)
        self.assertEqual(export.expires_at, at(2026, 11, 24, 0, 0))

    def test_disagreements_are_refused(self):
        listing = listed(summary(completed_row()))[0]
        for name, page in {"state": archive(status="Expired", items=[]), "creation date": archive(created="November 3, 2026")}.items():
            with self.subTest(name), self.assertRaises(takeout.UnreadableExports):
                takeout.with_archive(listing, takeout.parse_archive(page, COMPLETED_ID), NEW_YORK)


@snapshot_pending
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
