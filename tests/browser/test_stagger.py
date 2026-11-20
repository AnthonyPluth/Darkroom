import itertools
import re
import threading
import unittest
import zipfile
from unittest import mock

from sqlalchemy import select

from darkroom.domain import downloads, events, exports
from darkroom.storage.paths import Photos
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Export, ExportPart, GoogleAccount
from darkroom_browser import client, selectors
from darkroom_browser.jobs import session, status
from darkroom_browser.context import Cancelled, DownloadInterrupted, JobContext
from darkroom_browser.jobs import REGISTRY, download, stagger
from darkroom_browser.stall import FolderState
from tests.browser.takeout_pages import COMPLETED_ID
from tests.shared import add_person
from tests.browser.test_classify import SIGN_IN_NEEDED
from tests.browser.test_download import AFTER_THE_CLICK, ARCHIVE, NOW, SIZES, DownloadBrowser, DownloadWorld

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
        self.most_flying = 0
        self.kept_at = {}

    def flight(self, number, outcome):
        self.awaiting = None
        self.handles += 1
        handle = f"guid-{self.handles}"
        self.flying[handle] = {"number": number, "from": self.clock.now, "outcome": outcome}
        self.started.append((number, self.clock.now))
        self.most_flying = max(self.most_flying, len(self.flying))
        return handle

    def start_download(self, selector, part):
        self.awaiting = part.number
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
        if flight["outcome"] == "empty":
            return FolderState(0, 0, False)
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
        self.kept_at[number] = self.clock.now
        if outcome == "fails":
            raise DownloadInterrupted("network")
        if outcome == "refused":
            raise download.DownloadsFolderRefused("Chromium's download folder is a link or not where it should be")
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
        last = getattr(self, "awaiting", None)
        arrived = [number for number, due in self.late.items() if due is not None and self.clock.now >= due and number != last]
        for number in arrived:
            del self.late[number]
            self.cancelled.append(number)
        return len(arrived)


class StaggerWorld(DownloadWorld):
    def setUp(self):
        super().setUp()
        self.cancelling = threading.Event()

    def context(self, browser, job_id):
        return JobContext(job_id, "download", str(self.account), {"email": "jo@example.test", "export_id": self.export}, self.root, self.reporter,
                          lambda profile: browser, self.cancelling, None, self.clock.sleep, self.clock.monotonic, lambda: NOW)

    def run_job(self, browser, job_id=None):
        job_id = job_id or self.claim()
        return job_id, REGISTRY["download"](self.context(browser, job_id))

    def started_after(self, browser):
        first = browser.started[0][1]
        return [(number, round((at - first) / MINUTE)) for number, at in browser.started]

    def messages(self):
        return [message for kind, message in self.reporter.events if kind == "progress" and isinstance(message, str)]

    def recorded(self, kind):
        with db.session(self.engine) as opened:
            return [entry.detail for entry in reversed(events.recent(opened, type=kind))]


class StaggeredDownloadTests(StaggerWorld):
    def test_one_part_downloads_at_a_time_and_the_next_starts_only_when_the_one_before_is_kept(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        job_id, result = self.run_job(browser)
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 25), (3, 50)])
        self.assertEqual(browser.most_flying, 1)
        for (number, at), kept_before in zip(browser.started[1:], browser.kept[:2], strict=True):
            self.assertGreaterEqual(at, browser.kept_at[kept_before], f"part {number} was clicked before part {kept_before} was kept")
        self.assertEqual(browser.kept, [1, 2, 3])
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        self.assertEqual(sorted(path.name for path in self.folder().iterdir()), [f"takeout-20261102T031000Z-00{n}.zip" for n in (1, 2, 3)])
        self.assertEqual(result["telemetry"]["most_downloading"], 1)
        self.assertEqual([body["downloading"] for _, body in self.reporter.telemetry], [0, 0, 0])
        self.assertIn("downloading part 2 of 3; 25 minutes after the previous click, no prompt answered yet in this job", self.messages())
        self.assertTrue(self.messages()[-1].startswith("downloaded 3 of 3 parts; 3 part clicks and 0 verification prompts in "))
        self.assertTrue(self.messages()[-1].endswith("; at most 1 part downloading at once"))
        self.assertGreater(browser.strays, 0)
        self.finish(job_id)
        self.assertEqual(self.status(), "downloaded")

    def test_no_setting_of_the_environment_or_the_module_lets_two_parts_of_an_export_download_together(self):
        for name in ("INTERVAL_SECONDS", "MOST_PARTS", "backed_off", "PROMPTS_BEFORE_ONE_AT_A_TIME"):
            self.assertFalse(hasattr(stagger, name), name)
        job_id = self.claim()
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock, seconds=60 * MINUTE)
        old = {"TAKEOUT_STAGGER": "1", "TAKEOUT_STAGGER_MINUTES": "1", "TAKEOUT_STAGGER_MAX_PARTS": "4", "BROWSER_WORKERS": "8"}
        with mock.patch.dict("os.environ", old):
            result = REGISTRY["download"](self.context(browser, job_id))
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 60), (3, 120)])
        self.assertEqual((browser.most_flying, result["telemetry"]["most_downloading"]), (1, 1))

    def test_each_part_reports_its_own_progress(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        self.run_job(browser)
        progressed = [number for kind, number in self.reporter.events if kind == "progress" and isinstance(number, int)]
        self.assertEqual(set(progressed), {1, 2, 3})

    def test_a_stalled_part_is_cancelled_and_tried_again_before_the_next_part_starts(self):
        browser = StaggerBrowser(["stall", "ok", "ok", "ok"], self.clock)
        self.run_job(browser)
        self.assertEqual(browser.cancelled, [1])
        self.assertEqual(browser.kept, [1, 2, 3])
        self.assertEqual([number for number, _ in browser.started], [1, 1, 2, 3])
        self.assertEqual(browser.most_flying, 1)
        self.assertEqual(self.parts(), [("done", 2, 2), ("done", 1, 1), ("done", 1, 1)])
        self.assertEqual(self.recorded("export_part_attempt_failed"), ["part 1: interrupted"])

    def test_a_download_that_never_writes_a_byte_is_cancelled_after_the_stall_limit_and_retried(self):
        browser = StaggerBrowser(["empty", "ok", "ok", "ok"], self.clock)
        self.run_job(browser)
        self.assertEqual(browser.cancelled, [1])
        self.assertEqual(self.recorded("export_part_attempt_failed"), ["part 1: interrupted"])
        self.assertEqual(self.parts()[0], ("done", 2, 2))
        self.assertEqual(self.started_after(browser)[:2], [(1, 0), (1, 5)])

    def test_a_download_folder_refused_while_a_part_lands_stops_the_job_without_a_retry(self):
        browser = StaggerBrowser(["refused", "ok", "ok", "ok"], self.clock)
        with self.assertRaisesRegex(download.DownloadsFolderRefused, "is a link"):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [1])
        self.assertEqual(self.recorded("export_part_attempt_failed"), [])

    def test_a_prompt_on_a_click_waits_for_you_with_nothing_else_downloading_then_clicks_that_part_again_first(self):
        browser = StaggerBrowser(["ok", "verify", "ok", "ok"], self.clock)
        browser.verify_reads = 200
        job_id, _ = self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2, 2, 3])
        self.assertEqual(browser.most_flying, 1)
        events_seen = self.reporter.events
        waited = events_seen.index(("waiting", "verify"))
        self.assertEqual(events_seen[:waited].count(("done", 1)), 1)
        resumed = next(i for i, event in enumerate(events_seen) if event[0] == "progress" and str(event[1]).startswith("verified; resuming part 2"))
        between = events_seen[waited + 1:resumed]
        self.assertFalse([event for event in between if event[0] == "progress" and isinstance(event[1], str)])
        self.assertFalse([event for event in between if event[0] == "attempt"])
        self.assertEqual(self.parts(), [("done", 1, 1), ("done", 2, 1), ("done", 1, 1)])
        self.assertEqual([body["downloading"] for number, body in self.reporter.telemetry if body["event"] == "prompted"], [0])
        with db.session(self.engine) as opened:
            sign_in = opened.execute(select(Alert).where(Alert.kind == "sign_in_needed")).scalars().all()
            self.assertEqual(len(sign_in), 1)
            self.assertIsNotNone(sign_in[0].closed)
            self.assertEqual(opened.get(BrowserJob, job_id).status, "running")

    def short_of_space_until(self, minutes):
        start = self.clock.now
        return mock.patch.object(Photos, "free_bytes", lambda photos: 0 if self.clock.now < start + minutes * MINUTE else 400 * 10**9)

    def test_a_paused_answer_waits_and_asks_again_then_downloads_without_using_an_attempt(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        asked = []
        real = self.reporter.part

        def part(job_id, number, body):
            reply = real(job_id, number, body)
            if body["event"] == "space":
                asked.append((number, reply["paused_for_space"], reply["part"]["attempts"], reply["part"]["downloads_used"]))
            return reply

        self.reporter.part = part
        with self.short_of_space_until(22):
            _, result = self.run_job(browser)
        paused = [entry for entry in asked if entry[1]]
        self.assertEqual(len(paused), 5)
        self.assertEqual({entry[0] for entry in paused}, {1})
        self.assertEqual({entry[2:] for entry in paused}, {(0, 0)})
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        self.assertEqual(result["downloaded"], 3)
        self.assertNotIn("failed", [kind for kind, _ in self.reporter.events])
        self.assertEqual(self.recorded("export_part_attempt_failed"), [])
        self.assertEqual(sum(1 for message in self.messages() if message == stagger.PAUSED_WORDS), 1)
        self.assertEqual(sum(1 for message in self.messages() if message == stagger.RESUMED_WORDS), 1)

    def test_a_paused_attempt_after_a_space_check_with_room_still_pauses_without_using_an_attempt(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        asked = []
        real = self.reporter.part

        def part(job_id, number, body):
            reply = real(job_id, number, body)
            if body["event"] == "space":
                return dict(reply, allowed=True, paused_for_space=False)
            if body["event"] == "attempt":
                asked.append((number, reply["paused_for_space"], reply["part"]["attempts"], reply["part"]["downloads_used"]))
            return reply

        self.reporter.part = part
        with self.short_of_space_until(22):
            _, result = self.run_job(browser)
        paused = [entry for entry in asked if entry[1]]
        self.assertEqual(len(paused), 5, "the attempt's own answer still pauses when space ran out between the check and the attempt")
        self.assertEqual({entry[2:] for entry in paused}, {(0, 0)})
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(sum(1 for message in self.messages() if message == stagger.PAUSED_WORDS), 1)

    def test_the_wait_between_asks_is_fixed(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        starts = []
        real = self.reporter.part

        def part(job_id, number, body):
            if body["event"] == "space":
                starts.append(self.clock.now)
            return real(job_id, number, body)

        self.reporter.part = part
        with self.short_of_space_until(12):
            self.run_job(browser)
        gaps = {round(later - earlier) for earlier, later in itertools.pairwise(starts[:3])}
        self.assertTrue(all(stagger.SPACE_RETRY_SECONDS <= gap < stagger.SPACE_RETRY_SECONDS + stagger.POLL_SECONDS for gap in gaps))

    def test_only_new_parts_wait_a_part_already_downloading_finishes(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        start = self.clock.now
        low = mock.patch.object(Photos, "free_bytes", lambda photos: 400 * 10**9 if self.clock.now < start + 1 * MINUTE else 0)
        calls = []
        real_space = stagger.OnePartAtATime.may_start

        def may_start(run):
            calls.append(self.clock.now)
            if self.clock.now > start + 60 * MINUTE:
                self.cancelling.set()
            return real_space(run)

        with low, mock.patch.object(stagger.OnePartAtATime, "may_start", may_start), self.assertRaises(Cancelled):
            self.run_job(browser)
        self.assertEqual(browser.kept, [1])
        self.assertEqual(self.parts()[0], ("done", 1, 1))
        self.assertEqual(self.parts()[1:], [("pending", 0, 0), ("pending", 0, 0)])

    def test_the_pause_words_stay_on_the_last_message_while_the_next_part_waits_for_room(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        start = self.clock.now
        low = mock.patch.object(Photos, "free_bytes", lambda photos: 400 * 10**9 if self.clock.now < start + 1 * MINUTE else 0)
        real_space = stagger.OnePartAtATime.may_start

        def may_start(run):
            if self.clock.now > start + 60 * MINUTE:
                self.cancelling.set()
            return real_space(run)

        with low, mock.patch.object(stagger.OnePartAtATime, "may_start", may_start), self.assertRaises(Cancelled):
            self.run_job(browser)
        messages = self.messages()
        landed = [message for message in messages if message.startswith("part 1 downloaded")]
        self.assertEqual(landed, ["part 1 downloaded"])
        self.assertIn(stagger.PAUSED_WORDS, messages[-1])

    def test_a_landing_after_the_pause_ends_says_nothing_of_it(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        with self.short_of_space_until(12):
            self.run_job(browser)
        landed = [message for message in self.messages() if message.startswith("part ") and " downloaded" in message]
        self.assertTrue(landed)
        self.assertEqual([message for message in landed if "paused for space" in message], [])
        self.assertEqual(self.messages()[self.messages().index(stagger.RESUMED_WORDS):].count(stagger.PAUSED_WORDS), 0)

    def archive_loads(self, browser):
        loads = []
        real_goto = browser.goto

        def goto(url):
            if url == ARCHIVE:
                loads.append(self.clock.now)
            real_goto(url)

        browser.goto = goto
        return loads

    def space_answers(self):
        answers = []
        real = self.reporter.part

        def part(job_id, number, body):
            reply = real(job_id, number, body)
            if body["event"] == "space":
                answers.append((self.clock.now, reply["paused_for_space"]))
            return reply

        self.reporter.part = part
        return answers

    def test_a_pause_loads_no_takeout_page_until_the_server_says_there_is_room(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        loads, answers = self.archive_loads(browser), self.space_answers()
        start = self.clock.now
        with self.short_of_space_until(22):
            _, result = self.run_job(browser)
        paused = [at for at, held in answers if held]
        self.assertEqual(len(paused), 5)
        self.assertGreaterEqual(loads[0], start + 22 * MINUTE, "no Takeout page was loaded while the server answered paused")
        self.assertGreater(loads[0], paused[-1])
        self.assertEqual(len(loads), 3, "one Takeout page load per part, none for the pause")
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)

    def test_with_room_the_page_is_loaded_at_once_after_one_space_check(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        loads, answers = self.archive_loads(browser), self.space_answers()
        start = self.clock.now
        _, result = self.run_job(browser)
        self.assertEqual([held for _, held in answers], [False, False, False])
        self.assertLess(loads[0] - start, MINUTE)
        self.assertEqual(result["downloaded"], 3)
        self.assertNotIn(stagger.PAUSED_WORDS, " ".join(self.messages()))

    def test_an_unreadable_free_space_reading_pauses_nothing_and_loads_the_page_at_once(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        loads = self.archive_loads(browser)
        start = self.clock.now
        with mock.patch.object(Photos, "free_bytes", side_effect=OSError("unreadable")):
            _, result = self.run_job(browser)
        self.assertLess(loads[0] - start, MINUTE)
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        self.assertNotIn(stagger.PAUSED_WORDS, " ".join(self.messages()))

    def test_an_exhausted_part_still_gives_up_while_space_is_low_without_opening_takeout(self):
        with db.session(self.engine) as opened:
            exports.parts_of(opened, self.export)[0].attempts = 3
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        loads = self.archive_loads(browser)
        with mock.patch.object(Photos, "free_bytes", lambda photos: 0), self.assertRaisesRegex(download.DownloadFailed, "part 1"):
            self.run_job(browser)
        self.assertEqual(loads, [], "a part out of attempts gives up at the space check, before any Takeout page load")
        self.assertEqual(browser.started, [])
        self.assertEqual(self.parts()[0], ("failed", 0, 3))

    def test_a_long_pause_makes_no_page_loads_and_the_expiry_alert_still_opens(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        loads, answers = self.archive_loads(browser), self.space_answers()
        real_sleep = self.clock.sleep
        start = self.clock.now

        def sleep(seconds):
            if self.clock.now > start + 6 * 60 * MINUTE:
                self.cancelling.set()
            real_sleep(seconds)

        self.clock.sleep = sleep
        with self.short_of_space_until(10_000), self.assertRaises(Cancelled):
            self.run_job(browser)
        self.assertEqual(loads, [], "six hours paused, and Takeout was never opened")
        self.assertGreaterEqual(len(answers), 70)
        self.assertEqual(self.parts(), [("pending", 0, 0)] * 3)
        with db.session(self.engine) as opened:
            opened.get(Export, self.export).expires_at = NOW + 2 * 86400
            opened.flush()
            self.assertEqual(downloads.watch_expiry(opened, self.settings, NOW), 1)

    def another_accounts_part_downloading(self, size):
        with db.session(self.engine) as opened:
            person = add_person(opened, "sam")
            account = GoogleAccount(email="sam@example.test", default_person_id=person, created=1.0, session_state="ok")
            opened.add(account)
            opened.flush()
            exports.reconcile(opened, account, [exports.Listed("other-export", NOW - 600, "ready", NOW + 7 * 86400, (exports.ListedPart(1, size),))], NOW)
            theirs = opened.execute(select(ExportPart).join(Export).where(Export.account_id == account.id)).scalar_one()
            theirs.status = "downloading"
            return theirs.id

    def test_two_accounts_downloads_share_one_disk_and_the_first_part_waits_for_the_other_accounts_part(self):
        free = 400 * 10**9
        other = self.another_accounts_part_downloading(free - 10**9)
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        real_sleep = self.clock.sleep
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if self.clock.now >= 1000.0 + 12 * MINUTE:
                with db.session(self.engine) as opened:
                    opened.get(ExportPart, other).status = "done"
            real_sleep(seconds)

        self.clock.sleep = sleep
        with mock.patch.object(Photos, "free_bytes", lambda photos: free):
            _, result = self.run_job(browser)
        self.assertGreaterEqual(browser.started[0][1] - 1000.0, 12 * MINUTE, "no part of this job started while the other account's part still needed the room")
        self.assertEqual(sum(1 for message in self.messages() if message == stagger.PAUSED_WORDS), 1)
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3, "the wait spent no attempt and none of Google's downloads")

    def test_a_cancel_during_a_pause_ends_the_job_as_before(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        real_sleep = self.clock.sleep
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                self.cancelling.set()
            real_sleep(seconds)

        self.clock.sleep = sleep
        with self.short_of_space_until(10_000), self.assertRaises(Cancelled):
            self.run_job(browser)
        self.assertEqual(browser.started, [])
        self.assertEqual(self.parts(), [("pending", 0, 0)] * 3)

    def test_a_restart_requeues_the_part_that_was_in_flight(self):
        browser = StaggerBrowser(["lease lost"], self.clock)
        job_id = self.claim()
        with self.assertRaises(Cancelled):
            self.run_job(browser, job_id)
        self.assertEqual(self.parts(), [("downloading", 1, 1), ("pending", 0, 0), ("pending", 0, 0)])
        self.fail(job_id)
        self.assertEqual(self.parts(), [("pending", 1, 1), ("pending", 0, 0), ("pending", 0, 0)])
        with db.session(self.engine) as opened:
            self.assertEqual(exports.reconcile(opened, opened.get(GoogleAccount, self.account), [exports.Listed(COMPLETED_ID, NOW - 600, "ready", NOW + 7 * 86400, tuple(exports.ListedPart(n, size) for n, size in enumerate(SIZES, start=1)))], NOW).requeued, 1)
        second = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        _, result = self.run_job(second)
        self.assertEqual((result["downloaded"], second.downloaded), (3, [1, 2, 3]))
        self.assertEqual(self.parts(), [("done", 2, 2), ("done", 1, 1), ("done", 1, 1)])

    def test_a_part_that_gives_up_leaves_the_parts_after_it_pending(self):
        browser = StaggerBrowser(["ok", "corrupt", "corrupt", "corrupt"], self.clock, seconds=60 * MINUTE)
        with self.assertRaisesRegex(download.DownloadFailed, "^part 2 failed after 3 attempts"):
            self.run_job(browser)
        self.assertEqual(self.status(), "failed")
        self.assertEqual(self.parts(), [("done", 1, 1), ("failed", 3, 3), ("pending", 0, 0)])

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

    def test_a_late_download_delivered_during_the_sign_in_check_after_a_prompt_is_kept(self):
        browser = StaggerBrowser(["verify", "ok", "ok"], self.clock)
        original = browser.goto

        def goto(url):
            original(url)
            if url == selectors.TAKEOUT_URL and browser.downloaded == [1] and 1 not in browser.collected:
                browser.late[1] = self.clock.now

        browser.goto = goto
        self.run_job(browser)
        self.assertEqual((browser.downloaded, browser.collected, browser.cancelled), ([1, 2, 3], [1], []))
        self.assertEqual(self.parts()[0], ("done", 2, 1))

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

    def fronts_in_order(self, browser):
        browser.bring_to_front = lambda: self.reporter.events.append(("front", None))

    def test_a_prompt_while_staggering_brings_the_window_to_the_front_when_the_job_starts_waiting(self):
        browser = StaggerBrowser(["ok", "unrecognised then reauth", "ok", "ok"], self.clock)
        self.fronts_in_order(browser)
        self.run_job(browser)
        seen = self.reporter.events
        waited = seen.index(("waiting", "verify"))
        self.assertEqual(seen[waited + 1], ("front", None))
        self.assertEqual(seen.count(("front", None)), 1)
        self.assertEqual(self.parts()[1], ("done", 2, 1), "nothing else about the stagger changes")

    def test_a_sign_in_while_staggering_brings_the_window_to_the_front_once(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        self.signed_out_while_reopening(browser, 30)
        self.fronts_in_order(browser)
        _, result = self.run_job(browser)
        seen = self.reporter.events
        waited = seen.index(("waiting", "sign_in"))
        self.assertEqual(seen[waited + 1], ("front", None))
        self.assertEqual(seen.count(("front", None)), 1)
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 55), (3, 80)])
        self.assertEqual(result["downloaded"], 3)

    def test_a_download_that_never_waits_never_takes_the_front(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        self.run_job(browser)
        self.assertEqual(browser.fronted, 0)

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

    def test_a_part_whose_page_cannot_be_read_after_the_one_before_it_landed_stops_the_job_with_that_part_pending(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        real_content = browser.content

        def content():
            if browser.url == ARCHIVE and browser.downloaded == [1]:
                return "<html><head><title>Something else</title></head><body><h1>Nothing here</h1></body></html>"
            return real_content()

        browser.content = content
        with self.assertRaises(status.ExportsUnreadable):
            self.run_job(browser)
        self.assertEqual(browser.kept, [1])
        self.assertEqual(self.parts(), [("done", 1, 1), ("pending", 0, 0), ("pending", 0, 0)])

    def test_a_part_whose_page_cannot_be_read_with_nothing_in_flight_stops_at_once(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        real_content = browser.content
        browser.content = lambda: "<html><body><h1>Nothing here</h1></body></html>" if browser.url == ARCHIVE else real_content()
        with self.assertRaises(status.ExportsUnreadable):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [])

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

    def signed_out_while_reopening(self, browser, minutes):
        until = []
        real_goto, real_content = browser.goto, browser.content

        def goto(url):
            real_goto(url)
            if url == ARCHIVE and browser.downloaded == [1] and not until:
                until.append(self.clock.now + minutes * MINUTE)

        def content():
            if until and self.clock.now < until[0] and browser.url in (ARCHIVE, selectors.TAKEOUT_URL):
                return SIGN_IN_NEEDED
            return real_content()

        browser.goto, browser.content = goto, content

    def test_a_sign_in_while_the_next_parts_page_reopens_waits_for_you_and_clicks_nothing_until_then(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        self.signed_out_while_reopening(browser, 30)
        _, result = self.run_job(browser)
        events_seen = self.reporter.events
        waited = events_seen.index(("waiting", "sign_in"))
        resumed = events_seen.index(("progress", "signed in"))
        between = events_seen[waited + 1:resumed]
        self.assertFalse([event for event in between if event[0] in ("attempt", "clicked") or (event[0] == "progress" and isinstance(event[1], str))])
        self.assertEqual(self.started_after(browser), [(1, 0), (2, 55), (3, 80)])
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        clicked = [body for number, body in self.reporter.telemetry if body["event"] == "clicked"]
        self.assertEqual(clicked[1]["since_answer"], 0)
        with db.session(self.engine) as opened:
            sign_in = opened.execute(select(Alert).where(Alert.kind == "sign_in_needed")).scalars().all()
            self.assertEqual(len(sign_in), 1)
            self.assertIsNotNone(sign_in[0].closed)

    def test_a_sign_in_during_a_pause_keeps_the_pause_words_on_the_message(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        until = []
        opened = []
        real_goto, real_content = browser.goto, browser.content

        def goto(url):
            real_goto(url)
            if url == ARCHIVE:
                opened.append(url)
                if len(opened) == 1:
                    until.append(self.clock.now + 3 * MINUTE)

        def content():
            if until and self.clock.now < until[0] and browser.url in (ARCHIVE, selectors.TAKEOUT_URL):
                return SIGN_IN_NEEDED
            return real_content()

        browser.goto, browser.content = goto, content
        with self.short_of_space_until(22):
            self.run_job(browser)
        self.assertEqual(len(until), 1)
        signed_in = [message for message in self.messages() if message.startswith("signed in")]
        self.assertTrue(signed_in)
        self.assertTrue(all(stagger.PAUSED_WORDS in message for message in signed_in))
        messages = self.messages()
        self.assertLess(messages.index(stagger.PAUSED_WORDS), messages.index(signed_in[0]))
        self.assertLess(messages.index(signed_in[-1]), messages.index(stagger.RESUMED_WORDS), "the pause words stay until the attempt finds room")
        self.assertEqual(browser.fronted, 1, "a paused download that waits for a sign-in still brings its window to the front, once")

    def test_a_pause_alone_never_takes_the_front_and_keeps_its_words_on_every_message(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        with self.short_of_space_until(22):
            _, result = self.run_job(browser)
        self.assertEqual(browser.fronted, 0, "waiting for free space is not waiting for a person")
        messages = self.messages()
        paused, resumed = messages.index(stagger.PAUSED_WORDS), messages.index(stagger.RESUMED_WORDS)
        self.assertTrue(all(stagger.PAUSED_WORDS in message for message in messages[paused:resumed]))
        self.assertEqual(result["downloaded"], 3)

    def test_a_sign_in_that_never_comes_while_parts_download_times_out(self):
        browser = StaggerBrowser(["ok", "ok", "ok"], self.clock)
        self.signed_out_while_reopening(browser, 60 * 60)
        with self.assertRaises(session.SignInTimedOut):
            self.run_job(browser)
        self.assertEqual(self.parts()[0], ("done", 1, 1))


if __name__ == "__main__":
    unittest.main()
