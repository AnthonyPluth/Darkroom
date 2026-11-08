from __future__ import annotations

import re
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from sqlalchemy import Engine

from darkroom import monitoring, oidc
from darkroom.config import Settings, browser_token_usable, demo_allowed, no_auth_allowed
from darkroom.dates import utc_now
from darkroom.domain import devices
from darkroom.server import contract
from darkroom.server.common import Principal, Request, Response, error_response, first_query, header, json_response, page, redirect
from darkroom.storage import db

SESSION_COOKIE = "darkroom_session"
LOGIN_COOKIE = "darkroom_login"
LOCAL_OWNER = Principal("session", "local", None, "Local owner", ())
MAX_COOKIE_HEADER = 8192
WORKER = Principal("worker", "browser-worker", None, "Browser worker", ())
MAX_BEARER_HEADER = 1024
MAX_DEVICE_NAME = 100
CLIENT_STATE = re.compile(r"[A-Za-z0-9_-]{22,128}")
DEVICE_SIGNED_IN = "darkroom://signed-in"


def now() -> float:
    return utc_now().timestamp()


def engine_of(request: Request) -> Engine:
    if request.engine is None:
        raise RuntimeError("this route needs the database")
    return request.engine


def cookie(settings: Settings, name: str, value: str, max_age: int, path: str = "/") -> str:
    secure = "; Secure" if oidc.secure_cookie(settings) else ""
    return f"{name}={value}; Path={path}; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"


def cleared(settings: Settings, name: str, path: str = "/") -> str:
    return cookie(settings, name, "", 0, path)


def cookies_of(request: Request) -> dict[str, str]:
    raw = header(request, "Cookie") or ""
    found: dict[str, str] = {}
    if len(raw) > MAX_COOKIE_HEADER:
        return found
    for part in raw.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name and name not in found:
            found[name] = value.strip()
    return found


def expected_origins(request: Request) -> set[str]:
    origins = set()
    public = urlsplit(request.settings.public_url)
    if public.scheme and public.netloc:
        origins.add(f"{public.scheme}://{public.netloc.lower()}")
    host = header(request, "Host")
    if host:
        origins.update(f"{scheme}://{host.lower()}" for scheme in ("http", "https"))
    return origins


def same_origin(request: Request) -> bool:
    site = (header(request, "Sec-Fetch-Site") or "").strip().lower()
    if site and site != "same-origin":
        return False
    origin = header(request, "Origin")
    if origin is None:
        return True
    parts = urlsplit(origin.strip())
    if origin.strip().lower() == "null" or not parts.scheme or not parts.netloc:
        return False
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" in expected_origins(request)


def session_principal(request: Request) -> Principal | None:
    settings = request.settings
    if no_auth_allowed(settings):
        return LOCAL_OWNER
    if not (oidc.configured(settings) or demo_allowed(settings)):
        return None
    token = cookies_of(request).get(SESSION_COOKIE)
    if not token:
        return None
    moment = now()
    with db.session(engine_of(request)) as opened:
        who = oidc.session_user(opened, settings, token, moment)
        if who is None:
            return None
        renewed = oidc.renew_cookie(opened, token, moment)
    return Principal("session", who.sub, who.email, who.name, who.groups, renewed)


def bearer_of(request: Request) -> str | None:
    raw = header(request, "Authorization") or ""
    if len(raw) > MAX_BEARER_HEADER:
        return None
    scheme, _, token = raw.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def worker_principal(request: Request) -> Principal | None:
    expected = request.settings.browser_token
    if not browser_token_usable(expected):
        return None
    presented = bearer_of(request)
    if presented is None:
        return None
    if not secrets.compare_digest(presented.encode(), expected.encode()):
        return None
    return WORKER


def session_cookie(settings: Settings, token: str, seconds: int) -> str:
    return cookie(settings, SESSION_COOKIE, token, seconds)


def login(request: Request) -> Response:
    settings = request.settings
    if no_auth_allowed(settings):
        return redirect(oidc.safe_next(first_query(request, "next")))
    if demo_allowed(settings):
        return page(503, "Demo mode", "This Darkroom runs on made-up demo data and has no sign-in provider. "
                    "Open it with the session cookie that `run.py demo` wrote to demo-session.json in its data folder.")
    if not oidc.configured(settings):
        return page(503, "Sign-in is not set up", "Darkroom has no sign-in provider configured. Set OIDC_ISSUER and OIDC_CLIENT_ID.")
    try:
        with db.session(engine_of(request)) as opened:
            target, state = oidc.start_login(opened, settings, first_query(request, "next"), now())
    except oidc.SignInFailed as error:
        monitoring.log("sign-in could not start", reason=type(error).__name__)
        return page(502, "Can't reach sign-in", "Darkroom could not reach the sign-in provider. Try again in a moment.", ("/auth/login", "Try again"))
    return redirect(target, (cookie(settings, LOGIN_COOKIE, state, int(oidc.LOGIN_TTL), "/auth"),))


def device_name_of(request: Request) -> str | None:
    name = (first_query(request, "name") or "").strip()
    return name if name and len(name) <= MAX_DEVICE_NAME and name.isprintable() else None


def client_state_of(request: Request) -> str | None:
    value = first_query(request, "state") or ""
    return value if CLIENT_STATE.fullmatch(value) else None


def device_start(request: Request) -> Response:
    settings = request.settings
    if not oidc.configured(settings):
        return page(503, "Sign-in is not set up", "Darkroom has no sign-in provider configured, so the app cannot sign in yet.")
    name = device_name_of(request)
    if name is None:
        return page(400, "Couldn't sign you in", "The app did not say which iPhone this is. Close this and sign in again from the Darkroom app.")
    client_state = client_state_of(request)
    if client_state is None:
        return page(400, "Couldn't sign you in", "The app did not send a valid sign-in code. Close this and sign in again from the Darkroom app.")
    try:
        with db.session(engine_of(request)) as opened:
            target, state = oidc.start_login(opened, settings, None, now(), device_name=name, client_state=client_state)
    except oidc.SignInFailed as error:
        monitoring.log("device sign-in could not start", reason=type(error).__name__)
        return page(502, "Can't reach sign-in", "Darkroom could not reach the sign-in provider. Close this and try again in a moment.")
    return redirect(target, (cookie(settings, LOGIN_COOKIE, state, int(oidc.LOGIN_TTL), "/auth"),))


def device_refusal(reason: str, client_state: str | None, clear_login: str) -> Response | None:
    if client_state is None or not CLIENT_STATE.fullmatch(client_state):
        return None
    return redirect(f"{DEVICE_SIGNED_IN}#error={reason}&state={client_state}", (clear_login,))


def device_callback(request: Request, clear_login: str, moment: float) -> Response:
    settings = request.settings
    client_state: str | None = None
    try:
        with db.session(engine_of(request)) as opened:
            who, name, client_state = oidc.finish_device_login(opened, settings, request.query, cookies_of(request).get(LOGIN_COOKIE), moment)
            _, token = devices.enrol(opened, who.sub, who.email, who.email_verified, name, moment)
    except oidc.NotAllowed as error:
        monitoring.log("device sign-in refused", reason="not_allowed")
        returned = device_refusal("not_allowed", error.client_state, clear_login)
        if returned is not None:
            return returned
        return page(403, "Not allowed", f"You signed in as {error.who}, which is not allowed to use the Darkroom app.", None, (clear_login,),
                    "If this is your account, ask whoever runs Darkroom to add it to the family group in Pocket ID, then try again.")
    except devices.NoPerson as error:
        monitoring.log("device sign-in refused", reason="no_person")
        returned = device_refusal("no_person", client_state, clear_login)
        if returned is not None:
            return returned
        return page(403, "No person with this email", f"{error}. Ask whoever runs Darkroom to add your email to your person on the People page, then sign in again.",
                    None, (clear_login,))
    except oidc.ProviderUnreachable as error:
        monitoring.log("device sign-in failed", reason=type(error).__name__)
        return page(502, "Can't reach sign-in", "Darkroom could not reach the sign-in provider. Close this and try again in a moment.", None, (clear_login,))
    except oidc.SignInFailed as error:
        monitoring.log("device sign-in failed", reason=type(error).__name__)
        return page(403, "Couldn't sign you in", f"Something was wrong with this sign-in: {error}. Close this and try again from the Darkroom app.", None, (clear_login,))
    monitoring.log("device signed in")
    return redirect(f"{DEVICE_SIGNED_IN}#token={token}&state={client_state}", (clear_login,))


def callback(request: Request) -> Response:
    settings = request.settings
    if not oidc.configured(settings):
        return error_response("not found", 404)
    clear_login = cleared(settings, LOGIN_COOKIE, "/auth")
    moment = now()
    with db.session(engine_of(request)) as opened:
        kind = oidc.pending_kind(opened, first_query(request, "state"))
    if kind == "device":
        return device_callback(request, clear_login, moment)
    try:
        with db.session(engine_of(request)) as opened:
            signed = oidc.finish_login(opened, settings, request.query, cookies_of(request).get(LOGIN_COOKIE), moment)
            life = oidc.cookie_life(oidc.find_session(opened, signed.token) or oidc.AuthSession(expires=None), moment)
    except oidc.NotAllowed as error:
        monitoring.log("sign-in refused", reason="not_allowed")
        return page(403, "Not allowed", f"You signed in as {error.who}, which is not allowed to use this Darkroom. Darkroom shows nothing to this account.",
                    ("/auth/login", "Sign in with a different account"), (clear_login,),
                    "If this is your account, ask whoever runs Darkroom to allow it in Pocket ID, then try again.")
    except oidc.ProviderUnreachable as error:
        monitoring.log("sign-in failed", reason=type(error).__name__)
        return page(502, "Can't reach sign-in", "Darkroom could not reach the sign-in provider. Try again in a moment.", ("/auth/login", "Try again"), (clear_login,))
    except oidc.SignInFailed as error:
        monitoring.log("sign-in failed", reason=type(error).__name__)
        return page(403, "Couldn't sign you in", f"Something was wrong with this sign-in: {error}. Please try again.", ("/auth/login", "Try again"), (clear_login,))
    if signed.rechecks:
        monitoring.log("signed in", rechecks=True)
    else:
        monitoring.log("signed in; the provider granted no refresh token so this session cannot be re-checked and ends after the cap", rechecks=False, cap_days=oidc.NO_RECHECK_DAYS)
    return redirect(signed.next_path, (session_cookie(settings, signed.token, life), clear_login))


def logout(request: Request) -> Response:
    settings = request.settings
    token = cookies_of(request).get(SESSION_COOKIE)
    with db.session(engine_of(request)) as opened:
        target = oidc.logout(opened, settings, token, now())
    monitoring.log("signed out")
    return redirect(target, (cleared(settings, SESSION_COOKIE),))


def signed_out(request: Request) -> Response:
    return page(200, "Signed out", "You have signed out of Darkroom.", ("/auth/login", "Sign in again"))


def me(request: Request) -> Annotated[Response, contract.Me]:
    who = request.user
    if who is None:
        return error_response("sign in required", 401)
    return json_response({"sub": who.sub, "email": who.email or "", "name": who.name or "", "groups": list(who.groups)})
