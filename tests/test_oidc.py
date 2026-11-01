import dataclasses
import hashlib
import hmac
import json
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from darkroom import oidc, tls
from darkroom.storage import db
from darkroom.storage.models import AuthPending, AuthSession, User
from tests.fakes.oidc_provider import CLIENT_ID, CLIENT_SECRET, FakeProvider
from tests.shared import own_database, settings_from

NOW = 1_700_000_000.0


class ProviderCase(unittest.TestCase):
    client_secret = ""

    @classmethod
    def setUpClass(cls):
        cls.provider = FakeProvider(client_secret=cls.client_secret).start()
        cls.addClassCleanup(cls.provider.stop)

    def setUp(self):
        oidc.forget_caches()
        self.settings = settings_from(OIDC_ISSUER=self.provider.base, OIDC_CLIENT_ID=CLIENT_ID, OIDC_CLIENT_SECRET=self.client_secret,
                                      DARKROOM_PUBLIC_URL="https://darkroom.example", OIDC_ALLOWED_GROUPS="parents",
                                      OIDC_ALLOWED_EMAILS="allowed@example.test")
        self.engine = own_database(self)
        self.provider.issued.clear()
        self.provider.token_requests.clear()
        self.provider.userinfo_requests.clear()
        self.provider.hits.clear()
        self.provider.issuer_override = None

    def document(self):
        return oidc.discovery(self.settings, NOW)

    def token(self, nonce="nonce-1", kid="rsa-1", alg="RS256", **over):
        now = int(time.time())
        claims = {"iss": self.provider.base, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": nonce, **over}
        return self.provider.sign(claims, kid, alg)

    def begin(self, next_path="/people", now=NOW):
        with db.session(self.engine) as opened:
            url, state = oidc.start_login(opened, self.settings, next_path, now)
        return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}, state, url

    def grant(self, query, code="code-canary-0a1b2c3d", **extra):
        self.provider.issue(code, nonce=query["nonce"], challenge=query["code_challenge"], **extra)
        return code

    def finish(self, query, state, code, cookie=None, now=NOW):
        with db.session(self.engine) as opened:
            return oidc.finish_login(opened, self.settings, {"code": [code], "state": [state]}, state if cookie is None else cookie, now)


class DiscoveryTests(ProviderCase):
    def test_discovery_is_fetched_once_and_cached(self):
        self.assertEqual(self.document()["token_endpoint"], self.provider.base + "/token")
        self.document()
        self.assertEqual(self.provider.hits["/.well-known/openid-configuration"], 1)
        oidc.discovery(self.settings, NOW + oidc.DISCOVERY_TTL + 1)
        self.assertEqual(self.provider.hits["/.well-known/openid-configuration"], 2)

    def test_a_provider_naming_another_issuer_is_refused(self):
        self.provider.issuer_override = "https://someone-else.example"
        with self.assertRaises(oidc.SignInFailed):
            self.document()

    def test_an_unreachable_provider_is_reported_without_details(self):
        settings = dataclasses.replace(self.settings, oidc_issuer="http://127.0.0.1:9")
        with self.assertRaises(oidc.ProviderUnreachable) as caught:
            oidc.discovery(settings, NOW)
        self.assertNotIn("127.0.0.1", str(caught.exception))

    def test_endpoints_with_line_breaks_or_odd_schemes_are_refused_or_dropped(self):
        for bad in ("javascript:alert(1)", "https://id.example/a\r\nSet-Cookie: x", "", 5, "https://" + "a" * 2000):
            self.assertFalse(oidc.usable_endpoint(bad), bad)
        self.assertTrue(oidc.usable_endpoint(self.provider.base + "/authorize"))
        document = {**self.provider.discovery(), "end_session_endpoint": "https://id.example/x\ny", "userinfo_endpoint": 7}
        with mock.patch.object(oidc, "fetch_json", return_value=document):
            kept = oidc.discovery(self.settings, NOW)
        self.assertNotIn("end_session_endpoint", kept)
        self.assertNotIn("userinfo_endpoint", kept)
        oidc.forget_caches()
        document = {**self.provider.discovery(), "token_endpoint": "https://id.example/t\r\nX: y"}
        with mock.patch.object(oidc, "fetch_json", return_value=document), self.assertRaises(oidc.SignInFailed):
            oidc.discovery(self.settings, NOW)

    def test_an_oversized_or_non_json_reply_is_refused(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * (oidc.MAX_REPLY + 1)
        with mock.patch.object(tls, "urlopen", return_value=response), self.assertRaises(oidc.ProviderUnreachable):
            oidc.fetch_json("https://id.example/x")
        response.__enter__.return_value.read.return_value = b"not json"
        with mock.patch.object(tls, "urlopen", return_value=response), self.assertRaises(oidc.ProviderUnreachable):
            oidc.fetch_json("https://id.example/x")
        response.__enter__.return_value.read.return_value = b"[1]"
        with mock.patch.object(tls, "urlopen", return_value=response), self.assertRaises(oidc.ProviderUnreachable):
            oidc.fetch_json("https://id.example/x")

    def test_a_remote_http_issuer_is_refused_by_tls(self):
        settings = dataclasses.replace(self.settings, oidc_issuer="http://id.example")
        with mock.patch.object(tls, "urlopen", side_effect=tls.Refused("only https")) as opened:
            with self.assertRaises(oidc.ProviderUnreachable):
                oidc.discovery(settings, NOW)
        self.assertFalse(opened.call_args.kwargs["allow_http"])


class StartLoginTests(ProviderCase):
    def test_start_login_builds_a_pkce_request_and_remembers_it(self):
        query, state, url = self.begin("/people?tab=2")
        self.assertTrue(url.startswith(self.provider.base + "/authorize?"))
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], CLIENT_ID)
        self.assertEqual(query["redirect_uri"], "https://darkroom.example/auth/callback")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["state"], state)
        self.assertIn("openid", query["scope"])
        with db.session(self.engine) as opened:
            pending = opened.get(AuthPending, state)
            self.assertEqual((pending.kind, pending.next, pending.nonce), ("web", "/people?tab=2", query["nonce"]))
            self.assertEqual(oidc.b64url(hashlib.sha256(pending.verifier.encode()).digest()), query["code_challenge"])
            self.assertNotIn(pending.verifier, url)

    def test_old_and_surplus_pending_logins_and_expired_sessions_are_dropped(self):
        with db.session(self.engine) as opened:
            opened.add(AuthPending(state="old", nonce="n", verifier="v", created=NOW - oidc.LOGIN_TTL - 1))
            opened.add(User(sub="u1"))
            opened.flush()
            opened.add(AuthSession(token_hash="expired", sub="u1", created=NOW - 30 * 86_400, expires=NOW - 1))
            opened.add(AuthSession(token_hash="live", sub="u1", created=NOW, expires=NOW + 86_400))
        with mock.patch.object(oidc, "MAX_PENDING", 3):
            states = [self.begin(now=NOW + step)[1] for step in range(4)]
        with db.session(self.engine) as opened:
            kept = {row.state for row in opened.query(AuthPending)}
            self.assertEqual({row.token_hash for row in opened.query(AuthSession)}, {"live"})
        self.assertNotIn("old", kept)
        self.assertEqual(kept, set(states[-3:]))

    def test_next_must_be_a_path_on_this_site(self):
        for bad in ("https://evil.example", "//evil.example/x", "/\\evil.example", "relative", "/ok\r\nSet-Cookie: x", "", None, "/" + "a" * 2001):
            self.assertEqual(oidc.safe_next(bad), "/", bad)
        self.assertEqual(oidc.safe_next("/people?tab=2#x"), "/people?tab=2#x")


class FinishLoginTests(ProviderCase):
    def test_the_full_round_trip_gives_a_hashed_session(self):
        query, state, _ = self.begin("/exports")
        code = self.grant(query, groups=["parents", "family"])
        token, next_path = self.finish(query, state, code)
        self.assertEqual(next_path, "/exports")
        self.assertGreaterEqual(len(token), 43)
        form = self.provider.token_requests[-1]["form"]
        self.assertEqual((form["grant_type"], form["code"], form["redirect_uri"]), ("authorization_code", code, "https://darkroom.example/auth/callback"))
        self.assertEqual(form["client_id"], CLIENT_ID)
        with db.session(self.engine) as opened:
            session = opened.get(AuthSession, oidc.hash_token(token))
            self.assertEqual(session.sub, "user-1")
            self.assertEqual(session.expires, NOW + 14 * 86_400)
            self.assertIsNone(opened.get(AuthPending, state))
            user = opened.get(User, "user-1")
            self.assertEqual((user.email, user.name, json.loads(user.groups), user.last_seen), ("jo@example.test", "Jo Example", ["family", "parents"], NOW))
            self.assertEqual(oidc.session_user(opened, self.settings, token, NOW + 1).sub, "user-1")
            self.assertIsNone(oidc.session_user(opened, self.settings, token + "x", NOW + 1))

    def test_the_login_cookie_must_match_the_state(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"])
        for cookie in ("", "other-state"):
            with self.assertRaises(oidc.SignInFailed):
                self.finish(query, state, code, cookie=cookie)
        with db.session(self.engine) as opened:
            self.assertIsNotNone(opened.get(AuthPending, state))
        self.assertEqual(self.provider.token_requests, [])

    def test_a_state_is_used_once(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"])
        self.finish(query, state, code)
        self.grant(query, code="code-second-attempt", groups=["parents"])
        with self.assertRaises(oidc.SignInFailed):
            self.finish(query, state, "code-second-attempt")
        self.assertEqual(len(self.provider.token_requests), 1)

    def test_an_expired_pending_login_is_refused_and_removed(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"])
        with self.assertRaises(oidc.SignInFailed):
            self.finish(query, state, code, now=NOW + oidc.LOGIN_TTL + 1)
        self.assertEqual(self.provider.token_requests, [])

    def test_a_pending_device_login_is_not_a_web_login(self):
        with db.session(self.engine) as opened:
            opened.add(AuthPending(state="dev-state", nonce="n", verifier="v", kind="device", created=NOW))
        with self.assertRaises(oidc.SignInFailed):
            self.finish({}, "dev-state", "code-x")

    def test_provider_errors_and_missing_parameters_are_refused(self):
        _, state, _ = self.begin()
        with db.session(self.engine) as opened:
            for params in ({"error": ["access_denied"], "error_description": ["said no"]}, {"state": [state]}, {"code": ["c"]}):
                with self.assertRaises(oidc.SignInFailed):
                    oidc.finish_login(opened, self.settings, params, state, NOW)

    def test_a_wrong_verifier_is_refused_by_the_provider_and_the_sign_in_fails(self):
        query, state, _ = self.begin()
        self.provider.issue("code-bad-pkce", nonce=query["nonce"], challenge="not-the-challenge", groups=["parents"])
        with self.assertRaises(oidc.ProviderUnreachable):
            self.finish(query, state, "code-bad-pkce")

    def test_a_tampered_id_token_is_refused(self):
        query, state, _ = self.begin()
        code = self.grant(query, tamper=True, groups=["family"])
        with self.assertRaises(oidc.SignInFailed):
            self.finish(query, state, code)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)

    def test_a_nonce_from_another_sign_in_is_refused(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"], claims={"nonce": "someone-elses"})
        with self.assertRaises(oidc.SignInFailed):
            self.finish(query, state, code)

    def test_a_reply_without_an_id_token_is_refused(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"], omit_id_token=True)
        with self.assertRaises(oidc.SignInFailed):
            self.finish(query, state, code)

    def test_someone_outside_the_groups_and_emails_gets_no_session(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=["family"], email="stranger@example.test")
        with self.assertRaises(oidc.NotAllowed) as caught:
            self.finish(query, state, code)
        self.assertEqual(caught.exception.who, "stranger@example.test")
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)
            self.assertEqual(opened.query(User).count(), 0)

    def test_an_allowed_email_needs_to_be_verified(self):
        query, state, _ = self.begin()
        code = self.grant(query, groups=[], email="allowed@example.test", email_verified=False)
        with self.assertRaises(oidc.NotAllowed):
            self.finish(query, state, code)
        query, state, _ = self.begin()
        code = self.grant(query, groups=[], email="Allowed@Example.test")
        token, _ = self.finish(query, state, code)
        self.assertTrue(token)

    def test_groups_come_from_userinfo_when_the_id_token_has_none(self):
        self.provider.userinfo_groups = True
        self.addCleanup(setattr, self.provider, "userinfo_groups", False)
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"])
        token, _ = self.finish(query, state, code)
        self.assertTrue(token)
        self.assertEqual(self.provider.userinfo_requests, ["Bearer access-token-canary-4b2d8e0f"])
        query, state, _ = self.begin()
        code = self.grant(query, groups=["parents"], userinfo_sub="someone-else")
        with self.assertRaises(oidc.NotAllowed):
            self.finish(query, state, code)

    def test_userinfo_is_not_asked_when_the_id_token_names_groups(self):
        query, state, _ = self.begin()
        self.finish(query, state, self.grant(query, groups=["parents"]))
        self.assertEqual(self.provider.userinfo_requests, [])


class ClientSecretTests(ProviderCase):
    client_secret = CLIENT_SECRET

    def test_the_secret_goes_in_the_basic_header_and_never_in_the_url(self):
        query, state, url = self.begin()
        self.assertNotIn(CLIENT_SECRET, url)
        token, _ = self.finish(query, state, self.grant(query, groups=["parents"]))
        self.assertTrue(token)
        sent = self.provider.token_requests[-1]
        self.assertTrue(sent["authorization"].startswith("Basic "))
        self.assertNotIn("client_secret", sent["form"])

    def test_the_secret_goes_in_the_form_when_basic_is_not_offered(self):
        document = {**self.document(), "token_endpoint_auth_methods_supported": ["client_secret_post"]}
        with mock.patch.object(oidc, "discovery", return_value=document):
            query, state, _ = self.begin()
            token, _ = self.finish(query, state, self.grant(query, groups=["parents"]))
        self.assertTrue(token)
        sent = self.provider.token_requests[-1]
        self.assertIsNone(sent["authorization"])
        self.assertEqual(sent["form"]["client_secret"], CLIENT_SECRET)

    def test_a_symmetric_token_is_accepted_only_with_the_client_secret(self):
        now = int(time.time())
        claims = {"iss": self.provider.base, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": "n1"}
        signed = jwt.encode(claims, CLIENT_SECRET, algorithm="HS256")
        self.assertEqual(oidc.verify_id_token(self.settings, signed, "n1", self.document())["sub"], "user-1")
        without = dataclasses.replace(self.settings, oidc_client_secret="")
        with self.assertRaises(oidc.SignInFailed):
            oidc.verify_id_token(without, signed, "n1", self.document())
        wrong = jwt.encode(claims, "another-secret-entirely", algorithm="HS256")
        with self.assertRaises(oidc.SignInFailed):
            oidc.verify_id_token(self.settings, wrong, "n1", self.document())


class VerifyIdTokenTests(ProviderCase):
    def verify(self, token, nonce="nonce-1"):
        return oidc.verify_id_token(self.settings, token, nonce, self.document())

    def refused(self, token, nonce="nonce-1"):
        with self.assertRaises(oidc.SignInFailed) as caught:
            self.verify(token, nonce)
        return str(caught.exception)

    def test_rsa_and_ec_tokens_from_the_jwks_verify(self):
        self.assertEqual(self.verify(self.token())["sub"], "user-1")
        self.assertEqual(self.verify(self.token(kid="ec-1", alg="ES256"))["sub"], "user-1")
        self.assertEqual(self.provider.hits["/jwks"], 1)

    def test_a_token_signed_by_another_key_is_refused(self):
        other = rsa.generate_private_key(65537, 2048)
        now = int(time.time())
        claims = {"iss": self.provider.base, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": "nonce-1"}
        self.assertIn("did not check out", self.refused(jwt.encode(claims, other, algorithm="RS256", headers={"kid": "rsa-1"})))
        self.assertIn("does not publish", self.refused(jwt.encode(claims, other, algorithm="RS256", headers={"kid": "rsa-9"})))

    def test_alg_none_is_refused(self):
        now = int(time.time())
        claims = {"iss": self.provider.base, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": "nonce-1"}
        self.assertIn("not accepted", self.refused(jwt.encode(claims, None, algorithm="none")))
        header = oidc.b64url(json.dumps({"alg": 5}).encode())
        payload = oidc.b64url(json.dumps(claims).encode())
        self.assertIn("no signature algorithm", self.refused(f"{header}.{payload}."))
        self.assertIn("readable", self.refused("not-a-token"))

    def test_algorithm_confusion_with_the_public_key_is_refused(self):
        now = int(time.time())
        claims = {"iss": self.provider.base, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": "nonce-1"}
        pem = self.provider.rsa_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        signing_input = oidc.b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "rsa-1"}).encode()) + "." + oidc.b64url(json.dumps(claims).encode())
        forged_token = signing_input + "." + oidc.b64url(hmac.new(pem, signing_input.encode(), hashlib.sha256).digest())
        self.assertIn("not accepted", self.refused(forged_token))
        with_secret = dataclasses.replace(self.settings, oidc_client_secret="a-real-client-secret")
        with self.assertRaises(oidc.SignInFailed):
            oidc.verify_id_token(with_secret, forged_token, "nonce-1", self.document())

    def test_a_kid_pointing_at_a_key_of_another_type_is_refused(self):
        now = int(time.time())
        claims = {"iss": self.provider.base, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": "nonce-1"}
        self.assertIn("does not publish", self.refused(jwt.encode(claims, self.provider.ec_key, algorithm="ES256", headers={"kid": "rsa-1"})))

    def test_claims_are_checked(self):
        now = int(time.time())
        self.assertIn("expired", self.refused(self.token(exp=now - 600)))
        self.assertIn("future", self.refused(self.token(iat=now + 3600, nbf=now + 3600)))
        self.assertIn("different issuer", self.refused(self.token(iss="https://evil.example")))
        self.assertIn("did not check out", self.refused(self.token(aud="another-app")))
        self.assertIn("this sign-in", self.refused(self.token(nonce="other")))
        self.assertIn("this sign-in", self.refused(self.token(nonce=None)))
        self.assertIn("did not check out", self.refused(self.token(sub=None)))
        self.assertEqual(self.verify(self.token(exp=now - 30))["sub"], "user-1")

    def test_several_audiences_need_azp_to_be_us(self):
        self.assertIn("different app", self.refused(self.token(aud=[CLIENT_ID, "other"])))
        self.assertIn("different app", self.refused(self.token(aud=[CLIENT_ID, "other"], azp="other")))
        self.assertEqual(self.verify(self.token(aud=[CLIENT_ID, "other"], azp=CLIENT_ID))["sub"], "user-1")
        self.assertEqual(self.verify(self.token(aud=[CLIENT_ID]))["sub"], "user-1")

    def test_a_rotated_key_is_fetched_again_once(self):
        self.verify(self.token())
        self.addCleanup(setattr, self.provider, "rsa_kid", "rsa-1")
        self.provider.rsa_key, self.provider.rsa_kid = rsa.generate_private_key(65537, 2048), "rsa-2"
        self.assertEqual(self.verify(self.token(kid="rsa-2"))["sub"], "user-1")
        self.assertEqual(self.provider.hits["/jwks"], 2)
        self.assertIn("does not publish", self.refused(self.token(kid="rsa-3")))
        self.assertEqual(self.provider.hits["/jwks"], 3)

    def test_a_jwks_without_keys_is_refused(self):
        document = {**self.document(), "jwks_uri": self.provider.base + "/.well-known/openid-configuration"}
        with self.assertRaises(oidc.SignInFailed):
            oidc.verify_id_token(self.settings, self.token(), "nonce-1", document)
        document = {**self.document(), "jwks_uri": self.provider.base + "/missing"}
        with self.assertRaises(oidc.ProviderUnreachable):
            oidc.verify_id_token(self.settings, self.token(), "nonce-1", document)


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.settings = settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID=CLIENT_ID, OIDC_ALLOWED_GROUPS="Parents",
                                      OIDC_ALLOWED_EMAILS="Me@Example.test")

    def test_groups_or_a_verified_email_let_someone_in(self):
        who = oidc.authorize(self.settings, {"sub": "u1", "groups": ["parents", " Family "], "email": "X@Example.test", "name": " Jo "})
        self.assertEqual(who, oidc.Identity("u1", "x@example.test", "Jo", ("family", "parents")))
        who = oidc.authorize(self.settings, {"sub": "u2", "email": "me@example.test", "email_verified": True, "preferred_username": "me"})
        self.assertEqual((who.name, who.groups), ("me", ()))
        self.assertEqual(oidc.authorize(self.settings, {"sub": "u3", "groups": "parents"}).groups, ("parents",))

    def test_everyone_else_is_refused(self):
        for info in ({"sub": "u1"}, {"sub": "u1", "groups": ["family"]}, {"sub": "u1", "email": "me@example.test"},
                     {"sub": "u1", "email": "me@example.test", "email_verified": "true"}, {"sub": "u1", "email": "other@example.test", "email_verified": True},
                     {"sub": "u1", "groups": {"parents": True}}):
            with self.assertRaises(oidc.NotAllowed, msg=info):
                oidc.authorize(self.settings, info)
        with self.assertRaises(oidc.SignInFailed):
            oidc.authorize(self.settings, {"groups": ["parents"]})

    def test_nobody_is_let_in_when_no_list_is_set(self):
        empty = dataclasses.replace(self.settings, oidc_allowed_groups=(), oidc_allowed_emails=())
        with self.assertRaises(oidc.NotAllowed):
            oidc.authorize(empty, {"sub": "u1", "groups": ["parents"], "email": "me@example.test", "email_verified": True})


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.settings = settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID=CLIENT_ID, OIDC_ALLOWED_GROUPS="parents",
                                      OIDC_ALLOWED_EMAILS="me@example.test", DARKROOM_SESSION_DAYS="7")
        self.engine = own_database(self)
        with db.session(self.engine) as opened:
            opened.add(User(sub="u1", email="jo@example.test", name="Jo", groups='["parents"]', last_seen=NOW))
            opened.add(User(sub="u2", email="me@example.test", name="Me", groups="[]", last_seen=NOW))
            opened.flush()
            self.token = oidc.new_session(opened, self.settings, "u1", NOW)
            self.by_email = oidc.new_session(opened, self.settings, "u2", NOW)

    def expires(self, token):
        with db.session(self.engine) as opened:
            return opened.get(AuthSession, oidc.hash_token(token)).expires

    def test_only_a_hash_is_stored(self):
        with db.session(self.engine) as opened:
            hashes = [row.token_hash for row in opened.query(AuthSession)]
        self.assertNotIn(self.token, hashes)
        self.assertIn(oidc.hash_token(self.token), hashes)

    def test_a_session_expires(self):
        with db.session(self.engine) as opened:
            self.assertIsNotNone(oidc.session_user(opened, self.settings, self.token, NOW + 7 * 86_400 - 1))
            self.assertIsNone(oidc.session_user(opened, self.settings, self.token, NOW + 7 * 86_400))
            self.assertIsNone(opened.get(AuthSession, oidc.hash_token(self.token)))

    def test_a_session_in_use_is_extended_once_a_day_up_to_a_cap(self):
        with db.session(self.engine) as opened:
            self.assertIsNone(oidc.renew_session(opened, self.settings, self.token, NOW + 3600))
            self.assertEqual(oidc.renew_session(opened, self.settings, self.token, NOW + 2 * 86_400), 7 * 86_400)
            self.assertIsNone(oidc.renew_session(opened, self.settings, self.token, NOW + 2 * 86_400 + 60))
        self.assertEqual(self.expires(self.token), NOW + 9 * 86_400)
        with db.session(self.engine) as opened:
            for day in (8, 14, 20):
                self.assertEqual(oidc.renew_session(opened, self.settings, self.token, NOW + day * 86_400), 7 * 86_400)
            self.assertEqual(oidc.renew_session(opened, self.settings, self.token, NOW + 26 * 86_400), 4 * 86_400)
            self.assertIsNone(oidc.renew_session(opened, self.settings, self.token, NOW + 29 * 86_400))
            self.assertIsNone(oidc.renew_session(opened, self.settings, self.token, NOW + 31 * 86_400))
            self.assertIsNone(oidc.renew_session(opened, self.settings, "unknown-token", NOW + 2 * 86_400))
            self.assertIsNone(oidc.renew_session(opened, self.settings, None, NOW + 2 * 86_400))
        self.assertEqual(self.expires(self.token), NOW + oidc.SESSION_MAX_DAYS * 86_400)

    def test_a_session_ends_when_its_owner_is_no_longer_allowed(self):
        narrowed = dataclasses.replace(self.settings, oidc_allowed_groups=("admins",))
        with db.session(self.engine) as opened:
            self.assertIsNone(oidc.session_user(opened, narrowed, self.token, NOW + 1))
            self.assertIsNotNone(oidc.session_user(opened, narrowed, self.by_email, NOW + 1))
        without_email = dataclasses.replace(self.settings, oidc_allowed_emails=())
        with db.session(self.engine) as opened:
            self.assertIsNone(oidc.session_user(opened, without_email, self.by_email, NOW + 1))
            opened.query(User).filter(User.sub == "u1").delete()
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(AuthSession).count(), 0)

    def test_still_allowed_reads_the_stored_groups_and_email(self):
        self.assertTrue(oidc.still_allowed(self.settings, User(sub="a", groups='["parents"]')))
        self.assertTrue(oidc.still_allowed(self.settings, User(sub="a", email="ME@example.test")))
        self.assertFalse(oidc.still_allowed(self.settings, User(sub="a", email="other@example.test", groups='["family"]')))
        self.assertFalse(oidc.still_allowed(self.settings, User(sub="a", groups="not json")))
        self.assertFalse(oidc.still_allowed(self.settings, User(sub="a")))

    def test_access_lapsed(self):
        with db.session(self.engine) as opened:
            self.assertIsNone(oidc.access_lapsed(opened, self.settings, "u1", "jo@example.test", NOW + 86_400))
            self.assertEqual(oidc.access_lapsed(opened, self.settings, "u1", "jo@example.test", NOW + 8 * 86_400), "sign_in_lapsed")
            self.assertIsNone(oidc.access_lapsed(opened, self.settings, "u2", "me@example.test", NOW + 80 * 86_400))
            self.assertEqual(oidc.access_lapsed(opened, self.settings, "nobody", None, NOW), "user_removed")
            self.assertEqual(oidc.access_lapsed(opened, self.settings, None, None, NOW), "user_removed")
            narrowed = dataclasses.replace(self.settings, oidc_allowed_groups=("admins",))
            self.assertEqual(oidc.access_lapsed(opened, narrowed, "u1", "jo@example.test", NOW), "user_removed")
            unset = dataclasses.replace(self.settings, oidc_issuer="", oidc_client_id="")
            self.assertIsNone(oidc.access_lapsed(opened, unset, "nobody", None, NOW))

    def test_logout_ends_the_session_and_sweeps_expired_ones(self):
        with db.session(self.engine) as opened:
            opened.add(AuthSession(token_hash="old", sub="u1", created=NOW - 20 * 86_400, expires=NOW - 1))
        unset = dataclasses.replace(self.settings, oidc_issuer="", oidc_client_id="")
        with db.session(self.engine) as opened:
            self.assertEqual(oidc.logout(opened, unset, self.token, NOW), "/auth/signed-out")
            self.assertEqual(oidc.logout(opened, unset, None, NOW), "/auth/signed-out")
        with db.session(self.engine) as opened:
            self.assertEqual({row.token_hash for row in opened.query(AuthSession)}, {oidc.hash_token(self.by_email)})

    def test_logout_goes_to_the_provider_end_session_when_it_has_one(self):
        with_end = {"end_session_endpoint": "https://id.example/end?x=1"}
        with mock.patch.object(oidc, "discovery", return_value=with_end), db.session(self.engine) as opened:
            target = oidc.logout(opened, self.settings, self.token, NOW)
        self.assertTrue(target.startswith("https://id.example/end?x=1&"))
        query = parse_qs(urlsplit(target).query)
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["post_logout_redirect_uri"], ["/auth/signed-out"])
        with mock.patch.object(oidc, "discovery", return_value={}), db.session(self.engine) as opened:
            self.assertEqual(oidc.logout(opened, self.settings, None, NOW), "/auth/signed-out")
        with mock.patch.object(oidc, "discovery", side_effect=oidc.ProviderUnreachable("down")), db.session(self.engine) as opened:
            self.assertEqual(oidc.logout(opened, self.settings, None, NOW), "/auth/signed-out")

    def test_an_absurdly_long_cookie_matches_nothing(self):
        with db.session(self.engine) as opened:
            self.assertIsNone(oidc.find_session(opened, "x" * 201))


if __name__ == "__main__":
    unittest.main()
