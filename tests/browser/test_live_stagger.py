import tempfile
import unittest
from pathlib import Path

from darkroom_browser import live
from darkroom_browser.context import DownloadInterrupted, PartClick
from darkroom_browser.stall import FolderState
from tests.browser.test_late_download import PART_1, PART_2, REDIRECTED_1, REDIRECTED_2, Context, Tab
from tests.browser.test_stall import Clock

CLICK_1 = PartClick('a[href="1"]', PART_1, 1)
CLICK_2 = PartClick('a[href="2"]', PART_2, 2)


class Download:
    def __init__(self, folder, guid, url, name, page):
        self.folder = folder
        self.guid = guid
        self.url = url
        self.suggested_filename = name
        self.page = page
        self.cancelled = False
        self.error = None
        self.saved_as = guid

    def cancel(self):
        self.cancelled = True
        (self.folder / f"{self.guid}.crdownload").unlink(missing_ok=True)

    def failure(self):
        return "canceled" if self.cancelled else self.error

    def path(self):
        return self.folder / self.saved_as


class Expecting:
    def __init__(self, tab):
        self.tab = tab

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def value(self):
        return self.tab.started[-1]


class StaggerTab(Tab):
    def __init__(self, clock, folder):
        super().__init__(clock, folder, "https://takeout.google.com/manage/archive/x", "<h1>Takeout</h1>")
        self.started = []
        self.files_per_click = 1
        self.file_after_polls = 0
        self.pending_files = []

    def expect_download(self, timeout):
        return Expecting(self)

    def click(self, selector, timeout):
        self.clicked.append(selector)
        number = int(selector.split('"')[1])
        guid = f"guid-{len(self.started) + 1}"
        download = Download(self.folder, guid, REDIRECTED_1 if number == 1 else REDIRECTED_2, f"takeout-20261102T031000Z-00{number}.zip", self)
        self.started.append(download)
        self.emit("download", download)
        names = [guid] + [f"stray-{len(self.started)}-{n}" for n in range(1, self.files_per_click)]
        self.pending_files = [(self.file_after_polls, name) for name in names]
        self.drop_files()

    def drop_files(self):
        due = [name for polls, name in self.pending_files if polls <= 0]
        self.pending_files = [(polls - 1, name) for polls, name in self.pending_files if polls > 0]
        for name in due:
            (self.folder / f"{name}.crdownload").write_bytes(b"x" * 10)

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        self.drop_files()


class LiveStaggerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.downloads = Path(folder.name) / "downloads"
        self.downloads.mkdir()
        self.target = Path(folder.name) / "partial"
        self.clock = Clock()
        self.tab = StaggerTab(self.clock, self.downloads)
        self.browser = live.Live(self.tab, Context([self.tab]), None, self.downloads, None, self.clock)
        self.browser.watch_downloads()

    def finish(self, handle, size=50):
        (self.downloads / f"{handle}.crdownload").unlink()
        (self.downloads / handle).write_bytes(b"z" * size)

    def names(self):
        return sorted(path.name for path in self.downloads.iterdir())

    def test_each_download_is_matched_to_its_own_file_and_judged_on_it(self):
        first = self.browser.start_download(CLICK_1.selector, CLICK_1)
        (self.downloads / f"{first}.crdownload").write_bytes(b"x" * 30)
        second = self.browser.start_download(CLICK_2.selector, CLICK_2)
        self.assertEqual((first, second), ("guid-1", "guid-2"))
        self.assertEqual((self.browser.download_state(first), self.browser.download_state(second)), (FolderState(30, 1, True), FolderState(10, 1, True)))
        self.finish(second, 70)
        self.assertEqual(self.browser.download_state(second), FolderState(70, 1, False))
        path = self.browser.keep_download(second, self.target)
        self.assertEqual((path.name, path.read_bytes()), ("takeout-20261102T031000Z-002.zip", b"z" * 70))
        self.assertEqual(self.names(), ["guid-1.crdownload"])
        self.assertEqual(self.browser.download_state(second), FolderState(0, 0, False))
        self.assertEqual(self.browser.drop_strays(), 0)
        self.assertEqual(self.names(), ["guid-1.crdownload"])

    def test_a_file_that_appears_a_little_after_the_event_is_waited_for(self):
        self.tab.file_after_polls = 3
        self.assertEqual(self.browser.start_download(CLICK_1.selector, CLICK_1), "guid-1")

    def test_no_file_or_two_new_files_cancel_the_download(self):
        self.tab.file_after_polls = 1000
        with self.assertRaisesRegex(DownloadInterrupted, "could not tell which file"):
            self.browser.start_download(CLICK_1.selector, CLICK_1)
        self.assertTrue(self.tab.started[-1].cancelled)
        self.tab.file_after_polls, self.tab.files_per_click = 0, 2
        with self.assertRaisesRegex(DownloadInterrupted, "could not tell which file"):
            self.browser.start_download(CLICK_2.selector, CLICK_2)
        self.assertTrue(self.tab.started[-1].cancelled)
        self.assertEqual(self.browser.flights, {})

    def test_a_failed_or_misplaced_download_removes_only_its_own_files(self):
        first = self.browser.start_download(CLICK_1.selector, CLICK_1)
        second = self.browser.start_download(CLICK_2.selector, CLICK_2)
        self.tab.started[0].error = "network"
        self.finish(first)
        with self.assertRaisesRegex(DownloadInterrupted, "network"):
            self.browser.keep_download(first, self.target)
        self.assertEqual(self.names(), ["guid-2.crdownload"])
        self.tab.started[1].saved_as = "guid-1"
        self.finish(second)
        with self.assertRaisesRegex(DownloadInterrupted, "under another file"):
            self.browser.keep_download(second, self.target)
        self.assertEqual(self.names(), [])

    def test_cancelling_one_download_leaves_the_others_file(self):
        first = self.browser.start_download(CLICK_1.selector, CLICK_1)
        self.browser.start_download(CLICK_2.selector, CLICK_2)
        self.browser.cancel_download(first)
        self.assertTrue(self.tab.started[0].cancelled)
        self.assertFalse(self.tab.started[1].cancelled)
        self.assertEqual(self.names(), ["guid-2.crdownload"])
        self.browser.cancel_download(first)

    def test_strays_are_cancelled_and_files_no_download_owns_are_removed(self):
        own = self.browser.start_download(CLICK_1.selector, CLICK_1)
        stray = Download(self.downloads, "guid-x", REDIRECTED_2, "takeout-20261102T031000Z-002.zip", self.tab)
        self.tab.emit("download", stray)
        (self.downloads / "guid-x.crdownload").write_bytes(b"s")
        (self.downloads / "left-over").write_bytes(b"s")
        self.assertEqual(self.browser.drop_strays(), 2)
        self.assertTrue(stray.cancelled)
        self.assertEqual(self.names(), [f"{own}.crdownload"])

    def test_a_late_download_is_kept_only_for_the_part_last_clicked(self):
        self.browser.clicked = CLICK_1.selector
        self.assertIsNone(self.browser.late_download(CLICK_1))
        other = Download(self.downloads, "guid-late-2", REDIRECTED_2, "takeout-20261102T031000Z-002.zip", self.tab)
        self.tab.emit("download", other)
        self.assertIsNone(self.browser.late_download(CLICK_1))
        late = Download(self.downloads, "guid-late-1", REDIRECTED_1, "takeout-20261102T031000Z-001.zip", self.tab)
        self.tab.emit("download", late)
        (self.downloads / "guid-late-1.crdownload").write_bytes(b"l")
        self.assertEqual(self.browser.late_download(CLICK_1), "guid-late-1")
        self.assertEqual(self.browser.arrived, [other])
        self.assertIsNone(self.browser.late_download(CLICK_2))

    def test_staggering_needs_chromiums_download_folder(self):
        browser = live.Live(self.tab)
        with self.assertRaisesRegex(DownloadInterrupted, "download folder"):
            browser.start_download(CLICK_1.selector, CLICK_1)
        self.assertEqual(browser.download_state("guid-1"), FolderState(0, 0, False))
        browser.remove_files("guid-1")


if __name__ == "__main__":
    unittest.main()
