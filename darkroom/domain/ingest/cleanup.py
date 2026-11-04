from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.domain.ingest import unpack
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Export, ExportPart, ImportRun, Person, ReviewItem
from darkroom.storage.paths import Photos

DAY = 24 * 3600.0
KEEP_FOR = 14 * DAY


@dataclass
class CleanupReport:
    staging: int = 0
    parts: int = 0
    holds: int = 0
    kept: int = 0
    refused: int = 0


def imported_at(opened: Session, export: Export) -> float | None:
    found = opened.execute(
        select(func.max(ImportRun.finished)).where(ImportRun.export_id == export.id, ImportRun.status == "done")
    ).scalar_one_or_none()
    return None if found is None else float(found)


def unheld_reviews(opened: Session, export: Export) -> int:
    return int(opened.execute(
        select(func.count(ReviewItem.id)).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
        .where(DedupeDecision.export_id == export.id, ReviewItem.status == "pending", ReviewItem.hold_path.is_(None))
    ).scalar_one())


def remove(photos: Photos, path: Path, folder: bool, report: CleanupReport) -> bool:
    try:
        return paths.remove_inbox_tree(photos, path) if folder else paths.remove_inbox_file(photos, path)
    except (paths.NotInTheInbox, paths.OutsideRoot):
        report.refused += 1
        return False


def clean_export(opened: Session, photos: Photos, export: Export, report: CleanupReport) -> None:
    if unheld_reviews(opened, export):
        report.kept += 1
        return
    for slug in opened.execute(select(Person.slug).order_by(Person.id)).scalars():
        try:
            tree = photos.export_tree(slug, unpack.staging_name(export))
        except paths.OutsideRoot:
            continue
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
        if remove(photos, path, False, report) or not path.exists():
            report.holds += 1
            review.hold_path = None


def clean(opened: Session, photos: Photos, now: float) -> CleanupReport:
    report = CleanupReport()
    cutoff = now - KEEP_FOR
    for export in opened.execute(select(Export).where(Export.status == "imported").order_by(Export.id)).scalars().all():
        finished = imported_at(opened, export)
        if finished is None or finished > cutoff:
            continue
        clean_export(opened, photos, export, report)
    clean_holds(opened, photos, now, report)
    opened.flush()
    if report.staging or report.parts or report.holds or report.refused:
        detail = f"{report.staging} staging folders, {report.parts} zip parts, {report.holds} review holds removed; {report.refused} refused"
        events.record(opened, now, "cleanup", "inbox", detail)
    return report
