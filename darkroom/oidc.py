from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import jwt
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from darkroom import tls
from darkroom.config import SESSION_MAX_DAYS, Settings, local_host, oidc_configured
from darkroom.storage.models import AuthPending, AuthSession, User

LOGIN_TTL = 600.0
MAX_PENDING = 10_000
DISCOVERY_TTL = 3600.0
RENEW_AFTER = 86_400.0
LEEWAY = 60
MAX_REPLY = 1_000_000
SCOPES = "openid email profile groups"
ASYMMETRIC = {"RS256": "RSA", "RS384": "RSA", "RS512": "RSA", "PS256": "RSA", "PS384": "RSA", "PS512": "RSA",
              "ES256": "EC", "ES384": "EC", "ES512": "EC", "EdDSA": "OKP"}
SYMMETRIC = {"HS256", "HS384", "HS512"}
USER_AGENT = "Darkroom/0.1"

configured = oidc_configured

_discovery: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks: dict[str, list[dict[str, Any]]] = {}


class SignInFailed(Exception):
    pass


class ProviderUnreachable(SignInFailed):
    pass


class NotAllowed(SignInFailed):
    def __init__(self, who: str) -> None:
        super().__init__("not allowed")
        self.who = who


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str | None
    name: str | None
    groups: tuple[str, ...]
    email_verified: bool = False


def forget_caches() -> None:
    _discovery.clear()
    _jwks.clear()


def redirect_uri(settings: Settings) -> str:
    return settings.public_url + "/auth/callback"


def secure_cookie(settings: Settings) -> bool:
    return settings.public_url.startswith("https://")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def fetch_json(url: str, headers: Mapping[str, str] | None = None, data: bytes | None = None) -> dict[str, Any]:
    host = urlsplit(url).hostname or ""
    sent = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    try:
        with tls.urlopen(url, data=data, headers=sent, method="POST" if data is not None else "GET", allow_http=local_host(host)) as response:
            raw = response.read(MAX_REPLY + 1)
    except urllib.error.HTTPError as error:
        with error:
            raise ProviderUnreachable(f"the provider answered {error.code}") from error
    except (urllib.error.URLError, tls.Refused, OSError) as error:
        raise ProviderUnreachable("the provider could not be reached") from error
    if len(raw) > MAX_REPLY:
        raise ProviderUnreachable("the provider's reply was too large")
    try:
        body = json.loads(raw.decode())
    except ValueError as error:
        raise ProviderUnreachable("the provider's reply was not JSON") from error
    if not isinstance(body, dict):
        raise ProviderUnreachable("the provider's reply was not a JSON object")
    return body


def usable_endpoint(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")) or len(value) > 2000:
        return False
    return all(ord(char) > 32 and ord(char) != 127 for char in value)


def discovery(settings: Settings, now: float) -> dict[str, Any]:
    cached = _discovery.get(settings.oidc_issuer)
    if cached and now - cached[0] < DISCOVERY_TTL:
        return cached[1]
    document = fetch_json(settings.oidc_issuer + "/.well-known/openid-configuration")
    if str(document.get("issuer", "")).rstrip("/") != settings.oidc_issuer:
        raise SignInFailed("the provider names a different issuer than OIDC_ISSUER")
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not usable_endpoint(document.get(name)):
            raise SignInFailed(f"the provider's settings lack a usable {name}")
    for name in ("userinfo_endpoint", "end_session_endpoint"):
        if name in document and not usable_endpoint(document[name]):
            del document[name]
    _discovery[settings.oidc_issuer] = (now, document)
    return document


def safe_next(path: str | None) -> str:
    candidate = path or "/"
    if not candidate.startswith("/") or candidate.startswith(("//", "/\\")) or len(candidate) > 2000:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate) or "\\" in candidate:
        return "/"
    return candidate


def with_query(endpoint: str, params: dict[str, str]) -> str:
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)


def start_login(opened: Session, settings: Settings, next_path: str | None, now: float) -> tuple[str, str]:
    document = discovery(settings, now)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    opened.execute(delete(AuthPending).where(AuthPending.created < now - LOGIN_TTL))
    opened.execute(delete(AuthSession).where(AuthSession.expires <= now))
    surplus = select(AuthPending.state).order_by(AuthPending.created.desc()).offset(MAX_PENDING - 1)
    opened.execute(delete(AuthPending).where(AuthPending.state.in_(surplus)))
    opened.add(AuthPending(state=state, nonce=nonce, verifier=verifier, next=safe_next(next_path), kind="web", created=now))
    opened.flush()
    params = {"response_type": "code", "client_id": settings.oidc_client_id, "redirect_uri": redirect_uri(settings),
              "scope": SCOPES, "state": state, "nonce": nonce,
              "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return with_query(document["authorization_endpoint"], params), state


def take_pending(opened: Session, state: str, now: float) -> AuthPending:
    pending = opened.execute(select(AuthPending).where(AuthPending.state == state)).scalar_one_or_none()
    removed = 0
    if pending is not None:
        result = opened.execute(delete(AuthPending).where(AuthPending.state == state))
        assert isinstance(result, CursorResult)
        removed = result.rowcount
    opened.commit()
    if pending is None or removed != 1 or pending.kind != "web" or now - pending.created > LOGIN_TTL:
        raise SignInFailed("this sign-in has expired or was already used")
    return pending


def exchange_code(settings: Settings, document: dict[str, Any], code: str, verifier: str) -> dict[str, Any]:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(settings), "code_verifier": verifier}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    methods = document.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if settings.oidc_client_secret and "client_secret_basic" in methods:
        pair = f"{quote(settings.oidc_client_id, safe='')}:{quote(settings.oidc_client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        form["client_id"] = settings.oidc_client_id
        if settings.oidc_client_secret:
            form["client_secret"] = settings.oidc_client_secret
    return fetch_json(document["token_endpoint"], headers, urlencode(form).encode())


def published_keys(uri: str, refresh: bool) -> list[dict[str, Any]]:
    if refresh or uri not in _jwks:
        keys = fetch_json(uri).get("keys")
        if not isinstance(keys, list):
            raise SignInFailed("the provider's signing keys could not be read")
        _jwks[uri] = [key for key in keys if isinstance(key, dict)]
    return _jwks[uri]


def signing_key(document: dict[str, Any], kid: str | None, alg: str) -> Any:
    uri = document["jwks_uri"]
    for refresh in (False, True):
        for entry in published_keys(uri, refresh):
            if kid is not None and entry.get("kid") != kid:
                continue
            if entry.get("use", "sig") != "sig" or entry.get("kty") != ASYMMETRIC[alg] or entry.get("alg", alg) != alg:
                continue
            try:
                return jwt.PyJWK(entry, algorithm=alg).key
            except jwt.PyJWKError as error:
                raise SignInFailed("the provider's signing keys could not be read") from error
    raise SignInFailed("the ID token was signed with a key the provider does not publish")


def verify_id_token(settings: Settings, id_token: str, nonce: str, document: dict[str, Any]) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.DecodeError as error:
        raise SignInFailed("the provider did not return a readable ID token") from error
    alg = header.get("alg")
    if not isinstance(alg, str):
        raise SignInFailed("the ID token names no signature algorithm")
    if alg in ASYMMETRIC:
        kid = header.get("kid")
        key = signing_key(document, kid if isinstance(kid, str) else None, alg)
    elif alg in SYMMETRIC and settings.oidc_client_secret:
        key = settings.oidc_client_secret
    else:
        raise SignInFailed("the ID token's signature algorithm is not accepted")
    try:
        claims = jwt.decode(id_token, key, algorithms=[alg], audience=settings.oidc_client_id, leeway=LEEWAY,
                            options={"require": ["iss", "sub", "aud", "exp", "iat"], "verify_iss": False})
    except jwt.ExpiredSignatureError as error:
        raise SignInFailed("the ID token has expired") from error
    except jwt.ImmatureSignatureError as error:
        raise SignInFailed("the ID token is dated in the future") from error
    except jwt.InvalidTokenError as error:
        raise SignInFailed("the ID token did not check out") from error
    if str(claims.get("iss", "")).rstrip("/") != settings.oidc_issuer:
        raise SignInFailed("the ID token is from a different issuer")
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != settings.oidc_client_id:
        raise SignInFailed("the ID token is meant for a different app")
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
        raise SignInFailed("the ID token does not belong to this sign-in")
    return claims


def userinfo(document: dict[str, Any], access_token: str, sub: str) -> dict[str, Any]:
    endpoint = document.get("userinfo_endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return {}
    try:
        info = fetch_json(endpoint, {"Authorization": f"Bearer {access_token}"})
    except SignInFailed:
        return {}
    return info if info.get("sub") == sub else {}


def groups_of(info: Mapping[str, Any]) -> tuple[str, ...]:
    raw = info.get("groups")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({str(group).strip().lower() for group in raw if str(group).strip()}))


def email_of(info: Mapping[str, Any]) -> str | None:
    email = info.get("email")
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def allowed(settings: Settings, email: str | None, email_verified: bool, groups: tuple[str, ...]) -> bool:
    wanted = {group.lower() for group in settings.oidc_allowed_groups}
    if wanted & set(groups):
        return True
    emails = {address.lower() for address in settings.oidc_allowed_emails}
    return bool(email) and email_verified and email in emails


def authorize(settings: Settings, info: Mapping[str, Any]) -> Identity:
    sub = info.get("sub")
    if not isinstance(sub, str) or not sub:
        raise SignInFailed("the provider did not say who signed in")
    email = email_of(info)
    groups = groups_of(info)
    verified = info.get("email_verified") is True
    if not allowed(settings, email, verified, groups):
        raise NotAllowed(email or sub)
    name = info.get("name") or info.get("preferred_username")
    return Identity(sub, email, name.strip() if isinstance(name, str) and name.strip() else None, groups, verified)


def remember_user(opened: Session, who: Identity, now: float) -> User:
    user = opened.get(User, who.sub)
    if user is None:
        user = User(sub=who.sub)
        opened.add(user)
    user.email, user.email_verified, user.name = who.email, who.email_verified, who.name
    user.groups, user.last_seen = json.dumps(list(who.groups)), now
    opened.flush()
    return user


def session_life(settings: Settings) -> float:
    return min(settings.session_days, SESSION_MAX_DAYS) * 86_400


def new_session(opened: Session, settings: Settings, sub: str, now: float) -> str:
    token = secrets.token_urlsafe(32)
    opened.add(AuthSession(token_hash=hash_token(token), sub=sub, created=now, expires=now + session_life(settings)))
    opened.flush()
    return token


def finish_login(opened: Session, settings: Settings, query: Mapping[str, list[str]], login_cookie: str | None, now: float) -> tuple[str, str]:
    first = {name: values[0] for name, values in query.items() if values}
    if first.get("error"):
        raise SignInFailed("the provider refused the sign-in")
    state, code = first.get("state", ""), first.get("code", "")
    if not state or not code:
        raise SignInFailed("the sign-in reply was missing its code")
    if not login_cookie or not secrets.compare_digest(login_cookie, state):
        raise SignInFailed("this sign-in did not start in this browser")
    pending = take_pending(opened, state, now)
    document = discovery(settings, now)
    tokens = exchange_code(settings, document, code, pending.verifier)
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise SignInFailed("the provider did not return an ID token")
    claims = verify_id_token(settings, id_token, pending.nonce, document)
    info: dict[str, Any] = dict(claims)
    access_token = tokens.get("access_token")
    if isinstance(access_token, str) and access_token and "groups" not in claims:
        info = {**userinfo(document, access_token, str(claims["sub"])), **claims}
    who = authorize(settings, info)
    remember_user(opened, who, now)
    return new_session(opened, settings, who.sub, now), safe_next(pending.next)


def stored_groups(user: User) -> tuple[str, ...]:
    try:
        raw = json.loads(user.groups or "[]")
    except ValueError:
        return ()
    return groups_of({"groups": raw})


def still_allowed(settings: Settings, user: User) -> bool:
    return allowed(settings, email_of({"email": user.email}), bool(user.email_verified), stored_groups(user))


def find_session(opened: Session, token: str | None) -> AuthSession | None:
    if not token or len(token) > 200:
        return None
    return opened.get(AuthSession, hash_token(token))


def session_user(opened: Session, settings: Settings, token: str | None, now: float) -> Identity | None:
    found = find_session(opened, token)
    if found is None:
        return None
    user = opened.get(User, found.sub)
    if found.expires <= now or user is None or not still_allowed(settings, user):
        opened.delete(found)
        opened.flush()
        return None
    return Identity(user.sub, user.email, user.name, stored_groups(user), bool(user.email_verified))


def renew_session(opened: Session, settings: Settings, token: str | None, now: float) -> int | None:
    found = find_session(opened, token)
    life = session_life(settings)
    if found is None or found.expires <= now or found.expires > now + life - RENEW_AFTER:
        return None
    expires = min(now + life, found.created + SESSION_MAX_DAYS * 86_400)
    if expires <= found.expires:
        return None
    found.expires = expires
    opened.flush()
    return int(expires - now)


def access_lapsed(opened: Session, settings: Settings, sub: str | None, email: str | None, now: float) -> str | None:
    if not configured(settings):
        return None
    user = opened.get(User, sub) if sub else None
    if user is None or not still_allowed(settings, user):
        return "user_removed"
    by_email = allowed(settings, email_of({"email": user.email}), bool(user.email_verified), ())
    if not by_email and (user.last_seen is None or now - user.last_seen > session_life(settings)):
        return "sign_in_lapsed"
    return None


def provider_sign_out(settings: Settings, now: float) -> str | None:
    try:
        end = discovery(settings, now).get("end_session_endpoint")
    except SignInFailed:
        return None
    if not isinstance(end, str) or not end:
        return None
    return with_query(end, {"client_id": settings.oidc_client_id, "post_logout_redirect_uri": settings.public_url + "/auth/signed-out"})


def logout(opened: Session, settings: Settings, token: str | None, now: float) -> str:
    found = find_session(opened, token)
    if found is not None:
        opened.delete(found)
    opened.execute(delete(AuthSession).where(AuthSession.expires <= now))
    opened.flush()
    if not configured(settings):
        return "/auth/signed-out"
    return provider_sign_out(settings, now) or "/auth/signed-out"
