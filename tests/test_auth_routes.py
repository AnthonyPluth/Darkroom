import contextlib
import dataclasses
import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

from darkroom import oidc
from darkroom.server import auth
from darkroom.storage import db
from darkroom.storage.models import AuthPending, AuthSession, User
from tests.fakes.oidc_provider import CLIENT_ID, FakeProvider
from tests.privacy import no_leaks
from tests.shared import cookies_set, fetch, own_database, settings_from, start_server


class SignedInServer(unittest.TestCase):
    provider_options = {}
    env = {}

    @classmethod
    def setUpClass(cls):
        cls.provider = FakeProvider(**cls.provider_options).start()
        cls.addClassCleanup(cls.provider.stop)

    def setUp(self):
        oidc.forget_caches()
        self.provider.reset()
        self.engine = own_database(self)
        settings = settings_from(OIDC_ISSUER=self.provider.base, OIDC_CLIENT_ID=CLIENT_ID, OIDC_ALLOWED_GROUPS="parents", DARKROOM_SECRET_KEY="k", **self.env)
        self.base, self.settings = start_server(self, settings, self.engine)
        self.replies = []

    def get(self, path, cookies=None, headers=None):
        return self.call("GET", path, cookies, headers)

    def call(self, method, path, cookies=None, headers=None, body=None):
        sent = dict(headers or {})
        if cookies:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        status, reply_headers, body = fetch(self.base, method, path, sent, body)
        self.replies.append(body)
        return status, reply_headers, body

    def begin(self, next_path="/people"):
        status, headers, _ = self.get("/auth/login?next=" + quote(next_path, safe=""))
        self.assertEqual(status, 303)
        location = headers["Location"]
        self.assertTrue(location.startswith(self.provider.base + "/authorize?"), location)
        query = {name: values[0] for name, values in parse_qs(urlsplit(location).query).items()}
        cookie = headers.get_all("Set-Cookie")
        self.assertEqual(len(cookie), 1)
        self.assertRegex(cookie[0], rf"^{auth.LOGIN_COOKIE}={query['state']}; Path=/auth; Max-Age=600; HttpOnly; SameSite=Lax(; Secure)?$")
        return query

    def sign_in(self, next_path="/people", code="code-canary-9e8d7c6b", same_browser=True, **grant):
        query = self.begin(next_path)
        self.provider.issue(code, nonce=query["nonce"], challenge=query["code_challenge"], **{"groups": ["parents"], **grant})
        cookies = {auth.LOGIN_COOKIE: query["state"]} if same_browser else {}
        return self.call("GET", f"/auth/callback?code={code}&state={query['state']}", cookies)


class RoundTripTests(SignedInServer):
    def test_sign_in_then_me_then_sign_out(self):
        with no_leaks(self, "code-canary-9e8d7c6b", database=self.engine, replies=self.replies) as canaries:
            status, headers, body = self.sign_in("/people?tab=phones")
            self.assertEqual((status, headers["Location"]), (303, "/people?tab=phones"))
            baked = headers.get_all("Set-Cookie")
            self.assertEqual(len(baked), 2)
            token = cookies_set(headers)[auth.SESSION_COOKIE]
            canaries.add(token)
            canaries.update(self.provider.refresh_grants)
            self.assertRegex(baked[0], rf"^{auth.SESSION_COOKIE}=[A-Za-z0-9_-]{{43,}}; Path=/; Max-Age={400 * 86_400}; HttpOnly; SameSite=Lax$")
            self.assertEqual(baked[1], f"{auth.LOGIN_COOKIE}=; Path=/auth; Max-Age=0; HttpOnly; SameSite=Lax")
            status, _, body = self.get("/api/me", {auth.SESSION_COOKIE: token})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"sub": "user-1", "email": "jo@example.test", "name": "Jo Example", "groups": ["parents"]})
            self.assertEqual(self.get("/api/me")[0], 401)
            status, headers, body = self.call("POST", "/auth/logout", {auth.SESSION_COOKIE: token}, {"Origin": self.base})
            self.assertEqual(status, 303)
            target = urlsplit(headers["Location"])
            self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}", self.provider.base + "/end-session")
            self.assertEqual(parse_qs(target.query)["post_logout_redirect_uri"], [self.base + "/auth/signed-out"])
            self.assertEqual(headers.get_all("Set-Cookie"), [f"{auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"])
            self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 401)
            status, headers, body = self.get("/auth/signed-out")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn("signed out", body.decode())
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)
            self.assertEqual(opened.query(AuthPending).count(), 0)
            self.assertEqual(opened.get(User, "user-1").email, "jo@example.test")

    def test_someone_outside_the_group_sees_a_clear_page_and_gets_no_session(self):
        with no_leaks(self, "code-canary-9e8d7c6b", database=self.engine) as canaries:
            status, headers, body = self.sign_in(groups=["family"], email="kid@example.test")
            canaries.add("kid@example.test")
        self.assertEqual(status, 403)
        self.assertIn("text/html", headers["Content-Type"])
        text = body.decode()
        self.assertIn("Not authorized", text)
        self.assertIn("kid@example.test is not allowed", text)
        self.assertIn('href="/auth/login"', text)
        self.assertEqual(headers.get_all("Set-Cookie"), [f"{auth.LOGIN_COOKIE}=; Path=/auth; Max-Age=0; HttpOnly; SameSite=Lax"])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)
            self.assertEqual(opened.query(User).count(), 0)

    def test_a_sign_in_from_another_browser_or_replayed_is_refused(self):
        status, _, body = self.sign_in(same_browser=False)
        self.assertEqual(status, 403)
        self.assertIn("did not start in this browser", body.decode())
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthPending).count(), 1)
        query = self.begin()
        self.provider.issue("code-first-use-1234", nonce=query["nonce"], challenge=query["code_challenge"], groups=["parents"])
        callback = f"/auth/callback?code=code-first-use-1234&state={query['state']}"
        self.assertEqual(self.call("GET", callback, {auth.LOGIN_COOKIE: query["state"]})[0], 303)
        self.provider.issue("code-first-use-1234", nonce=query["nonce"], challenge=query["code_challenge"], groups=["parents"])
        status, _, body = self.call("GET", callback, {auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 403)
        self.assertIn("expired or was already used", body.decode())
        self.assertEqual(len([sent for sent in self.provider.token_requests if sent["form"].get("code") == "code-first-use-1234"]), 1)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 1)

    def test_the_callback_never_echoes_the_code_or_state(self):
        query = self.begin()
        with no_leaks(self, "code-canary-5544aabb", query["state"], replies=self.replies):
            status, _, body = self.call("GET", f"/auth/callback?code=code-canary-5544aabb&state={query['state']}", {auth.LOGIN_COOKIE: query["state"]})
            self.assertEqual(status, 403)
            self.assertIn("the provider answered 400", body.decode())
            status, _, body = self.call("GET", f"/auth/callback?code=code-canary-5544aabb&state={query['state']}", {auth.LOGIN_COOKIE: "x"})
            self.assertEqual(status, 403)
            status, _, body = self.call("GET", "/auth/callback?error=access_denied&error_description=code-canary-5544aabb")
            self.assertEqual(status, 403)
            self.assertIn("refused the sign-in", body.decode())

    def test_a_tampered_token_is_refused_without_a_session(self):
        status, _, body = self.sign_in(tamper=True, groups=["family"])
        self.assertEqual(status, 403)
        self.assertIn("did not check out", body.decode())
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)

    def test_next_is_kept_only_when_it_is_a_path_here(self):
        status, headers, _ = self.sign_in("https://evil.example/phish")
        self.assertEqual((status, headers["Location"]), (303, "/"))
        status, headers, _ = self.sign_in("//evil.example/phish", code="code-two-2222aaaa")
        self.assertEqual(headers["Location"], "/")

    def test_the_cookie_is_re_sent_once_a_day_and_the_session_does_not_expire(self):
        status, headers, _ = self.sign_in()
        token = cookies_set(headers)[auth.SESSION_COOKIE]
        with db.session(self.engine) as opened:
            created = opened.get(AuthSession, oidc.hash_token(token)).created
        status, headers, _ = self.get("/api/me", {auth.SESSION_COOKIE: token})
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get_all("Set-Cookie"))
        with mock.patch.object(auth, "now", return_value=created + 2 * 86_400):
            status, headers, _ = self.get("/api/me", {auth.SESSION_COOKIE: token})
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_all("Set-Cookie"), [f"{auth.SESSION_COOKIE}={token}; Path=/; Max-Age={400 * 86_400}; HttpOnly; SameSite=Lax"])
            status, headers, _ = self.get("/api/me", {auth.SESSION_COOKIE: token})
            self.assertIsNone(headers.get_all("Set-Cookie"))
        with mock.patch.object(auth, "now", return_value=created + 700 * 86_400):
            status, headers, _ = self.get("/api/me", {auth.SESSION_COOKIE: token})
            self.assertEqual(status, 200)
            self.assertEqual(len(headers.get_all("Set-Cookie")), 1)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(AuthSession, oidc.hash_token(token)).expires)

    def test_without_a_refresh_token_the_session_is_capped_and_the_log_says_so(self):
        self.provider.issue_refresh_tokens = False
        with contextlib.redirect_stderr(io.StringIO()) as err:
            status, headers, _ = self.sign_in()
        self.assertEqual(status, 303)
        self.assertIn("cannot be re-checked", err.getvalue())
        self.assertIn('"rechecks": false', err.getvalue())
        self.assertRegex(headers.get_all("Set-Cookie")[0], rf"Max-Age={30 * 86_400}; HttpOnly")
        token = cookies_set(headers)[auth.SESSION_COOKIE]
        with db.session(self.engine) as opened:
            row = opened.get(AuthSession, oidc.hash_token(token))
            self.assertEqual((row.expires, row.refresh_token_sealed), (row.created + 30 * 86_400, None))
        with mock.patch.object(auth, "now", return_value=row.created + 10 * 86_400):
            status, headers, _ = self.get("/api/me", {auth.SESSION_COOKIE: token})
            self.assertEqual(status, 200)
            self.assertRegex(headers.get_all("Set-Cookie")[0], rf"Max-Age={20 * 86_400}; HttpOnly")
        with mock.patch.object(auth, "now", return_value=row.created + 30 * 86_400):
            self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 401)

    def test_logging_out_of_a_renewable_session_leaves_no_cookie_behind(self):
        _, headers, _ = self.sign_in()
        token = cookies_set(headers)[auth.SESSION_COOKIE]
        with db.session(self.engine) as opened:
            created = opened.get(AuthSession, oidc.hash_token(token)).created
        with mock.patch.object(auth, "now", return_value=created + 2 * 86_400):
            status, headers, _ = self.call("POST", "/auth/logout", {auth.SESSION_COOKIE: token}, {"Origin": self.base})
        self.assertEqual(status, 303)
        self.assertEqual(headers.get_all("Set-Cookie"), [f"{auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"])
        self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 401)

    def test_an_unverified_email_no_longer_lets_an_old_session_through_when_groups_narrow(self):
        _, headers, _ = self.sign_in(groups=["parents"], email="kid@example.test", email_verified=False)
        token = cookies_set(headers)[auth.SESSION_COOKIE]
        self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 200)
        narrowed = dataclasses.replace(self.settings, oidc_allowed_groups=("admins",), oidc_allowed_emails=("kid@example.test",))
        real = oidc.session_user
        with mock.patch.object(oidc, "session_user", side_effect=lambda opened, settings, tok, now: real(opened, narrowed, tok, now)):
            self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 401)

    def test_a_session_ends_when_the_allowed_groups_change(self):
        _, headers, _ = self.sign_in()
        token = cookies_set(headers)[auth.SESSION_COOKIE]
        narrowed = dataclasses.replace(self.settings, oidc_allowed_groups=("admins",))
        real = oidc.session_user
        with mock.patch.object(oidc, "session_user", side_effect=lambda opened, settings, tok, now: real(opened, narrowed, tok, now)):
            self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 401)
        self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 401)

    def test_logout_needs_a_same_origin_request_and_a_session(self):
        _, headers, _ = self.sign_in()
        token = cookies_set(headers)[auth.SESSION_COOKIE]
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
            self.assertEqual(self.call("POST", "/auth/logout", {auth.SESSION_COOKIE: token}, headers)[0], 403, headers)
        self.assertEqual(self.call("POST", "/auth/logout", None, {"Origin": self.base})[0], 401)
        self.assertEqual(self.get("/api/me", {auth.SESSION_COOKIE: token})[0], 200)
        self.assertEqual(self.call("POST", "/auth/logout", {auth.SESSION_COOKIE: token}, {"Sec-Fetch-Site": "same-origin"})[0], 303)

    def test_the_login_page_survives_a_provider_outage(self):
        with mock.patch.object(oidc, "discovery", side_effect=oidc.ProviderUnreachable("down")):
            status, headers, body = self.get("/auth/login")
        self.assertEqual(status, 502)
        self.assertIn("could not reach", body.decode())
        self.assertIsNone(headers.get_all("Set-Cookie"))

    def test_a_line_break_in_a_redirect_never_reaches_the_wire(self):
        with mock.patch.object(oidc, "start_login", return_value=("https://id.example/authorize?x=1\r\nSet-Cookie: evil=1", "state")):
            status, headers, body = self.get("/auth/login")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "something went wrong"})
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIsNone(headers.get("Location"))
        with mock.patch.object(oidc, "start_login", return_value=("https://id.example/authorize", "state\r\nX: y")):
            status, headers, _ = self.get("/auth/login")
        self.assertEqual(status, 500)
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_an_unexpected_error_is_a_plain_500(self):
        with mock.patch.object(oidc, "start_login", side_effect=RuntimeError("token=should-not-show-up")), \
                no_leaks(self, "should-not-show-up", replies=self.replies):
            status, _, body = self.get("/auth/login")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "something went wrong"})


class SecureCookieTests(SignedInServer):
    env = {"DARKROOM_PUBLIC_URL": "https://darkroom.example"}

    def test_cookies_are_secure_when_the_public_url_is_https(self):
        query = self.begin()
        self.assertEqual(query["redirect_uri"], "https://darkroom.example/auth/callback")
        status, headers, _ = self.get("/auth/login")
        self.assertTrue(headers["Set-Cookie"].endswith("; SameSite=Lax; Secure"))
        self.provider.issue("code-https-abcd1234", nonce=query["nonce"], challenge=query["code_challenge"], groups=["parents"], redirect_uri="https://darkroom.example/auth/callback")
        status, headers, _ = self.call("GET", f"/auth/callback?code=code-https-abcd1234&state={query['state']}", {auth.LOGIN_COOKIE: query["state"]})
        self.assertEqual(status, 303)
        for baked in headers.get_all("Set-Cookie"):
            self.assertTrue(baked.endswith("; Secure"), baked)

    def test_origins_matching_the_public_url_or_the_host_are_same_origin(self):
        self.assertEqual(self.call("GET", "/auth/login")[0], 303)
        for origin in ("https://darkroom.example", "HTTPS://DARKROOM.EXAMPLE", self.base):
            self.assertEqual(self.call("POST", "/auth/logout", None, {"Origin": origin})[0], 401, origin)
        for origin in ("https://darkroom.example.evil", "http://darkroom.example:81", "darkroom.example"):
            self.assertEqual(self.call("POST", "/auth/logout", None, {"Origin": origin})[0], 403, origin)


class NoProviderTests(unittest.TestCase):
    def test_without_oidc_nobody_is_signed_in(self):
        base, _ = start_server(self, settings_from())
        status, _, body = fetch(base, "GET", "/auth/login")
        self.assertEqual(status, 503)
        self.assertIn("not set up", body.decode())
        self.assertEqual(fetch(base, "GET", "/auth/callback?code=x&state=y")[0], 404)
        self.assertEqual(fetch(base, "GET", "/api/me")[0], 401)
        self.assertEqual(fetch(base, "POST", "/auth/logout", {"Origin": base}, b"")[0], 401)

    def test_no_auth_on_a_local_address_signs_everyone_in_as_the_local_owner(self):
        base, _ = start_server(self, settings_from(DARKROOM_ALLOW_NO_AUTH="1"))
        status, _, body = fetch(base, "GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"sub": "local", "email": "", "name": "Local owner", "groups": []})
        status, headers, _ = fetch(base, "GET", "/auth/login?next=/people")
        self.assertEqual((status, headers["Location"]), (303, "/people"))
        status, headers, _ = fetch(base, "POST", "/auth/logout", {"Origin": base}, b"")
        self.assertEqual((status, headers["Location"]), (303, "/auth/signed-out"))
        self.assertEqual(fetch(base, "POST", "/auth/logout", {"Origin": "https://evil.example"}, b"")[0], 403)

    def test_no_auth_is_ignored_when_misused(self):
        base, _ = start_server(self, settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_HOST="0.0.0.0"))
        self.assertEqual(fetch(base, "GET", "/api/me")[0], 401)


class CookieParsingTests(unittest.TestCase):
    def request(self, cookie):
        from darkroom.server.common import Request

        return Request("GET", "/", {}, {"cookie": cookie}, b"", settings_from())

    def test_cookies_are_split_and_the_first_wins(self):
        self.assertEqual(auth.cookies_of(self.request("a=1; darkroom_session=tok; a=2; broken; =x")), {"a": "1", "darkroom_session": "tok"})
        self.assertEqual(auth.cookies_of(self.request("x" * 9000)), {})


if __name__ == "__main__":
    unittest.main()
