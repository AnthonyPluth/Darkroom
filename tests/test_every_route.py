import dataclasses
import json
import time
import unittest
from unittest import mock

from darkroom import oidc
from darkroom.server import routes
from darkroom.storage import db
from darkroom.storage.models import Device, Person, User
from tests.shared import fetch, own_database, settings_from, start_server

PUBLIC = {"GET /healthz", "GET /auth/login", "GET /auth/callback", "GET /auth/signed-out", "GET /auth/device/start"}
BEARER_KINDS = {"device", "worker"}
WORKER_TOKEN = "every-route-worker-token-canary-0123456789abcdef"
DEVICE_TOKEN = "every-route-device-token-canary-0123456789abcdef"
DEVICE_ROUTES = {"POST /api/device/assets/check", "PUT /api/device/assets/{id}/resources/{kind}", "POST /api/device/checkin",
                 "POST /api/device/eligible", "POST /api/device/eligible/confirm", "POST /api/device/deleted"}
PEOPLE_AND_STATUS_ROUTES = {
    "GET /api/people", "POST /api/people", "PATCH /api/people/{id}", "DELETE /api/people/{id}", "GET /api/people/{id}/devices", "DELETE /api/devices/{id}",
    "PATCH /api/devices/{id}",
    "GET /api/accounts", "POST /api/accounts", "PATCH /api/accounts/{id}", "DELETE /api/accounts/{id}",
    "POST /api/accounts/{id}/connect", "POST /api/accounts/{id}/schedule", "POST /api/accounts/{id}/schedule/checked", "GET /api/browser/view",
    "GET /api/pixels", "POST /api/pixels", "PATCH /api/pixels/{id}", "DELETE /api/pixels/{id}", "GET /api/status", "GET /api/events",
}
REVIEW_AND_IMPORT_ROUTES = {
    "GET /api/imports", "GET /api/imports/{id}", "POST /api/exports/{id}/import", "GET /api/review", "GET /api/review/{id}",
    "GET /api/review/{id}/thumb/{side}", "POST /api/review/{id}/resolve", "POST /api/review/{id}/undo", "GET /api/decisions",
    "POST /api/decisions/{id}/reverse", "GET /api/media", "POST /api/media/{id}/reassign",
}
CROSS_SITE_HEADERS = ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}, {"Sec-Fetch-Site": "same-site"})


def never_runs(request):
    raise AssertionError("the handler ran on a request the guard should have refused")


class EveryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom", DARKROOM_PUBLIC_URL="https://darkroom.example",
                                     DARKROOM_BROWSER_TOKEN=WORKER_TOKEN)

    def setUp(self):
        guarded = tuple(dataclasses.replace(route, handler=never_runs) for route in routes.ROUTES)
        patch = mock.patch.object(routes, "ROUTES", guarded)
        patch.start()
        self.addCleanup(patch.stop)
        self.engine = own_database(self)
        self.base, _ = start_server(self, self.settings, self.engine)

    def test_every_route_declares_a_known_auth_kind(self):
        self.assertTrue(routes.ROUTES)
        for route in routes.ROUTES:
            self.assertIn(route.auth, routes.AUTH_KINDS, f"{route.method} {route.path}")

    def test_the_public_routes_are_exactly_these(self):
        public = {f"{route.method} {route.path}" for route in routes.ROUTES if route.auth == "public"}
        self.assertEqual(public, PUBLIC, "a public route must be listed here, with a reason in the pull request")

    def test_the_people_accounts_pixels_devices_and_status_routes_are_session_routes(self):
        found = {f"{route.method} {route.path}": route.auth for route in routes.ROUTES}
        for key in PEOPLE_AND_STATUS_ROUTES:
            with self.subTest(route=key):
                self.assertEqual(found.get(key), "session")

    def test_the_review_imports_decisions_and_media_routes_are_session_routes(self):
        found = {f"{route.method} {route.path}": route.auth for route in routes.ROUTES}
        for key in REVIEW_AND_IMPORT_ROUTES:
            with self.subTest(route=key):
                self.assertEqual(found.get(key), "session")

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

    def test_the_worker_token_is_never_accepted_on_session_routes(self):
        found = [route for route in routes.ROUTES if route.auth == "session"]
        self.assertTrue(found)
        for route in found:
            for headers in ({"Authorization": f"Bearer {WORKER_TOKEN}"}, {"Cookie": f"darkroom_session={WORKER_TOKEN}"}):
                with self.subTest(route=f"{route.method} {route.path}", headers=headers):
                    sent = {**headers, "Origin": "https://darkroom.example"} if route.method != "GET" else headers
                    status, _, body = fetch(self.base, route.method, route.path, sent, None if route.method == "GET" else b"{}")
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body), {"error": "sign in required"})

    def test_a_session_cookie_is_never_accepted_on_worker_routes(self):
        found = [route for route in routes.ROUTES if route.auth == "worker"]
        self.assertTrue(found)
        for route in found:
            for headers in ({"Cookie": "darkroom_session=some-session-token"}, {"Cookie": f"darkroom_session={WORKER_TOKEN}"},
                            {"Authorization": f"Bearer {WORKER_TOKEN[:-1]}"}, {"Authorization": WORKER_TOKEN}, {"X-Worker-Token": WORKER_TOKEN}):
                with self.subTest(route=f"{route.method} {route.path}", headers=headers):
                    status, _, body = fetch(self.base, route.method, route.path, headers, None if route.method == "GET" else b"{}")
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body), {"error": "sign in required"})

    def test_every_kind_but_public_has_its_own_authenticator(self):
        from darkroom.server import handler

        self.assertEqual(set(handler.AUTHENTICATORS), {"session", "worker", "device"})

    def test_the_device_routes_are_device_routes(self):
        found = {f"{route.method} {route.path}": route.auth for route in routes.ROUTES}
        self.assertEqual({key for key, kind in found.items() if kind == "device"}, DEVICE_ROUTES)

    def a_live_device(self):
        with db.session(self.engine) as opened:
            person = Person(name="Jo", slug="jo", email="jo@example.test", originals_path="jo/originals", google_only_path="jo/google-only", created=1.0)
            opened.add(person)
            opened.flush()
            opened.add(User(sub="jo", email="jo@example.test", email_verified=True, groups='["parents"]', last_seen=time.time()))
            opened.add(Device(person_id=person.id, name="iPhone", token_hash=oidc.hash_token(DEVICE_TOKEN), sub="jo", created=1.0))

    def test_a_device_token_opens_only_device_routes(self):
        self.a_live_device()
        for route in (route for route in routes.ROUTES if route.auth in ("session", "worker")):
            for headers in ({"Authorization": f"Bearer {DEVICE_TOKEN}"}, {"Cookie": f"darkroom_session={DEVICE_TOKEN}"}):
                with self.subTest(route=f"{route.method} {route.path}", headers=headers):
                    sent = {**headers, "Origin": "https://darkroom.example"} if route.method != "GET" else headers
                    status, _, body = fetch(self.base, route.method, route.path, sent, None if route.method == "GET" else b"{}")
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body), {"error": "sign in required"})

    def test_session_cookies_and_the_worker_token_never_open_device_routes(self):
        self.a_live_device()
        found = [route for route in routes.ROUTES if route.auth == "device"]
        self.assertTrue(found)
        for route in found:
            for headers in ({"Cookie": "darkroom_session=some-session-token"}, {"Authorization": f"Bearer {WORKER_TOKEN}"},
                            {"Authorization": DEVICE_TOKEN}, {"Authorization": f"Bearer {DEVICE_TOKEN[:-1]}"}, {"X-Device-Token": DEVICE_TOKEN}):
                with self.subTest(route=f"{route.method} {route.path}", headers=headers):
                    status, _, body = fetch(self.base, route.method, route.path, {"Content-Length": "2", **headers}, b"{}")
                    self.assertEqual(status, 401)
                    self.assertEqual(json.loads(body), {"error": "sign in required"})

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
        self.assertNotIn("mcp", routes.AUTH_KINDS)
        retired = (*routes.ROUTES, routes.Route("GET", "/api/retired", never_runs, "mcp"))
        with mock.patch.object(routes, "ROUTES", retired):
            self.assertFalse(all(route.auth in routes.AUTH_KINDS for route in routes.ROUTES))
            status, _, _ = fetch(self.base, "GET", "/api/retired")
        self.assertEqual(status, 401)


class NoAuthModeTests(unittest.TestCase):
    def setUp(self):
        guarded = tuple(dataclasses.replace(route, handler=never_runs) for route in routes.ROUTES)
        patch = mock.patch.object(routes, "ROUTES", guarded)
        patch.start()
        self.addCleanup(patch.stop)
        self.base, _ = start_server(self, settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_BROWSER_TOKEN=WORKER_TOKEN))

    def test_the_local_owner_shortcut_never_opens_worker_or_device_routes(self):
        for route in (route for route in routes.ROUTES if route.auth in ("worker", "device")):
            with self.subTest(route=f"{route.method} {route.path}"):
                status, _, _ = fetch(self.base, route.method, route.path, {"Cookie": "darkroom_session=anything"}, b"{}")
                self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
