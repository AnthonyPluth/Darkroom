from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute, Session

from darkroom import config, monitoring
from darkroom.domain import events, index, media, writer_locks
from darkroom.domain.ingest import run as ingest_run
from darkroom.providers import exiftool
from darkroom.storage import db
from darkroom.storage.models import Export, ImportRun, MediaItem, Person, known_contents, on_unraid
from darkroom.storage.paths import OutsideRoot, Photos

BATCH = 50
EVENT = "video_times_reread"
SUBJECT = "video times"
CORRECTED_SOURCE = "quicktime_utc"
NOT_FINGERPRINTED: tuple[type[Exception], ...] = (index.Unusable, OSError, *media.UNDECODABLE)
IN_FLIGHT = frozenset({"unpacking", "importing"})
BUSY = ("an import is running or a Takeout import is in flight for a person it would read; nothing was changed. "
        "Run this again once the import has finished.")
DRY_RUN_HEADING = "Dry run: nothing was changed."
DRY_RUN_NOTE = "Run it again with --apply to correct the ones that would change."


class Busy(RuntimeError):
    pass


@dataclass(frozen=True)
class Times:
    captured_utc: float | None
    captured_local: str | None
    offset_minutes: int | None
    time_source: str


@dataclass(frozen=True)
class Stored:
    id: int
    path: str
    sha1: str
    bytes: int
    mtime: float | None
    times: Times


@dataclass
class Report:
    read: int = 0
    changed: int = 0
    unchanged: int = 0
    no_quicktime_date: int = 0
    unreadable: int = 0
    changed_since_indexed: int = 0

    def detail(self) -> str:
        return (f"{self.changed} corrected, {self.unchanged} unchanged, {self.no_quicktime_date} without a QuickTime date, "
                f"{self.unreadable} unreadable, {self.changed_since_indexed} changed since indexed")

    def summary(self, applied: bool) -> str:
        changed = f"Corrected: {self.changed}" if applied else f"Would change: {self.changed}"
        return (f"Indexed videos read again: {self.read}. {changed}. Unchanged: {self.unchanged}. No QuickTime date: {self.no_quicktime_date}. "
                f"Unreadable file: {self.unreadable}. Changed since indexed: {self.changed_since_indexed}.")


def a_video() -> ColumnElement[bool]:
    return or_(*(func.lower(MediaItem.path).like(f"%{suffix}") for suffix in sorted(media.VIDEO_SUFFIXES)))


def videos(opened: Session, person_ids: list[int], after: int) -> list[Stored]:
    query = (select(MediaItem.id, MediaItem.path, MediaItem.sha1, MediaItem.bytes, MediaItem.mtime, MediaItem.captured_utc, MediaItem.captured_local,
                    MediaItem.offset_minutes, MediaItem.time_source)
             .where(MediaItem.source == "phone", MediaItem.person_id.in_(person_ids), known_contents(), on_unraid(), a_video(), MediaItem.id > after)
             .order_by(MediaItem.id).limit(BATCH))
    return [Stored(row.id, row.path, row.sha1, row.bytes, row.mtime, Times(row.captured_utc, row.captured_local, row.offset_minutes, row.time_source))
            for row in opened.execute(query)]


def times_of(print_: media.Fingerprint) -> Times:
    return Times(print_.captured_utc, print_.captured_local, print_.offset_minutes, print_.time_source)


def on_disk(photos: Photos, row: Stored) -> tuple[Path | None, bool]:
    try:
        path = photos.inside(*Path(row.path).parts)
        found = path.stat()
    except (OutsideRoot, OSError):
        return None, False
    return path, found.st_size == row.bytes and found.st_mtime == row.mtime


def fingerprint(path: Path, entry: dict[str, object] | None) -> media.Fingerprint | None:
    try:
        return media.fingerprint(path, index.usable(entry))
    except NOT_FINGERPRINTED:
        return None


def examine(photos: Photos, rows: list[Stored], read: index.Reader, report: Report) -> list[tuple[Stored, Times]]:
    present: dict[Path, Stored] = {}
    for row in rows:
        path, same = on_disk(photos, row)
        if path is None:
            report.unreadable += 1
        elif not same:
            report.changed_since_indexed += 1
        else:
            present[path] = row
    tags = index.read_all(read, list(present))
    changes: list[tuple[Stored, Times]] = []
    for path, row in present.items():
        print_ = None if path in tags.unreadable or path in tags.deferred else fingerprint(path, tags.found.get(path))
        if print_ is None:
            report.unreadable += 1
        elif print_.sha1 != row.sha1:
            report.changed_since_indexed += 1
        elif print_.time_source != CORRECTED_SOURCE:
            report.no_quicktime_date += 1
        elif times_of(print_) == row.times:
            report.unchanged += 1
        else:
            changes.append((row, times_of(print_)))
    return changes


def same(column: InstrumentedAttribute[Any], value: object) -> ColumnElement[bool]:
    return column.is_(None) if value is None else column == value


def still_as_read(row: Stored) -> list[ColumnElement[bool]]:
    return [MediaItem.id == row.id, MediaItem.sha1 == row.sha1, MediaItem.bytes == row.bytes, same(MediaItem.mtime, row.mtime),
            same(MediaItem.captured_utc, row.times.captured_utc), same(MediaItem.captured_local, row.times.captured_local),
            same(MediaItem.offset_minutes, row.times.offset_minutes), MediaItem.time_source == row.times.time_source]


def write(opened: Session, changes: list[tuple[Stored, Times]], report: Report) -> None:
    db.write_lock(opened)
    for row, times in changes:
        result = opened.execute(update(MediaItem).where(*still_as_read(row)).values(
            captured_utc=times.captured_utc, captured_local=times.captured_local, offset_minutes=times.offset_minutes, time_source=times.time_source))
        if cast("CursorResult[Any]", result).rowcount == 1:
            report.changed += 1
        else:
            report.changed_since_indexed += 1
    opened.commit()


def in_flight(opened: Session, people: list[Person]) -> bool:
    ids = sorted(set().union(*(writer_locks.accounts_of(opened, person) for person in people)))
    owners = [person.id for person in people]
    theirs = or_(Export.account_id.in_(ids), Export.owner_person_id.in_(owners))
    busy = or_(Export.status.in_(IN_FLIGHT), Export.id.in_(ingest_run.waiting_exports(opened)),
               Export.id.in_(select(ImportRun.export_id).where(ImportRun.status.in_(ingest_run.WAITING))))
    return opened.execute(select(Export.id).where(theirs, busy).limit(1)).first() is not None


def reread(opened: Session, settings: config.Settings, people: list[Person], now: float, apply: bool = False,
           read: index.Reader = exiftool.read) -> Report:
    photos = Photos(settings.photos_root)
    report = Report()
    person_ids = [person.id for person in people]
    with contextlib.ExitStack() as stack:
        try:
            writer_locks.hold(stack, opened, settings, people, set().union(*(writer_locks.accounts_of(opened, person) for person in people)))
        except writer_locks.Busy as error:
            raise Busy(BUSY) from error
        if in_flight(opened, people):
            raise Busy(BUSY)
        opened.commit()
        after = 0
        while rows := videos(opened, person_ids, after):
            opened.commit()
            after = rows[-1].id
            report.read += len(rows)
            changes = examine(photos, rows, read, report)
            if apply and changes:
                write(opened, changes, report)
            elif changes:
                report.changed += len(changes)
        if apply:
            events.record(opened, now, EVENT, SUBJECT, report.detail())
            opened.commit()
    monitoring.log("video times reread", applied=apply, read=report.read, changed=report.changed, unchanged=report.unchanged,
                   no_quicktime_date=report.no_quicktime_date, unreadable=report.unreadable, changed_since_indexed=report.changed_since_indexed)
    return report
