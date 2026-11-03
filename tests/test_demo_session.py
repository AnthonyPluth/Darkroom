import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import run
from darkroom import config, oidc
from darkroom.dates import utc_now
from darkroom.domain import alerts, demo
from darkroom.providers import exiftool
from darkroom.server import auth, routes
from darkroom.server.common import json_response
from darkroom.storage import db
from darkroom.storage.models import AuthSession, User
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, settings_from, start_server
from tests.test_auth_routes import SignedInServer
from tests.test_every_route import PUBLIC

NOW = utc_now().timestamp()
PRODUCTION = {"OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom", "DARKROOM_PUBLIC_URL": "https://darkroom.example"}
NOT_LOOPBACK = ("0.0.0.0", "::", "192.168.1.4", "100.64.0.1", "localhost", "nas.local", "darkroom.example", "")


def handled(request):
    return json_response({"handled": True})


class DemoSessionCase(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.demo = settings_from(DARKROOM_DEMO="1")
        with db.session(self.engine) as opened:
            self.token = demo.sign_in(opened, self.demo, NOW)
        self.cookie = {"Cookie": f"{auth.SESSION_COOKIE}={self.token}"}

    def server(self, settings):
        base, _ = start_server(self, settings, self.engine)
        return base

    def sessions(self):
        with db.session(self.engine) as opened:
            return opened.query(AuthSession).count()


class DemoSessionTests(DemoSessionCase):
    def test_the_demo_member_is_signed_in_on_a_demo_server_bound_to_loopback(self):
        for host in ("127.0.0.1", "::1", "[::1]", "127.0.0.2"):
            with self.subTest(host=host):
                base = self.server(settings_from(DARKROOM_DEMO="1", DARKROOM_HOST=host))
                status, _, body = fetch(base, "GET", "/api/me", self.cookie)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"sub": "demo:avery", "email": "avery@example.com", "name": "Avery Example", "groups": ["parents"]})
                self.assertEqual(fetch(base, "GET", "/api/me")[0], 401)
                self.assertEqual(fetch(base, "GET", "/api/me", {"Cookie": f"{auth.SESSION_COOKIE}={self.token[:-1]}x"})[0], 401)

    def test_an_inherited_oidc_allowlist_does_not_lock_the_demo_member_out_nor_let_a_production_server_in(self):
        for groups in ("admins", ""):
            with self.subTest(groups=groups):
                base = self.server(settings_from(DARKROOM_DEMO="1", OIDC_ALLOWED_GROUPS=groups, OIDC_ALLOWED_EMAILS=""))
                status, _, body = fetch(base, "GET", "/api/me", self.cookie)
                self.assertEqual((status, json.loads(body)["sub"]), (200, "demo:avery"))
                self.assertEqual(self.sessions(), 1)
        for extra in ({}, {"OIDC_ALLOWED_GROUPS": "parents"}, {"OIDC_ALLOWED_GROUPS": "", "OIDC_ALLOWED_EMAILS": "avery@example.com"}):
            with self.subTest(extra=extra):
                with db.session(self.engine) as opened:
                    token = demo.sign_in(opened, self.demo, NOW)
                base = self.server(settings_from(**PRODUCTION, **extra))
                self.assertEqual(fetch(base, "GET", "/api/me", {"Cookie": f"{auth.SESSION_COOKIE}={token}"})[0], 401)

    def test_without_darkroom_demo_the_same_database_refuses_the_demo_cookie(self):
        base = self.server(settings_from())
        status, _, body = fetch(base, "GET", "/api/me", self.cookie)
        self.assertEqual((status, json.loads(body)), (401, {"error": "sign in required"}))
        self.assertEqual(fetch(base, "GET", "/api/status", self.cookie)[0], 401)

    def test_a_demo_server_listening_beyond_loopback_refuses_the_cookie_and_refuses_to_start(self):
        for host in NOT_LOOPBACK:
            with self.subTest(host=host):
                settings = settings_from(DARKROOM_DEMO="1", DARKROOM_HOST=host)
                refused = config.refuses_to_start(settings)
                self.assertEqual(len(refused), 1)
                self.assertIn("DARKROOM_DEMO=1 only works when DARKROOM_HOST is a loopback address", refused[0])
                self.assertFalse(config.demo_allowed(settings))
                base = self.server(settings)
                self.assertEqual(fetch(base, "GET", "/api/me", self.cookie)[0], 401)
        with contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.dict(os.environ, {"DARKROOM_DEMO": "1", "DARKROOM_HOST": "0.0.0.0"}, clear=True), \
                mock.patch.object(run.handler, "make_server") as made, mock.patch.object(run, "migrate") as migrated:
            self.assertEqual(run.main(["serve"]), 1)
        made.assert_not_called()
        migrated.assert_not_called()
        self.assertIn("loopback", err.getvalue())

    def test_a_production_configuration_refuses_the_demo_cookie_and_ends_the_session_with_or_without_the_flag(self):
        for extra in ({}, {"DARKROOM_DEMO": "1"}):
            with self.subTest(extra=extra):
                settings = settings_from(**PRODUCTION, **extra)
                self.assertFalse(config.demo_allowed(settings))
                self.assertEqual(len(config.refuses_to_start(settings)), 1 if extra else 0)
                with db.session(self.engine) as opened:
                    token = demo.sign_in(opened, self.demo, NOW)
                before = self.sessions()
                base = self.server(settings)
                self.assertEqual(fetch(base, "GET", "/api/me", {"Cookie": f"{auth.SESSION_COOKIE}={token}"})[0], 401)
                self.assertEqual(self.sessions(), before - 1)
        with contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.dict(os.environ, {**PRODUCTION, "DARKROOM_DEMO": "1"}, clear=True), \
                mock.patch.object(run.handler, "make_server") as made, mock.patch.object(run, "migrate"):
            self.assertEqual(run.main(["serve"]), 1)
        made.assert_not_called()
        self.assertIn("DARKROOM_DEMO=1 cannot be combined with OIDC sign-in", err.getvalue())

    def test_demo_mode_and_no_auth_together_sign_nobody_in_and_refuse_to_start(self):
        settings = settings_from(DARKROOM_DEMO="1", DARKROOM_ALLOW_NO_AUTH="1")
        self.assertEqual(config.refuses_to_start(settings), [config.NO_AUTH_WITH_DEMO])
        self.assertFalse(config.demo_allowed(settings) or config.no_auth_allowed(settings))
        base = self.server(settings)
        self.assertEqual(fetch(base, "GET", "/api/me")[0], 401)
        self.assertEqual(fetch(base, "GET", "/api/me", self.cookie)[0], 401)

    def test_a_real_session_is_refused_in_demo_mode_and_accepted_in_production(self):
        production = settings_from(**PRODUCTION)
        with db.session(self.engine) as opened:
            oidc.remember_user(opened, oidc.Identity("user-1", "jo@example.test", "Jo Example", ("parents",), True), NOW)
            real = oidc.new_session(opened, production, "user-1", NOW)
            self.assertEqual(oidc.session_user(opened, production, real, NOW).sub, "user-1")
            self.assertEqual(oidc.session_user(opened, self.demo, real, NOW), None)
            self.assertIsNone(opened.get(AuthSession, oidc.hash_token(real)))
            self.assertEqual(oidc.session_user(opened, self.demo, self.token, NOW).sub, "demo:avery")
            self.assertIsNone(oidc.session_user(opened, production, self.token, NOW))

    def test_the_every_route_guard_holds_in_demo_mode(self):
        guarded = tuple(routes.Route(route.method, route.path, handled, route.auth) for route in routes.ROUTES)
        patch = mock.patch.object(routes, "ROUTES", guarded)
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual({f"{r.method} {r.path}" for r in routes.ROUTES if r.auth == "public"}, PUBLIC)
        base = self.server(self.demo)
        for route in [r for r in routes.ROUTES if r.auth == "session"]:
            with self.subTest(route=f"{route.method} {route.path}"):
                body = None if route.method == "GET" else b"{}"
                origin = {} if route.method == "GET" else {"Origin": base, "Content-Type": "application/json"}
                self.assertEqual(fetch(base, route.method, route.path, origin, body)[0], 401)
                self.assertEqual(fetch(base, route.method, route.path, {"Authorization": f"Bearer {self.token}", **origin}, body)[0], 401)
                status, _, reply = fetch(base, route.method, route.path, {**self.cookie, **origin}, body)
                self.assertEqual((status, json.loads(reply)), (200, {"handled": True}))
                if route.method != "GET":
                    self.assertEqual(fetch(base, route.method, route.path, {**self.cookie, "Origin": "https://evil.example"}, body)[0], 403)
        for route in [r for r in routes.ROUTES if r.auth != "session" and r.auth != "public"]:
            with self.subTest(route=f"{route.method} {route.path}"):
                for headers in (self.cookie, {"Authorization": f"Bearer {self.token}"}):
                    self.assertEqual(fetch(base, route.method, route.path, headers, b"{}")[0], 401)

    def test_logout_ends_the_demo_session_and_the_login_page_explains_demo_mode(self):
        base = self.server(self.demo)
        status, headers, body = fetch(base, "GET", "/auth/login")
        self.assertEqual(status, 503)
        self.assertIn("demo-session.json", body.decode())
        status, headers, _ = fetch(base, "POST", "/auth/logout", {**self.cookie, "Origin": base}, b"")
        self.assertEqual((status, headers["Location"]), (303, "/auth/signed-out"))
        self.assertEqual(fetch(base, "GET", "/api/me", self.cookie)[0], 401)
        self.assertEqual(self.sessions(), 0)

    def test_the_demo_token_is_random_stored_hashed_and_never_logged_or_returned(self):
        replies = []
        with no_leaks(self, self.token, database=self.engine, replies=replies) as canaries:
            with db.session(self.engine) as opened:
                second = demo.sign_in(opened, self.demo, NOW)
            canaries.add(second)
            self.assertNotEqual(second, self.token)
            self.assertGreaterEqual(len(second), 43)
            with db.session(self.engine) as opened:
                hashes = {row.token_hash for row in opened.query(AuthSession)}
            self.assertEqual(hashes, {oidc.hash_token(self.token), oidc.hash_token(second)})
            base = self.server(self.demo)
            for headers in (self.cookie, {}, {"Cookie": f"{auth.SESSION_COOKIE}=wrong-{self.token}"}):
                replies.append(fetch(base, "GET", "/api/me", headers)[2])
            replies.append(fetch(self.server(settings_from()), "GET", "/api/me", self.cookie)[2])

    def test_a_demo_session_is_minted_only_for_a_demo_user_on_a_demo_server(self):
        with db.session(self.engine) as opened:
            with self.assertRaises(ValueError):
                oidc.demo_session(opened, self.demo, oidc.Identity("user-1", None, None, ("parents",)), NOW)
            with self.assertRaises(ValueError):
                oidc.demo_session(opened, settings_from(), demo.MEMBER, NOW)
            with self.assertRaises(ValueError):
                oidc.demo_session(opened, settings_from(**PRODUCTION, DARKROOM_DEMO="1"), demo.MEMBER, NOW)
            self.assertEqual(opened.query(User).count(), 1)
        self.assertEqual(self.sessions(), 1)

    def test_the_notifier_records_in_demo_mode_and_stays_disabled_otherwise(self):
        self.assertIs(alerts.notifier_for(self.demo), alerts.recorded)
        self.assertIs(alerts.notifier_for(settings_from()), alerts.undelivered)
        self.assertIs(alerts.notifier_for(settings_from(**PRODUCTION, DARKROOM_DEMO="1")), alerts.undelivered)
        alerts.RECORDED.clear()
        self.assertEqual(alerts.recorded(self.demo, "Title", "Message", None), alerts.SENT)
        self.assertEqual(alerts.RECORDED, [("Title", "Message", None)])
        for n in range(alerts.RECORDED_LIMIT + 5):
            alerts.recorded(self.demo, str(n), "", None)
        self.assertEqual(len(alerts.RECORDED), alerts.RECORDED_LIMIT)


class ProviderCannotNameADemoUserTests(SignedInServer):
    def test_a_provider_that_names_a_demo_user_is_refused_without_a_session(self):
        for info in ({"sub": "demo:avery", "groups": ["parents"]}, {"sub": "demo:", "email": "x@example.test", "email_verified": True, "groups": ["parents"]}):
            with self.assertRaisesRegex(oidc.SignInFailed, "demo user"):
                oidc.authorize(self.settings, info)
        status, _, body = self.sign_in(sub="demo:avery")
        self.assertEqual(status, 403)
        self.assertIn("named a demo user", body.decode())
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)
            self.assertEqual(opened.query(User).count(), 0)


class CheckConfigTests(unittest.TestCase):
    def check(self, folder, **values):
        base = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "http://127.0.0.1:8000"}
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {**base, **values}, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
            return run.main(["check-config"]), err.getvalue()

    def test_demo_mode_alone_on_loopback_is_a_complete_setup(self):
        with tempfile.TemporaryDirectory() as folder:
            code, err = self.check(folder, DARKROOM_DEMO="1")
        self.assertEqual((code, err), (0, ""))

    def test_demo_mode_with_a_provider_or_beyond_loopback_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            code, err = self.check(folder, DARKROOM_DEMO="1", OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom", DARKROOM_PUBLIC_URL="https://d.example")
            self.assertEqual(code, 1)
            self.assertIn("DARKROOM_DEMO=1 cannot be combined with OIDC sign-in", err)
            code, err = self.check(folder, DARKROOM_DEMO="1", DARKROOM_HOST="192.168.1.4")
            self.assertEqual(code, 1)
            self.assertIn("loopback address (127.0.0.1 or ::1), not 192.168.1.4", err)
            code, err = self.check(folder, DARKROOM_DEMO="1", DARKROOM_ALLOW_NO_AUTH="1")
            self.assertEqual(code, 1)
            self.assertEqual(err.count(config.NO_AUTH_WITH_DEMO), 1)

    def test_loopback_hosts(self):
        for host in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", " ::1 "):
            self.assertTrue(config.loopback_host(host), host)
        for host in NOT_LOOPBACK:
            self.assertFalse(config.loopback_host(host), host)


if __name__ == "__main__":
    unittest.main()
