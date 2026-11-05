from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import alerts, browser_jobs, events, exports
from darkroom.domain.ingest import run
from darkroom.storage.models import Alert, BrowserJob, Export, ExportPart, GoogleAccount, Person
from darkroom.storage.paths import OutsideRoot, Photos

MAX_DOWNLOADS = 5
MAX_ATTEMPTS = 3
EXPIRY_NOTICE = 4 * alerts.DAY
PART_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,195}\.zip$")
FAILURES = ("corrupt", "wrong_size", "not_started", "interrupted", "bad_name")
SIZE_SLACK = 0.06
MIN_SLACK = 1024 * 1024
MAX_PART_BYTES = 1 << 40
ATTEMPTED = json.dumps({"attempted": True})
EXPIRING = "export_expiring"


class NotADownload(ValueError):
    pass


class Refused(ValueError):
    pass


def folder_name(export: Export) -> str:
    return f"export-{export.id}"


def expiry_key(export_id: int) -> str:
    return f"export:expiring:{export_id}"


def export_of(opened: Session, job: BrowserJob) -> Export | None:
    if job.kind != exports.DOWNLOAD_KIND:
        return None
    export_id = json.loads(job.payload or "{}").get("export_id")
    if isinstance(export_id, bool) or not isinstance(export_id, int):
        return None
    export = opened.get(Export, export_id)
    return export if export is not None and export.account_id == job.account_id else None


def job_export(opened: Session, job_id: int) -> Export:
    job = browser_jobs.open_job(opened, job_id)
    if job.kind != exports.DOWNLOAD_KIND:
        raise NotADownload(f"browser job {job_id} is not a download")
    export = export_of(opened, job)
    if export is None:
        raise NotADownload(f"the export of browser job {job_id} is gone")
    return export


def waiting_export(opened: Session, job_id: int) -> Export:
    export = job_export(opened, job_id)
    if export.status not in exports.STILL_WAITING:
        raise Refused(f"export {export.id} is {export.status}, not waiting to be downloaded")
    return export


def person_of(opened: Session, export: Export) -> Person:
    person = exports.claim_owner(opened, export)
    if person is None:
        raise Refused("the export's account has no default person")
    return person


def export_folder(settings: Settings, person: Person, export: Export) -> Path:
    photos = Photos(settings.photos_root)
    try:
        inbox, folder = photos.inbox(person.slug), photos.export_tree(person.slug, folder_name(export))
    except OutsideRoot as error:
        raise Refused("the export's inbox folder is not inside the photo folder") from error
    written = photos.root / "_inbox" / person.slug / folder_name(export)
    if any(path.is_symlink() for path in (written, written.parent, written.parent.parent)) or folder.parent != inbox:
        raise Refused("the export's inbox folder is not inside the person's inbox")
    return folder


def part_plan(part: ExportPart) -> dict[str, object]:
    return {"number": part.number, "bytes": part.bytes, "status": part.status, "filename": part.filename,
            "downloads_used": part.downloads_used, "attempts": part.attempts}


def plan(opened: Session, settings: Settings, job_id: int) -> dict[str, object]:
    export = waiting_export(opened, job_id)
    person = person_of(opened, export)
    export_folder(settings, person, export)
    return {"export_id": export.id, "google_id": export.google_id or "", "person": person.slug, "folder": folder_name(export),
            "part_count": export.part_count or 0, "parts": [part_plan(part) for part in exports.parts_of(opened, export.id)]}


def part_of(opened: Session, export: Export, number: int) -> ExportPart:
    part = opened.execute(select(ExportPart).where(ExportPart.export_id == export.id, ExportPart.number == number)).scalar_one_or_none()
    if part is None:
        raise Refused(f"export {export.id} has no part {number}")
    return part


def exhausted(part: ExportPart) -> bool:
    return part.downloads_used >= MAX_DOWNLOADS or part.attempts >= MAX_ATTEMPTS


def give_up(opened: Session, export: Export, part: ExportPart, now: float) -> None:
    part.status = "failed"
    part.bytes_done = None
    export.status = "failed"
    events.record(opened, now, "export_part_failed", str(export.id), f"part {part.number}: {part.attempts} attempts, {part.downloads_used} downloads used")


def outcome(part: ExportPart, allowed: bool, gave_up: bool) -> dict[str, object]:
    return {"allowed": allowed, "gave_up": gave_up, "part": part_plan(part)}


def attempt(opened: Session, job_id: int, number: int, now: float) -> dict[str, object]:
    export = waiting_export(opened, job_id)
    part = part_of(opened, export, number)
    if part.status == "done":
        raise Refused(f"part {number} is already downloaded")
    if part.status == "failed" or exhausted(part):
        give_up(opened, export, part, now)
        return outcome(part, False, True)
    browser_jobs.open_job(opened, job_id).result = ATTEMPTED
    part.downloads_used += 1
    part.attempts += 1
    part.status = "downloading"
    part.bytes_done = 0
    export.status = "downloading"
    events.record(opened, now, "export_part_attempt", str(export.id), f"part {number}: attempt {part.attempts}, download {part.downloads_used} of {MAX_DOWNLOADS}")
    return outcome(part, True, False)


def downloading_part(opened: Session, job_id: int, number: int) -> tuple[Export, ExportPart]:
    export = waiting_export(opened, job_id)
    part = part_of(opened, export, number)
    if part.status != "downloading":
        raise Refused(f"part {number} is not being downloaded")
    return export, part


def progress(opened: Session, job_id: int, number: int, bytes_done: int, now: float) -> dict[str, object]:
    _, part = downloading_part(opened, job_id, number)
    if not 0 <= bytes_done <= MAX_PART_BYTES:
        raise Refused("bytes is out of range")
    part.bytes_done = bytes_done
    return outcome(part, True, False)


def size_fits(listed: int | None, actual: int) -> bool:
    if actual <= 0:
        return False
    if listed is None:
        return True
    return abs(actual - listed) <= max(listed * SIZE_SLACK, MIN_SLACK)


def part_done(opened: Session, settings: Settings, job_id: int, number: int, filename: str, size: int, now: float) -> dict[str, object]:
    export, part = downloading_part(opened, job_id, number)
    if not PART_FILE.match(filename):
        raise Refused("the part's file name is not a Takeout zip name")
    taken = opened.execute(select(ExportPart.id).where(ExportPart.export_id == export.id, ExportPart.filename == filename, ExportPart.id != part.id).limit(1)).first()
    if taken is not None:
        raise Refused("another part of this export already has that file name")
    path = export_folder(settings, person_of(opened, export), export) / filename
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise Refused(f"part {number} is not in the export's inbox folder at the size reported")
    if not size_fits(part.bytes, size):
        raise Refused(f"part {number} is not the size Takeout listed")
    part.status = "done"
    part.filename = filename
    part.bytes_done = size
    events.record(opened, now, "export_part_downloaded", str(export.id), f"part {number}")
    return outcome(part, True, False)


def part_failed(opened: Session, job_id: int, number: int, reason: str, now: float) -> dict[str, object]:
    if reason not in FAILURES:
        raise Refused(f"{reason!r} is not a reason a part fails")
    export = waiting_export(opened, job_id)
    part = part_of(opened, export, number)
    if part.status in ("done", "failed"):
        raise Refused(f"part {number} is {part.status}")
    events.record(opened, now, "export_part_attempt_failed", str(export.id), f"part {number}: {reason}")
    if exhausted(part):
        give_up(opened, export, part, now)
        return outcome(part, False, True)
    part.status = "corrupt" if reason in ("corrupt", "wrong_size") else "pending"
    part.bytes_done = None
    return outcome(part, True, False)


def finish(opened: Session, settings: Settings, job_id: int, now: float) -> Export:
    export = waiting_export(opened, job_id)
    if not exports.all_done(opened, export):
        raise Refused(f"not every part of export {export.id} is downloaded")
    export.inbox_dir = str(export_folder(settings, person_of(opened, export), export))
    export.status = "downloaded"
    alerts.close(opened, expiry_key(export.id), now)
    events.record(opened, now, "export_downloaded", str(export.id), f"{export.part_count} parts")
    run.request_import(opened, export.id)
    return export


def job_failed(opened: Session, job: BrowserJob, now: float) -> Export | None:
    export = export_of(opened, job)
    if export is None or export.status not in exports.STILL_WAITING:
        return None
    for part in exports.parts_of(opened, export.id):
        if part.status == "downloading":
            part.status = "pending"
            part.bytes_done = None
    if job.result != ATTEMPTED or exports.all_done(opened, export):
        return export
    export.download_tries += 1
    if export.download_tries >= exports.MAX_DOWNLOAD_JOBS:
        exports.give_up_downloading(opened, export, now)
    return export


def jobs_failed(opened: Session, jobs: Sequence[BrowserJob], now: float) -> int:
    return sum(1 for job in jobs if job_failed(opened, job, now) is not None)


def on_day(moment: float, zone: str) -> str:
    local = datetime.fromtimestamp(moment, ZoneInfo(zone))
    return f"{local.day} {local:%b %Y at %H:%M}"


def expiring_wording(opened: Session, settings: Settings, export: Export, expires_at: float) -> alerts.Wording:
    account = opened.get(GoogleAccount, export.account_id)
    label = f"{export.id} for {account.email}" if account is not None else str(export.id)
    return alerts.export_expiring(label, on_day(expires_at, settings.timezone))


def watch_expiry(opened: Session, settings: Settings, now: float) -> int:
    opened_now = 0
    waiting = opened.execute(select(Export).where(Export.status.in_(exports.STILL_WAITING), Export.ready_at.is_not(None), Export.expires_at.is_not(None))).scalars()
    still_open: set[int] = set()
    for export in waiting:
        expires_at = export.expires_at
        assert expires_at is not None
        if now >= expires_at or expires_at - now > EXPIRY_NOTICE:
            continue
        still_open.add(export.id)
        key = expiry_key(export.id)
        if opened.execute(select(Alert.id).where(Alert.key == key, Alert.closed.is_(None)).limit(1)).first() is None:
            link = f"{settings.public_url}/exports" if settings.public_url else None
            alerts.open_alert(opened, settings, key, EXPIRING, f"export {export.id}", expiring_wording(opened, settings, export, expires_at), link, now)
            opened_now += 1
    keep = {expiry_key(each) for each in still_open}
    for alert in opened.execute(select(Alert).where(Alert.kind == EXPIRING, Alert.closed.is_(None))).scalars():
        if alert.key not in keep:
            alert.closed = now
    return opened_now
