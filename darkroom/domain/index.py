from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events, media
from darkroom.providers import exiftool
from darkroom.storage.models import IndexSkip, MediaItem, Person
from darkroom.storage.paths import OutsideRoot, Photos

MEDIA_TYPES = ("image/", "video/")
SKIPPED_FOLDERS = {".stfolder", ".stversions", ".uploads_tmp", "uploads_tmp"}
Reader = Callable[[list[Path]], dict[Path, dict[str, object]]]
UNREADABLE = "exiftool could not read it"
NO_METADATA = "exiftool found no metadata"
NOT_MEDIA = "not a photo or video"
UNUSABLE = "its metadata could not be used"
SKIP_NOTE = "skipped until the file changes"


class Unusable(ValueError):
    pass


@dataclass
class IndexReport:
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    failed: int = 0
    skipped: int = 0


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
    return {skip.path: skip for skip in opened.execute(select(IndexSkip).where(IndexSkip.person_id == person.id)).scalars()}


def skip(opened: Session, person: Person, skips: dict[str, IndexSkip], file: Pending, reason: str, now: float, report: IndexReport) -> None:
    row = skips.get(file.key)
    if row is None:
        row = IndexSkip(person_id=person.id, path=file.key, bytes=file.size, mtime=file.mtime, reason=reason, skipped_at=now)
        opened.add(row)
        skips[file.key] = row
    row.bytes, row.mtime, row.reason, row.skipped_at = file.size, file.mtime, reason, now
    report.failed += 1
    events.record(opened, now, "index_failed", person.slug, f"{reason}, {SKIP_NOTE}")


def read_each(read: Reader, paths: list[Path]) -> tuple[dict[Path, dict[str, object]], set[Path]]:
    try:
        return read(paths), set()
    except exiftool.Unreadable:
        if len(paths) == 1:
            raise
    tags: dict[Path, dict[str, object]] = {}
    unreadable: set[Path] = set()
    for path in paths:
        try:
            tags.update(read([path]))
        except exiftool.Unreadable:
            unreadable.add(path)
    if len(unreadable) == len(paths):
        raise exiftool.Unreadable("exiftool could not read any of the files")
    return tags, unreadable


def usable(entry: dict[str, object] | None) -> dict[str, object]:
    if entry is None:
        raise Unusable(NO_METADATA)
    if "Error" in entry:
        raise Unusable(UNREADABLE)
    if not str(entry.get("File:MIMEType", "")).startswith(MEDIA_TYPES):
        raise Unusable(NOT_MEDIA)
    return entry


def index_files(opened: Session, photos: Photos, person: Person, files: list[Path], now: float, report: IndexReport, read: Reader) -> None:
    existing = {item.path: item for item in opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone")).scalars()}
    skips = skips_of(opened, person)
    pending: list[Pending] = []
    for path in files:
        key = relative(photos, path)
        try:
            stat = path.stat()
        except OSError:
            report.failed += 1
            continue
        item = existing.get(key)
        if item is not None and item.bytes == stat.st_size and item.mtime == stat.st_mtime:
            report.unchanged += 1
            continue
        known = skips.get(key)
        if known is not None and known.bytes == stat.st_size and known.mtime == stat.st_mtime:
            report.skipped += 1
            continue
        pending.append(Pending(path, key, item, stat.st_size, stat.st_mtime))
    if not pending:
        return
    try:
        tags, unreadable = read_each(read, [file.path for file in pending])
    except exiftool.Unreadable as error:
        report.failed += len(pending)
        events.record(opened, now, "index_failed", person.slug, str(error)[:200])
        return
    for file in pending:
        if file.path in unreadable:
            skip(opened, person, skips, file, UNREADABLE, now, report)
            continue
        try:
            print_ = media.fingerprint(file.path, usable(tags.get(file.path)))
        except OSError as error:
            report.failed += 1
            events.record(opened, now, "index_failed", person.slug, type(error).__name__)
            continue
        except Unusable as error:
            skip(opened, person, skips, file, str(error), now, report)
            continue
        except Exception:
            skip(opened, person, skips, file, UNUSABLE, now, report)
            continue
        item = file.item
        if item is None:
            item = MediaItem(person_id=person.id, source="phone", path=file.key, sha1="", bytes=0, indexed_at=now)
            opened.add(item)
            report.new += 1
        else:
            report.changed += 1
        apply(item, print_, file.mtime, now)
        known = skips.pop(file.key, None)
        if known is not None:
            opened.delete(known)


def forget_gone_skips(opened: Session, photos: Photos, person: Person, files: list[Path]) -> None:
    skips = skips_of(opened, person)
    if not skips:
        return
    present = {relative(photos, path) for path in files}
    for path, row in skips.items():
        if path not in present:
            opened.delete(row)


def backfill_dhash(opened: Session, photos: Photos, person: Person) -> int:
    missing = opened.execute(
        select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone", MediaItem.phash.is_not(None), MediaItem.dhash.is_(None))
    ).scalars().all()
    filled = 0
    for item in missing:
        try:
            path = photos.inside(*Path(item.path).parts)
        except OutsideRoot:
            continue
        item.dhash = media.perceptual_hashes(path).dhash
        filled += item.dhash is not None
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
    phone_items = (MediaItem.person_id == Person.id) & (MediaItem.source == "phone")
    query = (select(Person.slug, Person.name, func.count(MediaItem.id)).outerjoin(MediaItem, phone_items)
             .group_by(Person.id, Person.slug, Person.name).order_by(Person.id))
    return [(slug, name, int(count)) for slug, name, count in opened.execute(query).all()]


def index_file(opened: Session, photos: Photos, person: Person, path: Path, now: float, read: Reader = exiftool.read) -> IndexReport:
    report = IndexReport()
    resolved = path.resolve()
    root = photos.originals(person.slug)
    if resolved.is_file() and resolved.is_relative_to(root) and wanted(root, resolved):
        index_files(opened, photos, person, [resolved], now, report, read)
    opened.flush()
    return report
