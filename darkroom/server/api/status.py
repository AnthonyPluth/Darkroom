from __future__ import annotations

from typing import Annotated

from darkroom import VERSION, validate
from darkroom.domain import events
from darkroom.domain import status as overview
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, first_query, json_response, now
from darkroom.storage import db
from darkroom.storage.paths import Photos

DEFAULT_EVENTS = 50
MAX_EVENTS = 200


def healthz(request: Request) -> Annotated[Response, contract.Health]:
    return json_response({"ok": True, "version": VERSION})


def status(request: Request) -> Annotated[Response, contract.Status]:
    with db.session(engine_of(request)) as opened:
        reply = overview.overview(opened, request.settings, Photos(request.settings.photos_root), now())
    return json_response(reply)


def limit_of(request: Request) -> int:
    raw = first_query(request, "limit")
    if raw is None or raw == "":
        return DEFAULT_EVENTS
    if not raw.isdigit() or len(raw) > 6 or not 1 <= int(raw) <= MAX_EVENTS:
        raise validate.Invalid(f"limit must be a whole number from 1 to {MAX_EVENTS}")
    return int(raw)


def recent_events(request: Request) -> Annotated[Response, contract.Events]:
    try:
        limit = limit_of(request)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        listed = [{"id": e.id, "time": e.time, "type": e.type, "subject": e.subject, "detail": e.detail} for e in events.recent(opened, limit)]
    return json_response({"events": listed})
