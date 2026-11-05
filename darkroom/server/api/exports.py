from __future__ import annotations

from typing import Annotated

from darkroom.domain import browser_jobs, exports
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db

NO_EXPORT = "no such export"


def list_exports(request: Request) -> Annotated[Response, contract.Exports]:
    with db.session(engine_of(request)) as opened:
        listed = [exports.as_dict(opened, export) for export in exports.listing(opened)]
        schedules = exports.schedule_dicts(opened, now())
    return json_response({"exports": listed, "schedules": schedules})


def one(request: Request) -> Annotated[Response, contract.ExportReply]:
    export_id = path_id(request)
    if export_id is None:
        return error_response(NO_EXPORT, 404)
    try:
        with db.session(engine_of(request)) as opened:
            reply = exports.as_dict(opened, exports.one(opened, export_id))
    except exports.NoSuchExport:
        return error_response(NO_EXPORT, 404)
    return json_response({"export": reply})


def check_now(request: Request) -> Annotated[Response, contract.CheckNowReply]:
    with db.session(engine_of(request)) as opened:
        queued, skipped = exports.check_now(opened, now())
        jobs = [browser_jobs.as_dict(job, request.settings) for job in queued]
    return json_response({"jobs": jobs, "skipped": skipped}, 201 if jobs else 200)
