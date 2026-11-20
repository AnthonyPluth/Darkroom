from __future__ import annotations

import json
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

TOKEN = "fake-darkroom-worker-token-canary-0123456789abcdef"
SOCKET_NAME = "worker-api.sock"
LONG_KINDS = ("download",)


class FakeDarkroom:
    def __init__(self, token: str = TOKEN, on_tcp: bool = False, folder: Path | None = None) -> None:
        self.token = token
        self.queue: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.seen: list[str] = []
        self.lost: set[int] = set()
        self.outage = False
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def reply(self, value: object, status: int = 200) -> None:
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with fake.lock:
                    fake.seen.append(self.headers.get("Authorization") or "")
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self.reply({"error": "sign in required"}, 401)
                    return
                with fake.lock:
                    fake.calls.append((self.path, body))
                    if fake.outage:
                        self.reply({"error": "something went wrong"}, 500)
                        return
                    if self.path == "/internal/browser/jobs/claim":
                        self.reply({"job": fake.next_job(body.get("lane"))})
                        return
                    parts = self.path.split("/")
                    job_id, action = int(parts[4]), parts[5]
                    if job_id in fake.lost:
                        self.reply({"error": "this job is no longer running"}, 409)
                        return
                    if action in ("done", "failed"):
                        fake.lost.add(job_id)
                    self.reply({"job": {"id": job_id, "kind": "noop", "account": "1", "payload": {}}})

        self.server: socketserver.BaseServer
        self.base = ""
        self.held: tempfile.TemporaryDirectory[str] | None = None
        if on_tcp:
            self.server = HTTPServer(("127.0.0.1", 0), Handler)
            self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
            self.port = int(self.server.server_address[1])
        else:
            if folder is None:
                self.held = tempfile.TemporaryDirectory()
                folder = Path(self.held.name)
            self.folder = folder
            self.socket_path = folder / SOCKET_NAME
            self.server = socketserver.UnixStreamServer(str(self.socket_path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> FakeDarkroom:
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
        if self.held is not None:
            self.held.cleanup()
            self.held = None

    def next_job(self, lane: object) -> dict[str, Any] | None:
        allowed = [job for job in self.queue if lane != "interactive" or job["kind"] not in LONG_KINDS]
        if not allowed:
            return None
        self.queue.remove(allowed[0])
        return allowed[0]

    def enqueue(self, job_id: int, kind: str = "noop", account: str = "1", payload: dict[str, Any] | None = None) -> None:
        self.queue.append({"id": job_id, "kind": kind, "account": account, "payload": payload or {}})

    def actions(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _ in self.calls]

    def bodies(self, action: str) -> list[dict[str, Any]]:
        return [body for path, body in self.calls if path.endswith("/" + action)]
