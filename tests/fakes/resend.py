from __future__ import annotations

import contextlib
import json
import threading
import time
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PATH = "/emails"


@dataclass(frozen=True)
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, object]:
        parsed = json.loads(self.body)
        assert isinstance(parsed, dict)
        return parsed


class FakeResend:
    def __init__(self, test: unittest.TestCase):
        self.requests: list[Received] = []
        self.status = 200
        self.delay = 0.0
        self.location = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake.requests.append(Received(self.path, dict(self.headers.items()), body))
                if fake.delay:
                    time.sleep(fake.delay)
                self.send_response(fake.status)
                if fake.location:
                    self.send_header("Location", fake.location)
                reply = json.dumps({"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"} if fake.status < 300 else {"name": "validation_error", "message": "refused"}).encode()
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(reply)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = f"http://{HOST}:{self.server.server_address[1]}{PATH}"
