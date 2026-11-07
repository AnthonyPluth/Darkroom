from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from sqlalchemy import exists, func, inspect, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import accounts, dedupe, events, index, media, pixel_folder
from darkroom.domain.ingest import place
from darkroom.providers import exiftool
from darkroom.storage import locks, paths
from darkroom.storage.models import DedupeDecision, Export, LegacyFile, MediaItem, Person, PixelCopy, PixelPerson
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


class PixelFolderQueue:
    def offer(self, opened: Session, person: Person, original: MediaItem, now: float) -> bool:
        if not pixel_folder.pixels_serving(opened, person.id):
            return False
        try:
            pixel_folder.queue(opened, original, now)
        except pixel_folder.NotInOriginals:
            return False
        return True


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
    failed: int
    queued: int
    pixels: int


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


def proof_accounts(opened: Session, person: Person) -> set[int]:
    owned = select(Export.account_id).where(Export.status == "imported", Export.owner_person_id == person.id)
    return accounts.accounts_serving(opened, person.id) | set(opened.execute(owned).scalars())


def require_takeout(opened: Session, person: Person) -> None:
    imported = select(Export.id).where(Export.status == "imported", Export.account_id.in_(proof_accounts(opened, person))).limit(1)
    if opened.execute(imported).first() is None:
        raise NotReady("the legacy library is merged only after this person's Takeout is imported")


@contextmanager
def person_lock(settings: config.Settings, person: Person) -> Iterator[None]:
    try:
        with locks.exclusive(settings.data_dir / "locks" / f"legacy-import-{person.slug}.lock"):
            yield
    except locks.Held as error:
        raise Busy("a legacy import is already running for this person") from error


def files_of(root: Path) -> tuple[list[Path], set[str]]:
    unreadable: set[str] = set()

    def failed(error: OSError) -> None:
        folder = Path(error.filename) if error.filename else root
        unreadable.add(f"{folder.relative_to(root) if folder.is_relative_to(root) else '.'}/")

    found: list[Path] = []
    for folder, names, files in os.walk(root, onerror=failed):
        names[:] = [name for name in names if name not in index.SKIPPED_FOLDERS and not name.startswith(".")]
        for path in (Path(folder, name) for name in files):
            if not index.wanted(root, path):
                continue
            try:
                regular = stat.S_ISREG(os.lstat(path).st_mode)
            except OSError:
                unreadable.add(f"{path.relative_to(root)}/")
                continue
            if regular:
                found.append(path)
    return sorted(found), unreadable


def proofs(opened: Session, person: Person) -> tuple[dedupe.Shot, ...]:
    served = proof_accounts(opened, person)
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


def mark_failed(opened: Session, person: Person, root: Path, path: Path, known: dict[str, LegacyFile], now: float, reason: str, report: LegacyReport,
                key: str | None = None) -> None:
    key = key or str(path.relative_to(root))
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    row = known.get(key)
    if row is not None and not inspect(row).persistent:
        row = None
    if row is None:
        row = LegacyFile(person_id=person.id, path=key, bytes=size, mtime=mtime, sha1="", status="failed", seen_at=now)
        opened.add(row)
        known[key] = row
    row.bytes, row.mtime, row.sha1, row.status, row.tier = size, mtime, "", "failed", None
    row.matched_item_id = row.original_item_id = row.queued_at = None
    report.failed += 1
    events.record(opened, now, "legacy_failed", person.slug, reason)
    opened.commit()


def fingerprint_all(opened: Session, person: Person, root: Path, pending: list[Path], known: dict[str, LegacyFile], now: float, report: LegacyReport,
                    read: index.Reader) -> list[Seen]:
    if not pending:
        return []
    try:
        tags = read(pending)
    except exiftool.Unreadable as error:
        for path in pending:
            mark_failed(opened, person, root, path, known, now, type(error).__name__, report)
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
            stale = known.pop(str(path.relative_to(root)), None)
            if stale is not None:
                opened.delete(stale)
                opened.commit()
        except (OSError, ValueError) as error:
            mark_failed(opened, person, root, path, known, now, type(error).__name__, report)
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
    stem = source.stem.encode()[:STEM_LIMIT].decode(errors="ignore")
    return f"{stem}-{sha1[:SHA_PREFIX]}{source.suffix.lower()}"


def bring_in(opened: Session, photos: Photos, person: Person, seen: Seen, now: float, read: index.Reader) -> tuple[MediaItem, bool]:
    present = opened.execute(
        select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone", MediaItem.sha1 == seen.print_.sha1).order_by(MediaItem.id).limit(1)
    ).scalar_one_or_none()
    if present is not None:
        return present, False
    destination = photos.originals(person.slug) / LEGACY_FOLDER / copy_name(seen.path, seen.print_.sha1)
    with suppress(paths.AlreadyExists):
        paths.write_once(destination, place.chunks(seen.path))
    if media.sha1_of(destination)[0] != seen.print_.sha1:
        raise CopyMismatch("a different file is already at the copy's name")
    report = index.index_file(opened, photos, person, destination, now, read)
    item = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.path == index.relative(photos, destination))).scalar_one_or_none()
    if item is None or report.failed or item.sha1 != seen.print_.sha1:
        raise CopyMismatch("the copy in originals could not be checked against the legacy file")
    return item, True


def offer(opened: Session, person: Person, row: LegacyFile, queue: PixelQueue, now: float) -> bool:
    original = None if row.original_item_id is None else opened.get(MediaItem, row.original_item_id)
    if original is None or not queue.offer(opened, person, original, now):
        return False
    first = row.queued_at is None
    row.queued_at = now if first else row.queued_at
    return first


def settle(opened: Session, photos: Photos, person: Person, seen: Seen, decision: dedupe.Decision, known: dict[str, LegacyFile], now: float,
           read: index.Reader, queue: PixelQueue, report: LegacyReport) -> None:
    matched = decision.tier in MATCHING_TIERS
    original = None
    if matched:
        report.matched += 1
    else:
        original, copied = bring_in(opened, photos, person, seen, now, read)
        report.unmatched += 1
        report.copied += copied
        report.already_there += not copied
    row = known.get(seen.key)
    if row is None:
        row = LegacyFile(person_id=person.id, path=seen.key, bytes=0, mtime=0.0, sha1="", status="failed", seen_at=now)
        opened.add(row)
        known[seen.key] = row
    row.bytes, row.mtime, row.sha1, row.seen_at = seen.stat.st_size, seen.stat.st_mtime, seen.print_.sha1, now
    row.status, row.tier = "matched" if matched else "unmatched", decision.tier
    row.matched_item_id = decision.matched_item_id if matched else None
    row.original_item_id = None if original is None else original.id
    row.queued_at = None
    opened.flush()
    if not matched:
        report.queued += offer(opened, person, row, queue, now)
    report.new += 1
    opened.commit()


def retry_queue(opened: Session, person: Person, root: Path, known: dict[str, LegacyFile], queue: PixelQueue, now: float, report: LegacyReport,
                skip: set[str]) -> None:
    waiting = opened.execute(
        select(LegacyFile).where(LegacyFile.person_id == person.id, LegacyFile.status == "unmatched", LegacyFile.original_item_id.is_not(None)).order_by(LegacyFile.id)
    ).scalars().all()
    for row in waiting:
        key = row.path
        if key in skip:
            continue
        try:
            report.queued += offer(opened, person, row, queue, now)
            opened.commit()
        except OSError as error:
            opened.rollback()
            mark_failed(opened, person, root, root / key, known, now, type(error).__name__, report)


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
        listed, unreadable = files_of(root)
        for key in [key for key in known if key.endswith("/") and key not in unreadable]:
            opened.delete(known.pop(key))
        for key in sorted(unreadable):
            own = key[:-1] if key[:-1] in known else None
            mark_failed(opened, person, root, root / (own or key), known, now, "Unreadable", report, own or key)
        proved = {shot.id for shot in proofs(opened, person)}
        for path in listed:
            row = known.get(str(path.relative_to(root)))
            if row is None or row.status == "failed":
                pending.append(path)
                continue
            report.already_seen += 1
            try:
                stat = path.stat()
            except OSError as error:
                mark_failed(opened, person, root, path, known, now, type(error).__name__, report)
                continue
            if row.status == "matched" and row.matched_item_id not in proved:
                pending.append(path)
            elif (stat.st_size, stat.st_mtime) != (row.bytes, row.mtime):
                report.changed += 1
                pending.append(path)
        seen = fingerprint_all(opened, person, root, pending, known, now, report, read)
        for each, decision in zip(seen, decisions_for(opened, person, seen), strict=True):
            try:
                settle(opened, photos, person, each, decision, known, now, read, queue, report)
            except (CopyMismatch, OSError) as error:
                opened.rollback()
                mark_failed(opened, person, root, each.path, known, now, type(error).__name__, report)
        retry_queue(opened, person, root, known, queue, now, report, {each.key for each in seen})
        events.record(opened, now, "legacy_imported", person.slug, f"{report.new} new, {report.matched} matched, {report.unmatched} unmatched, {report.failed} failed")
        opened.commit()
    monitoring.log("legacy imported", person=person.slug, new=report.new, matched=report.matched, unmatched=report.unmatched, failed=report.failed)
    return report


def totals(opened: Session) -> list[Totals]:
    counts = {
        (person_id, status): int(count)
        for person_id, status, count in opened.execute(select(LegacyFile.person_id, LegacyFile.status, func.count(LegacyFile.id)).group_by(LegacyFile.person_id, LegacyFile.status))
    }
    sent = (
        select(LegacyFile.person_id, func.count(func.distinct(LegacyFile.id)))
        .join(PixelCopy, PixelCopy.item_id == LegacyFile.original_item_id)
        .join(PixelPerson, (PixelPerson.pixel_id == PixelCopy.pixel_id) & (PixelPerson.person_id == LegacyFile.person_id))
        .where(LegacyFile.status == "unmatched").group_by(LegacyFile.person_id)
    )
    queued = {person_id: int(count) for person_id, count in opened.execute(sent)}
    pixels = {person_id: int(count) for person_id, count in opened.execute(select(PixelPerson.person_id, func.count(PixelPerson.pixel_id)).group_by(PixelPerson.person_id))}
    found = []
    for person in opened.execute(select(Person).order_by(Person.id)).scalars():
        matched, unmatched, failed = (counts.get((person.id, status), 0) for status in ("matched", "unmatched", "failed"))
        found.append(Totals(person.slug, person.name, bool(person.legacy_path), matched + unmatched + failed, matched, unmatched, failed, queued.get(person.id, 0), pixels.get(person.id, 0)))
    return found


def describe(entry: Totals) -> str:
    if not entry.configured and not entry.files:
        return f"{entry.slug}: no legacy folder"
    waiting = entry.unmatched - entry.queued
    text = (f"{entry.slug}: {entry.files} legacy files, {entry.matched} matched in Google, {entry.unmatched + entry.failed} not backed up "
            f"({entry.failed} could not be imported yet, {entry.queued} sent to the Pixel folder, {waiting} waiting for it)")
    return text if entry.pixels or not waiting else f"{text}; no Pixel serves {entry.name}, so nothing can be sent"
