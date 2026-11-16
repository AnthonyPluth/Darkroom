from __future__ import annotations

from typing import Annotated

from darkroom import validate
from darkroom.config import demo_allowed
from darkroom.domain import demo
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now
from darkroom.storage import db

MAX_BODY = 1024
NOT_DEMO = "not found"


def syncthing_state(request: Annotated[Request, contract.DemoSyncthingBody]) -> Annotated[Response, contract.Ok]:
    if not demo_allowed(request.settings):
        return error_response(NOT_DEMO, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        state = validate.text(body, "state", limit=40)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    if state not in demo.SYNCTHING_STATES:
        return error_response(f"state must be one of {', '.join(demo.SYNCTHING_STATES)}", 400)
    with db.session(engine_of(request), writing=True) as opened:
        demo.syncthing_state(opened, request.settings, state, now())
    return json_response({"ok": True})
