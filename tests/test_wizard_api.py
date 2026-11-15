import contextlib
import io
import json
import os
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import run
from darkroom import oidc
from darkroom.server import common, handler, routes, wizard
from darkroom.server.api import wizard as setup
from darkroom.server.common import Request
from darkroom.storage import db, stored_settings
from darkroom.storage.models import Setting, StoredSetting, User
from darkroom.storage.settings_keys import SETUP_CLAIM, SETUP_LOCKED
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
        self.assertEqual(len(found), 7)
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
            self.assertEqual(status, 200, body)
            self.assertIn("Sign-in works", body.decode())
        self.assertIn(f"{wizard.CLAIM_COOKIE}=; Path=/; Max-Age=0", "\n".join(headers.get_all("Set-Cookie")))
        loaded = stored_settings.effective(self.settings, self.engine).settings
        self.assertEqual((loaded.oidc_issuer, loaded.oidc_client_id, loaded.oidc_client_secret), (self.provider.base, CLIENT_ID, CLIENT_SECRET))
        self.assertEqual((loaded.oidc_allowed_groups, loaded.oidc_allowed_emails), (("parents",), ()))
        self.assertIsNone(self.stored()["OIDC_CLIENT_SECRET"][0])
        self.assertEqual(self.json_call("GET", "/api/setup")[0], 401)
        self.assertEqual(self.json_call("POST", "/api/setup/claim", cookies={})[0], 401)
        with db.session(self.engine) as opened:
            self.assertEqual((opened.query(User).count(), opened.query(StoredSetting).count()), (0, 5))

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
        self.assertEqual(self.try_sign_in(code="test-code-two-9f8e7d")[0], 200)
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
        self.assertEqual(self.json_call("POST", "/api/setup/sign-in/test", headers={"Host": "PHOTOS.example.com"})[0], 401)
        self.assertEqual(self.json_call("POST", "/api/setup/address", {"public_url": self.base})[0], 200)
        self.assertEqual(self.try_sign_in()[0], 200)
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
        self.assertEqual(self.try_sign_in(code="test-code-kim-5a6b7c", groups=[], email="kim@example.com")[0], 200)
        restarted = self.restarted()
        self.assertEqual((restarted.oidc_allowed_groups, restarted.oidc_allowed_emails), ((), ("kim@example.com",)))
        self.assertTrue(oidc.allowed(restarted, "kim@example.com", True, ()))
        self.assertFalse(oidc.allowed(restarted, "jo@example.test", True, ("parents",)))


class NoKeyTests(WizardCase):
    def test_nothing_is_stored_without_a_secret_key(self):
        keyless = settings_from(DARKROOM_WIZARD="1")
        request = Request("POST", "/api/setup/immich", {}, {"Host": "127.0.0.1"}, b'{"immich_url": "http://immich.lan"}', keyless, self.engine, client="127.0.0.1")
        self.assertEqual((setup.set_immich(request).status, self.stored()), (503, {}))
        self.assertEqual(setup.propose_sign_in(request).status, 503)


if __name__ == "__main__":
    unittest.main()
