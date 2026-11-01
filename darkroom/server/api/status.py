from __future__ import annotations

from darkroom import VERSION
from darkroom.domain import browser_jobs
from darkroom.server.common import Request, Response, json_response
from darkroom.storage import db


def healthz(request: Request) -> Response:
    return json_response({"ok": True, "version": VERSION})


def status(request: Request) -> Response:
    if request.engine is None:
        raise RuntimeError("this route needs the database")
    with db.session(request.engine) as opened:
        seen = browser_jobs.worker_last_seen(opened)
    return json_response({"worker_last_seen": seen})
