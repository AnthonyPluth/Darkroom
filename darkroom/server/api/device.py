from __future__ import annotations

from typing import Annotated

from darkroom import validate
from darkroom.domain import devices
from darkroom.server import contract
from darkroom.server.common import Principal, Request, Response, engine_of, error_response, json_response, now
from darkroom.storage import db
from darkroom.storage.models import Person

MAX_VERSION = 50
SIGN_IN = "sign in required"


def device_of(request: Request) -> Principal | None:
    who = request.user
    return who if who is not None and who.kind == "device" and who.device_id is not None and who.person_id is not None else None


def device_me(request: Request) -> Annotated[Response, contract.DeviceMeReply]:
    who = device_of(request)
    if who is None or who.person_id is None or who.name is None:
        return error_response(SIGN_IN, 401)
    with db.session(engine_of(request)) as opened:
        person = opened.get(Person, who.person_id)
        if person is None:
            return error_response(SIGN_IN, 401)
        return json_response({"person": person.name, "device": who.name})


def checkin(request: Annotated[Request, contract.DeviceCheckinBody]) -> Annotated[Response, contract.Ok]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        body = validate.json_object(request.body, limit=16 * 1024)
        pending = validate.whole_number(body, "pending_uploads")
        version = validate.text_or_none(body, "app_version", limit=MAX_VERSION)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        devices.check_in(opened, who.device_id, pending, version, now())
    return json_response({"ok": True})

