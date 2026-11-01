from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

CLIENT_ID = "darkroom-web"
CLIENT_SECRET = "client-secret-canary-7f3a9c1e"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeProvider:
    def __init__(self, client_secret: str = "", end_session: bool = True, userinfo_groups: bool = False) -> None:
        self.client_secret = client_secret
        self.end_session = end_session
        self.userinfo_groups = userinfo_groups
        self.rsa_key = rsa.generate_private_key(65537, 2048)
        self.rsa_kid = "rsa-1"
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.issued: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, Any]] = []
        self.userinfo_requests: list[str | None] = []
        self.hits: dict[str, int] = {}
        self.issuer_override: str | None = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def reply(self, value: object, status: int = 200) -> None:
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                provider.hits[path] = provider.hits.get(path, 0) + 1
                if path == "/.well-known/openid-configuration":
                    return self.reply(provider.discovery())
                if path == "/jwks":
                    return self.reply(provider.jwks())
                if path == "/userinfo":
                    provider.userinfo_requests.append(self.headers.get("Authorization"))
                    return self.reply(provider.userinfo(self.headers.get("Authorization")))
                self.reply({"error": "not found"}, 404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                form = dict(parse_qsl(self.rfile.read(length).decode()))
                provider.token_requests.append({"form": form, "authorization": self.headers.get("Authorization")})
                if urlsplit(self.path).path != "/token":
                    return self.reply({"error": "not found"}, 404)
                answer, status = provider.token(form, self.headers.get("Authorization"))
                self.reply(answer, status)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> FakeProvider:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def discovery(self) -> dict[str, Any]:
        document = {"issuer": self.issuer_override or self.base, "authorization_endpoint": self.base + "/authorize",
                    "token_endpoint": self.base + "/token", "jwks_uri": self.base + "/jwks", "userinfo_endpoint": self.base + "/userinfo",
                    "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
                    "id_token_signing_alg_values_supported": ["RS256", "ES256"]}
        if self.end_session:
            document["end_session_endpoint"] = self.base + "/end-session"
        return document

    def jwks(self) -> dict[str, Any]:
        rsa_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.rsa_key.public_key()))
        ec_jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.ec_key.public_key()))
        return {"keys": [{**rsa_jwk, "kid": self.rsa_kid, "use": "sig", "alg": "RS256"}, {**ec_jwk, "kid": "ec-1", "use": "sig"}]}

    def claims(self, grant: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        moment = int(now or time.time())
        base = {"iss": self.base, "aud": CLIENT_ID, "sub": grant.get("sub", "user-1"), "iat": moment, "exp": moment + 300,
                "nonce": grant.get("nonce", ""), "email": grant.get("email", "jo@example.test"), "email_verified": grant.get("email_verified", True),
                "name": grant.get("name", "Jo Example")}
        if "groups" in grant and not self.userinfo_groups:
            base["groups"] = grant["groups"]
        return {**base, **grant.get("claims", {})}

    def sign(self, claims: dict[str, Any], kid: str = "rsa-1", alg: str = "RS256") -> str:
        key: Any = self.ec_key if kid == "ec-1" else self.rsa_key
        return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})

    def issue(self, code: str, **grant: Any) -> None:
        self.issued[code] = grant

    def token(self, form: dict[str, str], authorization: str | None) -> tuple[dict[str, Any], int]:
        grant = self.issued.pop(form.get("code", ""), None)
        if grant is None:
            return {"error": "invalid_grant"}, 400
        if self.client_secret:
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{self.client_secret}".encode()).decode()
            if authorization != expected and form.get("client_secret") != self.client_secret:
                return {"error": "invalid_client"}, 401
        if form.get("redirect_uri") != grant.get("redirect_uri", form.get("redirect_uri")):
            return {"error": "invalid_grant", "error_description": "redirect_uri"}, 400
        if b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != grant.get("challenge"):
            return {"error": "invalid_grant", "error_description": "PKCE"}, 400
        id_token = grant.get("id_token") or self.sign(self.claims(grant), grant.get("kid", "rsa-1"), grant.get("alg", "RS256"))
        if grant.get("tamper"):
            header, _, signature = id_token.split(".")
            forged = {**self.claims(grant), "email": "attacker@example.test", "groups": ["parents"]}
            id_token = f"{header}.{b64url(json.dumps(forged).encode())}.{signature}"
        answer: dict[str, Any] = {"token_type": "Bearer", "id_token": id_token}
        if grant.get("access_token", "access-token-canary-4b2d8e0f"):
            answer["access_token"] = grant.get("access_token", "access-token-canary-4b2d8e0f")
            self.issued["access:" + answer["access_token"]] = grant
        if grant.get("omit_id_token"):
            del answer["id_token"]
        return answer, 200

    def userinfo(self, authorization: str | None) -> dict[str, Any]:
        grant = self.issued.get("access:" + (authorization or "").removeprefix("Bearer "), {})
        info = {"sub": grant.get("userinfo_sub", grant.get("sub", "user-1")), "email": grant.get("email", "jo@example.test")}
        if self.userinfo_groups and "groups" in grant:
            info["groups"] = grant["groups"]
        return info
