from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from darkroom import monitoring, validate
from darkroom.dates import utc_now
from darkroom.domain import browser_jobs, downloads, exports, renewal
from darkroom.server import contract
from darkroom.server.common import Request, Response, error_response, json_response
from darkroom.storage import db
from darkroom.storage.models import BrowserJob

MAX_WORKER_BODY = 64 * 1024
MAX_MESSAGE = 500
MAX_RESULT = 32 * 1024
JOB_ID_DIGITS = 12


class NotFound(LookupError):
    pass


def now() -> float:
    return utc_now().timestamp()


def engine_of(request: Request) -> Engine:
    if request.engine is None:
        raise RuntimeError("this route needs the database")
    return request.engine


def job_id_of(request: Request) -> int:
    raw = request.params.get("id", "")
    if not raw.isdigit() or len(raw) > JOB_ID_DIGITS:
        raise NotFound("no such job")
    return int(raw)


def body_of(request: Request) -> dict[str, object]:
    return validate.json_object(request.body, limit=MAX_WORKER_BODY)


def number_of(request: Request) -> int:
    raw = request.params.get("number", "")
    if not raw.isascii() or not raw.isdigit() or len(raw) > 4:
        raise validate.Invalid("the part number must be a whole number")
    return int(raw)


def answer(request: Request, work: Callable[[Session, float], BrowserJob | None]) -> Response:
    def reply(opened: Session, moment: float) -> dict[str, object]:
        job = work(opened, moment)
        return {"job": browser_jobs.for_worker(job) if job is not None else None}

    return reply_with(request, reply)


def reply_with(request: Request, work: Callable[[Session, float], dict[str, object]]) -> Response:
    moment = now()
    try:
        with db.session(engine_of(request), writing=True) as opened:
            browser_jobs.worker_seen(opened, moment)
            body = work(opened, moment)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except (NotFound, browser_jobs.NoSuchJob):
        return error_response("no such job", 404)
    except browser_jobs.NotOpen:
        return error_response("this job is no longer running", 409)
    except (browser_jobs.BadReason, ValueError) as error:
        return error_response(str(error), 400)
    return json_response(body)


def claim(request: Annotated[Request, contract.ClaimBody]) -> Annotated[Response, contract.WorkerReply]:
    def work(opened: Session, moment: float) -> BrowserJob | None:
        body_of(request)
        job = browser_jobs.claim(opened, moment)
        if job is not None:
            monitoring.log("browser job claimed", kind=job.kind)
        return job

    return answer(request, work)


def heartbeat(request: Annotated[Request, contract.HeartbeatBody]) -> Annotated[Response, contract.WorkerReply]:
    def work(opened: Session, moment: float) -> BrowserJob:
        body_of(request)
        return browser_jobs.heartbeat(opened, job_id_of(request), moment)

    return answer(request, work)


def progress(request: Annotated[Request, contract.ProgressBody]) -> Annotated[Response, contract.WorkerReply]:
    def work(opened: Session, moment: float) -> BrowserJob:
        message = validate.text(body_of(request), "message", limit=MAX_MESSAGE)
        return browser_jobs.progress(opened, job_id_of(request), message, moment)

    return answer(request, work)


def waiting(request: Annotated[Request, contract.WaitingBody]) -> Annotated[Response, contract.WorkerReply]:
    def work(opened: Session, moment: float) -> BrowserJob:
        reason = validate.text(body_of(request), "reason", limit=20)
        job = browser_jobs.waiting(opened, request.settings, job_id_of(request), reason, moment)
        monitoring.log("browser job waiting for a person", reason=reason)
        return job

    return answer(request, work)


def done(request: Annotated[Request, contract.DoneBody]) -> Annotated[Response, contract.WorkerReply]:
    def work(opened: Session, moment: float) -> BrowserJob:
        body = body_of(request)
        result = body.get("result", {})
        if not isinstance(result, dict):
            raise validate.Invalid("result must be an object")
        if len(request.body) > MAX_RESULT:
            raise validate.Invalid("result is too large")
        job_id = job_id_of(request)
        kind = browser_jobs.open_job(opened, job_id).kind
        listing = exports.listing_from_result(result) if kind == exports.STATUS_KIND else None
        if kind == exports.DOWNLOAD_KIND:
            downloads.finish(opened, request.settings, job_id, moment)
            monitoring.log("export downloaded")
        job = browser_jobs.done(opened, job_id, result, moment)
        if job.kind in browser_jobs.SCHEDULE_KINDS:
            renewal.schedule_recorded(opened, request.settings, job, moment)
            monitoring.log("export schedule recorded", kind=job.kind)
        if listing is not None:
            found = exports.reconcile(opened, browser_jobs.account_of(opened, job.account_id), listing, moment)
            monitoring.log("export listing reconciled", announced=found.announced, in_progress=found.in_progress, expired=found.expired, requeued=found.requeued)
        monitoring.log("browser job done", kind=job.kind)
        return job

    return answer(request, work)


def failed(request: Annotated[Request, contract.FailedBody]) -> Annotated[Response, contract.WorkerReply]:
    def work(opened: Session, moment: float) -> BrowserJob:
        body = body_of(request)
        message = validate.text(body, "message", limit=MAX_MESSAGE)
        folder = validate.text(body, "failure_dir", required=False, limit=MAX_MESSAGE)
        files = body.get("files", [])
        if not isinstance(files, list) or len(files) > len(browser_jobs.CAPTURE_FILES) or not all(isinstance(name, str) for name in files):
            raise validate.Invalid("files must be a list of at most two file names")
        job = browser_jobs.failed(opened, request.settings, job_id_of(request), message, folder or None, files, moment)
        downloads.job_failed(opened, job, moment)
        renewal.job_failed(opened, request.settings, job, moment)
        monitoring.log("browser job failed", kind=job.kind, captured=job.failure_dir is not None)
        return job

    return answer(request, work)


def download_plan(request: Annotated[Request, contract.DownloadPlanBody]) -> Annotated[Response, contract.DownloadPlan]:
    def work(opened: Session, moment: float) -> dict[str, object]:
        body_of(request)
        browser_jobs.heartbeat(opened, job_id_of(request), moment)
        return downloads.plan(opened, request.settings, job_id_of(request))

    return reply_with(request, work)


def part(request: Annotated[Request, contract.PartBody]) -> Annotated[Response, contract.PartReply]:
    def work(opened: Session, moment: float) -> dict[str, object]:
        body = body_of(request)
        job_id, number = job_id_of(request), number_of(request)
        event = validate.text(body, "event", limit=20)
        browser_jobs.heartbeat(opened, job_id, moment)
        if event == "attempt":
            reply = downloads.attempt(opened, job_id, number, moment)
        elif event == "progress":
            reply = downloads.progress(opened, job_id, number, validate.whole_number(body, "bytes", maximum=downloads.MAX_PART_BYTES), moment)
        elif event == "done":
            filename = validate.text(body, "filename", limit=200)
            reply = downloads.part_done(opened, request.settings, job_id, number, filename, validate.whole_number(body, "bytes", minimum=1, maximum=downloads.MAX_PART_BYTES), moment)
        elif event == "failed":
            reply = downloads.part_failed(opened, job_id, number, validate.text(body, "reason", limit=20), moment)
        else:
            raise validate.Invalid("event must be attempt, progress, done or failed")
        if event != "progress":
            monitoring.log("export part reported", outcome=event, allowed=reply["allowed"] is True, gave_up=reply["gave_up"] is True)
        return reply

    return reply_with(request, work)
