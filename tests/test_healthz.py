import json
import threading
import unittest
import urllib.error
import urllib.request

from darkroom import VERSION, config
from darkroom.server import handler


class HealthzTests(unittest.TestCase):
    def setUp(self):
        self.server = handler.make_server(config.load(), host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers), error.read()

    def test_healthz_answers_ok_and_the_version(self):
        status, headers, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "version": VERSION})
        self.assertIn("default-src", headers["Content-Security-Policy"])

    def test_an_unknown_api_path_is_a_json_404(self):
        status, _, body = self.get("/api/nothing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_a_path_outside_the_static_folder_is_not_served(self):
        status, _, _ = self.get("/..%2f..%2fpyproject.toml")
        self.assertEqual(status, 404)

    def test_a_route_that_needs_sign_in_answers_401_until_an_authenticator_exists(self):
        from darkroom.server import routes
        from darkroom.server.api import status as status_api

        original = routes.ROUTES
        routes.ROUTES = (*original, routes.Route("GET", "/api/private", status_api.healthz, "session"))
        self.addCleanup(setattr, routes, "ROUTES", original)
        status, _, body = self.get("/api/private")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "sign in required"})


if __name__ == "__main__":
    unittest.main()
