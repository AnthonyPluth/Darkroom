from __future__ import annotations

import dataclasses
import ipaddress
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring, oidc
from darkroom.server import auth
from darkroom.server.common import Principal, Request, Response, engine_of, first_query, header, now, page, redirect
from darkroom.storage import db, secretbox, stored_settings
from darkroom.storage.models import Setting, User
from darkroom.storage.settings_keys import SETUP_CLAIM, SETUP_LOCKED, SETUP_SIGN_IN, SETUP_TESTED

CLAIM_COOKIE = "darkroom_setup"
TEST_COOKIE = "darkroom_setup_login"
CLAIM_MAX_AGE = 30 * 86_400
FIRST_VISITOR = Principal("setup", "setup-visitor", None, "First visitor", ())
FORWARDING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP")
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7"))
SIGN_IN_VARIABLES = ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ALLOWED_GROUPS", "OIDC_ALLOWED_EMAILS")
REOPENED_KEYS = (SETUP_CLAIM, SETUP_LOCKED, SETUP_SIGN_IN)
CLOSED, UNCLAIMED, CLAIMANT, OTHER = "closed", "unclaimed", "claimant", "other"
BEFORE_TEST, AFTER_TEST = "before_test", "after_test"
AFTER_TEST_SECONDS = 7 * 86_400
TEST_PATH = "/setup?proposal="
SETUP_PAGE = "/setup"


@dataclass(frozen=True)
class Tested:
    sub: str
    email: str
    session: str
    at: float


@dataclass(frozen=True)
class Window:
    phase: str
    claim: str = ""
    tested: Tested | None = None


SHUT = Window(CLOSED)


def private_peer(client: str) -> bool:
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in PRIVATE_NETWORKS)


def turned_on(settings: config.Settings) -> bool:
    return settings.wizard and not (settings.allow_no_auth or settings.demo)


def open_by_settings(settings: config.Settings) -> bool:
    return turned_on(settings) and not config.oidc_configured(settings)


def from_this_network(request: Request) -> bool:
    if any(header(request, name) is not None for name in FORWARDING_HEADERS):
        return False
    return private_peer(request.client) and auth.no_auth_address_allowed(request)


def read_tested(value: str | None) -> Tested | None:
    try:
        fields = json.loads(value or "")
    except ValueError:
        return None
    if not isinstance(fields, dict) or not all(isinstance(fields.get(name), str) for name in ("sub", "email", "session")):
        return None
    moment = fields.get("at")
    if isinstance(moment, bool) or not isinstance(moment, (int, float)) or not fields["sub"] or not fields["session"]:
        return None
    return Tested(fields["sub"], fields["email"], fields["session"], float(moment))


def someone_else_signed_in(opened: Session, sub: str | None) -> bool:
    others = select(User.sub).limit(1) if sub is None else select(User.sub).where(User.sub != sub).limit(1)
    return opened.scalars(others).first() is not None


def window(opened: Session, at: float) -> Window:
    if opened.get(Setting, SETUP_LOCKED) is not None:
        return SHUT
    row = opened.get(Setting, SETUP_TESTED)
    if row is None:
        return SHUT if someone_else_signed_in(opened, None) else Window(BEFORE_TEST, stored_claim(opened))
    found = read_tested(row.value)
    if found is None or at >= found.at + AFTER_TEST_SECONDS or someone_else_signed_in(opened, found.sub):
        return SHUT
    return Window(AFTER_TEST, stored_claim(opened), found)


def holds_claim(request: Request, claim_hash: str) -> bool:
    token = auth.cookies_of(request).get(CLAIM_COOKIE, "")
    return bool(claim_hash) and bool(token) and len(token) <= 200 and secrets.compare_digest(oidc.hash_token(token), claim_hash)


def holds_tested_session(opened: Session, request: Request, found: Tested, at: float) -> bool:
    token = auth.cookies_of(request).get(auth.SESSION_COOKIE, "")
    if not token or len(token) > 200 or not secrets.compare_digest(oidc.hash_token(token), found.session):
        return False
    who = oidc.session_user(opened, request.settings, token, at)
    return who is not None and who.sub == found.sub


def stored_claim(opened: Session) -> str:
    found = opened.get(Setting, SETUP_CLAIM)
    return found.value or "" if found is not None else ""


def standing(request: Request) -> tuple[str, str]:
    if not (turned_on(request.settings) and from_this_network(request)):
        return CLOSED, CLOSED
    moment = now()
    with db.session(engine_of(request)) as opened:
        found = window(opened, moment)
        if found.phase == AFTER_TEST and found.tested is not None:
            paired = holds_claim(request, found.claim) and holds_tested_session(opened, request, found.tested, moment)
            return (CLAIMANT, AFTER_TEST) if paired else (CLOSED, CLOSED)
    if found.phase != BEFORE_TEST or not open_by_settings(request.settings):
        return CLOSED, CLOSED
    if not found.claim:
        return UNCLAIMED, BEFORE_TEST
    return (CLAIMANT if holds_claim(request, found.claim) else OTHER), BEFORE_TEST


def visitor(request: Request) -> str:
    return standing(request)[0]


def principal(request: Request) -> Principal | None:
    return FIRST_VISITOR if visitor(request) == CLAIMANT else None


def claim_principal(request: Request) -> Principal | None:
    return None if visitor(request) == CLOSED else FIRST_VISITOR


def load_proposal(opened: Session, key: str) -> dict[str, str] | None:
    found = opened.get(Setting, SETUP_SIGN_IN)
    if found is None or not found.value:
        return None
    try:
        fields = json.loads(secretbox.unseal(found.value, key))
    except (secretbox.Unreadable, ValueError):
        return None
    if not isinstance(fields, dict) or not all(isinstance(fields.get(name), str) for name in (*SIGN_IN_VARIABLES, "id")):
        return None
    return fields


def save_proposal(opened: Session, fields: dict[str, str], key: str) -> None:
    db.upsert(opened, Setting, {"key": SETUP_SIGN_IN, "value": secretbox.seal(json.dumps(fields), key)}, ("key",))


def live_settings(request: Request) -> config.Settings:
    return stored_settings.effective(request.settings, engine_of(request)).settings


def proposed_settings(live: config.Settings, fields: dict[str, str]) -> config.Settings:
    groups = config.comma_list(fields["OIDC_ALLOWED_GROUPS"]) or config.groups_without_a_value(live)
    return dataclasses.replace(live, oidc_issuer=fields["OIDC_ISSUER"], oidc_client_id=fields["OIDC_CLIENT_ID"], oidc_client_secret=fields["OIDC_CLIENT_SECRET"],
                               oidc_allowed_groups=groups, oidc_allowed_emails=config.comma_list(fields["OIDC_ALLOWED_EMAILS"]))


def same_address(request: Request, public_url: str) -> bool:
    try:
        public = urlsplit(public_url)
    except ValueError:
        return False
    host = (header(request, "Host") or "").strip().lower()
    return public.scheme in ("http", "https") and bool(host) and public.netloc.lower() == host


def test_page(status: int, title: str, message: str, request: Request) -> Response:
    return page(status, title, message, cookies=(auth.cleared(request.settings, TEST_COOKIE, "/auth"),))


def callback(request: Request) -> Response:
    state = auth.cookies_of(request).get(TEST_COOKIE)
    if not state or first_query(request, "state") != state:
        return auth.callback(request)
    return finish_test(request, state)


def finish_test(request: Request, state: str) -> Response:
    settings = request.settings
    if standing(request) != (CLAIMANT, BEFORE_TEST):
        return test_page(403, "Not allowed", "This test sign-in did not start here, so nothing was saved.", request)
    live = live_settings(request)
    with db.session(engine_of(request)) as opened:
        fields = load_proposal(opened, settings.secret_key)
    if fields is None:
        return test_page(409, "Nothing to test", "There are no proposed sign-in settings to test, so nothing was saved.", request)
    tested_settings = proposed_settings(live, fields)
    try:
        with db.session(engine_of(request)) as opened:
            who, refresh_token = oidc.finish_test_login(opened, tested_settings, request.query, state, now(), TEST_PATH + fields["id"])
    except oidc.NotAllowed:
        monitoring.log("test sign-in refused", reason="not_allowed")
        return test_page(403, "Not allowed", "The account you signed in with is not allowed by the proposed settings, so nothing was saved. "
                         "Check the allowed groups and email addresses, then test again.", request)
    except oidc.SignInFailed as error:
        monitoring.log("test sign-in failed", reason=type(error).__name__)
        return test_page(403, "The test sign-in failed", f"Something was wrong with this sign-in: {error}. Nothing was saved.", request)
    with db.session(engine_of(request), writing=True) as opened:
        moment = now()
        if window(opened, moment).phase != BEFORE_TEST:
            return test_page(403, "Not allowed", "This test sign-in did not start here, so nothing was saved.", request)
        current = load_proposal(opened, settings.secret_key)
        if current is None or current["id"] != fields["id"]:
            return test_page(409, "The settings changed", "The proposed sign-in settings changed during the test, so nothing was saved. Test again.", request)
        for variable in SIGN_IN_VARIABLES:
            stored_settings.store(opened, variable, fields[variable], settings.secret_key, moment)
        oidc.remember_user(opened, who, moment)
        token = oidc.new_session(opened, tested_settings, who.sub, moment, refresh_token)
        life = oidc.cookie_life(oidc.find_session(opened, token) or oidc.AuthSession(expires=None), moment)
        opened.execute(delete(Setting).where(Setting.key == SETUP_SIGN_IN))
        tested = {"sub": who.sub, "email": who.email or "", "session": oidc.hash_token(token), "at": moment}
        opened.add(Setting(key=SETUP_TESTED, value=json.dumps(tested)))
    rechecks = oidc.seal_refresh_token(tested_settings, refresh_token) is not None
    monitoring.log("test sign-in passed; the sign-in settings are stored, the account is signed in and setup continues", rechecks=rechecks)
    return redirect(SETUP_PAGE, (auth.session_cookie(tested_settings, token, life), auth.cleared(settings, TEST_COOKIE, "/auth")))


def close(opened: Session) -> None:
    opened.execute(delete(Setting).where(Setting.key.in_((SETUP_CLAIM, SETUP_SIGN_IN))))
    db.upsert(opened, Setting, {"key": SETUP_LOCKED, "value": "1"}, ("key",))


def reopen(opened: Session) -> bool:
    if opened.get(Setting, SETUP_TESTED) is not None:
        close(opened)
        return False
    opened.execute(delete(Setting).where(Setting.key.in_(REOPENED_KEYS)))
    return True
