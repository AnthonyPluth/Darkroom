import re
import threading
import unittest
import zipfile
from unittest import mock

from sqlalchemy import select

from darkroom.domain import events, exports
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, GoogleAccount
from darkroom_browser import client
from darkroom_browser.context import Cancelled, DownloadInterrupted, JobContext, Stagger
from darkroom_browser.jobs import REGISTRY, download, stagger
from darkroom_browser.stall import FolderState
from tests.browser.takeout_pages import COMPLETED_ID
from tests.browser.test_download import AFTER_THE_CLICK, NOW, SIZES, DownloadBrowser, DownloadWorld

MINUTE = 60.0
PART_SECONDS = 25 * MINUTE
QUICK = ("corrupt", "bad name", "fails")


class StaggerBrowser(DownloadBrowser):
    def __init__(self, script, clock, seconds=PART_SECONDS, late=None):
        super().__init__(script)
        self.clock = clock
        self.seconds = seconds
        self.flying = {}
        self.started = []
        self.cancelled = []
        self.kept = []
        self.strays = 0
        self.late = late or {}
        self.handles = 0

    def download_to(self, selector, folder, progress=None):
        raise AssertionError("a staggered job starts downloads with start_download")

    def download_started(self):
        raise AssertionError("a staggered job asks for its own part's late download")

    def flight(self, number, outcome):
        self.handles += 1
        handle = f"guid-{self.handles}"
        self.flying[handle] = {"number": number, "from": self.clock.now, "outcome": outcome}
        self.started.append((number, self.clock.now))
        return handle

    def start_download(self, selector, part):
        self.selectors.append(selector)
        number = int(re.search(r"i=(\d+)", selector).group(1)) + 1
        self.downloaded.append(number)
        outcome = self.script.pop(0)
        if outcome == "verify":
            self.verifying = self.verify_reads
            raise TimeoutError("no download started")
        if outcome in AFTER_THE_CLICK:
            self.after_click = list(AFTER_THE_CLICK[outcome])
            raise TimeoutError("no download started")
        if outcome == "interrupted":
            raise DownloadInterrupted("network")
        if outcome == "lease lost":
            raise Cancelled("lost")
        return self.flight(number, outcome)

    def late_download(self, part):
        due = self.late.get(part.number)
        if due is None or self.clock.now < due:
            return None
        del self.late[part.number]
        self.collected.append(part.number)
        return self.flight(part.number, "ok")

    def download_state(self, handle):
        flight = self.flying[handle]
        elapsed = self.clock.now - flight["from"]
        if flight["outcome"] == "stall":
            elapsed = min(elapsed, 15 * MINUTE)
        seconds = MINUTE if flight["outcome"] in QUICK else self.seconds
        if elapsed >= seconds:
            return FolderState(40 * 1024, 1, False)
        return FolderState(int(40 * 1024 * elapsed / seconds) + 1, 1, True)

    def keep_download(self, handle, folder):
        flight = self.flying.pop(handle)
        number, outcome = flight["number"], flight["outcome"]
        self.kept.append(number)
        if outcome == "fails":
            raise DownloadInterrupted("network")
        name = "Takeout.zip?" if outcome == "bad name" else f"takeout-20261102T031000Z-00{number}.zip"
        path = folder / name
        if outcome == "corrupt":
            path.write_bytes(b"PK\x03\x04 not really a zip" * 2000)
            return path
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(f"Takeout/Google Photos/{number}.bin", bytes(40 * 1024))
        return path

    def cancel_download(self, handle):
        self.cancelled.append(self.flying.pop(handle)["number"])

    def drop_strays(self):
        self.strays += 1
        return 0


class StaggerWorld(DownloadWorld):
    interval = 10 * MINUTE
    most = 2

    def run_job(self, browser, job_id=None, stagger_with=None):
        job_id = job_id or self.claim()
        chosen = stagger_with if stagger_with is not None else Stagger(self.interval, self.most)
        ctx = JobContext(job_id, "download", str(self.account), {"email": "jo@example.test", "export_id": self.export}, self.root, self.reporter,
                         lambda profile: browser, threading.Event(), None, self.clock.sleep, self.clock.monotonic, lambda: NOW, stagger=chosen)
        return job_id, REGISTRY["download"](ctx)

    def started_after(self, browser):
        first = browser.started[0][1]
        return [(number, round((at - first) / MINUTE)) for number, at in browser.started]

    def messages(self):
        return [message for kind, message in self.reporter.events if kind == "progress" and isinstance(message, str)]

    def recorded(self, kind):
        with db.session(self.engine) as opened:
            return [entry.detail for entry in reversed(events.recent(opened, type=kind))]


class StaggeredDownloadTests(StaggerWorld):
    def test_parts_start_the_interval_apart_while_earlier_ones_download_up_to_the_most(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        job_id, result = self.run_job(browser)
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 10), (3, 25)])
        self.assertEqual(browser.kept, [1, 2, 3])
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        self.assertEqual(sorted(path.name for path in self.folder().iterdir()), [f"takeout-20261102T031000Z-00{n}.zip" for n in (1, 2, 3)])
        self.assertEqual(result["telemetry"]["most_downloading"], 2)
        self.assertEqual([body["downloading"] for _, body in self.reporter.telemetry], [0, 1, 1])
        self.assertIn("downloading part 2 of 3 with 1 other part downloading; 10 minutes after the previous click, no prompt answered yet in this job", self.messages())
        self.assertTrue(self.messages()[-1].startswith("downloaded 3 of 3 parts; 3 part clicks and 0 verification prompts in "))
        self.assertTrue(self.messages()[-1].endswith("; at most 2 parts downloading at once; staggered every 10 minutes at the end"))
        self.assertGreater(browser.strays, 0)
        self.finish(job_id)
        self.assertEqual(self.status(), "downloaded")

    def test_three_at_once_when_allowed(self):
        self.most = 3
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock, seconds=60 * MINUTE)
        self.run_job(browser)
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 10), (3, 20)])

    def test_each_part_reports_its_own_progress(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        self.run_job(browser)
        progressed = [number for kind, number in self.reporter.events if kind == "progress" and isinstance(number, int)]
        self.assertEqual(set(progressed), {1, 2, 3})

    def test_a_stalled_part_is_judged_on_its_own_and_only_its_download_is_cancelled(self):
        browser = StaggerBrowser(["stall", "ok", "ok", "ok"], self.clock)
        self.run_job(browser)
        self.assertEqual(browser.cancelled, [1])
        self.assertEqual(browser.kept, [2, 1, 3])
        self.assertEqual(self.parts(), [("done", 2, 2), ("done", 1, 1), ("done", 1, 1)])
        self.assertEqual(self.recorded("export_part_attempt_failed"), ["part 1: interrupted"])
        self.assertEqual(self.started_after(browser)[:3], [(1, 0), (2, 10), (1, 20)])

    def test_a_prompt_pauses_new_clicks_and_leaves_the_part_in_flight_running_and_counted_once(self):
        browser = StaggerBrowser(["ok", "verify", "ok", "ok"], self.clock)
        browser.verify_reads = 200
        job_id, _ = self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2, 2, 3])
        self.assertEqual(browser.kept[0], 1)
        events_seen = self.reporter.events
        waited = events_seen.index(("waiting", "verify"))
        resumed = next(i for i, event in enumerate(events_seen) if event[0] == "progress" and str(event[1]).startswith("verified; resuming part 2"))
        between = events_seen[waited + 1:resumed]
        self.assertIn(("done", 1), between)
        self.assertFalse([event for event in between if event[0] == "progress" and isinstance(event[1], str)])
        self.assertFalse([event for event in between if event[0] == "attempt"])
        self.assertEqual(self.parts(), [("done", 1, 1), ("done", 2, 1), ("done", 1, 1)])
        self.assertEqual([body["downloading"] for number, body in self.reporter.telemetry if body["event"] == "prompted"], [1])
        with db.session(self.engine) as opened:
            sign_in = opened.execute(select(Alert).where(Alert.kind == "sign_in_needed")).scalars().all()
            self.assertEqual(len(sign_in), 1)
            self.assertIsNotNone(sign_in[0].closed)
            self.assertEqual(opened.get(BrowserJob, job_id).status, "running")

    def test_a_prompt_halves_the_interval_and_a_second_returns_to_one_part_at_a_time(self):
        self.interval = 20 * MINUTE
        browser = StaggerBrowser(["ok", "verify", "ok", "verify", "ok", "ok"], self.clock, seconds=60 * MINUTE)
        self.run_job(browser)
        self.assertIn("Google asked to verify twice while staggering, so the rest of this job downloads one part at a time", self.messages())
        self.assertTrue(self.messages()[-1].endswith("back to one part at a time after Google asked to verify twice"))
        starts = [at for _, at in browser.started]
        self.assertEqual(len(starts), 3)
        self.assertGreaterEqual(starts[2] - starts[1], 60 * MINUTE)

    def test_the_back_off_rule(self):
        cases = {20 * MINUTE: [10, 5, 2.5, 2, 2], 3 * MINUTE: [2, 2], MINUTE: [1, 1]}
        for start, expected in cases.items():
            run = stagger.Staggered.__new__(stagger.Staggered)
            run.interval = start
            seen = []
            for _ in expected:
                run.interval = stagger.backed_off(run.interval)
                seen.append(run.interval / MINUTE)
            self.assertEqual(seen, expected, start)

    def test_a_new_part_waits_for_disk_room_and_says_so_once_a_wait(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        with mock.patch.object(stagger, "disk_free", lambda folder: stagger.DISK_SLACK + 40 * 1024):
            self.run_job(browser)
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 25), (3, 50)])
        self.assertEqual(sum(1 for message in self.messages() if "the disk has too little room" in message), 2)

    def test_room_counts_what_the_parts_in_flight_still_need(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        with mock.patch.object(stagger, "disk_free", lambda folder: stagger.DISK_SLACK + 60 * 1024):
            self.run_job(browser)
        number, minutes = self.started_after(browser)[1]
        self.assertEqual(number, 2)
        self.assertIn(minutes, (12, 13))

    def test_a_restart_requeues_every_part_that_was_in_flight(self):
        browser = StaggerBrowser(["ok", "lease lost"], self.clock)
        job_id = self.claim()
        with self.assertRaises(Cancelled):
            self.run_job(browser, job_id)
        self.assertEqual(self.parts(), [("downloading", 1, 1), ("downloading", 1, 1), ("pending", 0, 0)])
        self.fail(job_id)
        self.assertEqual(self.parts(), [("pending", 1, 1), ("pending", 1, 1), ("pending", 0, 0)])
        with db.session(self.engine) as opened:
            self.assertEqual(exports.reconcile(opened, opened.get(GoogleAccount, self.account), [exports.Listed(COMPLETED_ID, NOW - 600, "ready", NOW + 7 * 86400, tuple(exports.ListedPart(n, size) for n, size in enumerate(SIZES, start=1)))], NOW).requeued, 1)
        second = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        _, result = self.run_job(second)
        self.assertEqual((result["downloaded"], second.downloaded), (3, [1, 2, 3]))
        self.assertEqual(self.parts(), [("done", 2, 2), ("done", 2, 2), ("done", 1, 1)])

    def test_a_part_that_gives_up_returns_the_part_in_flight_to_pending(self):
        browser = StaggerBrowser(["ok", "corrupt", "corrupt", "corrupt"], self.clock, seconds=60 * MINUTE)
        with self.assertRaisesRegex(download.DownloadFailed, "^part 2 failed after 3 attempts"):
            self.run_job(browser)
        self.assertEqual(self.status(), "failed")
        self.assertEqual(self.parts(), [("pending", 1, 1), ("failed", 3, 3), ("pending", 0, 0)])

    def test_a_late_download_after_a_click_that_started_nothing_is_kept(self):
        browser = StaggerBrowser(["ok", "unrecognised then it arrives", "ok"], self.clock)
        browser.late = {2: None}
        original = browser.start_download

        def arriving(selector, part):
            if part.number == 2 and 2 in browser.late and browser.late[2] is None:
                browser.late[2] = self.clock.now + 2 * MINUTE
            return original(selector, part)

        browser.start_download = arriving
        self.run_job(browser)
        self.assertEqual(browser.collected, [2])
        self.assertEqual(browser.downloaded, [1, 2, 3])
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)

    def test_a_click_that_never_starts_is_not_started_after_the_back_off(self):
        browser = StaggerBrowser(["ok", "unrecognised", "ok", "ok"], self.clock)
        self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2, 2, 3])
        self.assertEqual(self.recorded("export_part_attempt_failed"), ["part 2: not_started"])
        self.assertEqual(self.parts()[1], ("done", 2, 2))

    def test_a_click_that_shows_a_prompt_only_later_is_a_prompt(self):
        browser = StaggerBrowser(["ok", "unrecognised then reauth", "ok", "ok"], self.clock)
        self.run_job(browser)
        self.assertEqual([event for event in self.reporter.events if event[0] == "waiting"], [("waiting", "verify")])
        self.assertEqual(self.parts()[1], ("done", 2, 1))

    def test_a_broken_finished_file_is_retried(self):
        for outcome, reason in (("bad name", "bad_name"), ("corrupt", "corrupt"), ("fails", "interrupted")):
            with self.subTest(outcome):
                self.setUp()
                browser = StaggerBrowser([outcome, "ok", "ok", "ok"], self.clock)
                self.run_job(browser)
                self.assertEqual(self.recorded("export_part_attempt_failed"), [f"part 1: {reason}"])
                self.assertEqual(self.parts()[0], ("done", 2, 2))

    def test_an_interrupted_click_at_a_prompt_waits_and_retries_without_asked_to_verify(self):
        browser = StaggerBrowser(["ok", "interrupted", "ok", "ok"], self.clock)
        browser.after_click = []
        original = browser.start_download

        def prompting(selector, part):
            try:
                return original(selector, part)
            except DownloadInterrupted:
                browser.verifying = 3
                raise

        browser.start_download = prompting
        self.run_job(browser)
        self.assertEqual(self.recorded("export_part_attempt_failed"), ["part 2: interrupted"])
        self.assertEqual([event for event in self.reporter.events if event[0] == "waiting"], [("waiting", "verify")])
        self.assertEqual(self.parts()[1], ("done", 2, 2))

    def darkroom_answering(self, status, raw):
        darkroom = client.Client(self.root / "no-socket-here" / "worker-api.sock", "t" * 40)
        darkroom.exchange = lambda path, data: (status, raw)
        return darkroom

    def lose_the_lease_on(self, event):
        darkroom = self.darkroom_answering(409, b'{"error": "this job is no longer running"}')
        real = self.reporter.part

        def part(job_id, number, body):
            if body["event"] == event and number == 2:
                return darkroom.part(job_id, number, body)
            return real(job_id, number, body)

        self.reporter.part = part

    def test_a_lost_lease_on_the_click_report_starts_no_download(self):
        self.lose_the_lease_on("clicked")
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        with self.assertRaises(client.LeaseLost):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [1])

    def test_a_lost_lease_on_a_progress_report_stops_the_job(self):
        self.lose_the_lease_on("progress")
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        with self.assertRaises(client.LeaseLost):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2])

    def test_a_refused_progress_report_is_ignored(self):
        darkroom = self.darkroom_answering(400, b'{"error": "part 1 is not being downloaded"}')
        real = self.reporter.part

        def part(job_id, number, body):
            if body["event"] == "progress":
                return darkroom.part(job_id, number, body)
            return real(job_id, number, body)

        self.reporter.part = part
        _, result = self.run_job(StaggerBrowser(["ok", "ok", "ok"], self.clock))
        self.assertEqual(result["downloaded"], 3)

    def test_without_the_setting_the_job_is_one_part_at_a_time(self):
        job_id = self.claim()
        browser = DownloadBrowser(["ok", "ok", "ok"])
        ctx = JobContext(job_id, "download", str(self.account), {"email": "jo@example.test", "export_id": self.export}, self.root, self.reporter,
                         lambda profile: browser, threading.Event(), None, self.clock.sleep, self.clock.monotonic, lambda: NOW)
        result = REGISTRY["download"](ctx)
        self.assertEqual((result["downloaded"], browser.downloaded), (3, [1, 2, 3]))
        self.assertNotIn("stagger", " ".join(self.messages()))


if __name__ == "__main__":
    unittest.main()
