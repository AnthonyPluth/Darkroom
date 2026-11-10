from __future__ import annotations

import calendar
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from darkroom import dates
from darkroom.domain import browser_jobs, events
from darkroom.storage.models import BrowserJob, Export, ExportPart, GoogleAccount, Person

State = Literal["in_progress", "ready", "expired"]
STATES: tuple[State, ...] = ("in_progress", "ready", "expired")
STATUS_KIND = "status"
DOWNLOAD_KIND = "download"
MAX_EXPORTS = 100
MAX_PARTS = 200
MAX_ID = 200
CADENCE_MONTHS = 1
SCHEDULE_SLACK = 86400.0
CHECK_START_HOUR = 3
CHECK_WINDOW_MINUTES = 240
CHECK_STEP_MINUTES = 37
LIST_LIMIT = 200
STILL_WAITING = ("announced", "downloading")
MAX_DOWNLOAD_JOBS = 3
MAX_PART_NUMBER = 2**31 - 1
MAX_PART_BYTES = 2**63 - 1


class UnreadableListing(ValueError):
    pass


class NoSuchExport(LookupError):
    pass


@dataclass(frozen=True)
class ListedPart:
    number: int
    bytes: int


@dataclass(frozen=True)
class Listed:
    google_id: str
    created_at: float
    state: State
    expires_at: float | None
    parts: tuple[ListedPart, ...]


@dataclass(frozen=True)
class Reconciled:
    announced: int = 0
    in_progress: int = 0
    expired: int = 0
    requeued: int = 0


def number_of(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not minimum <= value <= maximum:
        raise UnreadableListing(f"the export listing has a bad {name}")
    return float(value)


def instant_of(value: object, name: str) -> float:
    return number_of(value, name, 1, dates.LATEST_INSTANT)


def part_of(raw: object) -> ListedPart:
    if not isinstance(raw, dict):
        raise UnreadableListing("the export listing has a bad part")
    number = number_of(raw.get("number"), "part number", 1, MAX_PART_NUMBER)
    size = number_of(raw.get("bytes"), "part size", 0, MAX_PART_BYTES)
    if number != int(number) or size != int(size):
        raise UnreadableListing("the export listing has a bad part")
    return ListedPart(int(number), int(size))


def listed_of(raw: object) -> Listed:
    if not isinstance(raw, dict):
        raise UnreadableListing("the export listing has a bad export")
    google_id, state = raw.get("google_id"), raw.get("state")
    if not isinstance(google_id, str) or not 0 < len(google_id) <= MAX_ID:
        raise UnreadableListing("the export listing has a bad export id")
    if state not in STATES:
        raise UnreadableListing("the export listing has a bad state")
    created = instant_of(raw.get("created_at"), "creation time")
    expires = raw.get("expires_at")
    expires_at = None if expires is None else instant_of(expires, "expiry time")
    parts_raw = raw.get("parts", [])
    if not isinstance(parts_raw, list) or len(parts_raw) > MAX_PARTS:
        raise UnreadableListing("the export listing has bad parts")
    parts = tuple(sorted((part_of(each) for each in parts_raw), key=lambda part: part.number))
    if len({part.number for part in parts}) != len(parts):
        raise UnreadableListing("the export listing repeats a part")
    if state == "ready" and not parts:
        raise UnreadableListing("a ready export in the listing has no parts")
    return Listed(google_id, created, state, expires_at, parts)


def listing_from_result(result: dict[str, object]) -> list[Listed]:
    raw = result.get("exports")
    if not isinstance(raw, list) or len(raw) > MAX_EXPORTS:
        raise UnreadableListing("the export listing is missing or too long")
    listing = [listed_of(each) for each in raw]
    if len({each.google_id for each in listing}) != len(listing):
        raise UnreadableListing("the export listing repeats an export")
    return listing


def add_months(moment: float, months: int, zone: tzinfo = UTC) -> float:
    start = datetime.fromtimestamp(moment, zone)
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day).timestamp()


def next_export_at(account: GoogleAccount, now: float, zone: tzinfo = UTC) -> float | None:
    created, end = dates.instant_or_none(account.schedule_created), dates.instant_or_none(account.schedule_end)
    if created is None or end is None:
        return None
    for step in range(1, 13):
        moment = add_months(created, CADENCE_MONTHS * step, zone)
        if moment >= end:
            return None
        if moment > now:
            return moment
    return None


def last_export_at(opened: Session, account_id: int) -> float | None:
    found = opened.execute(select(Export.created_at).where(Export.account_id == account_id, Export.created_at.is_not(None))).scalars()
    return max((moment for moment in map(dates.instant_or_none, found) if moment is not None), default=None)


def kind_for(opened: Session, account: GoogleAccount, created_at: float) -> Literal["initial", "incremental", "manual"]:
    since = account.schedule_created
    if since is not None and created_at < since - SCHEDULE_SLACK:
        return "manual"
    earlier = select(func.count(Export.id)).where(Export.account_id == account.id, Export.kind != "manual")
    if since is not None:
        earlier = earlier.where(Export.created_at >= since - SCHEDULE_SLACK)
    return "incremental" if opened.execute(earlier).scalar_one() else "initial"


def add_parts(opened: Session, export: Export, item: Listed) -> None:
    for part in item.parts:
        opened.add(ExportPart(export_id=export.id, number=part.number, bytes=part.bytes))
    export.part_count = len(item.parts)
    opened.flush()


def expire(opened: Session, export: Export, now: float) -> None:
    export.status = "expired"
    events.record(opened, now, "export_expired", str(export.id))


def announce(opened: Session, account: GoogleAccount, export: Export, item: Listed, now: float) -> None:
    export.ready_at = now
    export.expires_at = item.expires_at
    add_parts(opened, export, item)
    events.record(opened, now, "export_announced", str(export.id), f"{len(item.parts)} parts")
    queue_download(opened, account, export, now)


def queue_download(opened: Session, account: GoogleAccount, export: Export, now: float) -> BrowserJob:
    return browser_jobs.queue(opened, account.id, DOWNLOAD_KIND, {"email": account.email, "export_id": export.id}, now)


def download_open(opened: Session, export: Export) -> bool:
    jobs = opened.execute(select(BrowserJob).where(BrowserJob.account_id == export.account_id, BrowserJob.kind == DOWNLOAD_KIND,
                                                   BrowserJob.status.in_(browser_jobs.QUEUED_STATUSES))).scalars()
    return any(json.loads(job.payload or "{}").get("export_id") == export.id for job in jobs)


def download_underway(opened: Session, account_id: int) -> bool:
    started = opened.execute(select(Export.id).where(Export.account_id == account_id, Export.status == "downloading").limit(1)).first()
    queued = opened.execute(select(BrowserJob.id).where(BrowserJob.account_id == account_id, BrowserJob.kind == DOWNLOAD_KIND,
                                                        BrowserJob.status.in_(browser_jobs.QUEUED_STATUSES)).limit(1)).first()
    return started is not None or queued is not None


def owner_of(opened: Session, export: Export) -> Person | None:
    person_id = export.owner_person_id
    if person_id is None:
        account = opened.get(GoogleAccount, export.account_id)
        person_id = None if account is None else account.default_person_id
    return None if person_id is None else opened.get(Person, person_id)


def claim_owner(opened: Session, export: Export) -> Person | None:
    person = owner_of(opened, export)
    if person is not None and export.owner_person_id is None:
        export.owner_person_id = person.id
    return person


def pin_owners(opened: Session, account: GoogleAccount) -> None:
    opened.execute(sql_update(Export).where(Export.account_id == account.id, Export.owner_person_id.is_(None)).values(owner_person_id=account.default_person_id))


def give_up_downloading(opened: Session, export: Export, now: float) -> None:
    export.status = "failed"
    events.record(opened, now, "export_download_failed", str(export.id), f"{export.download_tries} download jobs failed")


def all_done(opened: Session, export: Export) -> bool:
    parts = parts_of(opened, export.id)
    return bool(parts) and export.part_count == len(parts) and all(part.status == "done" for part in parts)


def retry_download(opened: Session, account: GoogleAccount, export: Export, item: Listed, now: float) -> str | None:
    if download_open(opened, export):
        return None
    if all_done(opened, export):
        if export.download_tries >= MAX_DOWNLOAD_JOBS:
            give_up_downloading(opened, export, now)
            return None
        queue_download(opened, account, export, now)
        events.record(opened, now, "export_download_requeued", str(export.id), "every part is downloaded")
        return "requeued"
    if is_late(item, now):
        expire(opened, export, now)
        return "expired"
    if export.download_tries >= MAX_DOWNLOAD_JOBS:
        give_up_downloading(opened, export, now)
        return None
    queue_download(opened, account, export, now)
    events.record(opened, now, "export_download_requeued", str(export.id), f"{export.download_tries} of {MAX_DOWNLOAD_JOBS} failed tries used")
    return "requeued"


def is_late(item: Listed, now: float) -> bool:
    return item.state == "expired" or (item.expires_at is not None and now >= item.expires_at)


def record_new(opened: Session, account: GoogleAccount, item: Listed, now: float) -> str:
    export = Export(account_id=account.id, kind=kind_for(opened, account, item.created_at), google_id=item.google_id, created_at=item.created_at,
                    expires_at=item.expires_at, status="announced", owner_person_id=account.default_person_id)
    opened.add(export)
    opened.flush()
    if item.state != "in_progress" and is_late(item, now):
        export.part_count = len(item.parts) or None
        expire(opened, export, now)
        return "expired"
    if item.state == "ready":
        announce(opened, account, export, item, now)
        return "announced"
    return "in_progress"


def update_known(opened: Session, account: GoogleAccount, export: Export, item: Listed, now: float) -> str | None:
    if export.status == "expired":
        return None
    if export.status in STILL_WAITING and export.ready_at is not None and all_done(opened, export):
        return retry_download(opened, account, export, item, now)
    if item.state == "expired":
        if export.status in STILL_WAITING:
            expire(opened, export, now)
            return "expired"
        return None
    if item.state == "ready" and export.ready_at is None and export.status == "announced":
        if is_late(item, now):
            expire(opened, export, now)
            return "expired"
        announce(opened, account, export, item, now)
        return "announced"
    if item.state == "ready" and export.ready_at is not None and export.status in STILL_WAITING:
        return retry_download(opened, account, export, item, now)
    return None


def reconcile(opened: Session, account: GoogleAccount, listing: Sequence[Listed], now: float) -> Reconciled:
    counts = {"announced": 0, "in_progress": 0, "expired": 0, "requeued": 0}
    for item in sorted(listing, key=lambda each: each.created_at):
        export = opened.execute(select(Export).where(Export.account_id == account.id, Export.google_id == item.google_id)).scalar_one_or_none()
        outcome = record_new(opened, account, item, now) if export is None else update_known(opened, account, export, item, now)
        if outcome is not None:
            counts[outcome] += 1
    return Reconciled(**counts)


def connected_accounts(opened: Session) -> list[GoogleAccount]:
    return list(opened.execute(select(GoogleAccount).where(GoogleAccount.session_state == "ok").order_by(GoogleAccount.id)).scalars())


def check_now(opened: Session, now: float, only: Sequence[int] | None = None) -> tuple[list[BrowserJob], int]:
    queued: list[BrowserJob] = []
    skipped = 0
    for account in connected_accounts(opened):
        if only is not None and account.id not in only:
            continue
        try:
            queued.append(browser_jobs.enqueue_once(opened, account.id, STATUS_KIND, now))
        except browser_jobs.Busy:
            raise
        except browser_jobs.AlreadyOpen:
            skipped += 1
    return queued, skipped


def check_slot(account_id: int, now: float, zone: ZoneInfo) -> float:
    local = datetime.fromtimestamp(now, zone)
    offset = (account_id * CHECK_STEP_MINUTES) % CHECK_WINDOW_MINUTES
    start = local.replace(hour=CHECK_START_HOUR, minute=0, second=0, microsecond=0)
    return (start + timedelta(minutes=offset)).timestamp()


def due_for_check(opened: Session, now: float, zone: ZoneInfo) -> list[int]:
    due = []
    for account in connected_accounts(opened):
        slot = check_slot(account.id, now, zone)
        if now < slot:
            continue
        today = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        recent = opened.execute(select(BrowserJob.id).where(BrowserJob.account_id == account.id, BrowserJob.kind == STATUS_KIND, BrowserJob.created >= today).limit(1)).first()
        if recent is None:
            due.append(account.id)
    return due


def check_due(opened: Session, now: float, zone: ZoneInfo) -> int:
    queued, _ = check_now(opened, now, due_for_check(opened, now, zone))
    return len(queued)


def part_progress(part: ExportPart) -> int:
    if part.status == "done":
        return 100
    if part.status != "downloading" or not part.bytes_done or not part.bytes:
        return 0
    return max(0, min(99, part.bytes_done * 100 // part.bytes))


def part_dict(part: ExportPart) -> dict[str, object]:
    return {
        "number": part.number, "filename": part.filename, "bytes": part.bytes, "progress": part_progress(part),
        "downloads_used": part.downloads_used, "attempts": part.attempts, "status": part.status,
    }


def parts_of(opened: Session, export_id: int) -> Sequence[ExportPart]:
    return opened.execute(select(ExportPart).where(ExportPart.export_id == export_id).order_by(ExportPart.number)).scalars().all()


def as_dict(opened: Session, export: Export) -> dict[str, object]:
    return {
        "id": export.id, "account_id": export.account_id, "kind": export.kind, "created_at": dates.instant_or_none(export.created_at),
        "ready_at": dates.instant_or_none(export.ready_at), "expires_at": dates.instant_or_none(export.expires_at), "part_count": export.part_count, "status": export.status, "stage": export.stage,
        "parts": [part_dict(part) for part in parts_of(opened, export.id)],
    }


def listing(opened: Session, limit: int = LIST_LIMIT) -> list[Export]:
    query = select(Export).order_by(Export.created_at.desc().nulls_last(), Export.id.desc()).limit(min(limit, LIST_LIMIT))
    return list(opened.execute(query).scalars())


def one(opened: Session, export_id: int) -> Export:
    export = opened.get(Export, export_id)
    if export is None:
        raise NoSuchExport(f"no export {export_id}")
    return export


def schedule_dicts(opened: Session, now: float, zone: tzinfo = UTC) -> list[dict[str, object]]:
    return [
        {"account_id": account.id, "last_export_at": last_export_at(opened, account.id), "next_export_at": next_export_at(account, now, zone)}
        for account in opened.execute(select(GoogleAccount).order_by(GoogleAccount.id)).scalars()
    ]
