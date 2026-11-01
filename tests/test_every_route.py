import json
import unittest
from unittest import mock

from darkroom.server import routes
from tests.shared import fetch, settings_from, start_server

PUBLIC = {"GET /healthz", "GET /auth/login", "GET /auth/callback", "GET /auth/signed-out"}
BEARER_KINDS = {"device", "worker", "mcp"}
CROSS_SITE_HEADERS = ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}, {"Sec-Fetch-Site": "same-site"})


def never_runs(request):
    raise AssertionError("the handler ran on a request the guard should have refused")


class EveryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom", DARKROOM_PUBLIC_URL="https://darkroom.example")

    def setUp(self):
        guarded = tuple(routes.Route(route.method, route.path, never_runs, route.auth) for route in routes.ROUTES)
        patch = mock.patch.object(routes, "ROUTES", guarded)
        patch.start()
        self.addCleanup(patch.stop)
        self.base, _ = start_server(self, self.settings)

    def test_every_route_declares_a_known_auth_kind(self):
        self.assertTrue(routes.ROUTES)
        for route in routes.ROUTES:
            self.assertIn(route.auth, routes.AUTH_KINDS, f"{route.method} {route.path}")

    def test_the_public_routes_are_exactly_these(self):
        public = {f"{route.method} {route.path}" for route in routes.ROUTES if route.auth == "public"}
        self.assertEqual(public, PUBLIC, "a public route must be listed here, with a reason in the pull request")

    def test_session_routes_answer_401_without_a_cookie(self):
        found = [route for route in routes.ROUTES if route.auth == "session"]
        self.assertTrue(found)
        for route in found:
            with self.subTest(route=f"{route.method} {route.path}"):
                headers = {"Origin": "https://darkroom.example"} if route.method != "GET" else {}
                status, _, body = fetch(self.base, route.method, route.path, headers, None if route.method == "GET" else b"{}")
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body), {"error": "sign in required"})

    def test_a_stale_session_cookie_is_401(self):
        for route in (route for route in routes.ROUTES if route.auth == "session" and route.method == "GET"):
            with self.subTest(route=route.path):
                status, _, _ = fetch(self.base, "GET", route.path, {"Cookie": "darkroom_session=not-a-real-token"})
                self.assertEqual(status, 401)

    def test_state_changing_session_routes_refuse_cross_site_requests(self):
        found = [route for route in routes.ROUTES if route.auth == "session" and route.method != "GET"]
        self.assertTrue(found)
        for route in found:
            for headers in CROSS_SITE_HEADERS:
                with self.subTest(route=f"{route.method} {route.path}", headers=headers):
                    status, _, body = fetch(self.base, route.method, route.path, {"Content-Type": "application/json", **headers}, b"{}")
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(body), {"error": "cross-site requests are refused"})

    def test_bearer_routes_answer_401_without_a_token(self):
        for route in (route for route in routes.ROUTES if route.auth in BEARER_KINDS):
            with self.subTest(route=f"{route.method} {route.path}"):
                status, _, _ = fetch(self.base, route.method, route.path, {}, None if route.method == "GET" else b"{}")
                self.assertEqual(status, 401)

    def test_a_new_unguarded_route_fails_this_test(self):
        extra = (*routes.ROUTES, routes.Route("GET", "/api/open", never_runs, "public"))
        with mock.patch.object(routes, "ROUTES", extra):
            public = {f"{route.method} {route.path}" for route in routes.ROUTES if route.auth == "public"}
            self.assertNotEqual(public, PUBLIC)
        unknown = (*routes.ROUTES, routes.Route("GET", "/api/odd", never_runs, "anyone"))
        with mock.patch.object(routes, "ROUTES", unknown):
            self.assertFalse(all(route.auth in routes.AUTH_KINDS for route in routes.ROUTES))
            status, _, _ = fetch(self.base, "GET", "/api/odd")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
