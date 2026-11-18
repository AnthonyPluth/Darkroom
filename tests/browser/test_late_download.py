import inspect
import tempfile
import unittest
from pathlib import Path

from darkroom_browser import live
from darkroom_browser.context import DownloadInterrupted, Look
from tests.browser.test_stall import Clock, FakeDownload, FakePage


def as_playwright_wraps(handler):
    inspect.signature(handler)
    if inspect.ismethod(handler):
        setattr(handler.__self__, "_pw_impl_instance_" + handler.__name__, object())
    else:
        handler._pw_impl_instance_ = object()


PART_1 = "https://takeout.google.com/takeout/download?j=export-1&i=0&user=made-up"
PART_2 = "https://takeout.google.com/takeout/download?j=export-1&i=1&user=made-up"


def part_download(folder, url=PART_1):
    download = FakeDownload(folder, {"done": True})
    download.url = url
    return download


class Tab(FakePage):
    def __init__(self, clock, folder, url, html, steps=None):
        super().__init__(clock, folder, steps or [], {"done": True})
        self.url = url
        self.html = html
        self.closed = False
        self.listeners = {}

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
        self.downloads = Path(folder.name) / "downloads"
        self.downloads.mkdir()
        self.target = Path(folder.name) / "partial"
        self.clock = Clock()
        self.main = Tab(self.clock, self.downloads, "https://takeout.google.com/manage/archive/x", "<h1>Takeout</h1>")
        self.context = Context([self.main])
        self.browser = live.Live(self.main, self.context, None, self.downloads, None, self.clock)
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

    def test_a_download_that_arrives_after_the_click_timed_out_is_kept_not_lost(self):
        self.assertFalse(self.browser.download_started())
        late = part_download(self.downloads)
        (self.downloads / "guid").write_bytes(b"part")
        self.main.emit("download", late)
        self.assertTrue(self.browser.download_started())
        path = self.browser.collect_download(self.target, PART_1)
        self.assertEqual(path, self.target / "takeout-001.zip")
        self.assertEqual(path.read_bytes(), b"part")
        self.assertFalse(self.browser.download_started())

    def test_a_download_that_starts_in_a_popup_is_kept_too(self):
        popup = Tab(self.clock, self.downloads, "https://takeout.google.com/takeout/download", "")
        self.context.open(popup)
        (self.downloads / "guid").write_bytes(b"part")
        popup.emit("download", part_download(self.downloads))
        self.assertTrue(self.browser.download_started())
        self.assertEqual(self.browser.collect_download(self.target, PART_1).name, "takeout-001.zip")

    def assert_not_kept(self, url):
        (self.downloads / "guid").write_bytes(b"someone else's zip")
        other = part_download(self.downloads, url)
        self.main.emit("download", other)
        with self.assertRaisesRegex(DownloadInterrupted, "not this part's"):
            self.browser.collect_download(self.target, PART_1)
        self.assertTrue(other.cancelled)
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.assertFalse(self.target.exists() and any(self.target.iterdir()))
        self.assertFalse(self.browser.download_started())

    def test_a_download_from_another_host_is_not_kept(self):
        self.assert_not_kept("https://other.example/takeout/download?j=export-1&i=0&user=made-up")

    def test_a_download_of_another_part_is_not_kept(self):
        self.assert_not_kept(PART_2)

    def test_a_download_of_another_export_is_not_kept(self):
        self.assert_not_kept("https://takeout.google.com/takeout/download?j=export-2&i=0&user=made-up")

    def test_a_download_without_a_readable_address_is_not_kept(self):
        self.assert_not_kept(None)

    def test_this_parts_download_is_kept_and_another_one_is_cancelled(self):
        (self.downloads / "guid").write_bytes(b"part")
        other, own = part_download(self.downloads, PART_2), part_download(self.downloads)
        self.main.emit("download", other)
        self.main.emit("download", own)
        self.assertEqual(self.browser.collect_download(self.target, PART_1).name, "takeout-001.zip")
        self.assertTrue(other.cancelled)
        self.assertFalse(own.cancelled)

    def test_a_file_in_chromiums_folder_counts_as_a_download_under_way(self):
        (self.downloads / "guid.crdownload").write_bytes(b"half")
        self.assertTrue(self.browser.download_started())

    def test_a_file_without_playwrights_event_is_waited_on_then_swept_and_reported_interrupted(self):
        (self.downloads / "guid.crdownload").write_bytes(b"half")
        with self.assertRaisesRegex(DownloadInterrupted, "without Playwright's event"):
            self.browser.collect_download(self.target, PART_1)
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.assertGreater(self.main.polls, 0)

    def test_the_event_of_a_download_the_click_itself_caught_is_not_kept_twice(self):
        (self.downloads / "guid").write_bytes(b"part")
        caught = self.main.download

        def click(selector, timeout):
            self.main.clicked.append(selector)
            self.main.emit("download", caught)

        self.main.click = click
        self.assertEqual(self.browser.download_to("a", self.target).name, "takeout-001.zip")
        self.assertFalse(self.browser.download_started())

    def test_a_download_chromium_failed_leaves_nothing_that_reads_as_a_download_under_way(self):
        (self.downloads / "guid").write_bytes(b"half")
        self.main.download = FakeDownload(self.downloads, {"done": True, "error": "net::ERR_FAILED"})
        with self.assertRaises(DownloadInterrupted):
            self.browser.download_to("a", self.target)
        self.assertFalse(self.browser.download_started())

    def test_a_late_event_from_an_earlier_click_is_forgotten_when_a_new_click_starts(self):
        self.main.emit("download", FakeDownload(self.downloads, {"done": True}))
        (self.downloads / "guid").write_bytes(b"part")
        self.browser.download_to("a", self.target)
        self.assertFalse(self.browser.download_started())


if __name__ == "__main__":
    unittest.main()
