import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from sqlalchemy import select, update

from darkroom.domain import browser_jobs, renewal
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, GoogleAccount
from darkroom_browser import __main__ as worker_main
from darkroom_browser import client as app_client
from darkroom_browser import config, context, runner
from darkroom_browser.client import Client, LeaseLost, Refused, Unreachable
from tests.browser.fake_darkroom import TOKEN, FakeDarkroom
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, settings_from, start_server, worker_socket_of
from tests.test_browser_jobs import add_account


class FakeBrowser:
    def __init__(self, html="<html><body>Manage your exports <input value='secret-form-value-canary'></body></html>", broken=False, close=None):
        self.html = html
        self.broken = broken
        self.on_close = close
        self.closed = False
        self.visited = []

    def goto(self, url):
        self.visited.append(url)

    def content(self):
        return self.html

    def click(self, selector):
        pass

    def type_text(self, selector, text):
        pass

    def wait_for(self, selector, timeout_ms=30_000):
        pass

    def screenshot(self, path):
        if self.broken:
            raise RuntimeError("no display")
        path.write_bytes(b"\x89PNG")

    def bring_to_front(self):
        self.fronted = getattr(self, "fronted", 0) + 1

    def close(self):
        if self.on_close is not None:
            self.on_close()
        self.closed = True


def settings_for(photos, poll=0.01, workers=1):
    return config.Settings(TOKEN, photos, ":99", poll, "test-worker", workers=workers)


def until(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.01)


def client_of(fake, token=TOKEN):
    return Client(fake.socket_path, token, folder=fake.folder)


def handed(value):
    read_end, write_end = os.pipe()
    os.write(write_end, value.encode())
    os.close(write_end)
    return read_end


class RunnerAgainstFakeDarkroom(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDarkroom().start()
        self.addCleanup(self.fake.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = Path(folder.name)
        self.browsers = []
        self.sleeps = []
        self.abandoned = threading.Event()

    def open_browser(self, profile_dir):
        self.assertEqual(profile_dir, self.photos / "_browser" / "1" / "profile")
        browser = FakeBrowser(broken=getattr(self, "browsers_broken", False), close=getattr(self, "browser_close", None))
        self.browsers.append(browser)
        return browser

    def make_runner(self, registry, heartbeat=0.05, give_up=30.0):
        settings = settings_for(self.photos)
        return runner.Runner(settings, client_of(self.fake), self.open_browser, registry, sleep=self.sleeps.append, heartbeat_seconds=heartbeat,
                             give_up_seconds=give_up, abandon=self.abandoned.set)

    def reported(self):
        return [action for action in self.fake.actions() if action in ("done", "failed")]

    def test_a_worker_cut_off_from_darkroom_exits_with_its_browser_before_the_lease_runs_out(self):
        def long_running(ctx):
            ctx.browser()
            self.fake.outage = True
            self.abandoned.wait(5)
            return {"ok": True}

        self.fake.enqueue(21)
        with contextlib.redirect_stderr(io.StringIO()):
            outcome = self.make_runner({"noop": long_running}, heartbeat=0.01, give_up=0.3).run_job(self.fake.queue.pop(0))
        self.assertEqual(outcome, "abandoned")
        self.assertTrue(self.abandoned.is_set())
        self.assertIn("heartbeat", self.fake.actions())
        self.assertEqual(self.reported(), [])

    def test_a_separate_browser_root_puts_the_profile_there(self):
        appdata = tempfile.TemporaryDirectory()
        self.addCleanup(appdata.cleanup)
        profiles = []
        settings = config.Settings(TOKEN, self.photos, ":99", 0.01, "test-worker", Path(appdata.name) / "browser")

        def opens(ctx):
            ctx.browser()
            return {"ok": True}

        def open_browser(profile_dir):
            profiles.append(profile_dir)
            return FakeBrowser()

        self.fake.enqueue(25)
        worker = runner.Runner(settings, client_of(self.fake), open_browser, {"noop": opens}, sleep=self.sleeps.append, heartbeat_seconds=0.05, abandon=self.abandoned.set)
        self.assertEqual(worker.run_job(self.fake.queue.pop(0)), "done")
        self.assertEqual(profiles, [Path(appdata.name) / "browser" / "1" / "profile"])

    def test_acknowledged_heartbeats_keep_a_long_job_alive(self):
        def long_running(ctx):
            ctx.browser()
            time.sleep(0.6)
            return {"ok": True}

        self.fake.enqueue(22)
        self.assertEqual(self.make_runner({"noop": long_running}, heartbeat=0.01, give_up=0.3).run_job(self.fake.queue.pop(0)), "done")
        self.assertFalse(self.abandoned.is_set())
        self.assertTrue(self.browsers[0].closed)

    def test_a_browser_that_does_not_close_is_never_reported_closed(self):
        def broken_close():
            raise RuntimeError("chromium did not exit")

        def opens(ctx):
            ctx.browser()
            return {"ok": True}

        def explodes(ctx):
            ctx.browser()
            raise TimeoutError("x")

        self.browser_close = broken_close
        for number, work in ((23, opens), (24, explodes)):
            with self.subTest(job=number):
                self.abandoned.clear()
                self.fake.enqueue(number)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.make_runner({"noop": work}).run_job(self.fake.queue.pop(0)), "abandoned")
                self.assertTrue(self.abandoned.is_set())
                self.assertEqual(self.reported(), [])

    def test_a_browser_close_that_hangs_ends_in_the_worker_exiting_not_in_a_report(self):
        self.browser_close = lambda: self.abandoned.wait(5)

        def opens(ctx):
            ctx.browser()
            return {"ok": True}

        def explodes(ctx):
            ctx.browser()
            raise TimeoutError("x")

        for number, work in ((25, opens), (26, explodes)):
            with self.subTest(job=number):
                self.abandoned.clear()
                self.fake.enqueue(number)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.make_runner({"noop": work}, heartbeat=0.01, give_up=0.3).run_job(self.fake.queue.pop(0)), "abandoned")
                self.assertTrue(self.abandoned.is_set())
                self.assertEqual(self.reported(), [])

    def test_the_worker_gives_up_well_inside_darkrooms_lease(self):
        self.assertLessEqual(runner.GIVE_UP_SECONDS + 2 * runner.HEARTBEAT_SECONDS, browser_jobs.LEASE_SECONDS)
        worker = runner.Runner(settings_for(self.photos), client_of(self.fake), self.open_browser, {})
        self.assertIs(worker.abandon, runner.exit_now)
        with mock.patch.object(runner.os, "_exit") as exits:
            runner.exit_now()
        exits.assert_called_once_with(runner.ABANDONED_EXIT)

    def test_claim_run_heartbeat_done(self):
        waited = threading.Event()

        def slow_noop(ctx):
            ctx.progress("working")
            waited.wait(0.2)
            return {"ok": True}

        with no_leaks(self, TOKEN):
            worker = self.make_runner({"noop": slow_noop})
            self.assertFalse(worker.tick())
            self.assertEqual(self.sleeps, [0.01])
            self.fake.enqueue(7, payload={"n": 1})
            self.assertTrue(worker.tick())
        actions = self.fake.actions()
        self.assertEqual(actions[:3], ["claim", "claim", "progress"])
        self.assertIn("heartbeat", actions)
        self.assertEqual(actions[-1], "done")
        self.assertEqual(self.fake.bodies("done"), [{"result": {"ok": True}}])
        self.assertEqual(self.fake.bodies("claim")[0], {"worker": "test-worker"})
        self.assertEqual(self.browsers, [], "a noop job opens no browser")

    def test_no_heartbeat_reaches_darkroom_after_done(self):
        def quick(ctx):
            time.sleep(0.05)
            return {"ok": True}

        for number in range(5):
            self.fake.enqueue(number + 1, payload={})
            worker = self.make_runner({"noop": quick}, heartbeat=0.001)
            self.assertTrue(worker.tick())
        actions = self.fake.actions()
        for position, action in enumerate(actions):
            if action == "done":
                self.assertNotEqual(actions[position + 1:position + 2], ["heartbeat"])

    def test_stopping_waits_for_a_heartbeat_that_is_still_in_flight(self):
        started = threading.Event()

        class Slow:
            def heartbeat(self, job_id):
                started.set()
                time.sleep(0.3)

        beat = runner.Heartbeat(Slow(), mock.Mock(job_id=1), 0.001)
        beat.start()
        self.assertTrue(started.wait(5))
        beat.stop()
        self.assertFalse(beat.thread.is_alive())

    def test_stopping_gives_up_on_a_heartbeat_that_never_finishes(self):
        started = threading.Event()
        release = threading.Event()

        class Stuck:
            def heartbeat(self, job_id):
                started.set()
                release.wait(5)

        beat = runner.Heartbeat(Stuck(), mock.Mock(job_id=1), 0.001)
        beat.start()
        self.assertTrue(started.wait(5))
        began = time.monotonic()
        with mock.patch.object(runner, "JOIN_SECONDS", 0.05):
            beat.stop()
        self.assertLess(time.monotonic() - began, 2)
        self.assertTrue(beat.thread.is_alive())
        again = time.monotonic()
        with mock.patch.object(runner, "JOIN_SECONDS", 5):
            beat.stop()
        self.assertLess(time.monotonic() - again, 1)
        release.set()
        beat.thread.join(5)

    def test_the_bundled_noop_job_runs_end_to_end(self):
        self.fake.enqueue(3)
        self.assertEqual(self.make_runner(None).run_job(self.fake.queue.pop(0)), "done")
        self.assertEqual(self.fake.bodies("progress"), [{"message": "noop: nothing to do"}])
        self.assertEqual(self.fake.bodies("done"), [{"result": {"ok": True, "kind": "noop", "playwright": None}}])

    def test_a_failing_job_captures_both_files_scrubbed_and_reports(self):
        def exploding(ctx):
            ctx.browser().goto("https://takeout.google.com/?token=abc")
            raise TimeoutError("selector not found: secret-url-canary")

        self.fake.enqueue(9)
        with no_leaks(self, "secret-url-canary", "secret-form-value-canary"):
            outcome = self.make_runner({"noop": exploding}).run_job(self.fake.queue.pop(0))
        self.assertEqual(outcome, "failed")
        folder = self.photos / "_browser" / "1" / "failures" / "9"
        self.assertEqual((folder / "screenshot.png").read_bytes(), b"\x89PNG")
        page = (folder / "page.html").read_text()
        self.assertIn("Manage your exports", page)
        self.assertNotIn("secret-form-value-canary", page)
        self.assertEqual(self.fake.bodies("failed"), [{"message": "TimeoutError", "failure_dir": "_browser/1/failures/9", "files": ["screenshot.png", "page.html"]}])
        self.assertTrue(self.browsers[0].closed)

    def test_the_browser_is_closed_before_done_or_failed_is_reported(self):
        seen = []

        def opens(ctx):
            ctx.browser()
            return {"ok": True}

        def explodes(ctx):
            ctx.browser()
            raise TimeoutError("x")

        for registry, report in (({"noop": opens}, "done"), ({"noop": explodes}, "failed")):
            self.fake.enqueue(11)
            worker = self.make_runner(registry)
            original = getattr(worker.client, report)

            def spy(*args, original=original):
                seen.append(self.browsers[-1].closed)
                return original(*args)

            setattr(worker.client, report, spy)
            worker.run_job(self.fake.queue.pop(0))
        self.assertEqual(seen, [True, True])

    def test_a_failed_screenshot_reports_only_the_page_snapshot(self):
        def exploding(ctx):
            ctx.browser()
            raise TimeoutError("x")

        self.browsers_broken = True
        self.fake.enqueue(10)
        with contextlib.redirect_stderr(io.StringIO()):
            self.make_runner({"noop": exploding}).run_job(self.fake.queue.pop(0))
        self.assertEqual(self.fake.bodies("failed"), [{"message": "TimeoutError", "failure_dir": "_browser/1/failures/10", "files": ["page.html"]}])

    def test_a_failure_before_the_browser_opened_reports_without_a_capture(self):
        def early(ctx):
            raise ValueError("bad payload")

        self.fake.enqueue(4)
        self.make_runner({"noop": early}).run_job(self.fake.queue.pop(0))
        self.assertEqual(self.fake.bodies("failed"), [{"message": "ValueError"}])
        self.assertFalse((self.photos / "_browser").exists())

    def test_an_unknown_kind_is_reported_as_failed(self):
        self.fake.enqueue(5, kind="upload")
        self.assertEqual(self.make_runner({"noop": lambda ctx: {}}).run_job(self.fake.queue.pop(0)), "failed")
        self.assertEqual(self.fake.bodies("failed"), [{"message": "UnknownJob"}])

    def test_a_lost_lease_cancels_the_job_instead_of_reporting_done(self):
        def patient(ctx):
            for _ in range(100):
                if ctx.cancelled.wait(0.02):
                    break
            ctx.progress("still here")
            return {"ok": True}

        self.fake.enqueue(11)
        self.fake.lost.add(11)
        outcome = self.make_runner({"noop": patient}, heartbeat=0.02).run_job(self.fake.queue.pop(0))
        self.assertEqual(outcome, "cancelled")
        self.assertEqual(self.fake.bodies("done"), [])
        self.assertEqual(self.fake.bodies("failed"), [])

    def test_a_lease_lost_on_done_is_cancelled_and_a_lost_lease_on_progress_too(self):
        self.fake.enqueue(12)
        self.fake.lost.add(12)
        self.assertEqual(self.make_runner({"noop": lambda ctx: {"ok": True}}).run_job(self.fake.queue.pop(0)), "cancelled")
        self.fake.enqueue(13)
        self.fake.lost.add(13)
        self.assertEqual(self.make_runner({"noop": lambda ctx: ctx.progress("x") or {}}).run_job(self.fake.queue.pop(0)), "cancelled")

    def test_an_unreachable_or_refusing_darkroom_backs_off(self):
        worker = self.make_runner({})
        self.fake.outage = True
        self.assertFalse(worker.tick())
        self.fake.stop()
        self.assertFalse(worker.tick())
        self.assertEqual(self.sleeps, [runner.BACKOFF_SECONDS, runner.BACKOFF_SECONDS])
        wrong = runner.Runner(settings_for(self.photos), client_of(self.fake, "x" * 40), self.open_browser, {}, sleep=self.sleeps.append)
        self.assertFalse(wrong.tick())

    def test_run_forever_stops_when_asked(self):
        worker = self.make_runner({})
        worker.sleep = lambda seconds: worker.stop()
        worker.run_forever()
        self.assertEqual(self.fake.actions(), ["claim"])

    def test_waiting_for_a_person_is_reported(self):
        def needs_person(ctx):
            ctx.wait_for_user("sign_in")
            return {}

        self.fake.enqueue(14)
        self.make_runner({"noop": needs_person}).run_job(self.fake.queue.pop(0))
        self.assertEqual(self.fake.bodies("waiting"), [{"reason": "sign_in"}])

    def test_a_failure_that_cannot_be_reported_is_logged_not_raised(self):
        def exploding(ctx):
            raise RuntimeError("x")

        self.fake.enqueue(15)
        job = self.fake.queue.pop(0)
        self.fake.outage = True
        self.assertEqual(self.make_runner({"noop": exploding}).run_job(job), "failed")
        self.assertEqual(len(self.fake.bodies("failed")), 1)


class ParallelRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDarkroom().start()
        self.addCleanup(self.fake.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = Path(folder.name)
        self.browsers = {}
        self.lock = threading.Lock()
        self.abandoned = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.ran = []

    def open_browser(self, profile_dir):
        account = profile_dir.parent.name
        browser = FakeBrowser()
        with self.lock:
            opened = [each for each in self.browsers.get(account, []) if not each.closed]
            self.assertEqual(opened, [], "one profile is never opened twice at once")
            self.browsers.setdefault(account, []).append(browser)
        return browser

    def opened(self, account):
        with self.lock:
            return list(self.browsers.get(account, []))

    def blocks(self, ctx):
        ctx.browser()
        self.ran.append((ctx.kind, ctx.account))
        while not self.release.wait(0.01):
            ctx.check()
        return {"ok": True}

    def quick(self, ctx):
        ctx.browser()
        self.ran.append((ctx.kind, ctx.account))
        return {"ok": True}

    def make_runner(self, workers, registry=None):
        registry = registry or {"download": self.blocks, "connect": self.quick, "status": self.quick}
        return runner.Runner(settings_for(self.photos, workers=workers), client_of(self.fake), self.open_browser, registry, heartbeat_seconds=0.05,
                             give_up_seconds=30.0, abandon=self.abandoned.set)

    def start(self, worker):
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()

        def stop():
            self.release.set()
            worker.stop()
            thread.join(10)

        self.addCleanup(stop)
        return thread

    def reports(self, job_id):
        return [action for path, _ in self.fake.calls if path.startswith(f"/internal/browser/jobs/{job_id}/") for action in [path.rsplit("/", 1)[-1]] if action in ("done", "failed")]

    def test_a_second_accounts_job_starts_while_the_first_accounts_download_runs(self):
        self.fake.enqueue(1, kind="download", account="1")
        self.fake.enqueue(2, kind="connect", account="2")
        self.start(self.make_runner(workers=2))
        until(lambda: self.reports(2) == ["done"])
        self.assertEqual(self.reports(1), [], "the download is still running")
        self.assertIn(("download", "1"), self.ran)
        self.release.set()
        until(lambda: self.reports(1) == ["done"])

    def test_a_download_paused_for_space_holds_only_its_own_accounts_loop(self):
        def paused(ctx):
            ctx.browser()
            self.ran.append((ctx.kind, ctx.account))
            ctx.progress("paused for space")
            while not self.release.wait(0.01):
                ctx.sleep(0.01)
                ctx.check()
            return {"ok": True}

        self.fake.enqueue(1, kind="download", account="1")
        self.start(self.make_runner(workers=3, registry={"download": paused, "connect": self.quick, "status": self.quick}))
        until(lambda: ("download", "1") in self.ran)
        self.fake.enqueue(2, kind="status", account="2")
        self.fake.enqueue(3, kind="download", account="3")
        until(lambda: self.reports(2) == ["done"] and ("download", "3") in self.ran)
        self.assertEqual(self.reports(1), [], "the paused download goes on waiting in its own loop")
        self.release.set()
        until(lambda: self.reports(1) == ["done"] and self.reports(3) == ["done"])

    def test_a_queued_sign_in_starts_at_once_while_downloads_fill_every_other_lane(self):
        self.fake.enqueue(1, kind="download", account="1")
        self.fake.enqueue(2, kind="download", account="2")
        self.start(self.make_runner(workers=2))
        until(lambda: ("download", "1") in self.ran)
        self.fake.enqueue(3, kind="connect", account="3")
        until(lambda: self.reports(3) == ["done"])
        self.assertEqual([job["id"] for job in self.fake.queue], [2], "the second download waits for a lane that may take it")
        lanes = {body.get("lane") for body in self.fake.bodies("claim")}
        self.assertEqual(lanes, {None, "interactive"})
        self.assertNotIn(("download", "2"), self.ran)
        self.release.set()
        until(lambda: self.reports(2) == ["done"])

    def test_the_interactive_lane_takes_no_download(self):
        self.fake.enqueue(1, kind="download", account="1")
        worker = self.make_runner(workers=2)
        self.assertEqual(worker.lanes(), ["interactive", None])
        self.assertFalse(worker.tick("interactive"))
        self.assertEqual([job["id"] for job in self.fake.queue], [1])
        self.assertEqual(self.fake.bodies("claim"), [{"worker": "test-worker", "lane": "interactive"}])
        self.release.set()
        self.assertTrue(worker.tick())
        self.assertEqual(self.reports(1), ["done"])

    def test_one_worker_takes_every_kind_on_its_one_loop_as_before(self):
        worker = self.make_runner(workers=1)
        self.assertEqual(worker.lanes(), [None])
        self.assertEqual(self.make_runner(workers=3).lanes(), ["interactive", None, None])
        self.release.set()
        self.fake.enqueue(1, kind="download", account="1")
        self.assertTrue(worker.tick())
        self.assertEqual(self.fake.bodies("claim"), [{"worker": "test-worker"}])

    def test_the_pool_runs_one_claim_loop_per_worker(self):
        worker = self.make_runner(workers=3)
        self.start(worker)
        until(lambda: len(worker.threads) == 3 and all(thread.is_alive() for thread in worker.threads))
        self.assertEqual(sorted(thread.name for thread in worker.threads), ["claims-0", "claims-1", "claims-2"])

    def test_never_two_jobs_for_one_account_even_if_darkroom_hands_out_a_second(self):
        self.fake.enqueue(1, kind="download", account="1")
        self.start(self.make_runner(workers=2))
        until(lambda: ("download", "1") in self.ran)
        with mock.patch.object(runner, "TAKE_SECONDS", 0.05):
            self.fake.enqueue(2, kind="connect", account="1")
            until(lambda: self.reports(2) == ["failed"])
        self.assertEqual(self.fake.bodies("failed"), [{"message": runner.IN_USE_MESSAGE}])
        self.assertEqual(self.ran, [("download", "1")], "the refused job ran nothing and opened no second browser")
        self.assertEqual(len(self.opened("1")), 1)
        self.release.set()
        until(lambda: self.reports(1) == ["done"])
        self.fake.enqueue(3, kind="connect", account="1")
        until(lambda: self.reports(3) == ["done"])
        self.assertEqual(self.ran[-1], ("connect", "1"), "once its job has ended the account's next job runs")

    def test_an_account_freed_just_after_the_claim_is_waited_for_not_refused(self):
        worker = self.make_runner(workers=2)
        self.assertTrue(worker.take("1"))
        threading.Timer(0.1, worker.give_back, ("1",)).start()
        self.fake.enqueue(1, kind="connect", account="1")
        self.assertTrue(worker.tick())
        self.assertEqual(self.reports(1), ["done"])

    def test_a_refusal_that_cannot_be_reported_is_logged_not_raised(self):
        worker = self.make_runner(workers=2)
        self.assertTrue(worker.take("1"))
        self.fake.enqueue(1, kind="connect", account="1")
        with mock.patch.object(runner, "TAKE_SECONDS", 0.01), mock.patch.object(worker.client, "failed", side_effect=Unreachable("x")), contextlib.redirect_stderr(io.StringIO()) as logged:
            self.assertFalse(worker.tick())
        self.assertIn("could not report the refused job", logged.getvalue())
        self.assertEqual(self.ran, [])

    def test_a_lost_lease_cancels_only_its_own_job(self):
        self.fake.lost.add(1)
        self.fake.enqueue(1, kind="download", account="1")
        self.fake.enqueue(2, kind="download", account="2")
        self.start(self.make_runner(workers=3))
        until(lambda: len(self.opened("1")) == 1 and self.opened("1")[0].closed)
        self.assertEqual(self.reports(1), [])
        self.assertIn(("download", "2"), self.ran)
        self.assertFalse(self.opened("2")[0].closed, "the other account's job keeps running")
        self.release.set()
        until(lambda: self.reports(2) == ["done"])

    def test_the_browser_is_closed_before_done_or_failed_is_reported_for_two_jobs_at_once(self):
        both = threading.Barrier(2, timeout=10)
        seen = {}

        def opens(ctx):
            ctx.browser()
            both.wait()
            return {"ok": True}

        def explodes(ctx):
            ctx.browser()
            both.wait()
            raise TimeoutError("x")

        worker = self.make_runner(workers=2, registry={"connect": opens, "status": explodes})
        for report in ("done", "failed"):
            original = getattr(worker.client, report)

            def spy(job_id, *args, original=original, report=report):
                seen[report] = [browser.closed for browser in self.opened(str(job_id))]
                return original(job_id, *args)

            setattr(worker.client, report, spy)
        self.fake.enqueue(1, kind="connect", account="1")
        self.fake.enqueue(2, kind="status", account="2")
        self.start(worker)
        until(lambda: len(seen) == 2)
        self.assertEqual(seen, {"done": [True], "failed": [True]})

    def test_stopping_with_two_jobs_running_ends_every_thread_and_closes_both_browsers(self):
        self.fake.enqueue(1, kind="download", account="1")
        self.fake.enqueue(2, kind="download", account="2")
        worker = self.make_runner(workers=3)
        thread = self.start(worker)
        until(lambda: len(self.ran) == 2)
        worker.stop()
        self.assertFalse(thread.is_alive() and thread.join(5))
        self.assertFalse(any(each.is_alive() for each in worker.threads))
        self.assertTrue(all(browser.closed for account in ("1", "2") for browser in self.opened(account)))
        self.assertEqual(self.reports(1) + self.reports(2), [], "a stopped job is not reported; its lease runs out as when the worker is interrupted")
        self.assertFalse(self.abandoned.is_set())

    def test_a_claim_loop_that_fails_stops_every_job_and_the_worker(self):
        self.fake.enqueue(1, kind="download", account="1")
        worker = self.make_runner(workers=2)
        original = worker.client.claim

        def claim(name, lane=None):
            if lane is None:
                until(lambda: ("status", "1") in self.ran)
                raise RuntimeError("an answer the worker cannot read")
            return original(name, lane) if self.fake.queue else None

        worker.client.claim = claim
        self.fake.queue[0]["kind"] = "status"
        worker.registry["status"] = self.blocks
        with contextlib.redirect_stderr(io.StringIO()) as logged, self.assertRaisesRegex(RuntimeError, "cannot read"):
            worker.run_forever()
        self.assertIn("a claim loop failed", logged.getvalue())
        self.assertTrue(all(browser.closed for browser in self.opened("1")))
        self.assertFalse(any(each.is_alive() for each in worker.threads))

    def test_every_job_shares_the_one_client_and_the_one_screen(self):
        worker = self.make_runner(workers=3)
        first = worker.context_for({"id": 1, "kind": "connect", "account": "1", "payload": {}})
        second = worker.context_for({"id": 2, "kind": "connect", "account": "2", "payload": {}})
        self.assertIs(first.screen, second.screen)
        self.assertIs(first.reporter, second.reporter)
        self.assertIs(first.reporter, worker.client)


class ScreenTests(unittest.TestCase):
    def context(self, job_id, screen, browser=None):
        browser = browser or FakeBrowser()
        reporter = mock.Mock()
        return context.JobContext(job_id, "download", str(job_id), {}, Path("/photos-unused"), reporter, lambda profile: browser, screen=screen), browser

    def fronted(self, browser):
        return getattr(browser, "fronted", 0)

    def test_the_longest_waiting_job_is_in_front(self):
        screen = context.Screen()
        first, first_browser = self.context(1, screen)
        second, second_browser = self.context(2, screen)
        first.browser()
        second.browser()
        first.wait_for_user("sign_in")
        self.assertEqual(self.fronted(first_browser), 1, "a job that starts waiting with nobody else waiting comes to the front at once")
        second.wait_for_user("verify")
        self.assertEqual(self.fronted(second_browser), 0, "a job that starts waiting later stays behind")
        second.check()
        self.assertEqual(self.fronted(second_browser), 0)
        first.check()
        self.assertEqual(self.fronted(first_browser), 2, "the longest-waiting job raises itself again after another window changed")
        first.check()
        self.assertEqual(self.fronted(first_browser), 2, "and only once per change")
        first.progress("signed in")
        second.check()
        self.assertEqual(self.fronted(second_browser), 1, "once the first job resumes, the next longest-waiting comes to the front")
        third, third_browser = self.context(3, screen)
        third.browser()
        second.check()
        self.assertEqual(self.fronted(second_browser), 2, "a window opened over the waiting one is covered again")
        self.assertEqual(self.fronted(third_browser), 0)
        second.close_browser()
        third.wait_for_user("sign_in")
        self.assertEqual(self.fronted(third_browser), 1, "a job that ended no longer holds the front")

    def test_a_job_that_waits_again_for_another_reason_keeps_its_place(self):
        screen = context.Screen()
        first, _ = self.context(1, screen)
        second, second_browser = self.context(2, screen)
        first.browser()
        second.browser()
        first.wait_for_user("sign_in")
        second.wait_for_user("sign_in")
        first.wait_for_user("wrong_account")
        second.check()
        self.assertEqual(self.fronted(second_browser), 0)

    def test_a_job_alone_comes_to_the_front_when_it_starts_waiting(self):
        ctx, browser = self.context(1, context.Screen())
        ctx.wait_for_user("sign_in")
        self.assertEqual(self.fronted(browser), 0, "no window is open yet")
        ctx.browser()
        ctx.check()
        self.assertEqual(self.fronted(browser), 1)
        alone = context.JobContext(5, "connect", "5", {}, Path("/photos-unused"), mock.Mock(), lambda profile: browser)
        alone.browser()
        alone.wait_for_user("verify")
        self.assertEqual(self.fronted(browser), 2)

    def test_a_window_that_cannot_come_to_the_front_is_logged_and_the_job_goes_on(self):
        class Stuck(FakeBrowser):
            def bring_to_front(self):
                raise RuntimeError("Target page, context or browser has been closed")

        ctx, _ = self.context(1, context.Screen(), Stuck())
        ctx.browser()
        with contextlib.redirect_stderr(io.StringIO()) as logged:
            ctx.wait_for_user("sign_in")
        self.assertIn("could not bring the browser window to the front", logged.getvalue())
        self.assertIn("RuntimeError", logged.getvalue())


class OnlyTheAppSocketTests(unittest.TestCase):
    def private_folder(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def test_a_stand_in_on_the_old_loopback_address_gets_no_token_and_hands_the_worker_no_job(self):
        stand_in = FakeDarkroom(on_tcp=True).start()
        self.addCleanup(stand_in.stop)
        stand_in.enqueue(7)
        folder, photos = self.private_folder(), self.private_folder()
        ticks, opened = [], []
        env = {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "DARKROOM_PORT": str(stand_in.port), "PHOTOS_ROOT": str(photos), "POLL_SECONDS": "1"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(app_client, "SOCKET_FOLDER", folder, create=True), \
                mock.patch.object(app_client, "APP_SOCKET", folder / "worker-api.sock", create=True), \
                mock.patch.object(runner.Runner, "run_forever", lambda worker: ticks.append(worker.tick())), \
                mock.patch.object(worker_main.live, "open_context", lambda *args, **kwargs: opened.append(args) or FakeBrowser()), mock.patch.object(worker_main.os, "umask"):
            self.assertEqual(worker_main.main(), 0)
        self.assertEqual((stand_in.seen, [job["id"] for job in stand_in.queue], opened, ticks), ([], [7], [], [False]))

    def test_a_socket_in_a_folder_others_can_enter_gets_no_token(self):
        folder = self.private_folder()
        fake = FakeDarkroom(folder=folder).start()
        self.addCleanup(fake.stop)
        fake.enqueue(3)
        for mode in (0o750, 0o705, 0o777):
            with self.subTest(mode=oct(mode)):
                folder.chmod(mode)
                with self.assertRaises(Unreachable):
                    Client(fake.socket_path, TOKEN, folder=folder).claim("w")
        folder.chmod(0o700)
        self.assertEqual((fake.seen, [job["id"] for job in fake.queue]), ([], [3]))
        self.assertEqual(Client(fake.socket_path, TOKEN, folder=folder).claim("w")["id"], 3)

    def test_a_socket_outside_the_private_folder_or_a_folder_that_is_a_link_gets_no_token(self):
        folder = self.private_folder()
        inner = folder / "inner"
        inner.mkdir(mode=0o700)
        fake = FakeDarkroom(folder=inner).start()
        self.addCleanup(fake.stop)
        link = folder / "link"
        link.symlink_to(inner)
        for socket_path, private in ((fake.socket_path, folder), (link / "worker-api.sock", link), (Path("worker-api.sock"), Path(".")),
                                     (inner / ".." / "inner" / "worker-api.sock", inner), (fake.socket_path, folder / "missing")):
            with self.subTest(socket_path=str(socket_path), folder=str(private)), self.assertRaises(Unreachable):
                Client(socket_path, TOKEN, folder=private).claim("w")
        self.assertEqual(fake.seen, [])

    def test_the_worker_reaches_the_app_only_on_the_socket_the_init_and_the_app_use(self):
        from darkroom import config as darkroom_config
        from darkroom_init import plan

        self.assertEqual(app_client.APP_SOCKET, Path("/run/darkroom/worker-api.sock"))
        self.assertEqual({app_client.APP_SOCKET, plan.WORKER_SOCKET, darkroom_config.WORKER_SOCKET}, {app_client.APP_SOCKET})
        self.assertEqual(app_client.SOCKET_FOLDER, plan.SOCKET_FOLDER)
        self.assertNotIn("DARKROOM_PORT", plan.worker_env({"DARKROOM_PORT": "8000"}))
        self.assertFalse(hasattr(config, "darkroom_url"))


class ClientTests(unittest.TestCase):
    def test_errors_are_typed_by_status(self):
        fake = FakeDarkroom().start()
        self.addCleanup(fake.stop)
        client = client_of(fake)
        fake.lost.add(1)
        with self.assertRaises(LeaseLost):
            client.heartbeat(1)
        with self.assertRaises(Refused) as refused:
            client_of(fake, "wrong" * 8).claim("w")
        self.assertEqual(refused.exception.status, 401)
        fake.outage = True
        with self.assertRaises(Refused):
            client.claim("w")
        fake.stop()
        with self.assertRaises(Unreachable):
            client.claim("w")
        self.assertEqual(Client.error_text(b"not json"), "unreadable error")
        self.assertEqual(Client.error_text(b"[1]"), "unknown error")

    def test_the_client_follows_no_redirects_and_wants_a_json_object(self):
        import socketserver
        from http.server import BaseHTTPRequestHandler

        replies = []

        class Odd(BaseHTTPRequestHandler):
            def do_POST(self):
                status, body = replies.pop(0)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "https://evil.example/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, format, *args):
                return None

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        socket_path = Path(folder.name) / "worker-api.sock"
        server = socketserver.UnixStreamServer(str(socket_path), Odd)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Client(socket_path, TOKEN, folder=Path(folder.name))
        for status, body, reason in ((302, b"", "redirected"), (200, b"[1]", "other than a JSON object")):
            replies.append((status, body))
            with self.subTest(status=status), self.assertRaisesRegex(Unreachable, reason):
                client.claim("w")
        replies.append((200, b""))
        self.assertIsNone(client.claim("w"))
        self.assertEqual(Client(socket_path, TOKEN).folder, app_client.SOCKET_FOLDER)


class EarlyAnswerTests(unittest.TestCase):
    def serve(self, answer):
        import socketserver

        class Early(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(64)
                answer(self.request)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        socket_path = Path(folder.name) / "worker-api.sock"
        server = socketserver.UnixStreamServer(str(socket_path), Early)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return Client(socket_path, TOKEN, folder=Path(folder.name))

    def test_an_answer_sent_before_the_body_was_read_is_still_read(self):
        body = b'{"error": "this job is no longer running"}'
        client = self.serve(lambda stream: stream.sendall(b"HTTP/1.1 409 Conflict\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)))
        with self.assertRaises(LeaseLost) as lost:
            client.part(1, 1, {"pad": "x" * 4_000_000})
        self.assertEqual((lost.exception.status, str(lost.exception)), (409, "darkroom answered 409: this job is no longer running"))

    def test_a_server_that_hangs_up_without_answering_is_unreachable(self):
        client = self.serve(lambda stream: None)
        with self.assertRaises(Unreachable):
            client.part(1, 1, {"pad": "x" * 4_000_000})


class ConfigTests(unittest.TestCase):
    def test_the_worker_refuses_to_start_without_a_usable_token(self):
        with tempfile.TemporaryDirectory() as folder:
            good = config.Settings(TOKEN, Path(folder), ":99", 15.0, "w")
            self.assertEqual(config.problems(good), [])
            self.assertEqual(len(config.problems(config.Settings("", Path(folder), ":99", 15.0, "w"))), 1)
            self.assertEqual(len(config.problems(config.Settings("short", Path(folder), ":99", 15.0, "w"))), 1)
            self.assertEqual(len(config.problems(config.Settings(TOKEN, Path(folder) / "missing", ":99", 0.0, "w"))), 2)

    def test_load_reads_the_environment(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"DARKROOM_PORT": "8123", "DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": "/p", "POLL_SECONDS": "5", "WORKER_NAME": "w1"}, clear=True):
            loaded = config.load()
        self.assertEqual((loaded.token, str(loaded.photos_root), loaded.display, loaded.poll_seconds, loaded.worker_name), (TOKEN, "/p", ":99", 5.0, "w1"))

    def test_browser_workers_defaults_to_three_and_is_read_from_the_environment(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": "/p"}, clear=True):
            self.assertEqual(config.load().workers, 3)
        for raw, read in (("1", 1), (" 5 ", 5), ("8", 8), ("0", 0), ("9", 9), ("two", 0), ("-1", 0), ("2.5", 0), ("", 3)):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": "/p", "BROWSER_WORKERS": raw}, clear=True):
                self.assertEqual(config.load().workers, read)

    def test_browser_workers_must_be_between_one_and_the_most(self):
        with tempfile.TemporaryDirectory() as folder:
            for workers in (1, 3, config.MOST_WORKERS):
                with self.subTest(workers=workers):
                    self.assertEqual(config.problems(config.Settings(TOKEN, Path(folder), ":99", 15.0, "w", workers=workers)), [])
            for workers in (0, -1, config.MOST_WORKERS + 1):
                with self.subTest(workers=workers):
                    self.assertEqual(config.problems(config.Settings(TOKEN, Path(folder), ":99", 15.0, "w", workers=workers)),
                                     [f"BROWSER_WORKERS must be a whole number from 1 to {config.MOST_WORKERS}"])

    def test_a_separate_browser_root_is_read_and_must_be_absolute(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": "/p"}, clear=True):
            self.assertIsNone(config.load().browser_root)
        with mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": "/p", "BROWSER_ROOT": "/data/browser"}, clear=True):
            self.assertEqual(config.load().browser_root, Path("/data/browser"))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(config.problems(config.Settings(TOKEN, Path(folder), ":99", 15.0, "w", Path("/data/browser"))), [])
            self.assertEqual(config.problems(config.Settings(TOKEN, Path(folder), ":99", 15.0, "w", Path("browser"))), ["BROWSER_ROOT must be an absolute path"])
            share = Path(folder) / "photos"
            share.mkdir()
            (Path(folder) / "linked").symlink_to(share)
            for root, refused in ((share, True), (share / "jo" / "originals", True), (Path(folder), True), (Path(folder) / "linked", True), (share / "_browser", False)):
                with self.subTest(root=str(root)):
                    found = config.problems(config.Settings(TOKEN, share, ":99", 15.0, "w", root))
                    self.assertEqual(found, ["BROWSER_ROOT must not be PHOTOS_ROOT, a folder inside it other than its _browser folder, or a folder that holds it"] if refused else [])

    def test_the_worker_refuses_to_start_with_a_browser_root_that_is_the_photo_share(self):
        from unittest import mock

        from darkroom_browser import __main__ as entry

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": folder, "BROWSER_ROOT": folder}, clear=True), \
                mock.patch.object(runner.Runner, "run_forever") as loop, no_leaks(self, TOKEN):
            self.assertEqual(entry.main(), 1)
        loop.assert_not_called()

    def test_main_opens_browsers_with_the_working_root_for_downloads(self):
        from unittest import mock

        from darkroom_browser import __main__ as entry

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": folder, "BROWSER_ROOT": "/data/browser"}, clear=True), \
                mock.patch.object(runner.Runner, "run_forever", side_effect=KeyboardInterrupt), mock.patch.object(entry.live, "open_context") as opened:
            made = []
            with mock.patch.object(entry, "Runner", side_effect=lambda *args: made.append(args) or runner.Runner(*args)):
                self.assertEqual(entry.main(), 0)
            made[0][2](Path("/data/browser/3/profile"))
        opened.assert_called_once_with(Path("/data/browser/3/profile"), working_root=Path(folder))

    def test_main_refuses_to_start_on_problems(self):
        from unittest import mock

        from darkroom_browser import __main__ as entry

        with mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed("short"))}, clear=True), no_leaks(self, TOKEN):
            self.assertEqual(entry.main(), 1)

    def test_main_runs_the_loop_until_interrupted(self):
        from unittest import mock

        from darkroom_browser import __main__ as entry

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "PHOTOS_ROOT": folder}, clear=True), \
                mock.patch.object(runner.Runner, "run_forever", side_effect=KeyboardInterrupt), no_leaks(self, TOKEN):
            self.assertEqual(entry.main(), 0)


class UnrecordedScheduleTests(unittest.TestCase):
    def test_a_schedule_whose_done_report_is_refused_blocks_a_second_schedule(self):
        engine = own_database(self)
        settings = settings_from(DARKROOM_PUBLIC_URL="https://darkroom.example")
        with db.session(engine) as opened:
            account = add_account(opened)
            job_id = browser_jobs.queue(opened, account, "schedule", {"email": "jo.takeout@example.test"}, 1.0).id
            browser_jobs.claim(opened, 2.0)

        class Stub:
            def heartbeat(self, job):
                return None

            def progress(self, job, message):
                return None

            def waiting(self, job, reason):
                return None

            def done(self, job, result):
                raise Refused(500, "database is locked")

            def failed(self, job, message, folder, files=None):
                with db.session(engine) as opened:
                    browser_jobs.failed(opened, settings, job, message, folder, files or [], 5.0)

        worker = runner.Runner(settings_for("http://unused", Path("/photos-unused")), Stub(), lambda profile: FakeBrowser(), {"schedule": lambda ctx: {"schedule_created": 1.0, "schedule_end": 2.0}})
        self.assertEqual(worker.run_job({"id": job_id, "kind": "schedule", "account": str(account), "payload": {}}), "failed")
        with db.session(engine) as opened:
            self.assertTrue(opened.get(BrowserJob, job_id).message.startswith("ScheduleNotConfirmed: the work finished"))
            with self.assertRaises(browser_jobs.AlreadyOpen):
                browser_jobs.enqueue_once(opened, account, "schedule", 9.0)

    def renewal_reported(self, work):
        engine = own_database(self)
        settings = settings_from(DARKROOM_PUBLIC_URL="https://darkroom.example", TZ="UTC")
        with db.session(engine) as opened:
            account = add_account(opened)
            opened.execute(update(GoogleAccount).where(GoogleAccount.id == account).values(schedule_created=1.0, schedule_end=1_900_000_000.0))
            job_id = browser_jobs.queue(opened, account, browser_jobs.RENEW_KIND, {"email": "jo.takeout@example.test"}, 1.0).id
            browser_jobs.claim(opened, 2.0)

        class Stub:
            def heartbeat(self, job):
                return None

            def progress(self, job, message):
                return None

            def done(self, job, result):
                raise Refused(500, "database is locked")

            def failed(self, job, message, folder, files=None):
                with db.session(engine) as opened:
                    failed = browser_jobs.failed(opened, settings, job, message, folder, files or [], 5.0)
                    renewal.job_failed(opened, settings, failed, 5.0)

        worker = runner.Runner(settings_for("http://unused", Path("/photos-unused")), Stub(), lambda profile: FakeBrowser(), {"renew": work})
        self.assertEqual(worker.run_job({"id": job_id, "kind": "renew", "account": str(account), "payload": {}}), "failed")
        with db.session(engine) as opened:
            return opened.get(BrowserJob, job_id).message, sorted(alert.kind for alert in opened.execute(select(Alert)).scalars() if alert.kind.startswith("schedule"))

    def test_a_renewal_whose_done_report_is_refused_is_left_unconfirmed_not_failed(self):
        message, kinds = self.renewal_reported(lambda ctx: {"schedule_created": 1_890_000_000.0, "schedule_end": 1_920_000_000.0})
        self.assertTrue(message.startswith("ScheduleNotConfirmed: the work finished"), message)
        self.assertEqual(kinds, ["schedule_renewal_unconfirmed"])

    def test_a_renewal_that_failed_inside_the_work_is_reported_as_failed(self):
        def broken(ctx):
            raise RuntimeError("page crashed")

        message, kinds = self.renewal_reported(broken)
        self.assertEqual(message, "RuntimeError")
        self.assertEqual(kinds, ["schedule_renewal_failed"])

    def test_other_kinds_and_failures_inside_the_work_keep_their_own_message(self):
        sent = []

        class Stub:
            def heartbeat(self, job):
                return None

            def progress(self, job, message):
                return None

            def done(self, job, result):
                raise Refused(500, "database is locked")

            def failed(self, job, message, folder, files=None):
                sent.append(message)

        settings = settings_for("http://unused", Path("/photos-unused"))

        def broken(ctx):
            raise RuntimeError("page crashed")

        worker = runner.Runner(settings, Stub(), lambda profile: FakeBrowser(), {"noop": lambda ctx: {}, "schedule": broken})
        worker.run_job({"id": 1, "kind": "noop", "account": "1", "payload": {}})
        worker.run_job({"id": 2, "kind": "schedule", "account": "1", "payload": {}})
        self.assertEqual(sent, ["Refused", "RuntimeError"])


class FailureMessageTests(unittest.TestCase):
    def test_only_the_worker_own_failures_carry_their_text(self):
        from darkroom_browser.jobs import schedule, session

        self.assertEqual(runner.failure_message(schedule.ScheduleNotConfirmed("check Takeout")), "ScheduleNotConfirmed: check Takeout")
        self.assertEqual(runner.failure_message(session.WrongAccount("a different account")), "WrongAccount: a different account")
        self.assertEqual(runner.failure_message(TimeoutError("waiting for selector https://takeout.example/secret")), "TimeoutError")

    def test_a_long_failure_is_cut_to_what_darkroom_records(self):
        from darkroom.server.api.browser_internal import MAX_MESSAGE
        from darkroom_browser.jobs import status

        self.assertEqual(runner.MAX_MESSAGE, MAX_MESSAGE)
        said = runner.failure_message(status.ExportsUnreadable("x" * 2 * MAX_MESSAGE))
        self.assertEqual(len(said), MAX_MESSAGE)
        self.assertTrue(said.startswith("ExportsUnreadable: xxx"))
        self.assertTrue(said.endswith("..."))
        self.assertEqual(runner.failure_message(status.ExportsUnreadable("x" * (MAX_MESSAGE - 19))), "ExportsUnreadable: " + "x" * (MAX_MESSAGE - 19))


class RegistryTests(unittest.TestCase):
    def test_the_registry_holds_noop_and_find_names_the_missing_kind(self):
        from darkroom_browser import jobs

        self.assertIs(jobs.find("noop"), jobs.noop)
        self.assertEqual(set(jobs.REGISTRY), {"noop", "connect", "schedule", "status", "download", "renew"})
        with self.assertRaises(jobs.UnknownJob):
            jobs.find("upload")


class ContextTests(unittest.TestCase):
    def test_a_live_browser_that_fails_to_close_says_so_after_stopping_playwright(self):
        from darkroom_browser import live

        context_part, playwright_part = mock.Mock(), mock.Mock()
        context_part.close.side_effect = RuntimeError("chromium did not exit")
        with self.assertRaisesRegex(RuntimeError, "did not exit"):
            live.Live(mock.Mock(), context_part, playwright_part).close()
        playwright_part.stop.assert_called_once_with()
        live.Live(mock.Mock(), mock.Mock(), mock.Mock()).close()

    def test_paths_and_cancellation(self):
        reporter = FakeDarkroom()
        self.addCleanup(reporter.stop)
        ctx = context.JobContext(5, "noop", "2", {}, Path("/photos"), reporter, lambda folder: FakeBrowser())
        self.assertEqual(ctx.profile_dir(), Path("/photos/_browser/2/profile"))
        self.assertEqual(ctx.failure_dir(), Path("/photos/_browser/2/failures/5"))
        self.assertEqual(ctx.failure_folder_name(), "_browser/2/failures/5")
        apart = context.JobContext(5, "noop", "2", {}, Path("/photos"), reporter, lambda folder: FakeBrowser(), browser_root=Path("/data/browser"))
        self.assertEqual((apart.profile_dir(), apart.failure_dir()), (Path("/data/browser/2/profile"), Path("/data/browser/2/failures/5")))
        self.assertEqual(apart.failure_folder_name(), "_browser/2/failures/5")
        self.assertIs(ctx.browser(), ctx.browser())
        ctx.cancelled.set()
        with self.assertRaises(context.Cancelled):
            ctx.browser()
        with self.assertRaises(context.Cancelled):
            ctx.progress("x")
        ctx.close_browser()
        self.assertIsNone(ctx.browser_open)
        ctx.close_browser()


class RunnerAgainstDarkroom(unittest.TestCase):
    def test_a_noop_job_runs_end_to_end_and_a_dead_worker_loses_its_lease(self):
        engine = own_database(self)
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        env = {"browser_token": TOKEN, "DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_PHOTOS_ROOT": photos.name, "DARKROOM_PUBLIC_URL": "http://127.0.0.1"}
        base, settings = start_server(self, settings_from(**env), engine)
        socket_path = worker_socket_of(base)
        with db.session(engine) as opened:
            account = add_account(opened)
            first = browser_jobs.enqueue(opened, account, "noop", None, 1.0).id
            second = browser_jobs.enqueue(opened, account, "noop", None, 2.0).id
        sleeps = []
        worker = runner.Runner(settings_for(Path(photos.name)), Client(socket_path, TOKEN, folder=socket_path.parent), lambda folder: FakeBrowser(), sleep=sleeps.append)
        with no_leaks(self, TOKEN, database=engine):
            self.assertEqual(fetch(base, "POST", "/internal/browser/jobs/claim", {"Authorization": f"Bearer {TOKEN}"}, b"{}")[0], 404)
            self.assertTrue(worker.tick())
            stuck = runner.Runner(settings_for(Path(photos.name)), Client(socket_path, TOKEN, folder=socket_path.parent), lambda folder: FakeBrowser(), {"noop": lambda ctx: {}},
                                  sleep=sleeps.append)
            claimed = stuck.client.claim("stuck-worker")
            self.assertEqual(claimed["id"], second)
            self.assertFalse(worker.tick())
        with db.session(engine) as opened:
            done = opened.get(BrowserJob, first)
            self.assertEqual((done.status, json.loads(done.result)), ("done", {"ok": True, "kind": "noop", "playwright": None}))
            self.assertEqual(browser_jobs.expire_leases(opened, settings, done.finished + browser_jobs.LEASE_SECONDS + 1)[0].id, second)
            self.assertEqual(opened.get(BrowserJob, second).status, "failed")
            self.assertEqual([alert.kind for alert in opened.query(Alert)], ["failure"])
        with self.assertRaises(LeaseLost):
            stuck.client.heartbeat(second)


if __name__ == "__main__":
    unittest.main()
