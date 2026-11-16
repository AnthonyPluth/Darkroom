import contextlib
import dataclasses
import io
import json
import os
import sqlite3
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import delete
from sqlalchemy.exc import OperationalError

import run
from darkroom import oidc
from darkroom.server import auth, common, handler, jobs, routes, wizard
from darkroom.server.api import wizard as setup
from darkroom.server.common import Request
from darkroom.storage import db, stored_settings
from darkroom.storage.models import Setting, StoredSetting, User
from darkroom.storage.settings_keys import SETUP_CLAIM, SETUP_LOCKED, SETUP_SIGN_IN, SETUP_TESTED
from tests.fakes.oidc_provider import CLIENT_ID, CLIENT_SECRET, FakeProvider
from tests.privacy import no_leaks
from tests.shared import cookies_set, fetch, own_database, own_database_url, settings_from, start_server

KEY = "wizard-test-secret-key-0123456789"
RESEND_KEY = "re_resend-key-canary-4b5c6d7e"
SECRET_CANARY = "client-secret-canary-7f3a9c1e"


class WizardCase(unittest.TestCase):
    env = {"DARKROOM_WIZARD": "1"}

    @classmethod
    def setUpClass(cls):
        cls.provider = FakeProvider(client_secret=CLIENT_SECRET).start()
        cls.addClassCleanup(cls.provider.stop)

    def setUp(self):
        oidc.forget_caches()
        self.provider.reset()
        self.engine = own_database(self)
        self.base, self.settings = start_server(self, settings_from(DARKROOM_SECRET_KEY=KEY, **self.env), self.engine)
        self.replies = []
        self.cookie = ""

    def call(self, method, path, body=None, cookies=None, headers=None):
        sent = {"Content-Type": "application/json", "Origin": self.base} if method != "GET" else {}
        jar = cookies if cookies is not None else ({wizard.CLAIM_COOKIE: self.cookie} if self.cookie else {})
        if jar:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
        sent.update(headers or {})
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        status, reply_headers, reply = fetch(self.base, method, path, sent, data)
        self.replies.append(reply)
        return status, reply_headers, reply

    def json_call(self, method, path, body=None, **extra):
        status, _, reply = self.call(method, path, body, **extra)
        return status, json.loads(reply)

    def claim(self):
        status, headers, reply = self.call("POST", "/api/setup/claim")
        self.assertEqual(status, 200, reply)
        self.cookie = cookies_set(headers)[wizard.CLAIM_COOKIE]
        return json.loads(reply)

    def propose(self, **changes):
        body = {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID, "oidc_client_secret": CLIENT_SECRET,
                "oidc_allowed_groups": ["parents"], "oidc_allowed_emails": [], **changes}
        body = {name: value for name, value in body.items() if value is not None}
        status, reply = self.json_call("POST", "/api/setup/sign-in", body)
        self.assertEqual(status, 200, reply)
        return reply

    def try_sign_in(self, code="test-code-canary-1a2b3c", **grant):
        status, reply = self.json_call("POST", "/api/setup/sign-in/test")
        self.assertEqual(status, 200, reply)
        query = {name: values[0] for name, values in parse_qs(urlsplit(reply["location"]).query).items()}
        self.assertEqual(query["redirect_uri"], self.base + "/auth/callback")
        self.provider.issue(code, nonce=query["nonce"], challenge=query["code_challenge"], **{"groups": ["parents"], **grant})
        jar = {wizard.TEST_COOKIE: query["state"], wizard.CLAIM_COOKIE: self.cookie}
        return self.call("GET", f"/auth/callback?code={code}&state={query['state']}", cookies=jar)

    def stored(self):
        return stored_settings.stored_values(self.engine)

    def restarted(self):
        return stored_settings.effective(settings_from(DARKROOM_SECRET_KEY=KEY, **self.env), self.engine).settings


class WindowTests(WizardCase):
    def test_the_first_visitor_claims_the_window_and_a_second_browser_is_refused(self):
        self.assertEqual(self.json_call("GET", "/api/setup")[0], 401)
        state = self.claim()
        self.assertIsNone(state["proposal"])
        self.assertEqual(state["redirect_uri"], self.base + "/auth/callback")
        self.assertEqual({entry["source"] for entry in state["settings"]}, {"default"})
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={}), (409, {"error": setup.CLAIMED_ELSEWHERE}))
        self.assertEqual(self.json_call("POST", "/api/setup/address", {"public_url": "http://tower.local:8000"}, cookies={})[0], 401)
        self.assertEqual(self.json_call("POST", "/api/setup/claim")[0], 200)
        self.assertEqual(self.json_call("GET", "/api/setup")[0], 200)

    def test_a_non_private_or_proxied_visitor_is_refused(self):
        self.claim()
        cookie = f"{wizard.CLAIM_COOKIE}={self.cookie}"
        for client in ("198.51.100.7", "2001:db8::1", "100.64.0.9", "169.254.1.1", "not-an-address", ""):
            with self.subTest(client=client):
                request = Request("GET", "/api/setup", {}, {"Cookie": cookie, "Host": "127.0.0.1"}, b"", self.settings, self.engine, client=client)
                self.assertEqual(handler.dispatch(request).status, 401)
        for allowed in ("192.168.1.20", "10.1.2.3", "::ffff:172.16.0.4", "fd00::5", "::1"):
            with self.subTest(client=allowed):
                request = Request("GET", "/api/setup", {}, {"Cookie": cookie, "Host": "127.0.0.1"}, b"", self.settings, self.engine, client=allowed)
                self.assertEqual(handler.dispatch(request).status, 200)
        for headers in ({"X-Forwarded-For": "192.168.1.20"}, {"Forwarded": "for=192.168.1.20"}, {"X-Real-IP": "10.0.0.2"}, {"Host": "rebound.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.json_call("GET", "/api/setup", headers=headers)[0], 401)

    def test_the_window_is_closed_once_anyone_has_signed_in(self):
        self.claim()
        with db.session(self.engine) as opened:
            opened.add(User(sub="user-1"))
        self.assertEqual(self.json_call("GET", "/api/setup")[0], 401)
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={})[0], 401)

    def test_reopen_setup_clears_the_claim_and_the_lock(self):
        url = own_database_url(self)
        engine = db.make_engine(url)
        self.addCleanup(engine.dispose)
        db.Base.metadata.create_all(engine)
        with db.session(engine) as opened:
            opened.add_all([Setting(key=SETUP_CLAIM, value="x"), Setting(key=SETUP_LOCKED, value="1")])
        with mock.patch.dict(os.environ, {"DATABASE_URL": url}, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["reopen-setup"]), 0)
        self.assertIn("reopened", out.getvalue())
        with db.session(engine) as opened:
            self.assertEqual(opened.query(Setting).count(), 0)


class StepTests(WizardCase):
    def test_addresses_are_checked_and_stored(self):
        self.claim()
        refused = {"ftp://tower.local": "must be an http:// or https:// address", "http://jo@tower.local": "must be an http:// or https:// address",
                   "https://tower.local/?x=1": "must be an http:// or https:// address", "http://photos.example.com": "must use https://",
                   "https://tower.local/darkroom": "without a path", "https://tower.local:99999": "must be an http:// or https:// address"}
        for value, message in refused.items():
            with self.subTest(value=value):
                status, reply = self.json_call("POST", "/api/setup/address", {"public_url": value})
                self.assertEqual(status, 400)
                self.assertIn(message, reply["error"])
                self.assertNotIn(value, reply["error"])
        status, reply = self.json_call("POST", "/api/setup/address", {"public_url": "https://photos.example.com/"})
        self.assertEqual(status, 200)
        entry = next(entry for entry in reply["settings"] if entry["variable"] == "DARKROOM_PUBLIC_URL")
        self.assertEqual(entry, {"variable": "DARKROOM_PUBLIC_URL", "source": "stored", "set": True, "value": "https://photos.example.com"})
        self.assertEqual(reply["redirect_uri"], "https://photos.example.com/auth/callback")
        self.assertEqual(self.json_call("POST", "/api/setup/immich", {"immich_url": "http://immich.lan:2283/"})[0], 200)
        self.assertEqual(self.stored()["IMMICH_URL"], ("http://immich.lan:2283", None))
        self.assertEqual(self.json_call("POST", "/api/setup/immich", {"immich_url": 5})[0], 400)

    def test_alerts_seal_the_key_and_never_echo_it(self):
        self.claim()
        with no_leaks(self, RESEND_KEY, database=self.engine, replies=self.replies):
            self.assertEqual(self.json_call("POST", "/api/setup/alerts", {"alert_email_from": "a@example.com", "alert_email_to": "b@example.com"}),
                             (400, {"error": "resend_api_key is required"}))
            status, reply = self.json_call("POST", "/api/setup/alerts", {"resend_api_key": RESEND_KEY, "alert_email_from": "alerts@example.com", "alert_email_to": "Jo <jo@example.com>"})
            self.assertEqual((status, reply["error"]), (400, "alert_email_to must be one email address, like alerts@example.com"))
            status, reply = self.json_call("POST", "/api/setup/alerts", {"resend_api_key": RESEND_KEY, "alert_email_from": "alerts@example.com", "alert_email_to": "jo@example.com"})
            self.assertEqual(status, 200)
            entry = next(entry for entry in reply["settings"] if entry["variable"] == "RESEND_API_KEY")
            self.assertEqual(entry, {"variable": "RESEND_API_KEY", "source": "stored", "set": True, "value": None})
            self.assertEqual(self.json_call("POST", "/api/setup/alerts", {"alert_email_from": "alerts@example.com", "alert_email_to": "kim@example.com"})[0], 200)
            for sealed in ("enc:v1:abc", "has space"):
                self.assertEqual(self.json_call("POST", "/api/setup/alerts", {"resend_api_key": sealed, "alert_email_from": "a@example.com", "alert_email_to": "b@example.com"})[0], 400)
            loaded = stored_settings.effective(self.settings, self.engine).settings
        self.assertEqual(loaded.alert_email_to, "kim@example.com")
        self.assertTrue(loaded.resend_api_key.startswith("enc:v1:"))

    def test_a_stranger_sees_nothing(self):
        self.claim()
        self.json_call("POST", "/api/setup/alerts", {"resend_api_key": RESEND_KEY, "alert_email_from": "alerts@example.com", "alert_email_to": "jo@example.com"})
        self.propose()
        self.replies.clear()
        with no_leaks(self, RESEND_KEY, SECRET_CANARY, "alerts@example.com", self.cookie, replies=self.replies):
            for method, path in (("GET", "/api/setup"), ("POST", "/api/setup/sign-in/test"), ("POST", "/api/setup/sign-in")):
                with self.subTest(path=path):
                    self.assertEqual(self.call(method, path, {}, cookies={wizard.CLAIM_COOKIE: "guess"})[0], 401)
            status, _, body = self.call("GET", "/api/setup")
            self.assertEqual(status, 200)
            self.assertNotIn(RESEND_KEY, body.decode())
            self.replies.remove(body)


class EnvironmentTests(WizardCase):
    env = {"DARKROOM_WIZARD": "1", "IMMICH_URL": "http://immich.lan:2283", "OIDC_ALLOWED_EMAILS": "jo@example.com"}

    def test_a_setting_from_the_environment_is_refused_and_never_shown(self):
        state = self.claim()
        entry = next(entry for entry in state["settings"] if entry["variable"] == "IMMICH_URL")
        self.assertEqual(entry, {"variable": "IMMICH_URL", "source": "environment", "set": True, "value": None})
        status, reply = self.json_call("POST", "/api/setup/immich", {"immich_url": "http://other.lan"})
        self.assertEqual((status, reply["error"]), (409, "IMMICH_URL is set in the environment, which always wins over the wizard; change it there"))
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in", {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID})[0], 409)
        self.assertEqual(self.stored(), {})


class WizardOffTests(WizardCase):
    env = {"OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom", "OIDC_ALLOWED_GROUPS": "parents"}

    def test_with_the_wizard_off_nothing_opens_and_the_environment_is_used_as_before(self):
        for method, path in (("POST", "/api/setup/claim"), ("GET", "/api/setup"), ("POST", "/api/setup/address")):
            self.assertEqual(self.json_call(method, path, {})[0], 401)
        self.assertEqual(self.stored(), {})
        self.assertEqual(stored_settings.effective(self.settings, self.engine).settings, self.settings)
        self.assertFalse(wizard.open_by_settings(self.settings))
        self.assertFalse(wizard.open_by_settings(settings_from(DARKROOM_SECRET_KEY=KEY)))

    def test_the_local_owner_without_sign_in_gets_no_wizard(self):
        local = settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_SECRET_KEY=KEY, DARKROOM_WIZARD="1")
        request = Request("GET", "/api/setup", {}, {"Host": "127.0.0.1"}, b"", local, self.engine, client="127.0.0.1")
        response = handler.dispatch(request)
        self.assertEqual((response.status, json.loads(response.body)), (401, {"error": "sign in required"}))


class MemberTests(WizardCase):
    env = {"DARKROOM_WIZARD": "1", "OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom"}

    def test_a_signed_in_member_cannot_use_any_setup_route(self):
        with db.session(self.engine) as opened:
            oidc.remember_user(opened, oidc.Identity("user-1", "jo@example.test", "Jo Example", ("parents",), True), 1.0)
            token = oidc.new_session(opened, self.settings, "user-1", common.now())
        jar = {"darkroom_session": token}
        self.assertEqual(self.json_call("GET", "/api/me", cookies=jar)[0], 200)
        bodies = {"/api/setup/address": {"public_url": "https://evil.example.com"}, "/api/setup/immich": {"immich_url": "https://evil.example.com"},
                  "/api/setup/sign-in": {"oidc_issuer": "https://evil.example.com", "oidc_client_id": "x"}}
        found = [route for route in routes.ROUTES if route.auth in routes.SETUP_KINDS]
        self.assertEqual(len(found), 8)
        for route in found:
            with self.subTest(route=f"{route.method} {route.path}"):
                status, reply = self.json_call(route.method, route.path, bodies.get(route.path, {}), cookies=jar)
                self.assertEqual((status, reply), (401, {"error": "sign in required"}))
        self.assertEqual(self.stored(), {})


class TestSignInTests(WizardCase):
    def test_sign_in_settings_take_effect_only_after_a_successful_test(self):
        self.claim()
        with no_leaks(self, SECRET_CANARY, "test-code-canary-1a2b3c", replies=self.replies):
            state = self.propose(oidc_allowed_emails=["Jo@Example.com"])
            self.assertEqual(state["proposal"], {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID, "has_client_secret": True,
                                                 "oidc_allowed_groups": ["parents"], "oidc_allowed_emails": ["jo@example.com"]})
            self.assertEqual(self.stored(), {})
            self.assertEqual(self.propose(oidc_client_secret=None)["proposal"]["has_client_secret"], True)
            status, headers, body = self.try_sign_in()
            self.assertEqual((status, headers["Location"], body), (303, "/setup", b""))
        baked = headers.get_all("Set-Cookie")
        self.assertFalse(any(line.startswith(wizard.CLAIM_COOKIE + "=") for line in baked))
        self.assertIn(f"{wizard.TEST_COOKIE}=; Path=/auth; Max-Age=0", "\n".join(baked))
        self.assertRegex(baked[0], rf"^{auth.SESSION_COOKIE}=[^;]+; Path=/; Max-Age={oidc.COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax$")
        loaded = stored_settings.effective(self.settings, self.engine).settings
        self.assertEqual((loaded.oidc_issuer, loaded.oidc_client_id, loaded.oidc_client_secret), (self.provider.base, CLIENT_ID, CLIENT_SECRET))
        self.assertEqual((loaded.oidc_allowed_groups, loaded.oidc_allowed_emails), (("parents",), ()))
        self.assertIsNone(self.stored()["OIDC_CLIENT_SECRET"][0])
        self.assertEqual(self.json_call("GET", "/api/setup")[0], 401)
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={})[0], 401)
        with db.session(self.engine) as opened:
            self.assertEqual((opened.query(User).count(), opened.query(StoredSetting).count()), (1, 5))
            self.assertIsNone(opened.get(Setting, SETUP_SIGN_IN))

    def test_a_browser_that_signs_in_at_the_provider_comes_back_to_the_setup_page_signed_in(self):
        self.claim()
        self.propose()
        self.provider.signs_in = {"sub": "owner-1", "email": "owner@example.com", "groups": ["parents"]}
        status, reply = self.json_call("POST", "/api/setup/sign-in/test")
        self.assertEqual(status, 200, reply)
        target = urlsplit(reply["location"])
        status, headers, _ = fetch(self.provider.base, "GET", f"{target.path}?{target.query}", {}, None)
        self.assertEqual(status, 302)
        back = urlsplit(headers["Location"])
        self.assertEqual(f"{back.scheme}://{back.netloc}{back.path}", self.base + "/auth/callback")
        state = parse_qs(target.query)["state"][0]
        status, headers, body = self.call("GET", f"{back.path}?{back.query}", cookies={wizard.TEST_COOKIE: state, wizard.CLAIM_COOKIE: self.cookie})
        self.assertEqual((status, headers["Location"], body), (303, "/setup", b""))
        jar = {wizard.CLAIM_COOKIE: self.cookie, auth.SESSION_COOKIE: cookies_set(headers)[auth.SESSION_COOKIE]}
        status, state_now = self.json_call("GET", "/api/setup", cookies=jar)
        self.assertEqual((status, state_now["phase"], state_now["tested_email"]), (200, "after_test", "owner@example.com"))

    def test_an_account_the_proposal_does_not_allow_saves_nothing(self):
        self.claim()
        self.propose(oidc_allowed_groups=["admins"])
        status, _, body = self.try_sign_in(groups=["family"])
        self.assertEqual(status, 403)
        self.assertIn("not allowed by the proposed settings", body.decode())
        self.assertEqual(self.stored(), {})
        self.assertEqual(self.json_call("GET", "/api/setup")[0], 200)

    def test_empty_groups_are_tested_as_the_default_group(self):
        self.claim()
        self.propose(oidc_allowed_groups=[], oidc_allowed_emails=["kim@example.com"])
        self.assertEqual(self.try_sign_in(groups=["family"], email="kim@example.com", email_verified=False)[0], 403)
        self.assertEqual(self.try_sign_in(code="test-code-two-9f8e7d")[0], 303)
        self.assertEqual(self.restarted().oidc_allowed_groups, ("parents",))

    def test_a_proposal_changed_during_the_test_saves_nothing(self):
        self.claim()
        self.propose()
        status, reply = self.json_call("POST", "/api/setup/sign-in/test")
        query = {name: values[0] for name, values in parse_qs(urlsplit(reply["location"]).query).items()}
        self.propose(oidc_allowed_groups=["everyone"])
        self.provider.issue("test-code-late-1234", nonce=query["nonce"], challenge=query["code_challenge"], groups=["parents"])
        jar = {wizard.TEST_COOKIE: query["state"], wizard.CLAIM_COOKIE: self.cookie}
        status, _, body = self.call("GET", f"/auth/callback?code=test-code-late-1234&state={query['state']}", cookies=jar)
        self.assertEqual(status, 403)
        self.assertIn("changed during the test", body.decode())
        self.assertEqual(self.stored(), {})

    def test_the_test_needs_a_proposal_an_address_and_the_claimant(self):
        self.claim()
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in/test"), (409, {"error": setup.NO_PROPOSAL}))
        status, reply = self.json_call("POST", "/api/setup/sign-in", {"oidc_issuer": "http://id.example.com", "oidc_client_id": CLIENT_ID})
        self.assertEqual((status, reply["error"]), (400, "oidc_issuer must use https:// for an address reachable beyond the LAN"))
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in", {"oidc_issuer": self.provider.base, "oidc_client_id": "has space"})[0], 400)
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in", {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID, "oidc_allowed_groups": ["a,b"]})[0], 400)
        self.propose()
        status, reply = self.json_call("POST", "/api/setup/sign-in/test")
        query = {name: values[0] for name, values in parse_qs(urlsplit(reply["location"]).query).items()}
        status, _, body = self.call("GET", f"/auth/callback?code=x&state={query['state']}", cookies={wizard.TEST_COOKIE: query["state"]})
        self.assertEqual(status, 403)
        self.assertIn("did not start here", body.decode())
        self.assertEqual(self.call("GET", f"/auth/callback?code=x&state={query['state']}", cookies={})[0], 404)

    def test_the_test_sign_in_runs_only_from_the_confirmed_address(self):
        self.claim()
        self.assertEqual(self.json_call("POST", "/api/setup/address", {"public_url": "https://photos.example.com"})[0], 200)
        self.propose()
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in/test"), (409, {"error": setup.ELSEWHERE}))
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in/test", headers={"Host": "PHOTOS.example.com"}),
                         (403, {"error": "cross-site requests are refused"}))
        status, reply = self.json_call("POST", "/api/setup/sign-in/test", headers={"Host": "PHOTOS.example.com", "Origin": "https://photos.example.com"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["location"].startswith(self.provider.base))
        self.assertEqual(self.json_call("POST", "/api/setup/address", {"public_url": self.base})[0], 200)
        self.assertEqual(self.try_sign_in()[0], 303)
        self.assertEqual(self.restarted().public_url, self.base)
        self.assertFalse(wizard.same_address(Request("GET", "/", {}, {}, b"", self.settings), self.base))
        self.assertFalse(wizard.same_address(Request("GET", "/", {}, {"Host": "127.0.0.1"}, b"", self.settings), "http://[::1"))

    def test_an_unreachable_provider_is_a_fixed_message(self):
        self.claim()
        self.propose(oidc_issuer="http://127.0.0.1:9")
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in/test"), (502, {"error": "the test sign-in could not start: the provider could not be reached"}))


class BlankGroupsTests(WizardCase):
    env = {"DARKROOM_WIZARD": "1", "OIDC_ALLOWED_GROUPS": ""}

    def test_a_blank_groups_variable_is_tested_as_no_groups_as_after_a_restart(self):
        self.claim()
        status, reply = self.json_call("POST", "/api/setup/sign-in", {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID})
        self.assertEqual((status, reply["error"]), (400, "set OIDC_ALLOWED_GROUPS or OIDC_ALLOWED_EMAILS so someone can sign in"))
        self.propose(oidc_allowed_groups=[], oidc_allowed_emails=["kim@example.com"])
        self.assertEqual(self.try_sign_in(groups=["parents"])[0], 403)
        self.assertEqual(self.stored(), {})
        self.assertEqual(self.try_sign_in(code="test-code-kim-5a6b7c", groups=[], email="kim@example.com")[0], 303)
        restarted = self.restarted()
        self.assertEqual((restarted.oidc_allowed_groups, restarted.oidc_allowed_emails), ((), ("kim@example.com",)))
        self.assertTrue(oidc.allowed(restarted, "kim@example.com", True, ()))
        self.assertFalse(oidc.allowed(restarted, "jo@example.test", True, ("parents",)))


SETUP_ROUTES = tuple((route.method, route.path) for route in routes.ROUTES if route.auth in routes.SETUP_KINDS)
TESTED_EMAIL = "jo@example.test"
OTHER_EMAIL = "kim.other@example.test"
REFUSED = (401, {"error": "sign in required"})


class AfterTestCase(WizardCase):
    def setUp(self):
        super().setUp()
        self.claim()
        state = self.propose()
        self.assertEqual((state["phase"], state["tested_email"]), ("before_test", None))
        status, headers, _ = self.try_sign_in()
        self.assertEqual((status, headers["Location"]), (303, "/setup"))
        self.session = cookies_set(headers)[auth.SESSION_COOKIE]
        self.replies.clear()

    def pair(self):
        return {wizard.CLAIM_COOKIE: self.cookie, auth.SESSION_COOKIE: self.session}

    def every_route(self, jar, headers=None):
        return {f"{method} {path}": self.json_call(method, path, {}, cookies=jar, headers=headers) for method, path in SETUP_ROUTES}

    def assert_refused_everywhere(self, jar, headers=None):
        for route, answer in self.every_route(jar, headers).items():
            with self.subTest(route=route, jar=sorted(jar), headers=headers):
                self.assertEqual(answer, REFUSED)

    def assert_closed_for_good(self):
        for jar in (self.pair(), {wizard.CLAIM_COOKIE: self.cookie}, {auth.SESSION_COOKIE: self.session}, {}):
            self.assert_refused_everywhere(jar)
        with mock.patch.dict(os.environ, {"DATABASE_URL": self.engine.url.render_as_string(hide_password=False)}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["reopen-setup"]), 0)
        self.assertEqual(out.getvalue().strip(), run.CLOSED_AFTER_TEST)
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={}), REFUSED)
        self.assert_refused_everywhere(self.pair())

    def direct(self, path, jar, settings=None, client="127.0.0.1", method="GET", **headers):
        cookie = "; ".join(f"{name}={value}" for name, value in jar.items())
        sent = {"Cookie": cookie, "Host": self.base.removeprefix("http://"), **headers}
        request = Request(method, path, {}, sent, b"{}" if method != "GET" else b"", settings or self.settings, self.engine, client=client)
        return handler.dispatch(request).status


class AfterTestTests(AfterTestCase):
    def test_the_tested_pair_carries_on_and_sign_in_works_without_a_restart(self):
        status, state = self.json_call("GET", "/api/setup", cookies=self.pair())
        self.assertEqual(status, 200)
        self.assertEqual((state["phase"], state["tested_email"], state["proposal"]), ("after_test", TESTED_EMAIL, None))
        self.assertEqual(self.json_call("POST", "/api/setup/immich", {"immich_url": "http://immich.lan:2283"}, cookies=self.pair())[0], 200)
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies=self.pair())[0], 200)
        jar = {auth.SESSION_COOKIE: self.session}
        self.assertEqual(self.json_call("GET", "/api/me", cookies=jar), (200, {"sub": "user-1", "email": TESTED_EMAIL, "name": "Jo Example", "groups": ["parents"]}))
        self.assertEqual(self.json_call("GET", "/api/people", cookies=jar)[0], 200)
        self.assertIsNone(self.settings.oidc_issuer or None)
        with db.session(self.engine) as opened:
            tested = json.loads(opened.get(Setting, SETUP_TESTED).value)
            self.assertEqual(set(tested), {"sub", "email", "session", "at"})
            self.assertNotEqual(tested["session"], self.session)
            self.assertEqual(oidc.find_session(opened, self.session).token_hash, tested["session"])
            self.assertIsNotNone(oidc.find_session(opened, self.session).refresh_token_sealed)

    def test_anything_but_the_pair_from_this_network_gets_401_on_every_setup_route(self):
        with db.session(self.engine) as opened:
            second = oidc.new_session(opened, self.settings, "user-1", common.now())
        with no_leaks(self, self.session, self.cookie, second, TESTED_EMAIL, SECRET_CANARY, replies=self.replies):
            self.assert_refused_everywhere({wizard.CLAIM_COOKIE: self.cookie})
            self.assert_refused_everywhere({auth.SESSION_COOKIE: self.session})
            self.assert_refused_everywhere({wizard.CLAIM_COOKIE: self.cookie, auth.SESSION_COOKIE: second})
            self.assert_refused_everywhere({wizard.CLAIM_COOKIE: "guess", auth.SESSION_COOKIE: self.session})
            for headers in ({"X-Forwarded-For": "192.168.1.20"}, {"Forwarded": "for=10.0.0.2"}, {"X-Real-IP": "10.0.0.2"}, {"Host": "rebound.example"}):
                self.assert_refused_everywhere(self.pair(), headers)
            for client in ("198.51.100.7", "2001:db8::1", "100.64.0.9", ""):
                for method, path in SETUP_ROUTES:
                    with self.subTest(client=client, route=path):
                        self.assertEqual(self.direct(path, self.pair(), client=client, method=method, Origin=self.base), 401)
            self.assertEqual(self.json_call("POST", "/api/setup/immich", {"immich_url": "http://x.lan"}, cookies=self.pair(), headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.json_call("GET", "/api/setup", cookies=self.pair())[0], 200)
        with db.session(self.engine) as opened:
            oidc.remember_user(opened, oidc.Identity("user-2", OTHER_EMAIL, "Kim Other", ("parents",), True), common.now())
            other = oidc.new_session(opened, self.settings, "user-2", common.now())
        self.replies.clear()
        with no_leaks(self, self.session, self.cookie, other, TESTED_EMAIL, replies=self.replies):
            self.assert_refused_everywhere({wizard.CLAIM_COOKIE: self.cookie, auth.SESSION_COOKIE: other})
            self.assert_refused_everywhere(self.pair())

    def test_a_second_claim_after_the_test_is_refused(self):
        with db.session(self.engine) as opened:
            held = opened.get(Setting, SETUP_CLAIM).value
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={}), REFUSED)
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={wizard.CLAIM_COOKIE: self.cookie}), REFUSED)
        status, headers, _ = self.call("POST", "/api/setup/claim", cookies=self.pair())
        self.assertEqual((status, headers.get_all("Set-Cookie")), (200, None))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Setting, SETUP_CLAIM).value, held)

    def test_the_address_and_the_sign_in_are_settled_by_the_test(self):
        for path, body in (("/api/setup/address", {"public_url": "http://other.lan:8000"}), ("/api/setup/sign-in", {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID}),
                           ("/api/setup/sign-in/test", {})):
            with self.subTest(path=path):
                self.assertEqual(self.json_call("POST", path, body, cookies=self.pair()), (409, {"error": setup.SETTLED}))
        self.assertNotIn("DARKROOM_PUBLIC_URL", self.stored())
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Setting, SETUP_SIGN_IN))

    def test_a_test_that_passes_while_a_step_is_saved_settles_that_step(self):
        with db.session(self.engine) as opened:
            tested = opened.get(Setting, SETUP_TESTED).value
            opened.delete(opened.get(Setting, SETUP_TESTED))
        stored_before = self.stored()

        def passes_meanwhile(request):
            with db.session(self.engine) as opened:
                if opened.get(Setting, SETUP_TESTED) is None:
                    opened.add(Setting(key=SETUP_TESTED, value=tested))
            return None

        steps = (("/api/setup/address", {"public_url": "http://other.lan:8000"}), ("/api/setup/sign-in", {"oidc_issuer": self.provider.base, "oidc_client_id": CLIENT_ID}),
                 ("/api/setup/sign-in/test", {}))
        for path, body in steps:
            with self.subTest(path=path):
                with db.session(self.engine) as opened:
                    opened.execute(delete(Setting).where(Setting.key == SETUP_TESTED))
                request = Request("POST", path, {}, {"Host": self.base.removeprefix("http://")}, json.dumps(body).encode(), self.settings, self.engine, client="127.0.0.1")
                handler_of = routes.find("POST", path).handler
                with mock.patch.object(setup, "after_the_test", side_effect=passes_meanwhile):
                    response = handler_of(dataclasses.replace(request, settings=handler.with_stored(request)))
                self.assertEqual((response.status, json.loads(response.body)), (409, {"error": setup.SETTLED}))
        self.assertEqual(self.stored(), stored_before)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Setting, SETUP_SIGN_IN))

    def test_turning_the_wizard_off_and_on_again_reopens_only_the_pair(self):
        off = dataclasses.replace(self.settings, wizard=False)
        self.assertEqual(self.direct("/api/setup", self.pair(), off), 401)
        self.assertEqual(self.direct("/api/setup", self.pair()), 200)
        self.assertEqual(self.direct("/api/setup/claim", {}, method="POST"), 401)
        self.assertEqual(self.direct("/api/setup", {wizard.CLAIM_COOKIE: self.cookie}), 401)
        for refused in (dataclasses.replace(self.settings, allow_no_auth=True), dataclasses.replace(self.settings, demo=True)):
            self.assertEqual(self.direct("/api/setup", self.pair(), refused), 401)


class ClosingTests(AfterTestCase):
    def test_finish_closes_setup_for_good(self):
        status, headers, reply = self.call("POST", "/api/setup/finish", cookies=self.pair())
        self.assertEqual((status, json.loads(reply)), (200, {"ok": True}))
        self.assertIn(f"{wizard.CLAIM_COOKIE}=; Path=/; Max-Age=0", "\n".join(headers.get_all("Set-Cookie")))
        self.assertEqual(self.json_call("GET", "/api/me", cookies={auth.SESSION_COOKIE: self.session})[0], 200)
        self.assert_closed_for_good()

    def test_signing_out_closes_setup_for_good(self):
        self.assertEqual(self.call("POST", "/auth/logout", cookies={auth.SESSION_COOKIE: self.session})[0], 303)
        self.assert_closed_for_good()

    def test_an_expired_tested_session_closes_setup_for_good(self):
        with db.session(self.engine) as opened:
            oidc.find_session(opened, self.session).expires = 1.0
        self.assert_closed_for_good()

    def test_a_failed_recheck_of_the_tested_session_closes_setup_for_good(self):
        with db.session(self.engine) as opened:
            oidc.find_session(opened, self.session).last_checked = 1.0
        self.provider.revoke_all()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            jobs.build(self.settings, self.engine, common.now()).run_one("sessions.recheck", common.now())
        with db.session(self.engine) as opened:
            self.assertIsNone(oidc.find_session(opened, self.session))
        self.assert_closed_for_good()

    def test_another_accounts_sign_in_closes_setup_for_good(self):
        status, headers, _ = self.call("GET", "/auth/login", cookies={})
        self.assertEqual(status, 303)
        query = {name: values[0] for name, values in parse_qs(urlsplit(headers["Location"]).query).items()}
        self.provider.issue("code-other-7c6b5a", nonce=query["nonce"], challenge=query["code_challenge"], sub="user-2", email=OTHER_EMAIL, groups=["parents"])
        status, headers, _ = self.call("GET", f"/auth/callback?code=code-other-7c6b5a&state={query['state']}", cookies={auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 303)
        other = cookies_set(headers)[auth.SESSION_COOKIE]
        self.assertEqual(self.json_call("GET", "/api/me", cookies={auth.SESSION_COOKIE: other})[1]["email"], OTHER_EMAIL)
        self.assert_refused_everywhere({wizard.CLAIM_COOKIE: self.cookie, auth.SESSION_COOKIE: other})
        self.assert_closed_for_good()

    def test_seven_days_after_the_test_setup_closes_for_good(self):
        with db.session(self.engine) as opened:
            tested_at = json.loads(opened.get(Setting, SETUP_TESTED).value)["at"]
        with mock.patch.object(wizard, "now", return_value=tested_at + wizard.AFTER_TEST_SECONDS - 60):
            self.assertEqual(self.json_call("GET", "/api/setup", cookies=self.pair())[0], 200)
        with mock.patch.object(wizard, "now", return_value=tested_at + wizard.AFTER_TEST_SECONDS):
            self.assert_refused_everywhere(self.pair())
        with mock.patch.dict(os.environ, {"DATABASE_URL": self.engine.url.render_as_string(hide_password=False)}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()):
            run.main(["reopen-setup"])
        self.assert_closed_for_good()

    def test_a_damaged_test_record_closes_setup_and_reopens_nothing(self):
        with db.session(self.engine) as opened:
            opened.get(Setting, SETUP_TESTED).value = "{not json"
        self.assert_closed_for_good()
        for value in ('{"sub": "", "email": "", "session": "x", "at": 1}', '{"sub": "a", "email": "", "session": "x", "at": true}', "[]"):
            self.assertIsNone(wizard.read_tested(value))

    def test_finish_needs_a_passed_test(self):
        self.assertEqual(self.json_call("POST", "/api/setup/finish", cookies=self.pair())[0], 200)
        self.assertEqual(self.json_call("POST", "/api/setup/finish", cookies=self.pair()), REFUSED)


class FinishBeforeTheTestTests(WizardCase):
    def test_finish_before_the_test_is_refused_and_changes_nothing(self):
        self.claim()
        self.assertEqual(self.json_call("POST", "/api/setup/finish"), (409, {"error": setup.NOT_TESTED}))
        self.assertEqual(self.json_call("GET", "/api/setup")[1]["phase"], "before_test")


class LiveSettingsTests(WizardCase):
    def test_stored_values_reach_the_running_jobs_without_a_restart(self):
        scheduler = jobs.build(self.settings, self.engine, common.now())
        self.claim()
        self.assertEqual(self.json_call("POST", "/api/setup/immich", {"immich_url": "http://immich.lan:2283"})[0], 200)
        self.assertEqual(self.json_call("POST", "/api/setup/alerts", {"resend_api_key": RESEND_KEY, "alert_email_from": "alerts@example.com", "alert_email_to": "jo@example.com"})[0], 200)
        seen = []

        def not_reached(settings):
            seen.append(settings.immich_url)
            raise jobs.immich.NotConfigured("not reached in this test")

        with mock.patch.object(jobs.immich.Client, "at", side_effect=not_reached):
            scheduler.run_one("immich.reconcile", common.now())
        self.assertEqual(seen, ["http://immich.lan:2283"])
        with mock.patch.object(jobs, "pusher_of", side_effect=lambda settings: seen.append(settings.alert_email_to)) as made:
            scheduler.run_one("alerts.push", common.now())
            scheduler.run_one("alerts.push", common.now())
        self.assertEqual((seen[1:], made.call_count), (["jo@example.com"], 1))

    def test_the_health_check_reads_no_stored_settings(self):
        with mock.patch.object(stored_settings, "effective", side_effect=AssertionError("read")):
            self.assertEqual(self.json_call("GET", "/healthz")[0], 200)
            self.assertEqual(self.json_call("GET", "/api/setup")[0], 500)

    def test_an_unreadable_database_keeps_the_started_settings_and_a_busy_one_is_retried(self):
        missing = db.make_engine("sqlite:////nonexistent-darkroom-folder/darkroom.db")
        self.addCleanup(missing.dispose)
        request = Request("GET", "/api/me", {}, {"Host": "127.0.0.1"}, b"", self.settings, missing, client="127.0.0.1")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(handler.with_stored(request), self.settings)
            self.assertEqual(handler.safe_dispatch(request).status, 401)
        locked = OperationalError("SELECT", {}, sqlite3.OperationalError("database is locked"))
        with mock.patch.object(stored_settings, "effective", side_effect=locked), contextlib.redirect_stdout(io.StringIO()):
            response = handler.safe_dispatch(dataclasses.replace(request, engine=self.engine))
        self.assertEqual((response.status, json.loads(response.body)), (503, {"error": handler.BUSY_MESSAGE}))

    def test_a_request_sees_the_stored_values_and_the_environment_still_wins(self):
        self.claim()
        self.json_call("POST", "/api/setup/immich", {"immich_url": "http://immich.lan:2283"})
        request = Request("GET", "/", {}, {}, b"", self.settings, self.engine)
        self.assertEqual(handler.with_stored(request).immich_url, "http://immich.lan:2283")
        self.assertIs(handler.with_stored(dataclasses.replace(request, engine=None)), self.settings)
        pinned = settings_from(DARKROOM_SECRET_KEY=KEY, DARKROOM_WIZARD="1", IMMICH_URL="http://pinned.lan")
        self.assertEqual(handler.with_stored(dataclasses.replace(request, settings=pinned)).immich_url, "http://pinned.lan")


class NoKeyTests(WizardCase):
    def test_nothing_is_stored_without_a_secret_key(self):
        keyless = settings_from(DARKROOM_WIZARD="1")
        request = Request("POST", "/api/setup/immich", {}, {"Host": "127.0.0.1"}, b'{"immich_url": "http://immich.lan"}', keyless, self.engine, client="127.0.0.1")
        self.assertEqual((setup.set_immich(request).status, self.stored()), (503, {}))
        self.assertEqual(setup.propose_sign_in(request).status, 503)


if __name__ == "__main__":
    unittest.main()
