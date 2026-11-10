from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from darkroom.domain import events, media
from darkroom.providers import exiftool
from darkroom.storage import db
from darkroom.storage.models import DedupeDecision, IndexSkip, MediaItem, Person
from darkroom.storage.paths import OutsideRoot, Photos

MEDIA_TYPES = ("image/", "video/")
SKIPPED_FOLDERS = {".stfolder", ".stversions", ".uploads_tmp", "uploads_tmp"}
Reader = Callable[[list[Path]], dict[Path, dict[str, object]]]
UNREADABLE = "exiftool could not read it"
NO_METADATA = "exiftool found no metadata"
NOT_MEDIA = "not a photo or video"
UNUSABLE = "its metadata could not be used"
SKIP_NOTE = "set aside and tried again in a day or when the file changes"
RETRY_SET_ASIDE_SECONDS = 86400.0
UNKNOWN_SHA1 = ""
ISOLATION_SECONDS = 300.0


class Unusable(ValueError):
    pass


@dataclass
class IndexReport:
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    failed: int = 0
    skipped: int = 0
    set_aside: int = 0


@dataclass(frozen=True)
class Pending:
    path: Path
    key: str
    item: MediaItem | None
    size: int
    mtime: float


def wanted(root: Path, path: Path) -> bool:
    parts = path.relative_to(root).parts
    if any(part in SKIPPED_FOLDERS or part.startswith(".") for part in parts):
        return False
    return path.suffix.lower() != ".xmp"


def walk(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for folder, names, files in os.walk(root):
        names[:] = [name for name in names if name not in SKIPPED_FOLDERS and not name.startswith(".")]
        found.extend(Path(folder, name) for name in files)
    return sorted(p for p in found if p.is_file() and wanted(root, p))


def relative(photos: Photos, path: Path) -> str:
    return str(path.resolve().relative_to(photos.root))


def apply(item: MediaItem, print_: media.Fingerprint, mtime: float, now: float) -> None:
    item.sha1, item.bytes, item.mtime, item.phash, item.dhash = print_.sha1, print_.bytes, mtime, print_.phash, print_.dhash
    item.width, item.height, item.duration_ms = print_.width, print_.height, print_.duration_ms
    item.make, item.model = print_.make, print_.model
    item.captured_utc, item.captured_local, item.offset_minutes, item.time_source = print_.captured_utc, print_.captured_local, print_.offset_minutes, print_.time_source
    item.lat, item.lon, item.gps_source = print_.lat, print_.lon, print_.gps_source
    item.indexed_at = now


def skips_of(opened: Session, person: Person) -> dict[str, IndexSkip]:
    query = select(IndexSkip).where(IndexSkip.person_id == person.id).execution_options(populate_existing=True)
    return {skip.path: skip for skip in opened.execute(query).scalars()}


def forget_skip(opened: Session, person: Person, path: str) -> None:
    opened.execute(delete(IndexSkip).where(IndexSkip.person_id == person.id, IndexSkip.path == path))


def still_set_aside(known: IndexSkip | None, size: int, mtime: float, now: float) -> bool:
    return known is not None and known.bytes == size and known.mtime == mtime and now - known.skipped_at < RETRY_SET_ASIDE_SECONDS


def forget_confirmation(opened: Session, item: MediaItem) -> None:
    item.confirmed_in_google = False
    opened.execute(update(DedupeDecision).where(DedupeDecision.matched_item_id == item.id).values(matched_item_id=None))


def unknown_contents(opened: Session, item: MediaItem, size: int, now: float) -> None:
    forget_confirmation(opened, item)
    item.sha1, item.bytes, item.mtime, item.phash, item.dhash = UNKNOWN_SHA1, size, None, None, None
    item.width = item.height = item.duration_ms = None
    item.make = item.model = None
    item.captured_utc, item.captured_local, item.offset_minutes, item.time_source = None, None, None, "unknown"
    item.lat, item.lon, item.gps_source = None, None, "none"
    item.indexed_at = now


def set_aside(opened: Session, person: Person, file: Pending, reason: str, now: float, report: IndexReport) -> None:
    if file.item is not None:
        unknown_contents(opened, file.item, file.size, now)
    forget_skip(opened, person, file.key)
    opened.execute(insert(IndexSkip).values(person_id=person.id, path=file.key, bytes=file.size, mtime=file.mtime, reason=reason, skipped_at=now))
    report.failed += 1
    report.set_aside += 1
    events.record(opened, now, "index_failed", person.slug, f"{reason}, {SKIP_NOTE}")


def failed(opened: Session, person: Person, detail: str, now: float, report: IndexReport) -> None:
    report.failed += 1
    events.record(opened, now, "index_failed", person.slug, detail)


@dataclass
class Tags:
    found: dict[Path, dict[str, object]]
    unreadable: set[Path]
    deferred: set[Path]
    error: str = ""


def isolate(read: Reader, group: list[Path], tags: Tags, deadline: float) -> None:
    unreadable: set[Path] = set()
    deferred: set[Path] = set()
    for path in group:
        if monotonic() >= deadline:
            deferred.add(path)
            continue
        try:
            tags.found.update(read([path]))
        except exiftool.TimedOut:
            deferred.add(path)
        except exiftool.Unreadable:
            unreadable.add(path)
    if len(unreadable) + len(deferred) == len(group):
        deferred |= unreadable
        unreadable = set()
    tags.unreadable |= unreadable
    tags.deferred |= deferred


def read_all(read: Reader, paths: list[Path]) -> Tags:
    tags = Tags({}, set(), set())
    deadline: float | None = None
    for start in range(0, len(paths), exiftool.BATCH):
        group = paths[start:start + exiftool.BATCH]
        try:
            tags.found.update(read(group))
            continue
        except exiftool.Unreadable as error:
            tags.error = str(error)[:120]
        if len(group) == 1:
            tags.deferred.update(group)
            continue
        if deadline is None:
            deadline = monotonic() + ISOLATION_SECONDS
        isolate(read, group, tags, deadline)
    return tags


def usable(entry: dict[str, object] | None) -> dict[str, object]:
    if entry is None:
        raise Unusable(NO_METADATA)
    if "Error" in entry:
        raise Unusable(UNREADABLE)
    if not str(entry.get("File:MIMEType", "")).startswith(MEDIA_TYPES):
        raise Unusable(NOT_MEDIA)
    return entry


@dataclass(frozen=True)
class Seen:
    path: Path
    key: str
    size: int
    mtime: float


def phone_items(opened: Session, person: Person) -> dict[str, MediaItem]:
    query = select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone").execution_options(populate_existing=True)
    return {item.path: item for item in opened.execute(query).scalars()}


def to_index(opened: Session, person: Person, seen: list[Seen], now: float) -> tuple[list[Pending], int, int]:
    existing, skips = phone_items(opened, person), skips_of(opened, person)
    pending: list[Pending] = []
    unchanged = skipped = 0
    for file in seen:
        item = existing.get(file.key)
        if item is not None and item.bytes == file.size and item.mtime == file.mtime:
            unchanged += 1
        elif still_set_aside(skips.get(file.key), file.size, file.mtime, now):
            skipped += 1
        else:
            pending.append(Pending(file.path, file.key, item, file.size, file.mtime))
    return pending, unchanged, skipped


@dataclass(frozen=True)
class Outcome:
    key: str
    print_: media.Fingerprint | None = None
    set_aside: str | None = None
    failure: str | None = None


def examine(read: Reader, pending: list[Pending]) -> tuple[list[Outcome], set[str], str]:
    tags = read_all(read, [file.path for file in pending])
    outcomes: list[Outcome] = []
    for file in pending:
        if file.path in tags.deferred:
            continue
        if file.path in tags.unreadable:
            outcomes.append(Outcome(file.key, set_aside=UNREADABLE))
            continue
        try:
            outcomes.append(Outcome(file.key, print_=media.fingerprint(file.path, usable(tags.found.get(file.path)))))
        except Unusable as error:
            outcomes.append(Outcome(file.key, set_aside=str(error)))
        except media.UNDECODABLE:
            outcomes.append(Outcome(file.key, set_aside=UNUSABLE))
        except Exception as error:
            outcomes.append(Outcome(file.key, failure=type(error).__name__))
    return outcomes, {file.key for file in pending if file.path in tags.deferred}, tags.error


def index_files(opened: Session, photos: Photos, person: Person, files: list[Path], now: float, report: IndexReport, read: Reader) -> None:
    seen: list[Seen] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            report.failed += 1
            continue
        seen.append(Seen(path, relative(photos, path), stat.st_size, stat.st_mtime))
    pending, unchanged, skipped = to_index(opened, person, seen, now)
    if not pending:
        report.unchanged += unchanged
        report.skipped += skipped
        return
    outcomes, deferred, error = examine(read, pending)
    db.write_lock(opened)
    current, unchanged, skipped = to_index(opened, person, seen, now)
    report.unchanged += unchanged
    report.skipped += skipped
    still = {file.key: file for file in current}
    examined = {outcome.key for outcome in outcomes}
    unread = [key for key in still if key in deferred or key not in examined]
    if unread:
        report.failed += len(unread)
        detail = f"exiftool could not read {len(unread)} file(s) now, tried again next run"
        events.record(opened, now, "index_failed", person.slug, f"{detail}: {error}" if error else detail)
    for outcome in outcomes:
        file = still.get(outcome.key)
        if file is None:
            continue
        if outcome.set_aside is not None:
            set_aside(opened, person, file, outcome.set_aside, now, report)
        elif outcome.failure is not None or outcome.print_ is None:
            failed(opened, person, outcome.failure or UNUSABLE, now, report)
        else:
            record(opened, person, file, outcome.print_, now, report)


def record(opened: Session, person: Person, file: Pending, print_: media.Fingerprint, now: float, report: IndexReport) -> None:
    item = file.item
    if item is None:
        item = MediaItem(person_id=person.id, source="phone", path=file.key, sha1="", bytes=0, indexed_at=now)
        opened.add(item)
        report.new += 1
    else:
        report.changed += 1
        if item.sha1 != print_.sha1:
            forget_confirmation(opened, item)
    apply(item, print_, file.mtime, now)
    forget_skip(opened, person, file.key)


def gone(photos: Photos, present: set[str], path: str) -> bool:
    if path in present:
        return False
    try:
        return not photos.inside(*Path(path).parts).exists()
    except OutsideRoot:
        return True


def forget_gone_skips(opened: Session, photos: Photos, person: Person, files: list[Path]) -> None:
    skips = skips_of(opened, person)
    if not skips:
        return
    present = {relative(photos, path) for path in files}
    if not any(gone(photos, present, path) for path in skips):
        return
    db.write_lock(opened)
    for path, row in skips_of(opened, person).items():
        if gone(photos, present, path):
            opened.execute(delete(IndexSkip).where(IndexSkip.id == row.id, IndexSkip.path == path, IndexSkip.bytes == row.bytes, IndexSkip.mtime == row.mtime,
                                                   IndexSkip.skipped_at == row.skipped_at))


@dataclass(frozen=True)
class Backfill:
    item_id: int
    sha1: str
    mtime: float | None
    dhash: str


def backfill_dhash(opened: Session, photos: Photos, person: Person) -> int:
    missing = opened.execute(
        select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone", MediaItem.phash.is_not(None), MediaItem.dhash.is_(None))
    ).scalars().all()
    computed: list[Backfill] = []
    for item in missing:
        try:
            path = photos.inside(*Path(item.path).parts)
        except OutsideRoot:
            continue
        dhash = media.perceptual_hashes(path).dhash
        if dhash is not None:
            computed.append(Backfill(item.id, item.sha1, item.mtime, dhash))
    if not computed:
        return 0
    db.write_lock(opened)
    filled = 0
    for each in computed:
        same_mtime = MediaItem.mtime.is_(None) if each.mtime is None else MediaItem.mtime == each.mtime
        result = opened.execute(update(MediaItem).where(MediaItem.id == each.item_id, MediaItem.sha1 == each.sha1, same_mtime, MediaItem.dhash.is_(None))
                                .values(dhash=each.dhash))
        filled += cast("CursorResult[Any]", result).rowcount
    return filled


def index_person(opened: Session, photos: Photos, person: Person, now: float, read: Reader = exiftool.read) -> IndexReport:
    report = IndexReport()
    files = walk(photos.originals(person.slug))
    index_files(opened, photos, person, files, now, report, read)
    forget_gone_skips(opened, photos, person, files)
    backfill_dhash(opened, photos, person)
    opened.flush()
    return report


def skipped_by_person(opened: Session) -> dict[int, int]:
    query = select(IndexSkip.person_id, func.count(IndexSkip.id)).group_by(IndexSkip.person_id)
    return {person_id: int(count) for person_id, count in opened.execute(query).all()}


def counts_by_person(opened: Session) -> list[tuple[str, str, int]]:
    phone = (MediaItem.person_id == Person.id) & (MediaItem.source == "phone") & (MediaItem.sha1 != UNKNOWN_SHA1)
    query = (select(Person.slug, Person.name, func.count(MediaItem.id)).outerjoin(MediaItem, phone)
             .group_by(Person.id, Person.slug, Person.name).order_by(Person.id))
    return [(slug, name, int(count)) for slug, name, count in opened.execute(query).all()]


def index_paths(opened: Session, photos: Photos, person: Person, paths: list[Path], now: float, read: Reader = exiftool.read) -> IndexReport:
    report = IndexReport()
    root = photos.originals(person.slug)
    wanted_paths = [resolved for resolved in (path.resolve() for path in paths) if resolved.is_file() and resolved.is_relative_to(root) and wanted(root, resolved)]
    if wanted_paths:
        index_files(opened, photos, person, wanted_paths, now, report, read)
    opened.flush()
    return report


def index_file(opened: Session, photos: Photos, person: Person, path: Path, now: float, read: Reader = exiftool.read) -> IndexReport:
    return index_paths(opened, photos, person, [path], now, read)
