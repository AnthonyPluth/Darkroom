import inspect
import tempfile
import unittest
from pathlib import Path

from darkroom_browser import live
from darkroom_browser.context import Look, PartClick
from tests.browser.test_stall import Clock, FakeDownload, FakePage


def as_playwright_wraps(handler):
    inspect.signature(handler)
    if inspect.ismethod(handler):
        setattr(handler.__self__, "_pw_impl_instance_" + handler.__name__, object())
    else:
        handler._pw_impl_instance_ = object()


PART_1 = "https://takeout.google.com/takeout/download?j=export-1&i=0&user=made-up"
PART_2 = "https://takeout.google.com/takeout/download?j=export-1&i=1&user=made-up"
CLICK_1 = PartClick('a[href="takeout/download?j=export-1&i=0&user=made-up"]', PART_1, 1)
REDIRECTED_1 = "https://takeout-download.usercontent.google.com/download/takeout-20261102T031000Z-001.zip?j=export-1&i=0&user=made-up"
REDIRECTED_2 = "https://takeout-download.usercontent.google.com/download/takeout-20261102T031000Z-002.zip?j=export-1&i=1&user=made-up"
UNNAMED = "https://takeout-download.usercontent.google.com/download/some-opaque-path"


class TimedOut:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        raise TimeoutError("no download within 30 seconds")


def part_download(folder, url=PART_1, page=None, name=None):
    download = FakeDownload(folder, {"done": True})
    download.url = url
    download.page = page
    if name is not None:
        download.suggested_filename = name
    return download


class Tab(FakePage):
    def __init__(self, clock, folder, url, html, steps=None):
        super().__init__(clock, folder, steps or [], {"done": True})
        self.url = url
        self.html = html
        self.closed = False
        self.listeners = {}
        self.opened_by = None

    def opener(self):
        return self.opened_by

    def on(self, event, callback):
        as_playwright_wraps(callback)
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event, value):
        for callback in self.listeners.get(event, []):
            callback(value)

    def is_closed(self):
        return self.closed

    def content(self):
        if self.html is None:
            raise RuntimeError("the page is navigating")
        return self.html


class Context:
    def __init__(self, pages):
        self.pages = list(pages)
        self.listeners = []

    def on(self, event, callback):
        assert event == "page"
        as_playwright_wraps(callback)
        self.listeners.append(callback)

    def open(self, page):
        self.pages.append(page)
        for callback in self.listeners:
            callback(page)


class LiveTabsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.downloads = Path(folder.name) / "_inbox" / ".downloads" / "3"
        self.downloads.mkdir(parents=True)
        self.target = Path(folder.name) / "partial"
        self.clock = Clock()
        self.main = Tab(self.clock, self.downloads, "https://takeout.google.com/manage/archive/x", "<h1>Takeout</h1>")
        self.context = Context([self.main])
        self.browser = live.Live(self.main, self.context, None, self.downloads, None)
        self.browser.watch_downloads()

    def test_the_fakes_refuse_a_handler_playwright_could_not_wrap(self):
        with self.assertRaises(AttributeError):
            self.main.on("download", [].append)

    def test_every_open_tab_is_read_the_jobs_own_tab_first(self):
        self.assertEqual(self.browser.tabs(), [Look("https://takeout.google.com/manage/archive/x", "<h1>Takeout</h1>", True)])
        popup = Tab(self.clock, self.downloads, "https://accounts.google.com/v3/signin/challenge/pwd", "<h1>Welcome</h1>")
        self.context.open(popup)
        self.assertEqual(self.browser.tabs(), [Look("https://takeout.google.com/manage/archive/x", "<h1>Takeout</h1>", True),
                                               Look("https://accounts.google.com/v3/signin/challenge/pwd", "<h1>Welcome</h1>", False)])
        popup.closed = True
        self.assertEqual(len(self.browser.tabs()), 1)

    def test_the_jobs_own_tab_comes_first_even_when_it_is_not_the_contexts_first(self):
        earlier = Tab(self.clock, self.downloads, "https://takeout.google.com/", "<h1>Takeout</h1>")
        self.context.pages.insert(0, earlier)
        self.main.url, self.main.html = "https://accounts.google.com/v3/signin/challenge/pwd", "<h1>Welcome</h1>"
        tabs = self.browser.tabs()
        self.assertEqual([(tab.address, tab.primary) for tab in tabs], [("https://accounts.google.com/v3/signin/challenge/pwd", True), ("https://takeout.google.com/", False)])

    def test_a_tab_that_cannot_be_read_while_it_navigates_reads_as_empty(self):
        self.context.open(Tab(self.clock, self.downloads, "https://accounts.google.com/", None))
        self.assertEqual(self.browser.tabs()[1], Look("https://accounts.google.com/", "", False))

    def test_without_a_context_the_tabs_are_the_jobs_own_page(self):
        browser = live.Live(self.main)
        browser.watch_downloads()
        self.assertEqual(len(browser.tabs()), 1)

    def click_that_times_out(self, selector=CLICK_1.selector):
        self.main.expect_download = lambda timeout: TimedOut()
        with self.assertRaises(TimeoutError):
            self.browser.start_download(selector, PartClick(selector, CLICK_1.href, CLICK_1.number))

    def arrives(self, url=PART_1, page=None, name=None):
        (self.downloads / "guid").write_bytes(b"part")
        download = part_download(self.downloads, url, page or self.main, name)
        (page or self.main).emit("download", download)
        return download

    def kept(self):
        handle = self.browser.late_download(CLICK_1)
        self.assertIsNotNone(handle)
        return self.browser.keep_download(handle, self.target)

    def test_a_download_that_arrives_after_the_click_timed_out_is_kept_not_lost(self):
        self.click_that_times_out()
        self.assertIsNone(self.browser.late_download(CLICK_1))
        self.arrives()
        path = self.kept()
        self.assertEqual(path, self.target / "takeout-001.zip")
        self.assertEqual(path.read_bytes(), b"part")
        self.assertIsNone(self.browser.late_download(CLICK_1))

    def test_a_download_that_starts_in_a_popup_the_click_opened_is_kept_too(self):
        self.click_that_times_out()
        popup = Tab(self.clock, self.downloads, "https://takeout.google.com/takeout/download", "")
        popup.opened_by = self.main
        self.context.open(popup)
        self.arrives(page=popup)
        self.assertEqual(self.kept().name, "takeout-001.zip")

    def test_a_download_redirected_to_googles_download_host_is_kept_when_it_names_this_export_and_part(self):
        self.click_that_times_out()
        self.arrives(REDIRECTED_1)
        self.assertEqual(self.kept().name, "takeout-001.zip")

    def test_a_redirected_download_without_its_ids_is_not_kept_even_when_named_for_this_part(self):
        self.assert_not_kept(UNNAMED, name="takeout-20261102T031000Z-001.zip")

    def test_another_exports_part_1_whose_redirect_dropped_the_ids_is_not_kept(self):
        popup = Tab(self.clock, self.downloads, "https://takeout.google.com/manage/archive/export-2", "")
        popup.opened_by = self.main
        self.context.open(popup)
        self.assert_not_kept(UNNAMED, page=popup, name="takeout-20260901T000000Z-001.zip")

    def test_a_download_from_a_host_that_is_not_googles_is_not_kept_even_with_this_parts_ids(self):
        self.assert_not_kept("https://other.example/takeout/download?j=export-1&i=0&user=made-up")

    def test_a_name_only_download_from_a_host_that_is_not_googles_is_not_kept(self):
        self.assert_not_kept("https://other.example/x-001.zip", name="x-001.zip")

    def assert_not_kept(self, url=PART_1, page=None, name=None, click=True):
        if click:
            self.click_that_times_out()
        other = self.arrives(url, page, name)
        self.assertIsNone(self.browser.late_download(CLICK_1))
        self.assertEqual(self.browser.drop_strays(), 1)
        self.assertTrue(other.cancelled)
        self.assertFalse(self.target.exists())
        self.assertIsNone(self.browser.late_download(CLICK_1))

    def test_a_download_of_another_part_is_not_kept(self):
        self.assert_not_kept(PART_2)

    def test_a_redirected_download_of_another_part_is_not_kept(self):
        self.assert_not_kept(REDIRECTED_2)

    def test_a_redirected_download_without_ids_named_for_another_part_is_not_kept(self):
        self.assert_not_kept(UNNAMED, name="takeout-20261102T031000Z-002.zip")

    def test_a_redirected_download_without_ids_or_a_part_number_is_not_kept(self):
        self.assert_not_kept(UNNAMED, name="photos.zip")

    def test_a_download_of_another_export_is_not_kept(self):
        self.assert_not_kept("https://takeout.google.com/takeout/download?j=export-2&i=0&user=made-up")
        self.assert_not_kept("https://takeout-download.usercontent.google.com/download/takeout-x-001.zip?j=export-2&i=0")

    def test_a_download_without_a_readable_address_is_not_kept(self):
        self.assert_not_kept(None)

    def test_a_plain_http_or_off_path_takeout_address_is_not_kept(self):
        self.assert_not_kept("http://takeout-download.usercontent.google.com/download/takeout-x-001.zip?j=export-1&i=0")
        self.assert_not_kept("https://takeout.google.com/settings?j=export-1&i=0")

    def test_a_download_in_a_tab_the_job_did_not_open_is_not_kept_even_for_this_part(self):
        stranger = Tab(self.clock, self.downloads, "https://takeout.google.com/manage", "")
        self.context.open(stranger)
        self.assert_not_kept(PART_1, page=stranger)

    def test_a_download_with_no_tab_is_not_kept(self):
        self.click_that_times_out()
        (self.downloads / "guid").write_bytes(b"part")
        orphan = part_download(self.downloads, PART_1, None)
        self.main.emit("download", orphan)
        self.assertIsNone(self.browser.late_download(CLICK_1))

    def test_a_download_the_job_never_clicked_this_parts_link_for_is_not_kept(self):
        self.assert_not_kept(PART_1, click=False)

    def test_a_download_after_a_click_on_another_link_is_not_kept(self):
        self.click_that_times_out('a[href="takeout/download?j=export-1&i=1&user=made-up"]')
        self.assert_not_kept(PART_1, click=False)

    def test_this_parts_download_is_kept_and_another_one_is_cancelled(self):
        self.click_that_times_out()
        other = self.arrives(PART_2)
        own = self.arrives(PART_1)
        self.assertEqual(self.kept().name, "takeout-001.zip")
        self.browser.drop_strays()
        self.assertTrue(other.cancelled)
        self.assertFalse(own.cancelled)

    def test_the_event_of_a_download_the_click_itself_caught_is_not_kept_twice(self):
        caught = self.main.download
        caught.url, caught.page = PART_1, self.main

        def click(selector, timeout):
            self.main.clicked.append(selector)
            (self.downloads / "guid.crdownload").write_bytes(b"part")
            self.main.emit("download", caught)

        self.main.click = click
        self.assertEqual(self.browser.start_download(CLICK_1.selector, CLICK_1), "guid")
        self.assertIsNone(self.browser.late_download(CLICK_1))

if __name__ == "__main__":
    unittest.main()
