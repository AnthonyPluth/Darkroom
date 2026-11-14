from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlsplit

from darkroom import config, monitoring, oidc, validate
from darkroom.server import auth, contract, wizard
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now
from darkroom.storage import db, secretbox, stored_settings
from darkroom.storage.models import Setting
from darkroom.storage.settings_keys import SETUP_CLAIM

MAX_BODY = 16 * 1024
MOST_ENTRIES = 20
CLAIMED_ELSEWHERE = "the setup wizard is already open in another browser; to start again there, run `python run.py reopen-setup` on the server"
NO_KEY = "Darkroom has no secret key to seal settings with; check the secret-key file in DARKROOM_DATA_DIR"
NO_PROPOSAL = "there are no proposed sign-in settings to test; send them first"
NO_ADDRESS = "confirm the address Darkroom is opened at first, so the sign-in can come back to it"


def from_environment(request: Request, *variables: str) -> Response | None:
    for variable in variables:
        if variable in request.settings.from_environment:
            return error_response(f"{variable} is set in the environment, which always wins over the wizard; change it there", 409)
    return None


def plain(value: str, name: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise validate.Invalid(f"{name} must not contain control characters")
    return value


def token_like(body: dict[str, object], name: str, *, limit: int = 200) -> str:
    value = plain(validate.text(body, name, limit=limit), name)
    if any(character.isspace() for character in value) or not value.isascii():
        raise validate.Invalid(f"{name} must be printable ASCII without spaces")
    return value


def address(body: dict[str, object], name: str, *, with_path: bool) -> str:
    value = plain(validate.text(body, name, limit=500), name)
    wrong = validate.Invalid(f"{name} must be an http:// or https:// address without a user name, query or fragment")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise wrong from error
    if port == 0:
        raise wrong
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc or parts.query or parts.fragment or "?" in value or "#" in value:
        raise wrong
    if any(character.isspace() for character in value) or not config.host_name(parts.hostname):
        raise wrong
    if not with_path and parts.path not in ("", "/"):
        raise validate.Invalid(f"{name} must be just the address, without a path")
    if parts.scheme == "http" and not config.local_host(parts.hostname):
        raise validate.Invalid(f"{name} must use https:// for an address reachable beyond the LAN")
    return value.rstrip("/")


def text_list(body: dict[str, object], name: str, *, emails: bool) -> list[str]:
    value = body.get(name, [])
    if not isinstance(value, list) or len(value) > MOST_ENTRIES or not all(isinstance(entry, str) for entry in value):
        raise validate.Invalid(f"{name} must be a list of at most {MOST_ENTRIES} texts")
    found: list[str] = []
    for entry in value:
        cleaned = plain(entry.strip(), name)
        if not cleaned or len(cleaned) > 254 or "," in cleaned:
            raise validate.Invalid(f"{name} entries must be non-empty, without commas")
        if emails and not config.ADDRESS.fullmatch(cleaned):
            raise validate.Invalid(f"{name} must hold email addresses, like jo@example.com")
        found.append(cleaned.lower() if emails else cleaned)
    return list(dict.fromkeys(found))


def one_email(body: dict[str, object], name: str) -> str:
    value = plain(validate.text(body, name, limit=254), name)
    if not config.ADDRESS.fullmatch(value):
        raise validate.Invalid(f"{name} must be one email address, like alerts@example.com")
    return value


def proposal_view(fields: dict[str, str] | None) -> contract.SetupProposal | None:
    if fields is None:
        return None
    return {"oidc_issuer": fields["OIDC_ISSUER"], "oidc_client_id": fields["OIDC_CLIENT_ID"], "has_client_secret": bool(fields["OIDC_CLIENT_SECRET"]),
            "oidc_allowed_groups": list(config.comma_list(fields["OIDC_ALLOWED_GROUPS"])), "oidc_allowed_emails": list(config.comma_list(fields["OIDC_ALLOWED_EMAILS"]))}


def state_of(request: Request) -> contract.SetupState:
    engine = engine_of(request)
    rows = stored_settings.stored_values(engine)
    found = stored_settings.combined(request.settings, rows)
    views: list[contract.SetupSetting] = []
    for entry in config.STORABLE:
        source = found.sources[entry.variable]
        shown = rows.get(entry.variable, (None, None))[0] if source == "stored" and not entry.secret else None
        views.append({"variable": entry.variable, "source": source, "set": source != "default", "value": shown})
    with db.session(engine) as opened:
        fields = wizard.load_proposal(opened, request.settings.secret_key)
    public_url = found.settings.public_url
    return {"settings": views, "redirect_uri": oidc.redirect_uri(found.settings) if public_url else None, "proposal": proposal_view(fields)}


def setup_state(request: Request) -> Annotated[Response, contract.SetupState]:
    return json_response(state_of(request))


def claim_setup(request: Request) -> Annotated[Response, contract.SetupState]:
    token = secrets.token_urlsafe(32)
    with db.session(engine_of(request), writing=True) as opened:
        if wizard.locked(opened):
            return error_response("sign in required", 401)
        held = wizard.stored_claim(opened)
        if held and not wizard.holds_claim(request, held):
            return error_response(CLAIMED_ELSEWHERE, 409)
        if not held:
            opened.add(Setting(key=SETUP_CLAIM, value=oidc.hash_token(token)))
    if held:
        return json_response(state_of(request))
    monitoring.log("setup wizard claimed by its first visitor")
    return json_response(state_of(request), cookies=(auth.cookie(request.settings, wizard.CLAIM_COOKIE, token, wizard.CLAIM_MAX_AGE),))


def store_all(request: Request, values: dict[str, str]) -> Response:
    if not request.settings.secret_key:
        return error_response(NO_KEY, 503)
    with db.session(engine_of(request), writing=True) as opened:
        moment = now()
        for variable, value in values.items():
            stored_settings.store(opened, variable, value, request.settings.secret_key, moment)
    return json_response(state_of(request))


def set_address(request: Annotated[Request, contract.SetupAddressBody]) -> Annotated[Response, contract.SetupState]:
    refused = from_environment(request, "DARKROOM_PUBLIC_URL")
    if refused is not None:
        return refused
    try:
        public_url = address(validate.json_object(request.body, limit=MAX_BODY), "public_url", with_path=False)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    return store_all(request, {"DARKROOM_PUBLIC_URL": public_url})


def set_immich(request: Annotated[Request, contract.SetupImmichBody]) -> Annotated[Response, contract.SetupState]:
    refused = from_environment(request, "IMMICH_URL")
    if refused is not None:
        return refused
    try:
        immich_url = address(validate.json_object(request.body, limit=MAX_BODY), "immich_url", with_path=True)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    return store_all(request, {"IMMICH_URL": immich_url})


def set_alerts(request: Annotated[Request, contract.SetupAlertsBody]) -> Annotated[Response, contract.SetupState]:
    refused = from_environment(request, "RESEND_API_KEY", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO")
    if refused is not None:
        return refused
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        values = {name.upper(): one_email(body, name) for name in ("alert_email_from", "alert_email_to")}
        if validate.present(body, "resend_api_key"):
            key = token_like(body, "resend_api_key")
            if secretbox.is_sealed(key):
                raise validate.Invalid("resend_api_key must be the key from Resend itself, not a sealed value")
            values["RESEND_API_KEY"] = key
        elif "RESEND_API_KEY" not in stored_settings.stored_values(engine_of(request)):
            raise validate.Invalid("resend_api_key is required")
    except validate.Invalid as error:
        return error_response(str(error), 400)
    return store_all(request, values)


def propose_sign_in(request: Annotated[Request, contract.SetupSignInBody]) -> Annotated[Response, contract.SetupState]:
    refused = from_environment(request, *wizard.SIGN_IN_VARIABLES)
    if refused is not None:
        return refused
    if not request.settings.secret_key:
        return error_response(NO_KEY, 503)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        fields = {"OIDC_ISSUER": address(body, "oidc_issuer", with_path=True), "OIDC_CLIENT_ID": token_like(body, "oidc_client_id"),
                  "OIDC_ALLOWED_GROUPS": ",".join(text_list(body, "oidc_allowed_groups", emails=False)),
                  "OIDC_ALLOWED_EMAILS": ",".join(text_list(body, "oidc_allowed_emails", emails=True)), "id": secrets.token_urlsafe(16)}
        given_secret = plain(validate.text(body, "oidc_client_secret", required=False, limit=500), "oidc_client_secret") if validate.present(body, "oidc_client_secret") else None
    except validate.Invalid as error:
        return error_response(str(error), 400)
    live = wizard.live_settings(request)
    if not live.public_url:
        return error_response(NO_ADDRESS, 409)
    with db.session(engine_of(request), writing=True) as opened:
        earlier = wizard.load_proposal(opened, request.settings.secret_key)
        fields["OIDC_CLIENT_SECRET"] = given_secret if given_secret is not None else (earlier or {}).get("OIDC_CLIENT_SECRET", "")
        problems = config.auth_problems(wizard.proposed_settings(live, fields))
        if problems:
            return error_response(problems[0], 400)
        wizard.save_proposal(opened, fields, request.settings.secret_key)
    return json_response(state_of(request))


def test_sign_in(request: Request) -> Annotated[Response, contract.SetupTestStart]:
    live = wizard.live_settings(request)
    with db.session(engine_of(request), writing=True) as opened:
        fields = wizard.load_proposal(opened, request.settings.secret_key)
        if fields is None:
            return error_response(NO_PROPOSAL, 409)
        try:
            target, state_value = oidc.start_login(opened, wizard.proposed_settings(live, fields), wizard.TEST_PATH + fields["id"], now())
        except oidc.SignInFailed as error:
            monitoring.log("test sign-in could not start", reason=type(error).__name__)
            return error_response(f"the test sign-in could not start: {error}", 502)
    baked = auth.cookie(request.settings, wizard.TEST_COOKIE, state_value, int(oidc.LOGIN_TTL), "/auth")
    return json_response({"location": target}, cookies=(baked,))
