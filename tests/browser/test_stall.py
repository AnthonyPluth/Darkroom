import tempfile
import unittest
from pathlib import Path

from darkroom_browser import live, stall
from darkroom_browser.context import DownloadInterrupted
from darkroom_browser.stall import FolderState, StallWatch, Verdict


IN_PROGRESS = FolderState(10, 1, True)


def quiet_for(seconds, state=IN_PROGRESS):
    watch = StallWatch(0.0, stall_seconds=100.0, settle_seconds=4.0)
    watch.judge(0.0, state)
    return watch.judge(seconds, state)


class StallWatchTests(unittest.TestCase):
    def test_the_first_look_and_any_growth_wait(self):
        watch = StallWatch(0.0, stall_seconds=100.0)
        self.assertIs(watch.judge(1.0, FolderState(0, 0, False)), Verdict.WAIT)
        self.assertIs(watch.judge(50.0, FolderState(5, 1, True)), Verdict.WAIT)
        self.assertIs(watch.judge(149.0, FolderState(6, 1, True)), Verdict.WAIT)
        self.assertIs(watch.judge(248.0, FolderState(6, 1, True)), Verdict.WAIT)

    def test_a_partial_download_that_stops_growing_is_stalled_only_after_the_limit(self):
        self.assertIs(quiet_for(99.9), Verdict.WAIT)
        self.assertIs(quiet_for(100.0), Verdict.STALLED)
        self.assertIs(quiet_for(5000.0), Verdict.STALLED)

    def test_an_empty_folder_that_never_fills_is_stalled_from_the_start(self):
        watch = StallWatch(10.0, stall_seconds=100.0)
        self.assertIs(watch.judge(10.0, FolderState(0, 0, False)), Verdict.WAIT)
        self.assertIs(watch.judge(109.0, FolderState(0, 0, False)), Verdict.WAIT)
        self.assertIs(watch.judge(110.0, FolderState(0, 0, False)), Verdict.STALLED)

    def test_a_finished_file_is_recognised_after_a_short_quiet_spell_not_the_stall_limit(self):
        done = FolderState(2_000_000, 1, False)
        self.assertIs(quiet_for(3.9, done), Verdict.WAIT)
        self.assertIs(quiet_for(4.0, done), Verdict.FINISHED)

    def test_a_file_that_is_still_being_renamed_into_place_is_not_finished(self):
        self.assertIs(quiet_for(50.0, FolderState(10, 2, True)), Verdict.WAIT)

    def test_the_stall_clock_restarts_when_the_folder_changes(self):
        watch = StallWatch(0.0, stall_seconds=100.0)
        watch.judge(0.0, FolderState(1, 1, True))
        watch.judge(90.0, FolderState(2, 1, True))
        self.assertIs(watch.judge(189.0, FolderState(2, 1, True)), Verdict.WAIT)
        self.assertIs(watch.judge(190.0, FolderState(2, 1, True)), Verdict.STALLED)

    def test_the_default_limit_is_five_minutes(self):
        self.assertEqual(stall.STALL_SECONDS, 300.0)


class FolderStateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_an_empty_folder(self):
        self.assertEqual(stall.folder_state(self.folder), FolderState(0, 0, False))

    def test_it_sums_regular_files_and_sees_chromiums_unfinished_marker(self):
        (self.folder / "guid.crdownload").write_bytes(b"x" * 30)
        (self.folder / "other").write_bytes(b"y" * 12)
        self.assertEqual(stall.folder_state(self.folder), FolderState(42, 2, True))

    def test_a_finished_file_has_no_marker(self):
        (self.folder / "guid").write_bytes(b"x" * 30)
        self.assertEqual(stall.folder_state(self.folder), FolderState(30, 1, False))

    def test_links_and_folders_are_not_counted(self):
        outside = self.folder / "outside"
        outside.write_bytes(b"z" * 100)
        inside = self.folder / "inside"
        inside.mkdir()
        (inside / "a-link").symlink_to(outside)
        (inside / "a-folder").mkdir()
        self.assertEqual(stall.folder_state(inside), FolderState(0, 0, False))


class FakeDownload:
    suggested_filename = "../takeout-001.zip"

    def __init__(self, folder, script):
        self.folder = folder
        self.script = script
        self.cancelled = False
        self.failures = 0

    def cancel(self):
        self.cancelled = True
        if self.script.get("cancel_raises"):
            raise RuntimeError("already finished")

    def failure(self):
        self.failures += 1
        if self.script.get("blocks") and not self.cancelled and not self.script.get("done"):
            raise AssertionError("failure() would block forever on a stalled download")
        if self.cancelled and not self.script.get("done"):
            return "canceled"
        return self.script.get("error")

    def path(self):
        return self.folder / "guid"


class Started:
    def __init__(self, download):
        self.value = download

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePage:
    def __init__(self, clock, folder, steps, script):
        self.clock = clock
        self.folder = folder
        self.steps = steps
        self.script = script
        self.polls = 0
        self.clicked = []
        self.download = FakeDownload(folder, script)

    def expect_download(self, timeout):
        return Started(self.download)

    def click(self, selector, timeout):
        self.clicked.append(selector)

    def wait_for_timeout(self, ms):
        self.polls += 1
        self.clock.now += ms / 1000
        if self.steps:
            self.steps.pop(0)()


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class LiveDownloadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.downloads = Path(folder.name) / "downloads"
        self.downloads.mkdir()
        self.target = Path(folder.name) / "partial"
        self.clock = Clock()

    def browser(self, steps, **script):
        page = FakePage(self.clock, self.downloads, steps, script)
        return live.Live(page, downloads=self.downloads, clock=self.clock), page

    def write(self, name, size):
        (self.downloads / name).write_bytes(b"x" * size)

    def finish(self):
        (self.downloads / "guid.crdownload").rename(self.downloads / "guid")

    def test_a_download_that_grows_then_finishes_is_moved_without_being_cancelled(self):
        steps = [lambda i=i: self.write("guid.crdownload", 10 + i) for i in range(200)]
        steps.append(self.finish)
        browser, page = self.browser(steps, done=True)
        (self.downloads / "guid.crdownload").write_bytes(b"whole")
        path = browser.download_to("a", self.target)
        self.assertEqual(path, self.target / "takeout-001.zip")
        self.assertFalse(page.download.cancelled)
        self.assertEqual(page.download.failures, 1)

    def test_a_stalled_download_is_cancelled_on_the_job_thread_and_reported_interrupted(self):
        steps = [lambda: self.write("guid.crdownload", 1000)]
        browser, page = self.browser(steps, blocks=True)
        with self.assertRaises(DownloadInterrupted) as raised:
            browser.download_to("a", self.target)
        self.assertIn("300 seconds", str(raised.exception))
        self.assertTrue(page.download.cancelled)
        self.assertGreaterEqual(self.clock.now - 1000.0, stall.STALL_SECONDS)
        self.assertLess(self.clock.now - 1000.0, stall.STALL_SECONDS + 10)
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_download_that_never_writes_a_byte_is_cancelled_too(self):
        browser, page = self.browser([], blocks=True)
        with self.assertRaises(DownloadInterrupted):
            browser.download_to("a", self.target)
        self.assertTrue(page.download.cancelled)

    def test_a_download_that_finished_just_as_it_stalled_is_kept(self):
        class Finished(FakeDownload):
            def cancel(self):
                (self.folder / "guid.crdownload").rename(self.folder / "guid")
                super().cancel()

            def failure(self):
                return None

        browser, page = self.browser([])
        page.download = Finished(self.downloads, {"cancel_raises": True})
        self.write("guid.crdownload", 5)
        page.steps = [lambda: None]
        path = browser.download_to("a", self.target)
        self.assertEqual(path.name, "takeout-001.zip")
        self.assertTrue(page.download.cancelled)

    def test_a_failure_chromium_reports_is_interrupted_with_its_text(self):
        self.write("guid", 5)
        browser, _page = self.browser([], error="net::ERR_CONNECTION_RESET" + "!" * 400, done=True)
        with self.assertRaises(DownloadInterrupted) as raised:
            browser.download_to("a", self.target)
        self.assertEqual(len(str(raised.exception)), 200)
        self.assertTrue(str(raised.exception).startswith("net::ERR_CONNECTION_RESET"))

    def test_progress_is_reported_every_thirty_seconds_from_the_job_thread(self):
        reports = []
        steps = [lambda i=i: self.write("guid.crdownload", 100 + i) for i in range(40)]
        steps.append(self.finish)
        browser, _page = self.browser(steps, done=True)
        browser.download_to("a", self.target, reports.append)
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports, sorted(reports))
        self.assertGreater(reports[0], 100)

    def test_a_progress_callback_that_raises_does_not_stop_the_download(self):
        def broken(size):
            raise RuntimeError("darkroom is down")

        steps = [lambda i=i: self.write("guid.crdownload", 100 + i) for i in range(40)]
        steps.append(self.finish)
        browser, _page = self.browser(steps, done=True)
        self.assertEqual(browser.download_to("a", self.target, broken).name, "takeout-001.zip")

    def test_without_a_downloads_folder_it_waits_on_chromium_as_before(self):
        page = FakePage(self.clock, self.downloads, [], {"error": "boom"})
        browser = live.Live(page)
        with self.assertRaises(DownloadInterrupted):
            browser.download_to("a", self.target)
        self.assertEqual(page.polls, 0)
        page = FakePage(self.clock, self.downloads, [], {})
        self.write("guid", 3)
        self.assertEqual(live.Live(page).download_to("a", self.target).name, "takeout-001.zip")

    def test_no_thread_is_started_to_watch_the_folder(self):
        import threading

        before = threading.active_count()
        steps = [self.finish]
        self.write("guid.crdownload", 1)
        browser, _page = self.browser(steps, done=True)
        browser.download_to("a", self.target)
        self.assertEqual(threading.active_count(), before)


if __name__ == "__main__":
    unittest.main()
