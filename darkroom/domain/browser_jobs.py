from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import alerts, events
from darkroom.storage import db
from darkroom.storage.models import BrowserJob, GoogleAccount, Setting
from darkroom.storage.paths import OutsideRoot, Photos

LEASE_SECONDS = 180.0
KINDS = ("noop", "connect", "schedule")
GUARDED_KINDS = ("connect", "schedule")
MAYBE_CREATED = "ScheduleNotConfirmed"
QUEUED_STATUSES = ("queued", "running", "waiting_for_user")
WAITING_REASONS = ("sign_in", "verify", "wrong_account")
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


class AlreadyOpen(ValueError):
    pass


class NothingToCheck(LookupError):
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
    if kind not in KINDS or kind in GUARDED_KINDS or not KIND.match(kind):
        raise UnknownKind(f"{kind!r} is not a browser job kind")
    return queue(opened, account_id, kind, payload, now)


def queue(opened: Session, account_id: int, kind: str, payload: dict[str, object] | None, now: float) -> BrowserJob:
    account_of(opened, account_id)
    job = BrowserJob(account_id=account_id, kind=kind, status="queued", payload=json.dumps(payload or {}), created=now)
    opened.add(job)
    opened.flush()
    events.record(opened, now, "browser_job_queued", str(job.id), kind)
    return job


def has_open_onboarding_job(opened: Session, account_id: int) -> bool:
    found = opened.execute(select(BrowserJob.id).where(BrowserJob.account_id == account_id, BrowserJob.kind.in_(GUARDED_KINDS), BrowserJob.status.in_(QUEUED_STATUSES)).limit(1)).first()
    return found is not None


def unchecked_schedule(opened: Session, account_id: int) -> BrowserJob | None:
    latest = opened.execute(select(BrowserJob).where(BrowserJob.account_id == account_id, BrowserJob.kind == "schedule").order_by(BrowserJob.id.desc()).limit(1)).scalar_one_or_none()
    if latest is None or latest.status != "failed" or json.loads(latest.result or "{}").get("checked"):
        return None
    message = latest.message or ""
    return latest if message.startswith(MAYBE_CREATED) or message == EXPIRED_MESSAGE else None


def is_unchecked(opened: Session, job: BrowserJob) -> bool:
    stuck = unchecked_schedule(opened, job.account_id)
    return stuck is not None and stuck.id == job.id


def mark_schedule_checked(opened: Session, account_id: int, now: float) -> BrowserJob:
    account_of(opened, account_id)
    job = unchecked_schedule(opened, account_id)
    if job is None:
        raise NothingToCheck("there is no schedule attempt waiting to be checked")
    job.result = json.dumps({"checked": True})
    events.record(opened, now, "schedule_checked", str(job.id))
    return job


def enqueue_once(opened: Session, account_id: int, kind: str, now: float) -> BrowserJob:
    try:
        locked = opened.execute(update(GoogleAccount).where(GoogleAccount.id == account_id).values(id=GoogleAccount.id).returning(GoogleAccount.id)).scalar_one_or_none()
    except OperationalError as error:
        if "database is locked" not in str(error.orig):
            raise
        raise AlreadyOpen("Darkroom is busy with another change; try again in a moment") from None
    if locked is None:
        raise UnknownAccount(f"no Google account {account_id}")
    account = account_of(opened, account_id)
    if kind == "schedule" and account.schedule_created is not None:
        raise AlreadyOpen("this account already has an export schedule")
    if kind == "schedule" and unchecked_schedule(opened, account_id) is not None:
        raise AlreadyOpen("the last schedule attempt may have created an export; check Takeout and say so before trying again")
    statuses = (*QUEUED_STATUSES, "done") if kind == "schedule" else QUEUED_STATUSES
    busy = opened.execute(select(BrowserJob.id).where(BrowserJob.account_id == account_id, BrowserJob.kind == kind, BrowserJob.status.in_(statuses)).limit(1)).first()
    if busy is not None:
        raise AlreadyOpen(f"a {kind} job for this account is already waiting, running or done")
    return queue(opened, account_id, kind, {"email": account.email}, now)


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
    previous = job.message
    job.message = message
    if job.status == "waiting_for_user":
        resumed_from_sign_in = previous in WAITING_REASONS
        job.status = "running"
        alerts.close(opened, sign_in_key(job.account_id), now)
        if resumed_from_sign_in:
            account = account_of(opened, job.account_id)
            account.session_state = "ok"
            account.session_checked = now
    return job


def waiting(opened: Session, settings: Settings, job_id: int, reason: str, now: float) -> BrowserJob:
    if reason not in WAITING_REASONS:
        raise BadReason(f"{reason!r} is not a reason to wait")
    job = heartbeat(opened, job_id, now)
    job.status = "waiting_for_user"
    job.message = reason
    account = account_of(opened, job.account_id)
    if reason in WAITING_REASONS:
        account.session_state = "needs_sign_in"
        account.session_checked = now
    kind, wording = ("wrong_account", alerts.wrong_account(account.email)) if reason == "wrong_account" else ("sign_in_needed", alerts.sign_in_needed(account.email))
    alerts.close_other_kinds(opened, sign_in_key(job.account_id), kind, now)
    alerts.open_alert(opened, settings, sign_in_key(job.account_id), kind, account.email, wording, settings.vnc_url or None, now)
    events.record(opened, now, "browser_job_waiting", str(job.id), reason)
    return job


def schedule_times(result: dict[str, object]) -> tuple[float, float]:
    created, end = result.get("schedule_created"), result.get("schedule_end")
    if isinstance(created, bool) or isinstance(end, bool) or not isinstance(created, int | float) or not isinstance(end, int | float):
        raise ValueError("a schedule job's result needs schedule_created and schedule_end as numbers")
    if not 0 < created < end:
        raise ValueError("schedule_end must be after schedule_created")
    return float(created), float(end)


def done(opened: Session, job_id: int, result: dict[str, object], now: float) -> BrowserJob:
    job = open_job(opened, job_id)
    times = schedule_times(result) if job.kind == "schedule" else None
    job.status = "done"
    job.result = json.dumps(result)
    job.finished = now
    job.heartbeat = now
    alerts.close(opened, sign_in_key(job.account_id), now)
    account = account_of(opened, job.account_id)
    account.session_state = "ok"
    account.session_checked = now
    if times is not None:
        account.schedule_created, account.schedule_end = times
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
    if job.status == "waiting_for_user":
        alerts.close(opened, sign_in_key(job.account_id), now)
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


def unresolved_jobs(opened: Session, account_id: int | None) -> list[BrowserJob]:
    query = select(BrowserJob).where(BrowserJob.status.in_(QUEUED_STATUSES))
    if account_id is not None:
        query = query.where(BrowserJob.account_id == account_id)
    found = list(opened.execute(query).scalars())
    accounts = {account_id} if account_id is not None else set(opened.execute(select(BrowserJob.account_id).where(BrowserJob.kind == "schedule", BrowserJob.status == "failed").distinct()).scalars())
    for each in accounts:
        stuck = unchecked_schedule(opened, each)
        if stuck is not None:
            found.append(stuck)
    return found


def as_dict(job: BrowserJob, settings: Settings | None = None, needs_check: bool = False) -> dict[str, object]:
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
        "needs_check": needs_check,
        "has_capture": job.failure_dir is not None,
        "capture_files": captured_files(settings, job, CAPTURE_FILES) if settings is not None and job.failure_dir is not None else [],
    }


def for_worker(job: BrowserJob) -> dict[str, object]:
    return {"id": job.id, "kind": job.kind, "account": str(job.account_id), "payload": json.loads(job.payload or "{}")}


def holds_profile(opened: Session, account_id: int, now: float) -> Literal["open", "lease_expired"] | None:
    mine = BrowserJob.account_id == account_id
    if opened.execute(select(func.count(BrowserJob.id)).where(mine, BrowserJob.status.in_(OPEN_STATUSES))).scalar_one():
        return "open"
    expired = and_(BrowserJob.status == "failed", BrowserJob.message == EXPIRED_MESSAGE, BrowserJob.finished > now - LEASE_SECONDS)
    if opened.execute(select(func.count(BrowserJob.id)).where(mine, expired)).scalar_one():
        return "lease_expired"
    return None
