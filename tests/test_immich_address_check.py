import contextlib
import socket
import ssl
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from darkroom import tls
from darkroom.providers import immich
from tests.fakes.immich import FakeImmich

REDIRECT_TARGET = "http://redirect-target-canary.example/elsewhere"


class Answering:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status, self.body, self.headers, self.paths = status, body, headers or {}, []
        answering = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                answering.paths.append((self.path, dict(self.headers)))
                self.send_response(answering.status)
                for name, value in answering.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(answering.body)))
                self.end_headers()
                self.wfile.write(answering.body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CheckAddressTests(unittest.TestCase):
    def answering(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> Answering:
        found = Answering(status, body, headers)
        self.addCleanup(found.stop)
        return found

    def test_immich_answers(self):
        with FakeImmich([]) as fake:
            self.assertEqual(immich.check_address(fake.url), "answered")
            self.assertEqual(fake.requests(), [("GET", "/api/server/ping")])

    def test_the_ping_goes_through_tls_with_no_key_and_a_short_limit(self):
        with FakeImmich([]) as fake, mock.patch.object(tls, "urlopen", wraps=tls.urlopen) as opened:
            self.assertEqual(immich.check_address(fake.url), "answered")
        self.assertEqual(opened.call_count, 1)
        (url,), options = opened.call_args
        self.assertEqual(url, fake.url + "/api/server/ping")
        self.assertEqual((options["timeout"], options["allow_http"]), (immich.CHECK_TIMEOUT, True))
        self.assertNotIn("x-api-key", {name.lower() for name in options["headers"]})
        self.assertLessEqual(immich.CHECK_TIMEOUT, 5.0)

    def test_nothing_answers(self):
        self.assertEqual(immich.check_address(f"http://127.0.0.1:{closed_port()}"), "no_answer")
        with FakeImmich([]) as fake:
            fake.mode("down")
            self.assertEqual(immich.check_address(fake.url), "no_answer")
        with socket.socket() as silent:
            silent.bind(("127.0.0.1", 0))
            silent.listen()
            self.assertEqual(immich.check_address(f"http://127.0.0.1:{silent.getsockname()[1]}", timeout=0.2), "no_answer")

    def test_a_server_that_does_not_speak_http_is_not_immich(self):
        self.assertEqual(immich.check_address(self.raw(b"SSH-2.0-OpenSSH_9.6\r\n")), "not_immich")

    def test_a_server_that_hangs_up_without_answering_answers_nothing(self):
        self.assertEqual(immich.check_address(self.raw(b"")), "no_answer")

    def raw(self, line: bytes) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)

        def answer() -> None:
            with contextlib.suppress(OSError):
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(line)
        threading.Thread(target=answer, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    def test_something_else_answers(self):
        replies = {"an error": (500, b'{"message": "Internal server error"}'), "a web page": (200, b"<html>router</html>"),
                   "other JSON": (200, b'{"res": "ping"}'), "a list": (200, b'["pong"]'), "too much": (200, b'{"res": "pong", "x": "' + b"a" * immich.CHECK_LIMIT + b'"}'),
                   "not found": (404, b"Cannot GET /api/server/ping")}
        for name, (status, body) in replies.items():
            with self.subTest(name=name):
                self.assertEqual(immich.check_address(self.answering(status, body).url), "not_immich")

    def test_a_redirect_is_not_followed(self):
        target = self.answering(200, b'{"res": "pong"}')
        moved = self.answering(302, b"", {"Location": target.url + "/api/server/ping"})
        self.assertEqual(immich.check_address(moved.url), "not_immich")
        self.assertEqual(target.paths, [])
        self.assertEqual(immich.check_address(self.answering(301, b"", {"Location": REDIRECT_TARGET}).url), "not_immich")

    def test_the_tls_connection_fails(self):
        plain = self.answering(200, b'{"res": "pong"}')
        self.assertEqual(immich.check_address(plain.url.replace("http://", "https://")), "tls_failed")

    def test_what_counts_as_a_tls_failure(self):
        self.assertTrue(tls.tls_failure(ssl.SSLError("handshake")))
        self.assertTrue(tls.tls_failure(urllib.error.URLError(ssl.SSLCertVerificationError("untrusted"))))
        self.assertFalse(tls.tls_failure(urllib.error.URLError(ConnectionRefusedError())))
        self.assertFalse(tls.tls_failure(OSError("no route")))


if __name__ == "__main__":
    unittest.main()
