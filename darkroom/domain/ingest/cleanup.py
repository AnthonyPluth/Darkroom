from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events, media
from darkroom.domain.ingest import unpack
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, Export, ExportPart, ImportRun, MediaItem, Person, ReviewItem
from darkroom.storage.paths import Photos

DAY = 24 * 3600.0
KEEP_FOR = 14 * DAY
DROPPING_GRACE = 3600.0
VERIFIED = "cleanup_holds_verified"


@dataclass
class CleanupReport:
    staging: int = 0
    parts: int = 0
    holds: int = 0
    kept: int = 0
    refused: int = 0
    failed: int = 0
    dropped: int = 0


def imported_at(opened: Session, export: Export) -> float | None:
    found = opened.execute(
        select(func.max(ImportRun.finished)).where(ImportRun.export_id == export.id, ImportRun.status == "done")
    ).scalar_one_or_none()
    return None if found is None else float(found)


def latest_done_run(opened: Session, export: Export) -> ImportRun | None:
    return opened.execute(
        select(ImportRun).where(ImportRun.export_id == export.id, ImportRun.status == "done").order_by(ImportRun.finished.desc(), ImportRun.id.desc()).limit(1)
    ).scalar_one_or_none()


def note_once(opened: Session, now: float, type: str, subject: str, detail: str) -> None:
    seen = opened.execute(select(Event.id).where(Event.type == type, Event.subject == subject).limit(1)).first()
    if seen is None:
        events.record(opened, now, type, subject, detail)


def staged_copy(opened: Session, photos: Photos, export: Export, source_path: str) -> Path | None:
    relative = PurePosixPath(source_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    for slug in opened.execute(select(Person.slug).order_by(Person.id)).scalars():
        try:
            staging = photos.staging(slug, unpack.staging_name(export))
        except paths.OutsideRoot:
            continue
        candidate = staging.joinpath(*relative.parts)
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def hold_file(photos: Photos, export: Export, review: ReviewItem) -> Path | None:
    if review.hold_path is None:
        return None
    relative = PurePosixPath(review.hold_path)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 5 or relative.parts[0] != "_inbox":
        return None
    try:
        folder = photos.review_hold(relative.parts[1], unpack.staging_name(export))
    except paths.OutsideRoot:
        return None
    hold = photos.root.joinpath(*relative.parts)
    if hold.parent != folder or hold.is_symlink() or not hold.is_file() or hold.resolve().parent != folder:
        return None
    return hold


def hold_is_safe(opened: Session, photos: Photos, export: Export, review: ReviewItem, decision: DedupeDecision) -> bool:
    hold = hold_file(photos, export, review)
    if hold is None or decision.source_path is None:
        return False
    staged = staged_copy(opened, photos, export, decision.source_path)
    return staged is not None and media.sha1_of(hold)[0] == media.sha1_of(staged)[0]


def pending_reviews(opened: Session, export: Export) -> list[ReviewItem]:
    return list(opened.execute(
        select(ReviewItem).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
        .where(DedupeDecision.export_id == export.id, ReviewItem.status == "pending").order_by(ReviewItem.id)
    ).scalars())


def verified_hashes(opened: Session, photos: Photos, export: Export) -> dict[str, str]:
    found = {}
    for review in pending_reviews(opened, export):
        hold = hold_file(photos, export, review)
        if hold is not None:
            found[str(review.id)] = media.sha1_of(hold)[0]
    return found


def holds_still_match(opened: Session, photos: Photos, export: Export) -> bool:
    saved = opened.execute(
        select(Event.detail).where(Event.type == VERIFIED, Event.subject == f"export {export.id}").order_by(Event.id).limit(1)
    ).scalar_one_or_none()
    try:
        expected = json.loads(saved) if saved else None
    except ValueError:
        return False
    if not isinstance(expected, dict):
        return False
    for review in pending_reviews(opened, export):
        hold = hold_file(photos, export, review)
        if hold is None or expected.get(str(review.id)) != media.sha1_of(hold)[0]:
            return False
    return True


def unsafe_reviews(opened: Session, photos: Photos, export: Export) -> int:
    return sum(
        1 for review, decision in opened.execute(
            select(ReviewItem, DedupeDecision).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
            .where(DedupeDecision.export_id == export.id, ReviewItem.status == "pending")
        ).all()
        if not hold_is_safe(opened, photos, export, review, decision)
    )


def remove(photos: Photos, path: Path, folder: bool, report: CleanupReport) -> bool:
    try:
        return paths.remove_inbox_tree(photos, path) if folder else paths.remove_inbox_file(photos, path)
    except (paths.NotInTheInbox, paths.OutsideRoot):
        report.refused += 1
        return False


def staging_trees(opened: Session, photos: Photos, export: Export) -> list[Path]:
    found = []
    for slug in opened.execute(select(Person.slug).order_by(Person.id)).scalars():
        try:
            found.append(photos.export_tree(slug, unpack.staging_name(export)))
        except paths.OutsideRoot:
            continue
    return found


def holds_verified(opened: Session, export: Export) -> bool:
    return opened.execute(select(Event.id).where(Event.type == VERIFIED, Event.subject == f"export {export.id}").limit(1)).first() is not None


def clean_export(opened: Session, photos: Photos, export: Export, now: float, report: CleanupReport) -> None:
    done = latest_done_run(opened, export)
    if done is not None and done.failed > 0:
        report.kept += 1
        note_once(opened, now, "cleanup_kept", f"export {export.id}", f"staging and zip parts kept: {done.failed} items could not be imported")
        return
    trees = staging_trees(opened, photos, export)
    verified = holds_verified(opened, export)
    if any(tree.exists() or tree.is_symlink() for tree in trees):
        if unsafe_reviews(opened, photos, export):
            report.kept += 1
            return
        if not verified:
            events.record(opened, now, VERIFIED, f"export {export.id}", json.dumps(verified_hashes(opened, photos, export), sort_keys=True))
            opened.commit()
    elif not verified or not holds_still_match(opened, photos, export):
        report.kept += 1
        return
    for tree in trees:
        if remove(photos, tree, True, report):
            report.staging += 1
    inbox = photos.inbox_root()
    for part in opened.execute(select(ExportPart).where(ExportPart.export_id == export.id).order_by(ExportPart.number)).scalars():
        if not export.inbox_dir or not part.filename:
            continue
        path = unpack.part_path(export, part)
        if not path.absolute().is_relative_to(inbox) and not path.resolve().is_relative_to(inbox):
            continue
        if remove(photos, path, False, report):
            report.parts += 1


def shared_hold(opened: Session, review: ReviewItem, cutoff: float) -> bool:
    others = opened.execute(select(ReviewItem).where(ReviewItem.hold_path == review.hold_path, ReviewItem.id != review.id)).scalars()
    return any(other.status == "pending" or other.resolved_at is None or other.resolved_at > cutoff for other in others)


def clean_holds(opened: Session, photos: Photos, now: float, report: CleanupReport) -> None:
    cutoff = now - KEEP_FOR
    resolved = opened.execute(
        select(ReviewItem).where(ReviewItem.status != "pending", ReviewItem.resolved_at.is_not(None), ReviewItem.resolved_at <= cutoff,
                                 ReviewItem.hold_path.is_not(None)).order_by(ReviewItem.id)
    ).scalars().all()
    for review in resolved:
        if review.hold_path is None or shared_hold(opened, review, cutoff):
            report.kept += 1
            continue
        relative = PurePosixPath(review.hold_path)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 5 or relative.parts[0] != "_inbox" or relative.parts[2] != "review":
            report.refused += 1
            continue
        path = photos.root.joinpath(*relative.parts)
        try:
            removed = remove(photos, path, False, report) or not path.exists()
        except OSError as error:
            report.failed += 1
            events.record(opened, now, "cleanup_failed", f"review {review.id}", type(error).__name__)
            continue
        if removed:
            report.holds += 1
            review.hold_path = None


def same_file_elsewhere(path: Path, sha1: str, dropping: Path) -> bool:
    return path != dropping and path.is_file() and not path.is_symlink() and paths.file_sha1(path) == sha1


def copy_kept(opened: Session, photos: Photos) -> Callable[[Path, str], bool]:
    def kept(dropping: Path, sha1: str) -> bool:
        for item in opened.execute(select(MediaItem).where(MediaItem.sha1 == sha1)).scalars():
            try:
                if same_file_elsewhere(photos.inside(item.path), sha1, dropping):
                    return True
            except paths.OutsideRoot:
                continue
        held = (select(ReviewItem, Export).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
                .join(Export, Export.id == DedupeDecision.export_id).where(ReviewItem.hold_path.is_not(None)))
        for review, export in opened.execute(held).all():
            hold = hold_file(photos, export, review)
            if hold is not None and same_file_elsewhere(hold, sha1, dropping):
                return True
        return False
    return kept


def clean(opened: Session, photos: Photos, now: float) -> CleanupReport:
    report = CleanupReport()
    cutoff = now - KEEP_FOR
    for export in opened.execute(select(Export).where(Export.status == "imported").order_by(Export.id)).scalars().all():
        finished = imported_at(opened, export)
        if finished is None or finished > cutoff:
            continue
        try:
            clean_export(opened, photos, export, now, report)
        except OSError as error:
            report.failed += 1
            events.record(opened, now, "cleanup_failed", f"export {export.id}", type(error).__name__)
    clean_holds(opened, photos, now, report)
    try:
        report.dropped = paths.sweep_dropping(photos, now - DROPPING_GRACE, copy_kept(opened, photos))
    except OSError as error:
        report.failed += 1
        events.record(opened, now, "cleanup_failed", "google-only", type(error).__name__)
    opened.flush()
    if report.staging or report.parts or report.holds or report.refused or report.failed or report.dropped:
        detail = (f"{report.staging} staging folders, {report.parts} zip parts, {report.holds} review holds removed; {report.refused} refused"
                  + (f", {report.failed} failed" if report.failed else "")
                  + (f", {report.dropped} interrupted removals cleared" if report.dropped else ""))
        events.record(opened, now, "cleanup", "inbox", detail)
    return report
