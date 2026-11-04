from __future__ import annotations

import json
import re
from collections.abc import Sequence

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import alerts, events
from darkroom.storage import db
from darkroom.storage.models import BrowserJob, GoogleAccount, Setting
from darkroom.storage.paths import OutsideRoot, Photos

LEASE_SECONDS = 180.0
KINDS = ("noop",)
WAITING_REASONS = ("sign_in", "verify")
OPEN_STATUSES = ("running", "waiting_for_user")
EXPIRED_MESSAGE = "the worker stopped reporting"
WORKER_LAST_SEEN = "worker_last_seen"
KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_LIST = 200
CAPTURE_FILES = ("screenshot.png", "page.html")


class UnknownKind(ValueError):
    pass


class UnknownAccount(LookupError):
    pass


class NoSuchJob(LookupError):
    pass


class NotOpen(ValueError):
    pass


class BadReason(ValueError):
    pass


def failure_folder(account_id: int, job_id: int) -> str:
    return f"_browser/{account_id}/failures/{job_id}"


def sign_in_key(account_id: int) -> str:
    return f"browser:sign-in:{account_id}"


def account_of(opened: Session, account_id: int) -> GoogleAccount:
    account = opened.get(GoogleAccount, account_id)
    if account is None:
        raise UnknownAccount(f"no Google account {account_id}")
    return account


def enqueue(opened: Session, account_id: int, kind: str, payload: dict[str, object] | None, now: float) -> BrowserJob:
    if kind not in KINDS or not KIND.match(kind):
        raise UnknownKind(f"{kind!r} is not a browser job kind")
    account_of(opened, account_id)
    job = BrowserJob(account_id=account_id, kind=kind, status="queued", payload=json.dumps(payload or {}), created=now)
    opened.add(job)
    opened.flush()
    events.record(opened, now, "browser_job_queued", str(job.id), kind)
    return job


def claim(opened: Session, now: float) -> BrowserJob | None:
    oldest = select(BrowserJob.id).where(BrowserJob.status == "queued").order_by(BrowserJob.id).limit(1).scalar_subquery()
    taken = opened.execute(
        update(BrowserJob)
        .where(BrowserJob.id == oldest, BrowserJob.status == "queued")
        .values(status="running", started=now, heartbeat=now)
        .returning(BrowserJob.id)
    ).scalar_one_or_none()
    if taken is None:
        return None
    job = opened.get(BrowserJob, taken)
    assert job is not None
    opened.refresh(job)
    events.record(opened, now, "browser_job_claimed", str(job.id), job.kind)
    return job


def open_job(opened: Session, job_id: int) -> BrowserJob:
    job = opened.get(BrowserJob, job_id)
    if job is None:
        raise NoSuchJob(f"no browser job {job_id}")
    if job.status not in OPEN_STATUSES:
        raise NotOpen(f"browser job {job_id} is {job.status}")
    return job


def heartbeat(opened: Session, job_id: int, now: float) -> BrowserJob:
    job = open_job(opened, job_id)
    job.heartbeat = now
    return job


def progress(opened: Session, job_id: int, message: str, now: float) -> BrowserJob:
    job = heartbeat(opened, job_id, now)
    job.message = message
    if job.status == "waiting_for_user":
        job.status = "running"
        alerts.close(opened, sign_in_key(job.account_id), now)
    return job


def waiting(opened: Session, settings: Settings, job_id: int, reason: str, now: float) -> BrowserJob:
    if reason not in WAITING_REASONS:
        raise BadReason(f"{reason!r} is not a reason to wait")
    job = heartbeat(opened, job_id, now)
    job.status = "waiting_for_user"
    job.message = reason
    account = account_of(opened, job.account_id)
    if reason == "sign_in":
        account.session_state = "needs_sign_in"
        account.session_checked = now
    alerts.open_alert(opened, settings, sign_in_key(job.account_id), "sign_in_needed", account.email, alerts.sign_in_needed(account.email),
                      settings.vnc_url or None, now)
    events.record(opened, now, "browser_job_waiting", str(job.id), reason)
    return job


def done(opened: Session, job_id: int, result: dict[str, object], now: float) -> BrowserJob:
    job = open_job(opened, job_id)
    job.status = "done"
    job.result = json.dumps(result)
    job.finished = now
    job.heartbeat = now
    alerts.close(opened, sign_in_key(job.account_id), now)
    account = account_of(opened, job.account_id)
    account.session_state = "ok"
    account.session_checked = now
    events.record(opened, now, "browser_job_done", str(job.id), job.kind)
    return job


def captured_files(settings: Settings, job: BrowserJob, files: Sequence[str]) -> list[str]:
    for name in files:
        if name not in CAPTURE_FILES:
            raise ValueError("files may only name screenshot.png or page.html")
    try:
        folder = Photos(settings.photos_root).failures(str(job.account_id), str(job.id))
    except OutsideRoot:
        return []
    return [name for name in CAPTURE_FILES if name in files and (folder / name).is_file()]


def capture_link(settings: Settings, job: BrowserJob, files: Sequence[str]) -> str | None:
    if not files or not settings.public_url:
        return None
    return f"{settings.public_url}/api/browser/failures/{job.id}/{files[0]}"


def fail(opened: Session, settings: Settings, job: BrowserJob, message: str, files: Sequence[str], now: float) -> BrowserJob:
    present = captured_files(settings, job, files)
    job.status = "failed"
    job.message = message
    job.finished = now
    job.failure_dir = failure_folder(job.account_id, job.id) if present else None
    link = capture_link(settings, job, present)
    account = account_of(opened, job.account_id)
    alerts.open_alert(opened, settings, f"browser:failed:{job.id}", "failure", str(job.id), alerts.failure(f"Browser job {job.id} ({job.kind}) for {account.email}"), link, now)
    events.record(opened, now, "browser_job_failed", str(job.id), message)
    return job


def failed(opened: Session, settings: Settings, job_id: int, message: str, failure_dir: str | None, files: Sequence[str], now: float) -> BrowserJob:
    job = open_job(opened, job_id)
    if failure_dir is not None and failure_dir != failure_folder(job.account_id, job.id):
        raise ValueError("failure_dir is not this job's failure folder")
    if files and failure_dir is None:
        raise ValueError("files need the failure_dir they were written to")
    return fail(opened, settings, job, message, files if failure_dir is not None else [], now)


def expire_leases(opened: Session, settings: Settings, now: float) -> list[BrowserJob]:
    stale = now - LEASE_SECONDS
    expired = []
    for job in opened.execute(select(BrowserJob).where(BrowserJob.status.in_(OPEN_STATUSES), BrowserJob.heartbeat < stale)).scalars():
        expired.append(fail(opened, settings, job, EXPIRED_MESSAGE, [], now))
    return expired


def list_jobs(opened: Session, account_id: int | None, limit: int = 50) -> Sequence[BrowserJob]:
    query = select(BrowserJob).order_by(BrowserJob.id.desc()).limit(min(limit, MAX_LIST))
    if account_id is not None:
        query = query.where(BrowserJob.account_id == account_id)
    return opened.execute(query).scalars().all()


def worker_seen(opened: Session, now: float) -> None:
    db.upsert(opened, Setting, {"key": WORKER_LAST_SEEN, "value": repr(now)}, ("key",))


def worker_last_seen(opened: Session) -> float | None:
    setting = opened.get(Setting, WORKER_LAST_SEEN)
    return float(setting.value) if setting is not None and setting.value else None


def as_dict(job: BrowserJob) -> dict[str, object]:
    return {
        "id": job.id,
        "account_id": job.account_id,
        "kind": job.kind,
        "status": job.status,
        "created": job.created,
        "started": job.started,
        "heartbeat": job.heartbeat,
        "finished": job.finished,
        "message": job.message or "",
        "has_capture": job.failure_dir is not None,
    }


def for_worker(job: BrowserJob) -> dict[str, object]:
    return {"id": job.id, "kind": job.kind, "account": str(job.account_id), "payload": json.loads(job.payload or "{}")}


def holds_profile(opened: Session, account_id: int, now: float) -> bool:
    recently = now - LEASE_SECONDS
    query = select(func.count(BrowserJob.id)).where(
        BrowserJob.account_id == account_id,
        or_(
            BrowserJob.status.in_(OPEN_STATUSES),
            and_(BrowserJob.status == "failed", BrowserJob.message == EXPIRED_MESSAGE, BrowserJob.finished > recently),
        ),
    )
    return bool(opened.execute(query).scalar_one())
