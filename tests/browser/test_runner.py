import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from darkroom.domain import browser_jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob
from darkroom_browser import config, context, runner
from darkroom_browser.client import Client, LeaseLost, Refused, Unreachable
from tests.browser.fake_darkroom import TOKEN, FakeDarkroom
from tests.privacy import no_leaks
from tests.shared import own_database, settings_from, start_server
from tests.test_browser_jobs import add_account


class FakeBrowser:
    def __init__(self, html="<html><body>Manage your exports <input value='secret-form-value-canary'></body></html>", broken=False):
        self.html = html
        self.broken = broken
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

    def download_to(self, selector, folder):
        return folder / "x"

    def screenshot(self, path):
        if self.broken:
            raise RuntimeError("no display")
        path.write_bytes(b"\x89PNG")

    def close(self):
        self.closed = True


def settings_for(base, photos, poll=0.01):
    return config.Settings(base, TOKEN, photos, ":99", poll, "test-worker")


class RunnerAgainstFakeDarkroom(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDarkroom().start()
        self.addCleanup(self.fake.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = Path(folder.name)
        self.browsers = []
        self.sleeps = []

    def open_browser(self, profile_dir):
        self.assertEqual(profile_dir, self.photos / "_browser" / "1" / "profile")
        browser = FakeBrowser(broken=getattr(self, "browsers_broken", False))
        self.browsers.append(browser)
        return browser

    def make_runner(self, registry, heartbeat=0.05):
        settings = settings_for(self.fake.base, self.photos)
        return runner.Runner(settings, Client(self.fake.base, TOKEN), self.open_browser, registry, sleep=self.sleeps.append, heartbeat_seconds=heartbeat)

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
        with mock.patch.object(beat.thread, "join", wraps=beat.thread.join) as joined:
            beat.stop()
        joined.assert_called_once_with()
        self.assertFalse(beat.thread.is_alive())

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
        self.fake.enqueue(5, kind="download")
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
        wrong = runner.Runner(settings_for(self.fake.base, self.photos), Client(self.fake.base, "x" * 40), self.open_browser, {}, sleep=self.sleeps.append)
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


class ClientTests(unittest.TestCase):
    def test_errors_are_typed_by_status(self):
        fake = FakeDarkroom().start()
        self.addCleanup(fake.stop)
        client = Client(fake.base, TOKEN)
        fake.lost.add(1)
        with self.assertRaises(LeaseLost):
            client.heartbeat(1)
        with self.assertRaises(Refused) as refused:
            Client(fake.base, "wrong" * 8).claim("w")
        self.assertEqual(refused.exception.status, 401)
        fake.outage = True
        with self.assertRaises(Refused):
            client.claim("w")
        fake.stop()
        with self.assertRaises(Unreachable):
            client.claim("w")
        self.assertEqual(Client.error_text(b"not json"), "unreadable error")
        self.assertEqual(Client.error_text(b"[1]"), "unknown error")

    def test_a_reply_that_drips_past_the_deadline_is_cut_off(self):
        class Dripping:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read1(self, size):
                self.reads += 1
                time.sleep(0.02)
                return b" "

        class Opener:
            def __init__(self, response):
                self.response = response

            def open(self, request, timeout):
                return self.response

        dripping = Dripping()
        client = Client("http://darkroom.test", TOKEN)
        client.opener = Opener(dripping)
        with self.assertRaises(Unreachable):
            client.post("/x", {}, deadline=time.monotonic() + 0.1)
        self.assertGreater(dripping.reads, 0)
        self.assertLess(dripping.reads, 50)

    def test_a_heartbeat_reply_is_read_in_chunks_and_parsed(self):
        class Reply:
            def __init__(self):
                self.parts = [b'{"ok"', b": true}"]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read1(self, size):
                return self.parts.pop(0) if self.parts else b""

        class Opener:
            def open(self, request, timeout):
                return Reply()

        client = Client("http://darkroom.test", TOKEN)
        client.opener = Opener()
        self.assertEqual(client.post("/x", {}, deadline=time.monotonic() + 5), {"ok": True})

    def test_the_client_follows_no_redirects(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Redirecting(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "https://evil.example/")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                return None

        server = HTTPServer(("127.0.0.1", 0), Redirecting)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(Unreachable):
            Client(f"http://127.0.0.1:{server.server_address[1]}", TOKEN).claim("w")


class ConfigTests(unittest.TestCase):
    def test_the_worker_refuses_to_start_without_a_usable_token(self):
        with tempfile.TemporaryDirectory() as folder:
            good = config.Settings("http://darkroom:8000", TOKEN, Path(folder), ":99", 15.0, "w")
            self.assertEqual(config.problems(good), [])
            self.assertEqual(len(config.problems(config.Settings("http://darkroom:8000", "", Path(folder), ":99", 15.0, "w"))), 1)
            self.assertEqual(len(config.problems(config.Settings("http://darkroom:8000", "short", Path(folder), ":99", 15.0, "w"))), 1)
            self.assertEqual(len(config.problems(config.Settings("ftp://darkroom", TOKEN, Path(folder), ":99", 15.0, "w"))), 1)
            self.assertEqual(len(config.problems(config.Settings("http://", TOKEN, Path(folder) / "missing", ":99", 0.0, "w"))), 3)

    def test_load_reads_the_environment(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"DARKROOM_URL": "http://d:8000/", "DARKROOM_BROWSER_TOKEN": TOKEN, "PHOTOS_ROOT": "/p", "POLL_SECONDS": "5", "WORKER_NAME": "w1"}, clear=True):
            loaded = config.load()
        self.assertEqual((loaded.darkroom_url, loaded.token, str(loaded.photos_root), loaded.display, loaded.poll_seconds, loaded.worker_name), ("http://d:8000", TOKEN, "/p", ":99", 5.0, "w1"))

    def test_main_refuses_to_start_on_problems(self):
        from unittest import mock

        from darkroom_browser import __main__ as entry

        with mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN": "short"}, clear=True), no_leaks(self, TOKEN):
            self.assertEqual(entry.main(), 1)

    def test_main_runs_the_loop_until_interrupted(self):
        from unittest import mock

        from darkroom_browser import __main__ as entry

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"DARKROOM_BROWSER_TOKEN": TOKEN, "PHOTOS_ROOT": folder}, clear=True), \
                mock.patch.object(runner.Runner, "run_forever", side_effect=KeyboardInterrupt), no_leaks(self, TOKEN):
            self.assertEqual(entry.main(), 0)


class RegistryTests(unittest.TestCase):
    def test_the_registry_holds_noop_and_find_names_the_missing_kind(self):
        from darkroom_browser import jobs

        self.assertIs(jobs.find("noop"), jobs.noop)
        self.assertEqual(set(jobs.REGISTRY), {"noop"})
        with self.assertRaises(jobs.UnknownJob):
            jobs.find("download")


class ContextTests(unittest.TestCase):
    def test_paths_and_cancellation(self):
        reporter = FakeDarkroom()
        ctx = context.JobContext(5, "noop", "2", {}, Path("/photos"), reporter, lambda folder: FakeBrowser())
        self.assertEqual(ctx.profile_dir(), Path("/photos/_browser/2/profile"))
        self.assertEqual(ctx.failure_dir(), Path("/photos/_browser/2/failures/5"))
        self.assertEqual(ctx.failure_folder_name(), "_browser/2/failures/5")
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
        env = {"DARKROOM_BROWSER_TOKEN": TOKEN, "DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_PHOTOS_ROOT": photos.name, "DARKROOM_PUBLIC_URL": "http://127.0.0.1"}
        base, settings = start_server(self, settings_from(**env), engine)
        with db.session(engine) as opened:
            account = add_account(opened)
            first = browser_jobs.enqueue(opened, account, "noop", None, 1.0).id
            second = browser_jobs.enqueue(opened, account, "noop", None, 2.0).id
        sleeps = []
        worker = runner.Runner(settings_for(base, Path(photos.name)), Client(base, TOKEN), lambda folder: FakeBrowser(), sleep=sleeps.append)
        with no_leaks(self, TOKEN, database=engine):
            self.assertTrue(worker.tick())
            stuck = runner.Runner(settings_for(base, Path(photos.name)), Client(base, TOKEN), lambda folder: FakeBrowser(), {"noop": lambda ctx: {}}, sleep=sleeps.append)
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
