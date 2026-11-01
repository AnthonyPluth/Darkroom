import json
import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import browser_jobs
from darkroom.server import auth, routes
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob
from darkroom.storage.paths import Photos
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, settings_from, start_server
from tests.test_browser_jobs import add_account

TOKEN = "worker-token-canary-0123456789abcdef0123456789"
WORKER_ROUTES = ("/internal/browser/jobs/claim", "/internal/browser/jobs/{id}/heartbeat", "/internal/browser/jobs/{id}/progress",
                 "/internal/browser/jobs/{id}/waiting", "/internal/browser/jobs/{id}/done", "/internal/browser/jobs/{id}/failed")
SESSION_ROUTES = ("/api/status", "/api/browser/jobs", "/api/browser/failures/{id}/screenshot.png", "/api/browser/failures/{id}/page.html")


class WorkerApiServer(unittest.TestCase):
    token = TOKEN
    extra_env = {}

    def setUp(self):
        self.engine = own_database(self)
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        self.photos = Path(photos.name)
        env = {"DARKROOM_BROWSER_TOKEN": self.token, "DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_PHOTOS_ROOT": photos.name,
               "DARKROOM_PUBLIC_URL": "http://127.0.0.1", "VNC_URL": "https://browser.example/vnc.html", **self.extra_env}
        self.base, self.settings = start_server(self, settings_from(**env), self.engine)
        self.replies = []
        with db.session(self.engine) as opened:
            self.account = add_account(opened)

    def worker(self, method, path, body=None, token=None):
        headers = {"Authorization": f"Bearer {token or self.token}", "Content-Type": "application/json"}
        status, _, reply = fetch(self.base, method, path, headers, json.dumps(body if body is not None else {}).encode())
        self.replies.append(reply)
        return status, json.loads(reply) if reply else None

    def owner(self, method, path, body=None):
        headers = {"Content-Type": "application/json", "Origin": self.base} if method != "GET" else {}
        status, reply_headers, reply = fetch(self.base, method, path, headers, json.dumps(body).encode() if body is not None else None)
        self.replies.append(reply)
        return status, reply_headers, reply

    def enqueue(self):
        with db.session(self.engine) as opened:
            return browser_jobs.enqueue(opened, self.account, "noop", {"n": 1}, 1.0).id

    def claim(self):
        status, reply = self.worker("POST", "/internal/browser/jobs/claim")
        self.assertEqual(status, 200)
        return reply["job"]


class WorkerRoundTripTests(WorkerApiServer):
    def test_claim_heartbeat_progress_done(self):
        with no_leaks(self, TOKEN, database=self.engine, replies=self.replies):
            self.assertEqual(self.worker("POST", "/internal/browser/jobs/claim"), (200, {"job": None}))
            job_id = self.enqueue()
            job = self.claim()
            self.assertEqual(job, {"id": job_id, "kind": "noop", "account": str(self.account), "payload": {"n": 1}})
            self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/heartbeat")[0], 200)
            self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/progress", {"message": "on the downloads page"})[0], 200)
            self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/done", {"result": {"ok": True}})[0], 200)
            self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/heartbeat"), (409, {"error": "this job is no longer running"}))
            status, _, body = self.owner("GET", "/api/browser/jobs")
            self.assertEqual(status, 200)
            (listed,) = json.loads(body)["jobs"]
            self.assertEqual((listed["id"], listed["status"], listed["message"], listed["has_capture"]), (job_id, "done", "on the downloads page", False))
            status, _, body = self.owner("GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertIsInstance(json.loads(body)["worker_last_seen"], float)

    def test_waiting_opens_the_sign_in_alert(self):
        job_id = self.enqueue()
        self.claim()
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/waiting", {"reason": "nap"})[0], 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/waiting", {"reason": "sign_in"})[0], 200)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job_id).status, "waiting_for_user")
            alert = opened.execute(select(Alert)).scalar_one()
            self.assertEqual((alert.kind, alert.link), ("sign_in_needed", "https://browser.example/vnc.html"))

    def test_failed_with_a_capture_serves_the_files_to_the_owner_only(self):
        job_id = self.enqueue()
        self.claim()
        folder = Photos(self.photos).failures(str(self.account), str(job_id))
        folder.mkdir(parents=True)
        (folder / "screenshot.png").write_bytes(b"\x89PNG not really")
        (folder / "page.html").write_text("<html><body>snapshot</body></html>")
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/failed", {"message": "TimeoutError", "failure_dir": "/etc"})[0], 400)
        status, _ = self.worker("POST", f"/internal/browser/jobs/{job_id}/failed", {"message": "TimeoutError", "failure_dir": f"_browser/{self.account}/failures/{job_id}"})
        self.assertEqual(status, 200)
        status, headers, body = self.owner("GET", f"/api/browser/failures/{job_id}/screenshot.png")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/png", b"\x89PNG not really"))
        status, headers, body = self.owner("GET", f"/api/browser/failures/{job_id}/page.html")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn(b"snapshot", body)
        for path in (f"/api/browser/failures/{job_id + 1}/page.html", "/api/browser/failures/x/page.html", "/api/browser/failures/../page.html",
                     f"/api/browser/failures/{'9' * 40}/page.html"):
            with self.subTest(path=path):
                self.assertEqual(self.owner("GET", path)[0], 404)
        status, _, body = fetch(self.base, "GET", f"/api/browser/failures/{job_id}/page.html", {"Authorization": f"Bearer {TOKEN}"}, None)
        self.assertEqual(status, 200, "no-auth mode lets the owner in; the token itself is not what let this through")
        with db.session(self.engine) as opened:
            alert = opened.execute(select(Alert)).scalar_one()
            self.assertEqual(alert.link, f"http://127.0.0.1/api/browser/failures/{job_id}/screenshot.png")

    def test_a_capture_folder_that_is_missing_is_404(self):
        job_id = self.enqueue()
        self.claim()
        self.worker("POST", f"/internal/browser/jobs/{job_id}/failed", {"message": "Crashed", "failure_dir": f"_browser/{self.account}/failures/{job_id}"})
        self.assertEqual(self.owner("GET", f"/api/browser/failures/{job_id}/screenshot.png")[0], 404)
        self.worker("POST", f"/internal/browser/jobs/{self.enqueue()}/failed", {"message": "Crashed"})

    def test_the_owner_queues_jobs(self):
        status, _, body = self.owner("POST", "/api/browser/jobs", {"account_id": self.account, "kind": "noop"})
        self.assertEqual(status, 201)
        job = json.loads(body)["job"]
        self.assertEqual((job["status"], job["kind"], job["account_id"]), ("queued", "noop", self.account))
        self.assertEqual(self.owner("POST", "/api/browser/jobs", {"account_id": self.account, "kind": "download"})[0], 400)
        self.assertEqual(self.owner("POST", "/api/browser/jobs", {"account_id": self.account + 9, "kind": "noop"})[0], 404)
        self.assertEqual(self.owner("POST", "/api/browser/jobs", {"account_id": "1", "kind": "noop"})[0], 400)
        self.assertEqual(self.owner("GET", f"/api/browser/jobs?account={self.account}")[0], 200)
        self.assertEqual(json.loads(self.owner("GET", f"/api/browser/jobs?account={self.account + 1}")[2]), {"jobs": []})
        self.assertEqual(self.owner("GET", "/api/browser/jobs?account=jo")[0], 400)
        self.assertEqual(self.claim()["id"], job["id"])

    def test_bad_bodies_and_ids_are_refused(self):
        job_id = self.enqueue()
        self.claim()
        status, _, _ = fetch(self.base, "POST", f"/internal/browser/jobs/{job_id}/progress", {"Authorization": f"Bearer {TOKEN}"}, b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/progress", {"message": "x" * 501})[0], 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/progress", {})[0], 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/done", {"result": [1]})[0], 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/done", {"result": {"blob": "x" * 40_000}})[0], 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/heartbeat", {"pad": "x" * 70_000})[0], 400)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id + 1}/heartbeat")[0], 404)
        self.assertEqual(self.worker("POST", "/internal/browser/jobs/abc/heartbeat")[0], 404)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{'1' * 30}/heartbeat")[0], 404)
        self.assertEqual(self.worker("POST", f"/internal/browser/jobs/{job_id}/heartbeat")[0], 200)


class WorkerTokenTests(WorkerApiServer):
    def test_wrong_short_or_missing_tokens_are_401_and_never_logged(self):
        with no_leaks(self, TOKEN, replies=self.replies):
            for token in ("", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), "x" * 32):
                with self.subTest(token=token):
                    status, reply = self.worker("POST", "/internal/browser/jobs/claim", token=token or "-")
                    self.assertEqual((status, reply), (401, {"error": "sign in required"}))
            for header in ({"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": f"Bearer {TOKEN} extra"}, {"Authorization": "Bearer " + "x" * 2000},
                           {"Cookie": f"{auth.SESSION_COOKIE}={TOKEN}"}, {"X-Worker-Token": TOKEN}):
                with self.subTest(header=header):
                    status, _, _ = fetch(self.base, "POST", "/internal/browser/jobs/claim", header, b"{}")
                    self.assertEqual(status, 401)
            self.assertEqual(self.worker("POST", "/internal/browser/jobs/claim")[0], 200)

    def test_the_worker_token_is_not_a_session_and_cross_site_rules_do_not_apply_to_it(self):
        status, _, _ = fetch(self.base, "POST", "/internal/browser/jobs/claim", {"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"}, b"{}")
        self.assertEqual(status, 200)

    def test_every_worker_and_session_route_is_in_the_table(self):
        paths = {route.path for route in routes.ROUTES}
        for path in WORKER_ROUTES:
            self.assertIn(path, paths)
            self.assertEqual(routes.find("POST", path).auth, "worker")
        for path in SESSION_ROUTES:
            self.assertIn(path, paths)
            self.assertEqual(routes.find("GET", path).auth, "session")


class ShortTokenTests(WorkerApiServer):
    token = "short-token"

    def test_a_token_under_32_characters_is_never_accepted(self):
        self.assertEqual(self.worker("POST", "/internal/browser/jobs/claim")[0], 401)


class NoTokenTests(WorkerApiServer):
    token = ""

    def test_an_unset_token_refuses_every_worker_request(self):
        for header in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer x"}):
            with self.subTest(header=header):
                status, _, _ = fetch(self.base, "POST", "/internal/browser/jobs/claim", header, b"{}")
                self.assertEqual(status, 401)


class TokenConfigTests(unittest.TestCase):
    def test_check_config_flags_a_short_token_and_accepts_a_long_one(self):
        from darkroom import config

        short = settings_from(DARKROOM_BROWSER_TOKEN="too-short")
        self.assertTrue(any("DARKROOM_BROWSER_TOKEN" in line for line in config.problems(short)))
        fine = settings_from(DARKROOM_BROWSER_TOKEN=TOKEN)
        self.assertFalse(any("DARKROOM_BROWSER_TOKEN" in line for line in config.problems(fine)))
        unset = settings_from()
        self.assertFalse(any("DARKROOM_BROWSER_TOKEN" in line for line in config.problems(unset)))
        self.assertFalse(config.browser_token_usable("a" * 31 + " "))
        self.assertFalse(config.browser_token_usable("é" * 40))
        self.assertTrue(config.browser_token_usable("a" * 32))
        self.assertEqual(os.environ.get("DARKROOM_BROWSER_TOKEN"), None)


if __name__ == "__main__":
    unittest.main()
