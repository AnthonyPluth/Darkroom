from __future__ import annotations

from typing import Annotated
from zoneinfo import ZoneInfo

from sqlalchemy.exc import OperationalError

from darkroom.domain import browser_jobs, downloads, exports
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db

NO_EXPORT = "no such export"
BUSY = "Darkroom is busy with another change; try again in a moment"


def list_exports(request: Request) -> Annotated[Response, contract.Exports]:
    with db.session(engine_of(request)) as opened:
        listed = [exports.as_dict(opened, export) for export in exports.listing(opened)]
        schedules = exports.schedule_dicts(opened, now(), ZoneInfo(request.settings.timezone))
    return json_response({"exports": listed, "schedules": schedules})


def export_one(request: Request) -> Annotated[Response, contract.ExportReply]:
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
    try:
        with db.session(engine_of(request)) as opened:
            queued, skipped = exports.check_now(opened, now())
            jobs = [browser_jobs.as_dict(job, request.settings) for job in queued]
    except browser_jobs.Busy as error:
        return error_response(str(error), 503)
    return json_response({"jobs": jobs, "skipped": skipped}, 201 if jobs else 200)


def retry_download(request: Request) -> Annotated[Response, contract.RetryDownloadReply]:
    export_id = path_id(request)
    if export_id is None:
        return error_response(NO_EXPORT, 404)
    try:
        with db.session(engine_of(request)) as opened:
            export, retried = downloads.retry_failed(opened, export_id, now())
            reply = {"export": exports.as_dict(opened, export), "retried": retried}
    except exports.NoSuchExport:
        return error_response(NO_EXPORT, 404)
    except downloads.CannotRetry as error:
        return error_response(str(error), 409)
    except browser_jobs.Busy as error:
        return error_response(str(error), 503)
    except OperationalError as error:
        if not db.is_busy(error):
            raise
        return error_response(BUSY, 503)
    return json_response(reply)
