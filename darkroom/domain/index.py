from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events, media
from darkroom.providers import exiftool
from darkroom.storage.models import MediaItem, Person
from darkroom.storage.paths import Photos

MEDIA_TYPES = ("image/", "video/")
SKIPPED_FOLDERS = {".stfolder", ".stversions", ".uploads_tmp", "uploads_tmp"}
Reader = Callable[[list[Path]], dict[Path, dict[str, object]]]


@dataclass
class IndexReport:
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    failed: int = 0


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
    item.sha1, item.bytes, item.mtime, item.phash = print_.sha1, print_.bytes, mtime, print_.phash
    item.width, item.height, item.duration_ms = print_.width, print_.height, print_.duration_ms
    item.make, item.model = print_.make, print_.model
    item.captured_utc, item.captured_local, item.offset_minutes, item.time_source = print_.captured_utc, print_.captured_local, print_.offset_minutes, print_.time_source
    item.lat, item.lon, item.gps_source = print_.lat, print_.lon, print_.gps_source
    item.indexed_at = now


def index_files(opened: Session, photos: Photos, person: Person, files: list[Path], now: float, report: IndexReport, read: Reader) -> None:
    existing = {item.path: item for item in opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone")).scalars()}
    pending: list[tuple[Path, str, MediaItem | None, float]] = []
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
        pending.append((path, key, item, stat.st_mtime))
    if not pending:
        return
    try:
        tags = read([path for path, *_ in pending])
    except exiftool.Unreadable as error:
        report.failed += len(pending)
        events.record(opened, now, "index_failed", person.slug, str(error)[:200])
        return
    for path, key, item, mtime in pending:
        entry = tags.get(path)
        try:
            if entry is None or "Error" in entry:
                raise ValueError(str(entry.get("Error")) if entry else "no metadata")
            if not str(entry.get("File:MIMEType", "")).startswith(MEDIA_TYPES):
                raise ValueError("not a photo or video")
            print_ = media.fingerprint(path, entry)
        except (OSError, ValueError) as error:
            report.failed += 1
            events.record(opened, now, "index_failed", person.slug, type(error).__name__)
            continue
        if item is None:
            item = MediaItem(person_id=person.id, source="phone", path=key, sha1="", bytes=0, indexed_at=now)
            opened.add(item)
            report.new += 1
        else:
            report.changed += 1
        apply(item, print_, mtime, now)


def index_person(opened: Session, photos: Photos, person: Person, now: float, read: Reader = exiftool.read) -> IndexReport:
    report = IndexReport()
    files = walk(photos.originals(person.slug))
    index_files(opened, photos, person, files, now, report, read)
    opened.flush()
    return report


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
