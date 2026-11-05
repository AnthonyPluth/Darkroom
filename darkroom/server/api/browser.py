from __future__ import annotations

from pathlib import Path
from typing import Annotated

from darkroom import validate
from darkroom.domain import browser_jobs
from darkroom.server.api.browser_internal import NotFound, engine_of, job_id_of, now
from darkroom.server import contract
from darkroom.server.common import Request, Response, error_response, first_query, json_response, viewer_url
from darkroom.storage import db
from darkroom.storage.models import BrowserJob
from darkroom.storage.paths import OutsideRoot, Photos

MAX_CAPTURE_BYTES = 32 * 1024 * 1024
SNAPSHOT_CSP = "default-src 'none'; sandbox"
CAPTURE_FILES = {"screenshot.png": "image/png", "page.html": "text/html; charset=utf-8"}


def account_filter(request: Request) -> int | None:
    raw = first_query(request, "account")
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or len(raw) > 12:
        raise validate.Invalid("account must be a whole number")
    return int(raw)


def list_jobs(request: Request) -> Annotated[Response, contract.BrowserJobs]:
    try:
        account = account_filter(request)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        listed = {job.id: job for job in (*browser_jobs.list_jobs(opened, account), *browser_jobs.unresolved_jobs(opened, account))}
        stuck = {job.id for job in listed.values() if job.kind in browser_jobs.SCHEDULE_KINDS and browser_jobs.is_unchecked(opened, job)}
        jobs = [browser_jobs.as_dict(listed[job_id], request.settings, job_id in stuck) for job_id in sorted(listed, reverse=True)]
    return json_response({"jobs": jobs})


def view(request: Request) -> Annotated[Response, contract.BrowserView]:
    with db.session(engine_of(request)) as opened:
        seen = browser_jobs.worker_last_seen(opened)
    return json_response({"vnc_url": viewer_url(request.settings.vnc_url), "worker_last_seen": seen})


def enqueue(request: Annotated[Request, contract.EnqueueBody]) -> Annotated[Response, contract.BrowserJobReply]:
    try:
        body = validate.json_object(request.body, limit=64 * 1024)
        account_id = validate.whole_number(body, "account_id", minimum=1)
        kind = validate.text(body, "kind", limit=40)
        with db.session(engine_of(request)) as opened:
            job = browser_jobs.enqueue(opened, account_id, kind, None, now())
            reply = browser_jobs.as_dict(job, request.settings)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except browser_jobs.UnknownKind:
        return error_response(f"kind must be one of {', '.join(kind for kind in browser_jobs.KINDS if kind not in browser_jobs.GUARDED_KINDS)}", 400)
    except browser_jobs.UnknownAccount:
        return error_response("no such account", 404)
    return json_response({"job": reply}, 201)


def capture_file(request: Request, name: str) -> Response:
    try:
        job_id = job_id_of(request)
    except NotFound:
        return error_response("no such job", 404)
    with db.session(engine_of(request)) as opened:
        job = opened.get(BrowserJob, job_id)
        if job is None or job.failure_dir is None:
            return error_response("no such capture", 404)
        account_id = job.account_id
    try:
        folder = Photos(request.settings.photos_root).failures(str(account_id), str(job_id))
    except OutsideRoot:
        return error_response("no such capture", 404)
    path = folder / name
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_CAPTURE_BYTES:
        return error_response("no such capture", 404)
    return Response(200, Path(path).read_bytes(), CAPTURE_FILES[name], {"Content-Security-Policy": SNAPSHOT_CSP, "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})


def screenshot(request: Request) -> Response:
    return capture_file(request, "screenshot.png")


def page_snapshot(request: Request) -> Response:
    return capture_file(request, "page.html")
