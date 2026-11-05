from __future__ import annotations

from typing import Annotated

from darkroom import validate
from darkroom.domain import devices, eligibility
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db


def revoke_device(request: Request) -> Annotated[Response, contract.DeviceReply]:
    device_id = path_id(request)
    if device_id is None:
        return error_response("no such device", 404)
    try:
        with db.session(engine_of(request)) as opened:
            reply = devices.as_dict(devices.revoke(opened, device_id, now()))
    except devices.NoSuchDevice:
        return error_response("no such device", 404)
    return json_response({"device": reply})


def update_device(request: Annotated[Request, contract.DevicePatchBody]) -> Annotated[Response, contract.DeviceReply]:
    device_id = path_id(request)
    if device_id is None:
        return error_response("no such device", 404)
    try:
        body = validate.json_object(request.body, limit=16 * 1024)
        enabled = validate.boolean(body, "cleanup_enabled")
        icloud_off = validate.boolean(body, "icloud_photos_off", required=False)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    try:
        with db.session(engine_of(request)) as opened:
            reply = devices.as_dict(eligibility.set_cleanup(opened, device_id, enabled, icloud_off, now()))
    except eligibility.NoSuchDevice:
        return error_response("no such device", 404)
    except eligibility.NotAllowed as error:
        return error_response(str(error), 409)
    except eligibility.Busy:
        return error_response("Darkroom is busy with another change; try again in a moment", 503)
    return json_response({"device": reply})
