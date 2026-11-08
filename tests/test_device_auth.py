import time
import unittest
from urllib.parse import parse_qs, quote, urlsplit

from darkroom import oidc
from darkroom.domain import devices
from darkroom.server import auth
from darkroom.storage import db
from darkroom.storage.models import AuthPending, Device, Event, Person, User
from tests.fakes.oidc_provider import CLIENT_ID, FakeProvider
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, settings_from, start_server

CODE = "device-code-canary-1a2b3c4d"
APP_STATE = "app-attempt-state-0123456789"


class DeviceSignInCase(unittest.TestCase):
    env: dict[str, str] = {}

    @classmethod
    def setUpClass(cls):
        cls.provider = FakeProvider().start()
        cls.addClassCleanup(cls.provider.stop)

    def setUp(self):
        oidc.forget_caches()
        self.provider.reset()
        self.engine = own_database(self)
        settings = settings_from(OIDC_ISSUER=self.provider.base, OIDC_CLIENT_ID=CLIENT_ID, OIDC_ALLOWED_GROUPS="parents", DARKROOM_SECRET_KEY="k", **self.env)
        self.base, self.settings = start_server(self, settings, self.engine)
        self.replies = []
        with db.session(self.engine) as opened:
            person = Person(name="Jo", slug="jo", email="Jo@Example.test", originals_path="jo/originals", google_only_path="jo/google-only", created=1.0)
            opened.add(person)
            opened.flush()
            self.person_id = person.id

    def call(self, method, path, cookies=None, headers=None, body=None):
        sent = dict(headers or {})
        if cookies:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        status, reply_headers, reply = fetch(self.base, method, path, sent, body)
        self.replies.append(reply)
        return status, reply_headers, reply

    def start(self, name="Jo's iPhone", app_state=APP_STATE):
        status, headers, _ = self.call("GET", "/auth/device/start?name=" + quote(name, safe="") + "&state=" + app_state)
        self.assertEqual(status, 303)
        location = headers["Location"]
        self.assertTrue(location.startswith(self.provider.base + "/authorize?"), location)
        return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}

    def sign_in(self, name="Jo's iPhone", same_browser=True, **grant):
        query = self.start(name)
        return self.finish(query, same_browser, **grant)

    def finish(self, query, same_browser=True, **grant):
        self.provider.issue(CODE, nonce=query["nonce"], challenge=query["code_challenge"], **{"groups": ["family"], **grant})
        cookies = {auth.LOGIN_COOKIE: query["state"]} if same_browser else {}
        return self.call("GET", f"/auth/callback?code={CODE}&state={query['state']}", cookies)

    def sign_in_with_an_unusable_state(self, **grant):
        query = self.start()
        with db.session(self.engine) as opened:
            opened.query(AuthPending).update({"client_state": "not a usable state!"})
        return self.finish(query, **grant)

    def token_from(self, headers):
        location = headers["Location"]
        self.assertTrue(location.startswith("darkroom://signed-in#token="), location)
        token, state = location.split("#token=", 1)[1].split("&state=")
        self.assertEqual(state, APP_STATE)
        return token

    def live(self, token):
        with db.session(self.engine) as opened:
            return opened.query(Device).filter(Device.token_hash == oidc.hash_token(token), Device.revoked_at.is_(None)).count() == 1

    def device_count(self):
        with db.session(self.engine) as opened:
            return opened.query(Device).count()


class DeviceSignInTests(DeviceSignInCase):
    def test_a_round_trip_ties_the_iphone_to_the_person_and_the_token_works(self):
        with no_leaks(self, CODE, database=self.engine, replies=self.replies) as canaries:
            status, headers, body = self.sign_in()
            self.assertEqual(status, 303)
            token = self.token_from(headers)
            canaries.add(token)
            self.assertEqual(headers.get_all("Set-Cookie"), [f"{auth.LOGIN_COOKIE}=; Path=/auth; Max-Age=0; HttpOnly; SameSite=Lax"])
            self.assertEqual(body, b"")
        self.assertGreaterEqual(len(token), 43)
        self.assertNotIn(token.encode(), b"".join(self.replies))
        with db.session(self.engine) as opened:
            device = opened.query(Device).one()
            self.assertEqual((device.person_id, device.name, device.token_hash), (self.person_id, "Jo's iPhone", oidc.hash_token(token)))
            self.assertEqual((device.pending_uploads, device.app_version, device.revoked_at), (0, None, None))
            self.assertIsNotNone(device.last_checkin)
            self.assertEqual(opened.query(AuthPending).count(), 0)
            self.assertEqual([event.type for event in opened.query(Event)], ["device_signed_in"])

    def test_parents_may_sign_in_an_iphone_too(self):
        status, headers, _ = self.sign_in(groups=["parents"])
        self.assertEqual(status, 303)
        self.token_from(headers)

    def test_an_unknown_email_gets_a_clear_page_and_no_device(self):
        status, headers, body = self.sign_in_with_an_unusable_state(email="nobody@example.test")
        self.assertEqual(status, 403)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("No person with this email", body.decode())
        self.assertNotIn("darkroom://", headers.get("Location") or "")
        self.assertEqual(self.device_count(), 0)

    def test_an_unverified_email_or_two_people_with_it_create_no_device(self):
        self.assertEqual(self.sign_in_with_an_unusable_state(email_verified=False)[0], 403)
        with db.session(self.engine) as opened:
            opened.add(Person(name="Jo Two", slug="jo-two", email="jo@example.test", originals_path="x", google_only_path="y", created=1.0))
        status, _, body = self.sign_in_with_an_unusable_state()
        self.assertEqual(status, 403)
        self.assertIn("more than one person has this email", body.decode())
        self.assertEqual(self.device_count(), 0)

    def test_someone_outside_the_device_groups_is_not_allowed(self):
        status, _, body = self.sign_in_with_an_unusable_state(groups=["guests"])
        self.assertEqual(status, 403)
        self.assertIn("not allowed to use the Darkroom app", body.decode())
        self.assertEqual(self.device_count(), 0)

    def test_a_sign_in_from_another_browser_or_a_failed_exchange_creates_no_device(self):
        status, _, body = self.sign_in(same_browser=False)
        self.assertEqual(status, 403)
        self.assertIn("did not start in this browser", body.decode())
        query = self.start()
        status, _, _ = self.call("GET", f"/auth/callback?code=never-issued&state={query['state']}", {auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 403)
        self.assertEqual(self.device_count(), 0)

    def test_an_unreachable_provider_at_the_callback_is_502(self):
        query = self.start()
        self.provider.outage = True
        status, _, body = self.call("GET", f"/auth/callback?code={CODE}&state={query['state']}", {auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 502)
        self.assertIn("could not reach the sign-in provider", body.decode())
        self.assertEqual(self.device_count(), 0)

    def test_start_needs_the_apps_state(self):
        for state in ("", "short", "x" * 129, "a" * 21 + "!", "a" * 21 + "%0A"):
            with self.subTest(state=state):
                status, _, body = self.call("GET", f"/auth/device/start?name=Jo&state={state}")
                self.assertEqual(status, 400)
                self.assertIn("did not send a valid sign-in code", body.decode())
        self.assertEqual(self.call("GET", "/auth/device/start?name=Jo")[0], 400)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthPending).count(), 0)

    def test_the_callback_echoes_the_state_of_its_own_attempt_only(self):
        first = self.start(app_state="first-attempt-state-0000000")
        second = self.start(app_state="second-attempt-state-000000")
        self.provider.issue(CODE, nonce=second["nonce"], challenge=second["code_challenge"], groups=["family"])
        status, headers, _ = self.call("GET", f"/auth/callback?code={CODE}&state={second['state']}", {auth.LOGIN_COOKIE: second["state"]})
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].endswith("&state=second-attempt-state-000000"), headers["Location"])
        self.assertNotIn("first-attempt-state", headers["Location"])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthPending).one().client_state, "first-attempt-state-0000000")
        self.assertNotEqual(first["state"], second["state"])

    def test_a_replayed_callback_is_refused_and_creates_no_second_device(self):
        query = self.start()
        self.provider.issue(CODE, nonce=query["nonce"], challenge=query["code_challenge"], groups=["family"])
        path = f"/auth/callback?code={CODE}&state={query['state']}"
        cookies = {auth.LOGIN_COOKIE: query["state"]}
        self.assertEqual(self.call("GET", path, cookies)[0], 303)
        self.assertEqual(self.call("GET", path, cookies)[0], 403)
        self.assertEqual(self.device_count(), 1)

    def test_a_pending_sign_in_without_the_apps_state_never_finishes(self):
        query = self.start()
        with db.session(self.engine) as opened:
            opened.query(AuthPending).update({"client_state": None})
        self.provider.issue(CODE, nonce=query["nonce"], challenge=query["code_challenge"], groups=["family"])
        status, headers, _ = self.call("GET", f"/auth/callback?code={CODE}&state={query['state']}", {auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 403)
        self.assertNotIn("darkroom://", headers.get("Location") or "")
        self.assertEqual(self.device_count(), 0)
        self.assertEqual(self.provider.token_requests, [])
        self.assertEqual(self.provider.userinfo_requests, [])

    def test_start_needs_a_name_and_a_reachable_provider(self):
        for path in ("/auth/device/start", "/auth/device/start?name=", "/auth/device/start?name=" + "x" * 101, "/auth/device/start?name=a%0Ab"):
            path += "&state=" + APP_STATE if "?" in path else "?state=" + APP_STATE
            with self.subTest(path=path):
                status, _, body = self.call("GET", path)
                self.assertEqual(status, 400)
                self.assertIn("did not say which iPhone", body.decode())
        self.provider.outage = True
        oidc.forget_caches()
        self.assertEqual(self.call("GET", f"/auth/device/start?name=Jo&state={APP_STATE}")[0], 502)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthPending).count(), 0)

    def test_a_device_sign_in_cannot_finish_as_a_web_session(self):
        query = self.start()
        with db.session(self.engine) as opened:
            with self.assertRaises(oidc.SignInFailed):
                oidc.finish_login(opened, self.settings, {"code": [CODE], "state": [query["state"]]}, query["state"], time.time())

    def test_a_device_token_is_never_accepted_as_a_session(self):
        token = self.token_from(self.sign_in()[1])
        for headers in ({"Authorization": f"Bearer {token}"}, {"Cookie": f"{auth.SESSION_COOKIE}={token}"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("GET", "/api/me", headers=headers)[0], 401)
                self.assertEqual(self.call("GET", "/api/people", headers=headers)[0], 401)


class DeviceSignInErrorRedirectTests(DeviceSignInCase):
    EMAIL = "canary.nobody@example.test"

    def refused_fragment(self, headers):
        location = headers["Location"]
        self.assertTrue(location.startswith("darkroom://signed-in#error="), location)
        return dict(part.split("=", 1) for part in location.split("#", 1)[1].split("&"))

    def test_an_unknown_email_returns_to_the_app_with_no_person_and_its_state(self):
        with no_leaks(self, CODE, self.EMAIL, database=self.engine, replies=self.replies):
            status, headers, body = self.sign_in(email=self.EMAIL)
        self.assertEqual((status, body), (303, b""))
        self.assertEqual(self.refused_fragment(headers), {"error": "no_person", "state": APP_STATE})
        self.assertEqual(headers.get_all("Set-Cookie"), [f"{auth.LOGIN_COOKIE}=; Path=/auth; Max-Age=0; HttpOnly; SameSite=Lax"])
        self.assertNotIn("token", headers["Location"])
        self.assertEqual(self.device_count(), 0)

    def test_an_unverified_email_or_two_people_with_it_return_no_person(self):
        status, headers, _ = self.sign_in(email_verified=False)
        self.assertEqual((status, self.refused_fragment(headers)["error"]), (303, "no_person"))
        with db.session(self.engine) as opened:
            opened.add(Person(name="Jo Two", slug="jo-two", email="jo@example.test", originals_path="x", google_only_path="y", created=1.0))
        status, headers, _ = self.sign_in()
        self.assertEqual((status, self.refused_fragment(headers)["error"]), (303, "no_person"))
        self.assertEqual(self.device_count(), 0)

    def test_someone_outside_the_device_groups_returns_not_allowed_without_their_email(self):
        with no_leaks(self, CODE, self.EMAIL, database=self.engine, replies=self.replies):
            status, headers, body = self.sign_in(groups=["guests"], email=self.EMAIL)
        self.assertEqual((status, body), (303, b""))
        self.assertEqual(self.refused_fragment(headers), {"error": "not_allowed", "state": APP_STATE})
        self.assertNotIn("example.test", headers["Location"])
        self.assertEqual(self.device_count(), 0)

    def test_the_fragment_carries_the_state_of_its_own_attempt_only(self):
        self.start(app_state="first-attempt-state-0000000")
        second = self.start(app_state="second-attempt-state-000000")
        status, headers, _ = self.finish(second, email=self.EMAIL)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "darkroom://signed-in#error=no_person&state=second-attempt-state-000000")

    def test_a_refusal_without_the_apps_state_keeps_the_page(self):
        for refusal in ({"email": self.EMAIL}, {"groups": ["guests"]}):
            with self.subTest(refusal=refusal):
                query = self.start()
                with db.session(self.engine) as opened:
                    opened.query(AuthPending).update({"client_state": None})
                status, headers, body = self.finish(query, **refusal)
                self.assertEqual(status, 403)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertNotIn("darkroom://", headers.get("Location") or "")
                self.assertTrue(body)

    def test_a_refusal_with_an_unusable_stored_state_keeps_the_page(self):
        query = self.start()
        with db.session(self.engine) as opened:
            opened.query(AuthPending).update({"client_state": "bad state&error=x#"})
        status, headers, _ = self.finish(query, email=self.EMAIL)
        self.assertEqual(status, 403)
        self.assertNotIn("darkroom://", headers.get("Location") or "")

    def test_a_replayed_refused_attempt_gets_the_page_not_the_redirect(self):
        query = self.start()
        self.assertEqual(self.finish(query, email=self.EMAIL)[0], 303)
        status, headers, body = self.finish(query, email=self.EMAIL)
        self.assertEqual(status, 403)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("this sign-in has expired or was already used", body.decode())
        self.assertNotIn("darkroom://", headers.get("Location") or "")

    def test_an_unknown_state_or_another_browser_gets_the_page(self):
        status, headers, _ = self.call("GET", f"/auth/callback?code={CODE}&state=never-started", {auth.LOGIN_COOKIE: "never-started"})
        self.assertEqual(status, 403)
        self.assertNotIn("darkroom://", headers.get("Location") or "")
        query = self.start()
        status, headers, _ = self.finish(query, same_browser=False, email=self.EMAIL)
        self.assertEqual(status, 403)
        self.assertNotIn("darkroom://", headers.get("Location") or "")

    def test_failures_that_are_not_those_two_keep_their_pages(self):
        query = self.start()
        self.provider.outage = True
        status, headers, _ = self.call("GET", f"/auth/callback?code={CODE}&state={query['state']}", {auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 502)
        self.assertNotIn("darkroom://", headers.get("Location") or "")


class RevokedAndLapsedTests(DeviceSignInCase):
    def signed_in(self):
        return self.token_from(self.sign_in()[1])

    def test_signing_in_again_after_a_revoke_makes_a_new_device_and_the_old_token_stays_refused(self):
        old = self.signed_in()
        with db.session(self.engine) as opened:
            devices.revoke(opened, opened.query(Device).one().id, time.time())
        new = self.signed_in()
        self.assertNotEqual(old, new)
        self.assertFalse(self.live(old))
        self.assertTrue(self.live(new))


class BoundToTheEnrollingAccountTests(DeviceSignInCase):
    def test_the_device_records_the_account_that_signed_it_in(self):
        self.token_from(self.sign_in(sub="jo-account")[1])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(Device).one().sub, "jo-account")

    def test_an_email_reassigned_to_another_account_ends_the_old_token(self):
        old = self.token_from(self.sign_in(sub="jo-account")[1])
        with db.session(self.engine) as opened:
            opened.query(User).filter_by(sub="jo-account").update({"email": "jo.old@example.test"})
            opened.add(User(sub="someone-else", email="jo@example.test", email_verified=True, groups='["family"]', last_seen=time.time() - 60))
        new = self.token_from(self.sign_in(sub="someone-else")[1])
        self.assertEqual((self.live(old), self.live(new)), (False, True))
        with db.session(self.engine) as opened:
            first = opened.query(Device).filter_by(sub="jo-account").one()
            self.assertIsNotNone(first.revoked_at)
            self.assertFalse(first.cleanup_enabled)

    def test_an_account_recreated_with_the_same_email_does_not_keep_the_old_token_alive(self):
        old = self.token_from(self.sign_in(sub="jo-account")[1])
        with db.session(self.engine) as opened:
            opened.query(User).filter_by(sub="jo-account").update({"last_seen": time.time() - 60})
            opened.add(User(sub="jo-recreated", email="jo@example.test", email_verified=True, groups='["family"]', last_seen=time.time()))
        new = self.token_from(self.sign_in(sub="jo-recreated")[1])
        self.assertEqual((self.live(old), self.live(new)), (False, True))

    def test_signing_in_again_with_the_same_account_keeps_its_other_iphones(self):
        first = self.token_from(self.sign_in("Jo's iPhone", sub="jo-account")[1])
        second = self.token_from(self.sign_in("Jo's iPad", sub="jo-account")[1])
        self.assertEqual((self.live(first), self.live(second)), (True, True))


class NotConfiguredTests(unittest.TestCase):
    def test_without_a_provider_device_sign_in_and_device_routes_are_closed(self):
        engine = own_database(self)
        base, _ = start_server(self, settings_from(DARKROOM_ALLOW_NO_AUTH="1"), engine)
        status, _, body = fetch(base, "GET", f"/auth/device/start?name=Jo&state={APP_STATE}")
        self.assertEqual(status, 503)
        self.assertIn("no sign-in provider", body.decode())


if __name__ == "__main__":
    unittest.main()
