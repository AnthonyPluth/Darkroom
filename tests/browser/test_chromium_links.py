import json
import os
import unittest
from typing import ClassVar
from zoneinfo import ZoneInfo

from darkroom_browser.jobs import download
from darkroom_browser.live import Live
from darkroom_browser.pages import takeout, takeout_data
from tests.browser import test_takeout_data as data
from tests.browser.takeout_pages import (COMPLETED_ID, EXPIRED_ID, MADE_UP_USER, MANAGE_PAGE, SIZES, archive, archive_url, completed_row, downloaded_page_parts, embedded_parts,
                                         expired_row, in_progress_row, items_linked, row, summary, with_blocks)
from tests.browser.test_parse_exports import ARCHIVE_PAGE, NOW, PART_FORMS, PARTS_ELSEWHERE, ROW_FORMS, ROWS_ELSEWHERE

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

REQUIRED = os.environ.get("DARKROOM_CHROMIUM_TESTS") == "1"
NEW_YORK = ZoneInfo("America/New_York")
UNLOADABLE = {"a malformed bracketed page address"}
MORE_PART_FORMS = {
    "encoded characters in the query": ("/takeout/download?{query}&amp;next=%2F%5C%252e", None, ARCHIVE_PAGE),
    "parameters whose names only contain j, i or user": ("/takeout/download?{query}&amp;xj=1&amp;ji=2&amp;usera=3&amp;j_=4&amp;other=j%3D5", None, ARCHIVE_PAGE),
}


def part_href(index):
    return f"https://takeout.google.com/takeout/download?j={COMPLETED_ID}&i={index}&user={MADE_UP_USER}"


class ChromiumLinkTests(unittest.TestCase):
    pages: ClassVar[dict[str, str]] = {}

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            cls.unavailable("Playwright is not installed here")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chromium", headless=True)
        except PlaywrightError as error:
            cls.playwright.stop()
            cls.unavailable(f"Chromium did not start: {str(error).splitlines()[0]}")
        cls.context = cls.browser.new_context()
        cls.context.route("**/*", cls.serve)
        cls.page = cls.context.new_page()

    @classmethod
    def unavailable(cls, reason):
        if REQUIRED:
            raise AssertionError(f"DARKROOM_CHROMIUM_TESTS=1 but {reason}")
        raise unittest.SkipTest(reason)

    @classmethod
    def tearDownClass(cls):
        cls.context.close()
        cls.browser.close()
        cls.playwright.stop()

    @classmethod
    def serve(cls, route):
        page = cls.pages.get(route.request.url)
        if page is None:
            route.abort()
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page)

    def load(self, address, page):
        type(self).pages = {address: page}
        live = Live(self.page)
        live.goto(address)
        return live

    def resolved_by_chromium(self, live, raw):
        return live.link_targets(download.part_link(raw))

    def test_chromium_resolves_every_accepted_part_link_to_the_address_the_parser_checked(self):
        for name, (form, base, address) in {**PART_FORMS, **MORE_PART_FORMS}.items():
            with self.subTest(name):
                live = self.load(address, archive(items=items_linked(form), base=base))
                parts = takeout.parse_archive(live.content(), live.current_url(), COMPLETED_ID).parts
                self.assertEqual(len(parts), 3)
                for index, part in enumerate(parts):
                    self.assertTrue(part.href.startswith(part_href(index)))
                    self.assertEqual(self.resolved_by_chromium(live, part.link), [part.href])
                    self.assertTrue(download.leads_where_checked(live, download.part_link(part.link), part.href))

    def test_chromium_reads_a_refused_part_link_nowhere_the_parser_would_have_resolved_it_differently(self):
        for name, (form, base, address) in PARTS_ELSEWHERE.items():
            with self.subTest(name):
                live = self.load(address, archive(items=items_linked(form), base=base))
                with self.assertRaises(takeout.UnreadableExports):
                    takeout.parse_archive(live.content(), live.current_url(), COMPLETED_ID)
                reader = takeout.read_archive(live.content())
                raws = {raw for item in reader.items for raw, _ in takeout.numbered_link(item)}
                self.assertTrue(raws)
                for raw in raws:
                    ours = takeout.resolved(raw, address, base)
                    chromiums = set(self.resolved_by_chromium(live, raw))
                    self.assertEqual(len(chromiums), 1)
                    if ours is not None:
                        self.assertEqual(chromiums, {ours})

    def test_a_link_chromium_writes_differently_is_refused_at_the_click(self):
        form = "/takeout/download?{query}&amp;note='%C3%A9"
        live = self.load(ARCHIVE_PAGE, archive(items=items_linked(form)))
        part = takeout.parse_archive(live.content(), live.current_url(), COMPLETED_ID).parts[0]
        self.assertEqual(self.resolved_by_chromium(live, part.link), [part.href.replace("'", "%27")])
        self.assertFalse(download.leads_where_checked(live, download.part_link(part.link), part.href))

    def test_chromium_resolves_every_accepted_export_link_to_the_export_page_the_parser_read(self):
        for name, (form, base, address) in ROW_FORMS.items():
            with self.subTest(name):
                raw = form.format(COMPLETED_ID)
                live = self.load(address, summary(row(href=raw), expired_row(), base=base))
                found = takeout.parse_exports(live.content(), live.current_url(), NOW, NEW_YORK)
                self.assertEqual([each.google_id for each in found], [COMPLETED_ID, EXPIRED_ID])
                self.assertEqual(takeout.resolved(raw, address, base), archive_url(COMPLETED_ID))
                self.assertEqual(self.resolved_by_chromium(live, raw), [archive_url(COMPLETED_ID)])

    def test_the_embedded_data_survives_chromiums_serialisation_of_the_page(self):
        listing = self.load(MANAGE_PAGE, data.list_page(data.completed(), data.in_progress(), data.expired(),
                                                         rows=(completed_row(), in_progress_row(), expired_row())))
        found = takeout.read_exports(listing.content(), listing.current_url(), NOW, NEW_YORK)
        self.assertIsNone(found.unused)
        self.assertEqual([each.created_at for each in found.listings], [data.CREATED_MS / 1000, data.IN_PROGRESS_CREATED_MS / 1000, data.EXPIRED_CREATED_MS / 1000])
        detail = self.load(ARCHIVE_PAGE, data.detail_page(data.completed(parts=embedded_parts(downloaded=(1,))), data.expired(),
                                                          page=archive(items=downloaded_page_parts(count=3, sizes=SIZES))))
        read = takeout.parse_archive(detail.content(), detail.current_url(), COMPLETED_ID, now=NOW)
        self.assertIsNone(read.unused)
        self.assertEqual([(part.google_downloads, part.bytes) for part in read.parts], [(2, SIZES[0]), (0, SIZES[1]), (0, SIZES[2])])
        for part in read.parts:
            self.assertEqual(self.resolved_by_chromium(detail, part.link), [part.href])

    def test_a_closing_tag_inside_a_string_is_cut_by_chromium_as_by_the_parser(self):
        raw = json.dumps([[[None, data.completed()]], "made-up </script><script>window.madeUp = 1</script>"])
        page = with_blocks(archive(), [data.raw_block(raw, key="ds:0")])
        live = self.load(ARCHIVE_PAGE, page)
        reader = takeout_data.ScriptReader()
        reader.feed(page)
        reader.close()
        self.assertEqual(self.page.evaluate("Array.from(document.scripts, script => script.text)"), reader.scripts)
        self.assertEqual(takeout_data.read_embedded(live.content(), NOW).unused, takeout_data.NOT_DATA)
        self.assertEqual(takeout_data.read_embedded(page, NOW).unused, takeout_data.NOT_DATA)
        read = takeout.parse_archive(live.content(), live.current_url(), COMPLETED_ID, now=NOW)
        self.assertEqual((len(read.parts), read.unused), (3, takeout_data.NOT_DATA))

    def test_chromium_reads_a_refused_export_link_nowhere_the_parser_would_have_resolved_it_differently(self):
        for name, (form, base, address) in ROWS_ELSEWHERE.items():
            if name in UNLOADABLE:
                continue
            with self.subTest(name):
                raw = form.format(COMPLETED_ID)
                live = self.load(address, summary(row(href=raw), base=base))
                self.assertEqual(takeout.parse_exports(live.content(), live.current_url(), NOW, NEW_YORK), [])
                ours = takeout.resolved(raw, address, base)
                chromiums = self.resolved_by_chromium(live, raw)
                self.assertEqual(len(chromiums), 1)
                if ours is not None:
                    self.assertEqual(chromiums, [ours])


if __name__ == "__main__":
    unittest.main()
