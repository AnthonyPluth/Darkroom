from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from darkroom import monitoring, validate
from darkroom.dates import utc_now
from darkroom.domain import browser_jobs
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


def answer(request: Request, work: Callable[[Session, float], BrowserJob | None]) -> Response:
    moment = now()
    try:
        with db.session(engine_of(request)) as opened:
            browser_jobs.worker_seen(opened, moment)
            job = work(opened, moment)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except (NotFound, browser_jobs.NoSuchJob):
        return error_response("no such job", 404)
    except browser_jobs.NotOpen:
        return error_response("this job is no longer running", 409)
    except (browser_jobs.BadReason, ValueError) as error:
        return error_response(str(error), 400)
    return json_response({"job": browser_jobs.for_worker(job) if job is not None else None})


def claim(request: Request) -> Response:
    def work(opened: Session, moment: float) -> BrowserJob | None:
        body_of(request)
        job = browser_jobs.claim(opened, moment)
        if job is not None:
            monitoring.log("browser job claimed", kind=job.kind)
        return job

    return answer(request, work)


def heartbeat(request: Request) -> Response:
    def work(opened: Session, moment: float) -> BrowserJob:
        body_of(request)
        return browser_jobs.heartbeat(opened, job_id_of(request), moment)

    return answer(request, work)


def progress(request: Request) -> Response:
    def work(opened: Session, moment: float) -> BrowserJob:
        message = validate.text(body_of(request), "message", limit=MAX_MESSAGE)
        return browser_jobs.progress(opened, job_id_of(request), message, moment)

    return answer(request, work)


def waiting(request: Request) -> Response:
    def work(opened: Session, moment: float) -> BrowserJob:
        reason = validate.text(body_of(request), "reason", limit=20)
        job = browser_jobs.waiting(opened, request.settings, job_id_of(request), reason, moment)
        monitoring.log("browser job waiting for a person", reason=reason)
        return job

    return answer(request, work)


def done(request: Request) -> Response:
    def work(opened: Session, moment: float) -> BrowserJob:
        body = body_of(request)
        result = body.get("result", {})
        if not isinstance(result, dict):
            raise validate.Invalid("result must be an object")
        if len(request.body) > MAX_RESULT:
            raise validate.Invalid("result is too large")
        job = browser_jobs.done(opened, job_id_of(request), result, moment)
        monitoring.log("browser job done", kind=job.kind)
        return job

    return answer(request, work)


def failed(request: Request) -> Response:
    def work(opened: Session, moment: float) -> BrowserJob:
        body = body_of(request)
        message = validate.text(body, "message", limit=MAX_MESSAGE)
        folder = validate.text(body, "failure_dir", required=False, limit=MAX_MESSAGE)
        files = body.get("files", [])
        if not isinstance(files, list) or len(files) > len(browser_jobs.CAPTURE_FILES) or not all(isinstance(name, str) for name in files):
            raise validate.Invalid("files must be a list of at most two file names")
        job = browser_jobs.failed(opened, request.settings, job_id_of(request), message, folder or None, files, moment)
        monitoring.log("browser job failed", kind=job.kind, captured=job.failure_dir is not None)
        return job

    return answer(request, work)
