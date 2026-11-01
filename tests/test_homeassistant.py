import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from darkroom import config
from darkroom.providers import homeassistant


def settings(url="", token="", service=""):
    return config.Settings(**{**config.load().__dict__, "ha_url": url, "ha_token": token, "ha_notify_service": service})


class FakeHomeAssistant(HTTPServer):
    def __init__(self, status=200):
        self.requests = []
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.path, dict(self.headers.items()), json.loads(self.rfile.read(length))))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"[]")

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class NotifyTests(unittest.TestCase):
    def test_the_exact_url_headers_and_body(self):
        server = FakeHomeAssistant()
        self.addCleanup(server.stop)
        outcome = homeassistant.notify(settings(server.url, "tok", "mobile_app_phone"), "Title", "Message", "https://d.example/x")
        self.assertEqual(outcome, homeassistant.SENT)
        path, headers, body = server.requests[0]
        self.assertEqual(path, "/api/services/notify/mobile_app_phone")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(body, {"title": "Title", "message": "Message", "data": {"url": "https://d.example/x"}})

    def test_no_link_means_no_data(self):
        server = FakeHomeAssistant()
        self.addCleanup(server.stop)
        homeassistant.notify(settings(server.url, "tok", "svc"), "T", "M")
        self.assertEqual(server.requests[0][2], {"title": "T", "message": "M"})

    def test_unset_configuration_makes_no_network_call(self):
        with mock.patch("darkroom.tls.urlopen") as opened:
            self.assertEqual(homeassistant.notify(settings(), "T", "M"), homeassistant.DISABLED)
            self.assertEqual(homeassistant.notify(settings("http://ha", "tok", ""), "T", "M"), homeassistant.DISABLED)
        opened.assert_not_called()

    def test_an_unreachable_server_is_tried_twice_and_reported_without_the_token(self):
        calls = []
        with mock.patch("darkroom.tls.urlopen", side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("down"))), \
                mock.patch("sys.stderr") as stderr:
            outcome = homeassistant.notify(settings("http://ha", "secret-token", "svc"), "T", "M")
        self.assertEqual(outcome, homeassistant.FAILED)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("secret-token", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_an_error_reply_counts_as_failure(self):
        server = FakeHomeAssistant(status=500)
        self.addCleanup(server.stop)
        with mock.patch("sys.stderr"):
            self.assertEqual(homeassistant.notify(settings(server.url, "tok", "svc"), "T", "M"), homeassistant.FAILED)
        self.assertEqual(len(server.requests), 2)


if __name__ == "__main__":
    unittest.main()
