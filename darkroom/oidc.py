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
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session

from darkroom import tls
from darkroom.config import Settings, demo_allowed, local_host, oidc_configured
from darkroom.storage import secretbox
from darkroom.storage.models import AuthPending, AuthSession, User

LOGIN_TTL = 600.0
DEMO_SUB = "demo:"
MAX_PENDING = 10_000
DISCOVERY_TTL = 3600.0
RENEW_COOKIE_AFTER = 86_400.0
COOKIE_MAX_AGE = 400 * 86_400
NO_RECHECK_DAYS = 30
RECHECK_AFTER = 86_400.0
LEEWAY = 60
MAX_REPLY = 1_000_000
SCOPES = "openid email profile groups offline_access"
ASYMMETRIC = {"RS256": "RSA", "RS384": "RSA", "RS512": "RSA", "PS256": "RSA", "PS384": "RSA", "PS512": "RSA",
              "ES256": "EC", "ES384": "EC", "ES512": "EC", "EdDSA": "OKP"}
SYMMETRIC = {"HS256", "HS384", "HS512"}
USER_AGENT = "Darkroom/0.1"
REFUSED_STATUSES = (400, 401, 403)

configured = oidc_configured

_discovery: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks: dict[str, list[dict[str, Any]]] = {}


class SignInFailed(Exception):
    pass


class ProviderUnreachable(SignInFailed):
    pass


class ProviderRefused(SignInFailed):
    def __init__(self, status: int) -> None:
        super().__init__(f"the provider answered {status}")
        self.status = status


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


@dataclass(frozen=True)
class SignedIn:
    token: str
    next_path: str
    rechecks: bool


@dataclass
class RecheckSummary:
    checked: int = 0
    kept: int = 0
    ended: int = 0
    capped: int = 0
    unreachable: int = 0

    def notable(self) -> bool:
        return bool(self.ended or self.capped or self.unreachable)

    def detail(self) -> str:
        return f"checked={self.checked} kept={self.kept} ended={self.ended} capped={self.capped} unreachable={self.unreachable}"


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
            if error.code in REFUSED_STATUSES:
                raise ProviderRefused(error.code) from error
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


def recheck_problems(settings: Settings, now: float) -> list[str]:
    if not configured(settings):
        return []
    try:
        document = discovery(settings, now)
    except SignInFailed as error:
        return [f"OIDC_ISSUER could not be read ({error}); sessions cannot be re-checked until it can"]
    scopes = document.get("scopes_supported")
    if isinstance(scopes, list) and "offline_access" not in scopes:
        return [f"the provider does not offer offline_access, so sessions cannot be re-checked and end after {NO_RECHECK_DAYS} days"]
    return []


def safe_next(path: str | None) -> str:
    candidate = path or "/"
    if not candidate.startswith("/") or candidate.startswith(("//", "/\\")) or len(candidate) > 2000:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate) or "\\" in candidate:
        return "/"
    return candidate


def with_query(endpoint: str, params: dict[str, str]) -> str:
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)


def start_login(opened: Session, settings: Settings, next_path: str | None, now: float, device_name: str | None = None) -> tuple[str, str]:
    document = discovery(settings, now)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    opened.execute(delete(AuthPending).where(AuthPending.created < now - LOGIN_TTL))
    sweep_expired(opened, now)
    surplus = select(AuthPending.state).order_by(AuthPending.created.desc()).offset(MAX_PENDING - 1)
    opened.execute(delete(AuthPending).where(AuthPending.state.in_(surplus)))
    kind = "web" if device_name is None else "device"
    opened.add(AuthPending(state=state, nonce=nonce, verifier=verifier, next=safe_next(next_path) if device_name is None else None, kind=kind,
                           device_name=device_name, created=now))
    opened.flush()
    params = {"response_type": "code", "client_id": settings.oidc_client_id, "redirect_uri": redirect_uri(settings),
              "scope": SCOPES, "state": state, "nonce": nonce,
              "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return with_query(document["authorization_endpoint"], params), state


def pending_kind(opened: Session, state: str | None) -> str | None:
    if not state or len(state) > 200:
        return None
    return opened.execute(select(AuthPending.kind).where(AuthPending.state == state)).scalar_one_or_none()


def take_pending(opened: Session, state: str, now: float, kind: str = "web") -> AuthPending:
    pending = opened.execute(select(AuthPending).where(AuthPending.state == state)).scalar_one_or_none()
    removed = 0
    if pending is not None:
        result = opened.execute(delete(AuthPending).where(AuthPending.state == state))
        assert isinstance(result, CursorResult)
        removed = result.rowcount
    opened.commit()
    if pending is None or removed != 1 or pending.kind != kind or now - pending.created > LOGIN_TTL:
        raise SignInFailed("this sign-in has expired or was already used")
    return pending


def token_request(settings: Settings, document: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
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


def exchange_code(settings: Settings, document: dict[str, Any], code: str, verifier: str) -> dict[str, Any]:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(settings), "code_verifier": verifier}
    return token_request(settings, document, form)


def refresh_tokens(settings: Settings, document: dict[str, Any], refresh_token: str) -> dict[str, Any]:
    return token_request(settings, document, {"grant_type": "refresh_token", "refresh_token": refresh_token})


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


def verify_id_token(settings: Settings, id_token: str, nonce: str | None, document: dict[str, Any]) -> dict[str, Any]:
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
    if nonce is not None and (not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce)):
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


def claims_from(settings: Settings, document: dict[str, Any], tokens: Mapping[str, Any], nonce: str | None, sub: str | None) -> dict[str, Any]:
    id_token = tokens.get("id_token")
    claims: dict[str, Any] = {}
    if isinstance(id_token, str):
        claims = dict(verify_id_token(settings, id_token, nonce, document))
        if sub is not None and claims.get("sub") != sub:
            raise SignInFailed("the ID token names someone else")
    elif nonce is not None:
        raise SignInFailed("the provider did not return an ID token")
    access_token = tokens.get("access_token")
    known = claims.get("sub", sub)
    if isinstance(access_token, str) and access_token and isinstance(known, str) and "groups" not in claims:
        claims = {**userinfo(document, access_token, known), **claims}
    return claims


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


def is_demo_sub(sub: str) -> bool:
    return sub.startswith(DEMO_SUB)


def accepted_here(settings: Settings, sub: str) -> bool:
    return demo_allowed(settings) if is_demo_sub(sub) else configured(settings)


def authorize(settings: Settings, info: Mapping[str, Any]) -> Identity:
    sub = info.get("sub")
    if not isinstance(sub, str) or not sub:
        raise SignInFailed("the provider did not say who signed in")
    if is_demo_sub(sub):
        raise SignInFailed("the provider named a demo user")
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


def seal_refresh_token(settings: Settings, refresh_token: object) -> str | None:
    if not isinstance(refresh_token, str) or not refresh_token or not settings.secret_key:
        return None
    return secretbox.seal(refresh_token, settings.secret_key)


def no_recheck_cap(now: float) -> float:
    return now + NO_RECHECK_DAYS * 86_400


def new_session(opened: Session, settings: Settings, sub: str, now: float, refresh_token: object = None) -> str:
    token = secrets.token_urlsafe(32)
    sealed = seal_refresh_token(settings, refresh_token)
    opened.add(AuthSession(token_hash=hash_token(token), sub=sub, created=now, expires=None if sealed else no_recheck_cap(now),
                           refresh_token_sealed=sealed, last_checked=now, cookie_renewed=now))
    opened.flush()
    return token


def demo_session(opened: Session, settings: Settings, who: Identity, now: float) -> str:
    if not is_demo_sub(who.sub):
        raise ValueError("a demo session is only minted for a demo user")
    if not demo_allowed(settings):
        raise ValueError("a demo session is only minted for a server in demo mode")
    remember_user(opened, who, now)
    return new_session(opened, settings, who.sub, now)


def callback_state(query: Mapping[str, list[str]], login_cookie: str | None) -> tuple[str, str]:
    first = {name: values[0] for name, values in query.items() if values}
    if first.get("error"):
        raise SignInFailed("the provider refused the sign-in")
    state, code = first.get("state", ""), first.get("code", "")
    if not state or not code:
        raise SignInFailed("the sign-in reply was missing its code")
    if not login_cookie or not secrets.compare_digest(login_cookie, state):
        raise SignInFailed("this sign-in did not start in this browser")
    return state, code


def finish_login(opened: Session, settings: Settings, query: Mapping[str, list[str]], login_cookie: str | None, now: float) -> SignedIn:
    state, code = callback_state(query, login_cookie)
    pending = take_pending(opened, state, now)
    document = discovery(settings, now)
    tokens = exchange_code(settings, document, code, pending.verifier)
    who = authorize(settings, claims_from(settings, document, tokens, pending.nonce, None))
    remember_user(opened, who, now)
    token = new_session(opened, settings, who.sub, now, tokens.get("refresh_token"))
    return SignedIn(token, safe_next(pending.next), seal_refresh_token(settings, tokens.get("refresh_token")) is not None)


def authorize_device(settings: Settings, info: Mapping[str, Any]) -> Identity:
    sub = info.get("sub")
    if not isinstance(sub, str) or not sub:
        raise SignInFailed("the provider did not say who signed in")
    if is_demo_sub(sub):
        raise SignInFailed("the provider named a demo user")
    email = email_of(info)
    groups = groups_of(info)
    if not device_groups(settings) & set(groups):
        raise NotAllowed(email or sub)
    name = info.get("name") or info.get("preferred_username")
    return Identity(sub, email, name.strip() if isinstance(name, str) and name.strip() else None, groups, info.get("email_verified") is True)


def device_groups(settings: Settings) -> set[str]:
    return {group.lower() for group in settings.oidc_device_groups}


def finish_device_login(opened: Session, settings: Settings, query: Mapping[str, list[str]], login_cookie: str | None, now: float) -> tuple[Identity, str]:
    state, code = callback_state(query, login_cookie)
    pending = take_pending(opened, state, now, "device")
    document = discovery(settings, now)
    tokens = exchange_code(settings, document, code, pending.verifier)
    who = authorize_device(settings, claims_from(settings, document, tokens, pending.nonce, None))
    remember_user(opened, who, now)
    return who, pending.device_name or "iPhone"


def device_access_lapsed(opened: Session, settings: Settings, email: str | None, now: float) -> str | None:
    if not configured(settings):
        return "sign_in_unavailable"
    address = email_of({"email": email})
    if address is None:
        return "user_removed"
    users = opened.execute(select(User).where(func.lower(User.email) == address, User.email_verified.is_(True))).scalars().all()
    allowed_users = [user for user in users if device_groups(settings) & set(stored_groups(user))]
    if not allowed_users:
        return "user_removed"
    if all(user.last_seen is None or now - user.last_seen > NO_RECHECK_DAYS * 86_400 for user in allowed_users):
        return "sign_in_lapsed"
    return None


def stored_groups(user: User) -> tuple[str, ...]:
    try:
        raw = json.loads(user.groups or "[]")
    except ValueError:
        return ()
    return groups_of({"groups": raw})


def still_allowed(settings: Settings, user: User) -> bool:
    return allowed(settings, email_of({"email": user.email}), bool(user.email_verified), stored_groups(user))


def session_accepted(settings: Settings, user: User) -> bool:
    if not accepted_here(settings, user.sub):
        return False
    return is_demo_sub(user.sub) or still_allowed(settings, user)


def find_session(opened: Session, token: str | None) -> AuthSession | None:
    if not token or len(token) > 200:
        return None
    return opened.get(AuthSession, hash_token(token))


def expired(found: AuthSession, now: float) -> bool:
    return found.expires is not None and found.expires <= now


def session_user(opened: Session, settings: Settings, token: str | None, now: float) -> Identity | None:
    found = find_session(opened, token)
    if found is None:
        return None
    user = opened.get(User, found.sub)
    if expired(found, now) or user is None or not session_accepted(settings, user):
        opened.delete(found)
        opened.flush()
        return None
    return Identity(user.sub, user.email, user.name, stored_groups(user), bool(user.email_verified))


def cookie_life(found: AuthSession, now: float) -> int:
    remaining = COOKIE_MAX_AGE if found.expires is None else int(found.expires - now)
    return max(0, min(COOKIE_MAX_AGE, remaining))


def renew_cookie(opened: Session, token: str | None, now: float) -> int | None:
    found = find_session(opened, token)
    if found is None or expired(found, now) or (found.cookie_renewed is not None and now - found.cookie_renewed < RENEW_COOKIE_AFTER):
        return None
    found.cookie_renewed = now
    opened.flush()
    return cookie_life(found, now)


def access_lapsed(opened: Session, settings: Settings, sub: str | None, email: str | None, now: float) -> str | None:
    if not configured(settings):
        return None
    user = opened.get(User, sub) if sub else None
    if user is None or not still_allowed(settings, user):
        return "user_removed"
    by_email = allowed(settings, email_of({"email": user.email}), bool(user.email_verified), ())
    if not by_email and (user.last_seen is None or now - user.last_seen > NO_RECHECK_DAYS * 86_400):
        return "sign_in_lapsed"
    return None


def sweep_expired(opened: Session, now: float) -> None:
    opened.execute(delete(AuthSession).where(AuthSession.expires.is_not(None), AuthSession.expires <= now))


def end_sessions_of(opened: Session, sub: str) -> int:
    result = opened.execute(delete(AuthSession).where(AuthSession.sub == sub))
    assert isinstance(result, CursorResult)
    return result.rowcount


def revoke_sessions(opened: Session, *, email: str | None = None, sub: str | None = None, everyone: bool = False) -> int:
    if everyone:
        result = opened.execute(delete(AuthSession))
        assert isinstance(result, CursorResult)
        return result.rowcount
    if email:
        subs = select(User.sub).where(func.lower(User.email) == email.strip().lower())
        result = opened.execute(delete(AuthSession).where(AuthSession.sub.in_(subs)))
        assert isinstance(result, CursorResult)
        return result.rowcount
    return end_sessions_of(opened, sub) if sub else 0


def recheck_session(opened: Session, settings: Settings, found: AuthSession, now: float) -> str:
    try:
        refresh_token = secretbox.unseal(found.refresh_token_sealed or "", settings.secret_key)
    except secretbox.Unreadable:
        found.refresh_token_sealed, found.expires, found.last_checked = None, no_recheck_cap(now), now
        return "capped"
    try:
        document = discovery(settings, now)
        tokens = refresh_tokens(settings, document, refresh_token)
        info = claims_from(settings, document, tokens, None, found.sub)
        if not info:
            found.expires, found.last_checked = found.expires or no_recheck_cap(now), now
            return "capped"
        info = {**info, "sub": found.sub}
        name = info.get("name") or info.get("preferred_username")
        remember_user(opened, Identity(found.sub, email_of(info), name.strip() if isinstance(name, str) and name.strip() else None,
                                       groups_of(info), info.get("email_verified") is True), now)
        who = authorize(settings, info)
    except ProviderUnreachable:
        return "unreachable"
    except NotAllowed:
        end_sessions_of(opened, found.sub)
        return "ended"
    except ProviderRefused:
        opened.delete(found)
        return "ended"
    except SignInFailed:
        found.expires, found.last_checked = found.expires or no_recheck_cap(now), now
        return "capped"
    assert who.sub == found.sub
    found.refresh_token_sealed = seal_refresh_token(settings, tokens.get("refresh_token")) or found.refresh_token_sealed
    found.expires, found.last_checked = None, now
    return "kept"


def recheck_due(opened: Session, settings: Settings, now: float) -> RecheckSummary:
    summary = RecheckSummary()
    sweep_expired(opened, now)
    if not configured(settings):
        return summary
    due = select(AuthSession).where(AuthSession.refresh_token_sealed.is_not(None),
                                    (AuthSession.last_checked.is_(None)) | (AuthSession.last_checked <= now - RECHECK_AFTER))
    for found in opened.execute(due).scalars().all():
        if opened.get(AuthSession, found.token_hash) is None:
            continue
        summary.checked += 1
        outcome = recheck_session(opened, settings, found, now)
        setattr(summary, outcome, getattr(summary, outcome) + 1)
        opened.flush()
    return summary


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
    sweep_expired(opened, now)
    opened.flush()
    if not configured(settings):
        return "/auth/signed-out"
    return provider_sign_out(settings, now) or "/auth/signed-out"
