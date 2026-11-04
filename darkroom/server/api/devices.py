from __future__ import annotations

from typing import Annotated

from darkroom.domain import devices
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
