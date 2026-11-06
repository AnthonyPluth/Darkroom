from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, exports, media, xmp
from darkroom.domain.ingest import pairing, unpack
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Export, MediaItem, Person, ReviewItem
from darkroom.storage.paths import Photos

CHUNK = 1024 * 1024
ANOTHER_FILE = "~"


class PlaceFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    item: TakeoutItem
    fields: MediaItem
    decision: DedupeDecision


@dataclass
class PlaceReport:
    placed: int = 0
    already_there: int = 0
    held: int = 0
    sidecars: int = 0
    skipped: int = 0
    refreshed: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Placed:
    path: Path
    copied: bool


def chunks(source: Path) -> Iterator[bytes]:
    with source.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk


def month_of(fields: MediaItem) -> tuple[int, int] | None:
    local = dedupe.wall_clock(fields.captured_local)
    if local is not None:
        return local.year, local.month
    utc = dedupe.instant(fields.captured_utc)
    if utc is None:
        return None
    try:
        moment = datetime.fromtimestamp(utc, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.year, moment.month


def google_only_folder(photos: Photos, slug: str, fields: MediaItem) -> Path:
    month = month_of(fields)
    if month is None:
        return photos.google_only_undated(slug)
    try:
        return photos.google_only(slug, *month)
    except paths.OutsideRoot:
        return photos.google_only_undated(slug)


def plain_name(path: Path) -> tuple[str, str]:
    counted = pairing.NUMBERED.match(path.stem)
    stem = counted.group("stem") if counted and counted.group("stem") else path.stem
    if stem.startswith("."):
        stem = f"_{stem.lstrip('.')}"
    return stem or "_", path.suffix


def names(source: Path, sha1: str) -> tuple[str, str]:
    stem, suffix = plain_name(source)
    return f"{stem}{suffix}", f"{stem}{ANOTHER_FILE}{sha1[:8]}{suffix}"


def holds(destination: Path, sha1: str) -> bool:
    return destination.is_file() and not destination.is_symlink() and media.sha1_of(destination)[0] == sha1


def adopted(destination: Path, marking: Callable[[Path, bool], None] | None) -> Placed:
    if marking is not None:
        marking(destination, False)
    return Placed(destination, False)


def copy_into(folder: Path, source: Path, sha1: str, marking: Callable[[Path, bool], None] | None = None) -> Placed:
    for name in names(source, sha1):
        destination = folder / name
        if destination.parent != folder:
            raise PlaceFailed("the file name is not a plain name")
        if destination.exists() or destination.is_symlink():
            if holds(destination, sha1):
                return adopted(destination, marking)
            continue
        if marking is not None:
            marking(destination, True)
        try:
            written = paths.write_once(destination, chunks(source))
        except paths.AlreadyExists:
            if holds(destination, sha1):
                return adopted(destination, marking)
            continue
        if written != sha1:
            destination.unlink(missing_ok=True)
            raise PlaceFailed("the staged file changed while it was copied")
        return Placed(destination, True)
    raise PlaceFailed("both names are taken by other files")


def copy_exactly(destination: Path, source: Path, sha1: str, token: str) -> Placed:
    record = paths.placing_record(destination, token)
    paths.mark_placing(destination, token, paths.Placing(destination.name, sha1, True, False))
    try:
        written = paths.write_once(destination, chunks(source))
    except paths.AlreadyExists:
        record.unlink(missing_ok=True)
        raise
    if written != sha1:
        destination.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
        raise PlaceFailed("the file changed while it was copied")
    return Placed(destination, True)


def relative(photos: Photos, path: Path) -> str:
    return str(path.relative_to(photos.root))


def counted(report: PlaceReport, placed: Placed) -> None:
    if placed.copied:
        report.placed += 1
    else:
        report.already_there += 1


def place_google_only(opened: Session, photos: Photos, export: Export, entry: Entry, report: PlaceReport, read: xmp.Reader, write: xmp.Writer,
                      copied: Callable[[Placed], None] = lambda placed: None) -> MediaItem:
    person = exports.owner_of(opened, export)
    if person is None:
        raise PlaceFailed("the export's account has no default person")
    folder = google_only_folder(photos, person.slug, entry.fields)
    placed = copy_into(folder, entry.item.path, entry.fields.sha1)
    copied(placed)
    counted(report, placed)
    path = relative(photos, placed.path)
    row = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.path == path)).scalar_one_or_none()
    if row is None:
        row = entry.fields
        row.person_id, row.source, row.path, row.export_id = person.id, "takeout", path, export.id
        opened.add(row)
        opened.flush()
    entry.decision.takeout_item_id = row.id
    if xmp.fill(placed.path, xmp.GoogleMetadata.of(entry.fields.sidecar_json), True, read, write):
        report.sidecars += 1
    return row


def hold_for_review(opened: Session, photos: Photos, export: Export, entry: Entry, report: PlaceReport) -> ReviewItem | None:
    review = opened.execute(select(ReviewItem).where(ReviewItem.decision_id == entry.decision.id)).scalar_one_or_none()
    person = None if entry.decision.person_id is None else opened.get(Person, entry.decision.person_id)
    if review is None or review.status != "pending" or person is None:
        report.skipped += 1
        return None
    folder = photos.review_hold(person.slug, unpack.staging_name(export))
    placed = copy_into(folder, entry.item.path, entry.fields.sha1)
    report.held += 1
    review.hold_path, review.sidecar_json, review.hold_sha1 = relative(photos, placed.path), entry.fields.sidecar_json, entry.fields.sha1
    return review


def matched_original(opened: Session, photos: Photos, decision: DedupeDecision) -> Path | None:
    if decision.decided_by != "auto" or decision.tier not in dedupe.CONFIRMING_TIERS or decision.matched_item_id is None:
        return None
    matched = opened.get(MediaItem, decision.matched_item_id)
    person = None if matched is None else opened.get(Person, matched.person_id)
    if matched is None or person is None or matched.source != "phone":
        return None
    original = photos.original(person.slug, matched.path)
    return original if original.is_file() and not original.is_symlink() else None


def fill_original(opened: Session, photos: Photos, entry: Entry, report: PlaceReport, read: xmp.Reader, write: xmp.Writer) -> None:
    original = matched_original(opened, photos, entry.decision)
    if original is None:
        report.skipped += 1
        return
    if xmp.fill(original, xmp.GoogleMetadata.of(entry.fields.sidecar_json), False, read, write):
        report.sidecars += 1


def matched_takeout(opened: Session, decision: DedupeDecision) -> MediaItem | None:
    if decision.decided_by != "auto" or decision.tier != 1 or decision.matched_item_id is None:
        return None
    matched = opened.get(MediaItem, decision.matched_item_id)
    return matched if matched is not None and matched.source == "takeout" else None


def refresh_takeout(photos: Photos, matched: MediaItem, entry: Entry, report: PlaceReport, read: xmp.Reader, write: xmp.Writer) -> None:
    old, new = xmp.GoogleMetadata.of(matched.sidecar_json), xmp.GoogleMetadata.of(entry.fields.sidecar_json)
    if old == new:
        report.skipped += 1
        return
    try:
        done = xmp.refresh(photos.google_only_file(matched.path), old, new, read, write)
    except (paths.NotALibraryCopy, paths.OutsideRoot, paths.NotASidecar):
        report.skipped += 1
        return
    if done is None:
        report.skipped += 1
        return
    matched.sidecar_json = entry.fields.sidecar_json
    if not done:
        report.skipped += 1
        return
    if xmp.LATITUDE in done:
        matched.lat, matched.lon, matched.gps_source = new.lat, new.lon, "google"
    report.refreshed.append(matched.id)


def place(opened: Session, photos: Photos, export: Export, entries: Iterable[Entry],
          read: xmp.Reader = exiftool.read, write: xmp.Writer = exiftool.write_xmp) -> PlaceReport:
    report = PlaceReport()
    for entry in entries:
        decision = entry.decision
        if decision.export_id != export.id or decision.account_id != export.account_id:
            raise PlaceFailed("the decision belongs to another export")
        if decision.reversed_at is not None or decision.decided_by != "auto":
            report.skipped += 1
        elif decision.tier == 5:
            place_google_only(opened, photos, export, entry, report, read, write)
        elif decision.tier == 4:
            hold_for_review(opened, photos, export, entry, report)
        elif (matched := matched_takeout(opened, decision)) is not None:
            refresh_takeout(photos, matched, entry, report, read, write)
        elif decision.tier in dedupe.CONFIRMING_TIERS:
            fill_original(opened, photos, entry, report, read, write)
        else:
            report.skipped += 1
    opened.flush()
    return report
