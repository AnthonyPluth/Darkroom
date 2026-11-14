from __future__ import annotations

import dataclasses
import ipaddress
import json
import secrets
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring, oidc
from darkroom.server import auth
from darkroom.server.common import Principal, Request, Response, engine_of, first_query, header, now, page
from darkroom.storage import db, secretbox, stored_settings
from darkroom.storage.models import Setting, User
from darkroom.storage.settings_keys import SETUP_CLAIM, SETUP_LOCKED, SETUP_SIGN_IN

CLAIM_COOKIE = "darkroom_setup"
TEST_COOKIE = "darkroom_setup_login"
CLAIM_MAX_AGE = 30 * 86_400
FIRST_VISITOR = Principal("setup", "setup-visitor", None, "First visitor", ())
FORWARDING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP")
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7"))
SIGN_IN_VARIABLES = ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ALLOWED_GROUPS", "OIDC_ALLOWED_EMAILS")
REOPENED_KEYS = (SETUP_CLAIM, SETUP_LOCKED, SETUP_SIGN_IN)
CLOSED, UNCLAIMED, CLAIMANT, OTHER = "closed", "unclaimed", "claimant", "other"
TEST_PATH = "/setup?proposal="


def private_peer(client: str) -> bool:
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in PRIVATE_NETWORKS)


def open_by_settings(settings: config.Settings) -> bool:
    return settings.wizard and not (config.oidc_configured(settings) or config.no_auth_allowed(settings) or config.demo_allowed(settings))


def from_this_network(request: Request) -> bool:
    if any(header(request, name) is not None for name in FORWARDING_HEADERS):
        return False
    return private_peer(request.client) and auth.no_auth_address_allowed(request)


def locked(opened: Session) -> bool:
    return opened.get(Setting, SETUP_LOCKED) is not None or opened.scalars(select(User.sub).limit(1)).first() is not None


def holds_claim(request: Request, claim_hash: str) -> bool:
    token = auth.cookies_of(request).get(CLAIM_COOKIE, "")
    return bool(token) and len(token) <= 200 and secrets.compare_digest(oidc.hash_token(token), claim_hash)


def stored_claim(opened: Session) -> str:
    found = opened.get(Setting, SETUP_CLAIM)
    return found.value or "" if found is not None else ""


def visitor(request: Request) -> str:
    if not (open_by_settings(request.settings) and from_this_network(request)):
        return CLOSED
    with db.session(engine_of(request)) as opened:
        if locked(opened):
            return CLOSED
        claim = stored_claim(opened)
    if not claim:
        return UNCLAIMED
    return CLAIMANT if holds_claim(request, claim) else OTHER


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
    if principal(request) is None:
        return test_page(403, "Not allowed", "This test sign-in did not start here, so nothing was saved.", request)
    live = live_settings(request)
    with db.session(engine_of(request)) as opened:
        fields = load_proposal(opened, settings.secret_key)
    if fields is None:
        return test_page(409, "Nothing to test", "There are no proposed sign-in settings to test, so nothing was saved.", request)
    try:
        with db.session(engine_of(request)) as opened:
            oidc.finish_test_login(opened, proposed_settings(live, fields), request.query, state, now(), TEST_PATH + fields["id"])
    except oidc.NotAllowed:
        monitoring.log("test sign-in refused", reason="not_allowed")
        return test_page(403, "Not allowed", "The account you signed in with is not allowed by the proposed settings, so nothing was saved. "
                         "Check the allowed groups and email addresses, then test again.", request)
    except oidc.SignInFailed as error:
        monitoring.log("test sign-in failed", reason=type(error).__name__)
        return test_page(403, "The test sign-in failed", f"Something was wrong with this sign-in: {error}. Nothing was saved.", request)
    with db.session(engine_of(request), writing=True) as opened:
        current = load_proposal(opened, settings.secret_key)
        if current is None or current["id"] != fields["id"]:
            return test_page(409, "The settings changed", "The proposed sign-in settings changed during the test, so nothing was saved. Test again.", request)
        moment = now()
        for variable in SIGN_IN_VARIABLES:
            stored_settings.store(opened, variable, fields[variable], settings.secret_key, moment)
        opened.execute(delete(Setting).where(Setting.key.in_((SETUP_SIGN_IN, SETUP_CLAIM))))
        db.upsert(opened, Setting, {"key": SETUP_LOCKED, "value": "1"}, ("key",))
    monitoring.log("test sign-in passed; the sign-in settings are stored and the setup window is closed")
    return page(200, "Sign-in works", "Darkroom stored these sign-in settings. Restart Darkroom to turn sign-in on, then sign in.", ("/auth/login", "Sign in"),
                (auth.cleared(settings, TEST_COOKIE, "/auth"), auth.cleared(settings, CLAIM_COOKIE)))


def reopen(opened: Session) -> None:
    opened.execute(delete(Setting).where(Setting.key.in_(REOPENED_KEYS)))
