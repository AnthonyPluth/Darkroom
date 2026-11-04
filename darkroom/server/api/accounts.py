from __future__ import annotations

from typing import Annotated

from darkroom import validate
from darkroom.domain import accounts
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
        with db.session(engine_of(request)) as opened:
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
        with db.session(engine_of(request)) as opened:
            account = accounts.update(opened, account_id, email, person_id, now())
            reply = accounts.as_dict(opened, account)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except accounts.NoSuchAccount:
        return error_response(NO_ACCOUNT, 404)
    except accounts.NoSuchPerson:
        return error_response(NO_PERSON, 404)
    except (accounts.EmailTaken, accounts.StillConfirming) as error:
        return error_response(str(error), 409)
    return json_response({"account": reply})


def delete_account(request: Request) -> Annotated[Response, contract.Ok]:
    account_id = path_id(request)
    if account_id is None:
        return error_response(NO_ACCOUNT, 404)
    try:
        with db.session(engine_of(request)) as opened:
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
