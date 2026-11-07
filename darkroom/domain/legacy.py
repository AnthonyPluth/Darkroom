from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import accounts, dedupe, events, index, media
from darkroom.domain.ingest import place
from darkroom.providers import exiftool
from darkroom.storage import locks, paths
from darkroom.storage.models import DedupeDecision, Export, LegacyFile, MediaItem, Person
from darkroom.storage.paths import Photos

LEGACY_FOLDER = "legacy"
MATCHING_TIERS = frozenset({1, 2})
STEM_LIMIT = 100
SHA_PREFIX = 8


class BadSource(ValueError):
    pass


class NoSource(LookupError):
    pass


class NotReady(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class CopyMismatch(RuntimeError):
    pass


class NotMedia(ValueError):
    pass


class PixelQueue(Protocol):
    def offer(self, opened: Session, person: Person, original: MediaItem, now: float) -> bool: ...


class NoQueue:
    def offer(self, opened: Session, person: Person, original: MediaItem, now: float) -> bool:
        return False


@dataclass
class LegacyReport:
    new: int = 0
    matched: int = 0
    unmatched: int = 0
    copied: int = 0
    already_there: int = 0
    queued: int = 0
    already_seen: int = 0
    changed: int = 0
    ignored: int = 0
    failed: int = 0


@dataclass(frozen=True)
class Totals:
    slug: str
    name: str
    configured: bool
    files: int
    matched: int
    unmatched: int
    queued: int


@dataclass(frozen=True)
class Seen:
    path: Path
    key: str
    stat: os.stat_result
    print_: media.Fingerprint


def overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def check_source(opened: Session, photos: Photos, person: Person, root: Path) -> None:
    if not root.is_absolute():
        raise BadSource("the legacy folder must be an absolute path")
    if not root.is_dir():
        raise BadSource("the legacy folder does not exist")
    if overlaps(root.resolve(), photos.root):
        raise BadSource("the legacy folder and the photo folder must not overlap")
    others = opened.execute(select(Person.legacy_path).where(Person.id != person.id, Person.legacy_path.is_not(None))).scalars()
    if any(other and overlaps(root.resolve(), Path(other)) for other in others):
        raise BadSource("the legacy folder overlaps another person's legacy folder")


def set_source(opened: Session, photos: Photos, person: Person, folder: str, now: float) -> None:
    root = Path(folder)
    check_source(opened, photos, person, root)
    person.legacy_path = str(root.resolve())
    opened.flush()
    events.record(opened, now, "legacy_source_set", person.slug)


def source_root(opened: Session, photos: Photos, person: Person) -> Path:
    if not person.legacy_path:
        raise NoSource("this person has no legacy folder; set one with --dir")
    root = Path(person.legacy_path)
    check_source(opened, photos, person, root)
    return root


def require_takeout(opened: Session, person: Person) -> None:
    served = accounts.accounts_serving(opened, person.id)
    imported = select(Export.id).where(Export.status == "imported", (Export.account_id.in_(served)) | (Export.owner_person_id == person.id)).limit(1)
    if opened.execute(imported).first() is None:
        raise NotReady("the legacy library is merged only after this person's Takeout is imported")


@contextmanager
def person_lock(settings: config.Settings, person: Person) -> Iterator[None]:
    try:
        with locks.exclusive(settings.data_dir / "locks" / f"legacy-import-{person.slug}.lock"):
            yield
    except locks.Held as error:
        raise Busy("a legacy import is already running for this person") from error


def files_of(root: Path) -> list[Path]:
    return [path for path in index.walk(root) if not path.is_symlink()]


def proofs(opened: Session, person: Person) -> tuple[dedupe.Shot, ...]:
    served = accounts.accounts_serving(opened, person.id)
    if not served:
        return ()
    undone = exists().where(DedupeDecision.takeout_item_id == MediaItem.id, DedupeDecision.reversed_at.is_not(None))
    in_takeout = (
        select(MediaItem).join(Export, Export.id == MediaItem.export_id)
        .where(MediaItem.person_id == person.id, MediaItem.source == "takeout", Export.account_id.in_(served), ~undone)
    )
    matched_original = (
        select(MediaItem).join(DedupeDecision, DedupeDecision.matched_item_id == MediaItem.id)
        .where(MediaItem.person_id == person.id, MediaItem.source == "phone", DedupeDecision.account_id.in_(served),
               DedupeDecision.reversed_at.is_(None), DedupeDecision.decided_by == "auto", DedupeDecision.tier.in_(dedupe.CONFIRMING_TIERS))
    )
    found = {item.id: item for query in (in_takeout, matched_original) for item in opened.execute(query).scalars()}
    return tuple(replace(dedupe.shot_of(found[item_id]), source="phone") for item_id in sorted(found))


def fingerprint_all(opened: Session, person: Person, root: Path, pending: list[Path], now: float, report: LegacyReport, read: index.Reader) -> list[Seen]:
    if not pending:
        return []
    try:
        tags = read(pending)
    except exiftool.Unreadable as error:
        report.failed += len(pending)
        events.record(opened, now, "legacy_failed", person.slug, str(error)[:200])
        return []
    found: list[Seen] = []
    for path in pending:
        entry = tags.get(path)
        try:
            if entry is None or "Error" in entry:
                raise ValueError("no metadata")
            if not str(entry.get("File:MIMEType", "")).startswith(index.MEDIA_TYPES):
                raise NotMedia("not a photo or video")
            found.append(Seen(path, str(path.relative_to(root)), path.stat(), media.fingerprint(path, entry)))
        except NotMedia:
            report.ignored += 1
        except (OSError, ValueError) as error:
            report.failed += 1
            events.record(opened, now, "legacy_failed", person.slug, type(error).__name__)
    return found


def candidate_of(seen: Seen) -> dedupe.TakeoutCandidate:
    print_ = seen.print_
    return dedupe.TakeoutCandidate(
        source_path=seen.key, sha1=print_.sha1, phash=print_.phash, captured_utc=print_.captured_utc, captured_local=print_.captured_local,
        width=print_.width, height=print_.height, duration_ms=print_.duration_ms, make=print_.make, model=print_.model,
        is_video=seen.path.suffix.lower() in media.VIDEO_SUFFIXES,
    )


def decisions_for(opened: Session, person: Person, seen: list[Seen]) -> list[dedupe.Decision]:
    shots = proofs(opened, person)
    known = dedupe.PersonIndex(account_id=0, default_person_id=person.id, person_ids=frozenset({person.id}), phone=shots)
    return dedupe.decide_all(known, [candidate_of(each) for each in seen], dedupe.DedupeSettings())


def copy_name(source: Path, sha1: str) -> str:
    return f"{source.stem[:STEM_LIMIT]}-{sha1[:SHA_PREFIX]}{source.suffix.lower()}"


def bring_in(opened: Session, photos: Photos, person: Person, seen: Seen, now: float, read: index.Reader) -> tuple[MediaItem, bool]:
    present = opened.execute(
        select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone", MediaItem.sha1 == seen.print_.sha1).order_by(MediaItem.id).limit(1)
    ).scalar_one_or_none()
    if present is not None:
        return present, False
    destination = photos.originals(person.slug) / LEGACY_FOLDER / copy_name(seen.path, seen.print_.sha1)
    with suppress(paths.AlreadyExists):
        paths.write_once(destination, place.chunks(seen.path))
    report = index.index_file(opened, photos, person, destination, now, read)
    item = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.path == index.relative(photos, destination))).scalar_one_or_none()
    if item is None or report.failed or item.sha1 != seen.print_.sha1:
        raise CopyMismatch("the copy in originals could not be checked against the legacy file")
    return item, True


def offer(opened: Session, person: Person, row: LegacyFile, queue: PixelQueue, now: float) -> bool:
    original = None if row.original_item_id is None else opened.get(MediaItem, row.original_item_id)
    if original is None or not queue.offer(opened, person, original, now):
        return False
    row.queued_at = now
    return True


def settle(opened: Session, photos: Photos, person: Person, seen: Seen, decision: dedupe.Decision, now: float, read: index.Reader,
           queue: PixelQueue, report: LegacyReport) -> None:
    matched = decision.tier in MATCHING_TIERS
    row = LegacyFile(
        person_id=person.id, path=seen.key, bytes=seen.stat.st_size, mtime=seen.stat.st_mtime, sha1=seen.print_.sha1,
        status="matched" if matched else "unmatched", tier=decision.tier, matched_item_id=decision.matched_item_id if matched else None, seen_at=now,
    )
    if matched:
        report.matched += 1
    else:
        original, copied = bring_in(opened, photos, person, seen, now, read)
        row.original_item_id = original.id
        report.unmatched += 1
        report.copied += copied
        report.already_there += not copied
    opened.add(row)
    opened.flush()
    if not matched:
        report.queued += offer(opened, person, row, queue, now)
    report.new += 1
    opened.commit()


def retry_queue(opened: Session, person: Person, queue: PixelQueue, now: float, report: LegacyReport) -> None:
    waiting = opened.execute(
        select(LegacyFile).where(LegacyFile.person_id == person.id, LegacyFile.status == "unmatched", LegacyFile.queued_at.is_(None)).order_by(LegacyFile.id)
    ).scalars().all()
    for row in waiting:
        report.queued += offer(opened, person, row, queue, now)
    opened.commit()


def import_person(opened: Session, settings: config.Settings, person: Person, now: float, read: index.Reader = exiftool.read,
                  queue: PixelQueue | None = None) -> LegacyReport:
    photos = Photos(settings.photos_root)
    root = source_root(opened, photos, person)
    require_takeout(opened, person)
    queue = queue or NoQueue()
    report = LegacyReport()
    with person_lock(settings, person):
        known = {row.path: row for row in opened.execute(select(LegacyFile).where(LegacyFile.person_id == person.id)).scalars()}
        pending: list[Path] = []
        for path in files_of(root):
            row = known.get(str(path.relative_to(root)))
            if row is None:
                pending.append(path)
                continue
            report.already_seen += 1
            stat = path.stat()
            report.changed += (stat.st_size, stat.st_mtime) != (row.bytes, row.mtime)
        retry_queue(opened, person, queue, now, report)
        seen = fingerprint_all(opened, person, root, pending, now, report, read)
        for each, decision in zip(seen, decisions_for(opened, person, seen), strict=True):
            try:
                settle(opened, photos, person, each, decision, now, read, queue, report)
            except (CopyMismatch, OSError) as error:
                opened.rollback()
                report.failed += 1
                events.record(opened, now, "legacy_failed", person.slug, type(error).__name__)
                opened.commit()
        events.record(opened, now, "legacy_imported", person.slug, f"{report.new} new, {report.matched} matched, {report.unmatched} unmatched, {report.failed} failed")
        opened.commit()
    monitoring.log("legacy imported", person=person.slug, new=report.new, matched=report.matched, unmatched=report.unmatched, failed=report.failed)
    return report


def totals(opened: Session) -> list[Totals]:
    counts = {
        (person_id, status): int(count)
        for person_id, status, count in opened.execute(select(LegacyFile.person_id, LegacyFile.status, func.count(LegacyFile.id)).group_by(LegacyFile.person_id, LegacyFile.status))
    }
    queued = {
        person_id: int(count)
        for person_id, count in opened.execute(select(LegacyFile.person_id, func.count(LegacyFile.id)).where(LegacyFile.queued_at.is_not(None)).group_by(LegacyFile.person_id))
    }
    found = []
    for person in opened.execute(select(Person).order_by(Person.id)).scalars():
        matched, unmatched = counts.get((person.id, "matched"), 0), counts.get((person.id, "unmatched"), 0)
        found.append(Totals(person.slug, person.name, bool(person.legacy_path), matched + unmatched, matched, unmatched, queued.get(person.id, 0)))
    return found


def describe(entry: Totals) -> str:
    if not entry.configured and not entry.files:
        return f"{entry.slug}: no legacy folder"
    waiting = entry.unmatched - entry.queued
    return (f"{entry.slug}: {entry.files} legacy files, {entry.matched} matched in Google, {entry.unmatched} not in Google "
            f"({entry.queued} sent to the Pixel folder, {waiting} waiting for it)")
