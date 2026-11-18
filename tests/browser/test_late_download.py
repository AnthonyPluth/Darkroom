import tempfile
import unittest
from pathlib import Path

from darkroom_browser import live
from darkroom_browser.context import DownloadInterrupted, Look
from tests.browser.test_stall import Clock, FakeDownload, FakePage


class Tab(FakePage):
    def __init__(self, clock, folder, url, html, steps=None):
        super().__init__(clock, folder, steps or [], {"done": True})
        self.url = url
        self.html = html
        self.closed = False
        self.listeners = {}

    def on(self, event, callback):
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

    def test_a_look_reads_the_newest_open_tab_and_counts_the_open_tabs(self):
        self.assertEqual(self.browser.look(), Look("https://takeout.google.com/manage/archive/x", "<h1>Takeout</h1>", 1))
        popup = Tab(self.clock, self.downloads, "https://accounts.google.com/v3/signin/challenge/pwd", "<h1>Welcome</h1>")
        self.context.open(popup)
        self.assertEqual(self.browser.look(), Look("https://accounts.google.com/v3/signin/challenge/pwd", "<h1>Welcome</h1>", 2))
        popup.closed = True
        self.assertEqual(self.browser.look().tabs, 1)
        self.assertEqual(self.browser.look().address, "https://takeout.google.com/manage/archive/x")

    def test_a_tab_that_cannot_be_read_while_it_navigates_reads_as_empty(self):
        self.context.open(Tab(self.clock, self.downloads, "https://accounts.google.com/", None))
        self.assertEqual(self.browser.look(), Look("https://accounts.google.com/", "", 2))

    def test_without_a_context_the_look_is_the_jobs_own_page(self):
        browser = live.Live(self.main)
        browser.watch_downloads()
        self.assertEqual(browser.look().tabs, 1)

    def test_a_download_that_arrives_after_the_click_timed_out_is_kept_not_lost(self):
        self.assertFalse(self.browser.download_started())
        late = FakeDownload(self.downloads, {"done": True})
        (self.downloads / "guid").write_bytes(b"part")
        self.main.emit("download", late)
        self.assertTrue(self.browser.download_started())
        path = self.browser.collect_download(self.target)
        self.assertEqual(path, self.target / "takeout-001.zip")
        self.assertEqual(path.read_bytes(), b"part")
        self.assertFalse(self.browser.download_started())

    def test_a_download_that_starts_in_a_popup_is_kept_too(self):
        popup = Tab(self.clock, self.downloads, "https://takeout.google.com/takeout/download", "")
        self.context.open(popup)
        (self.downloads / "guid").write_bytes(b"part")
        popup.emit("download", FakeDownload(self.downloads, {"done": True}))
        self.assertTrue(self.browser.download_started())
        self.assertEqual(self.browser.collect_download(self.target).name, "takeout-001.zip")

    def test_a_file_in_chromiums_folder_counts_as_a_download_under_way(self):
        (self.downloads / "guid.crdownload").write_bytes(b"half")
        self.assertTrue(self.browser.download_started())

    def test_a_file_without_playwrights_event_is_waited_on_then_swept_and_reported_interrupted(self):
        (self.downloads / "guid.crdownload").write_bytes(b"half")
        with self.assertRaisesRegex(DownloadInterrupted, "without Playwright's event"):
            self.browser.collect_download(self.target)
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
