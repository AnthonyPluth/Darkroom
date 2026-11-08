from __future__ import annotations

from typing import Annotated

from darkroom import validate
from darkroom.domain import accounts, browser_jobs, dedupe, renewal
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db
from darkroom.storage.paths import Photos, ProfileNotRemoved

MAX_BODY = 16 * 1024
NO_ACCOUNT = "no such Google account"
NO_PERSON = "no such person"


def list_accounts(request: Request) -> Annotated[Response, contract.Accounts]:
    with db.session(engine_of(request)) as opened:
        listed = [accounts.as_dict(opened, account) for account in accounts.listing(opened)]
    return json_response({"accounts": listed})


def create_account(request: Annotated[Request, contract.AccountCreateBody]) -> Annotated[Response, contract.AccountReply]:
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        email = validate.email(body, "email")
        person_id = validate.whole_number(body, "default_person_id", minimum=1)
        with db.session(engine_of(request), writing=True) as opened:
            account = accounts.create(opened, email, person_id, now())
            reply = accounts.as_dict(opened, account)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except accounts.NoSuchPerson:
        return error_response(NO_PERSON, 404)
    except accounts.EmailTaken as error:
        return error_response(str(error), 409)
    return json_response({"account": reply}, 201)


def update_account(request: Annotated[Request, contract.AccountPatchBody]) -> Annotated[Response, contract.AccountReply]:
    account_id = path_id(request)
    if account_id is None:
        return error_response(NO_ACCOUNT, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        email = validate.email(body, "email") if validate.present(body, "email") else None
        person_id = validate.whole_number(body, "default_person_id", minimum=1) if validate.present(body, "default_person_id") else None
        with db.session(engine_of(request), writing=True) as opened:
            accounts.lock(opened, account_id)
            before = accounts.served_people(opened, account_id)
            account = accounts.update(opened, account_id, email, person_id, now())
            dedupe.confirm_newly_served(opened, account.id, before)
            reply = accounts.as_dict(opened, account)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except accounts.NoSuchAccount:
        return error_response(NO_ACCOUNT, 404)
    except accounts.NoSuchPerson:
        return error_response(NO_PERSON, 404)
    except browser_jobs.Busy as error:
        return error_response(str(error), 503)
    except (accounts.EmailTaken, accounts.StillConfirming, accounts.JobOpen) as error:
        return error_response(str(error), 409)
    return json_response({"account": reply})


def delete_account(request: Request) -> Annotated[Response, contract.Ok]:
    account_id = path_id(request)
    if account_id is None:
        return error_response(NO_ACCOUNT, 404)
    try:
        with db.session(engine_of(request), writing=True) as opened:
            accounts.delete(opened, Photos(request.settings.photos_root), account_id, now())
    except accounts.NoSuchAccount:
        return error_response(NO_ACCOUNT, 404)
    except accounts.InUse as error:
        return error_response(str(error), 409)
    except accounts.Busy as error:
        return error_response(str(error), 503)
    except ProfileNotRemoved as error:
        return error_response(f"{error}, so the account was not deleted", 500)
    return json_response({"ok": True})


def start_job(request: Request, kind: str) -> Response:
    account_id = path_id(request)
    if account_id is None:
        return error_response(NO_ACCOUNT, 404)
    try:
        with db.session(engine_of(request), writing=True) as opened:
            reply = browser_jobs.as_dict(browser_jobs.enqueue_once(opened, account_id, kind, now()), request.settings)
    except browser_jobs.UnknownAccount:
        return error_response(NO_ACCOUNT, 404)
    except browser_jobs.AlreadyOpen as error:
        return error_response(str(error), 409)
    return json_response({"job": reply}, 201)


def schedule_checked(request: Annotated[Request, contract.ScheduleCheckedBody]) -> Annotated[Response, contract.BrowserJobReply]:
    account_id = path_id(request)
    if account_id is None:
        return error_response(NO_ACCOUNT, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY) if request.body else {}
        found = validate.boolean(body, "found", required=False)
        with db.session(engine_of(request), writing=True) as opened:
            reply = browser_jobs.as_dict(renewal.checked(opened, request.settings, account_id, found, now()), request.settings)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except browser_jobs.UnknownAccount:
        return error_response(NO_ACCOUNT, 404)
    except (browser_jobs.NothingToCheck, browser_jobs.NotExtended) as error:
        return error_response(str(error), 409)
    return json_response({"job": reply})


def renew_schedule(request: Request) -> Annotated[Response, contract.BrowserJobReply]:
    account_id = path_id(request)
    if account_id is None:
        return error_response(NO_ACCOUNT, 404)
    try:
        with db.session(engine_of(request), writing=True) as opened:
            reply = browser_jobs.as_dict(renewal.renew_now(opened, request.settings, account_id, now()), request.settings)
    except browser_jobs.UnknownAccount:
        return error_response(NO_ACCOUNT, 404)
    except (browser_jobs.AlreadyOpen, renewal.NoSchedule, renewal.NeedsSignIn, renewal.NotDue) as error:
        return error_response(str(error), 409)
    return json_response({"job": reply}, 201)


def connect(request: Request) -> Annotated[Response, contract.BrowserJobReply]:
    return start_job(request, "connect")


def create_schedule(request: Request) -> Annotated[Response, contract.BrowserJobReply]:
    return start_job(request, "schedule")
