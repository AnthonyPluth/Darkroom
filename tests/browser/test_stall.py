import unittest

from darkroom_browser import stall
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


if __name__ == "__main__":
    unittest.main()
