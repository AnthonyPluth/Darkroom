from __future__ import annotations

from typing import Annotated

from darkroom import validate
from darkroom.domain import hold_resolutions, holds, review
from darkroom.server import contract
from darkroom.server.api.review import BadQuery, who, whole
from darkroom.server.common import Request, Response, engine_of, error_response, first_query, json_response, now, path_id
from darkroom.storage import db
from darkroom.storage.models import ManagedImportReceipt

MAX_BODY = 1024
NO_HOLD = "no such held photo"
FIELDS = ("action", "offset_minutes", "taken_local")


def list_holds(request: Request) -> Annotated[Response, contract.Holds]:
    source = first_query(request, "source") or None
    if source is not None and source not in holds.SOURCES:
        return error_response(f"source is one of {', '.join(holds.SOURCES)}", 400)
    try:
        person, after = whole(request, "person"), whole(request, "after")
    except BadQuery as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        found, total = holds.listing(opened, person, source, after)
        hold_resolutions.attach(opened, found)
    return json_response({"holds": found, "total": total})


def resolve_hold(request: Annotated[Request, contract.HoldResolveBody]) -> Annotated[Response, contract.HoldReply]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_HOLD, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        if set(body) - set(FIELDS):
            raise hold_resolutions.Refused("the request has a field that no resolution takes")
        action = validate.text(body, "action", limit=20)
        at = now()
        with db.session(engine_of(request), writing=True) as opened:
            row = hold_resolutions.resolve(opened, wanted, action, body.get("offset_minutes"), body.get("taken_local"), review.person_of_session(opened, *who(request)), at)
            reply = hold_resolutions.view(row, opened.get_one(ManagedImportReceipt, wanted))
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except hold_resolutions.Refused as error:
        return error_response(str(error), 422)
    except hold_resolutions.NoSuchHold:
        return error_response(NO_HOLD, 404)
    except hold_resolutions.Conflict as error:
        return error_response(str(error), 409)
    return json_response({"resolution": reply})


def undo_hold(request: Request) -> Annotated[Response, contract.HoldReply]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_HOLD, 404)
    try:
        at = now()
        with db.session(engine_of(request), writing=True) as opened:
            row = hold_resolutions.undo(opened, wanted, review.person_of_session(opened, *who(request)), at)
            reply = hold_resolutions.view(row, opened.get_one(ManagedImportReceipt, wanted))
    except hold_resolutions.NoSuchHold:
        return error_response(NO_HOLD, 404)
    except hold_resolutions.Conflict as error:
        return error_response(str(error), 409)
    return json_response({"resolution": reply})
