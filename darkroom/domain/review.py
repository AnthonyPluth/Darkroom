from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from darkroom.domain import accounts, dedupe, events, media, thumbs, xmp
from darkroom.domain.ingest import cleanup, metadata, pairing, place, sidecar
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Export, GoogleAccount, MediaItem, Person, ReviewItem
from darkroom.storage.paths import Photos

UNDO_WINDOW = cleanup.KEEP_FOR
STATUSES = ("pending", "kept_both", "skipped", "undone")
RESOLVED = ("kept_both", "skipped")
ACTIONS = {"keep_both": "kept_both", "skip": "skipped"}
SIDES = ("library", "export")
UNPLACED = "unplaced"
PAGE_SIZE = 50
LIST_LIMIT = 500


class NoSuchReview(LookupError):
    pass


class NoSuchDecision(LookupError):
    pass


class NoSuchItem(LookupError):
    pass


class WrongState(ValueError):
    pass


class TooLate(ValueError):
    pass


class NotServed(ValueError):
    pass


@dataclass
class Changed:
    export: Export
    people: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class Found:
    review: ReviewItem
    decision: DedupeDecision
    export: Export


def name_of(path: str | None) -> str | None:
    return None if path is None else PurePosixPath(path).name


def person_relative(path: str) -> str:
    parts = PurePosixPath(path).parts
    return "/".join(parts[1:]) if len(parts) > 1 else path


def found(opened: Session, review_id: int) -> Found:
    review = opened.get(ReviewItem, review_id)
    decision = None if review is None else opened.get(DedupeDecision, review.decision_id)
    export = None if decision is None else opened.get(Export, decision.export_id)
    if review is None or decision is None or export is None:
        raise NoSuchReview("no such review item")
    return Found(review, decision, export)


def undo_until(review: ReviewItem) -> float | None:
    return None if review.status not in RESOLVED or review.resolved_at is None else review.resolved_at + UNDO_WINDOW


def summary(opened: Session, review: ReviewItem, decision: DedupeDecision) -> dict[str, object]:
    matched = None if decision.matched_item_id is None else opened.get(MediaItem, decision.matched_item_id)
    kept = None if decision.takeout_item_id is None else opened.get(MediaItem, decision.takeout_item_id)
    return {
        "id": review.id, "status": review.status, "decision_id": decision.id, "export_id": decision.export_id, "account_id": decision.account_id,
        "tier": decision.tier, "reason": decision.reason, "decided_at": decision.decided_at, "reversed": decision.reversed_at is not None,
        "resolved_at": review.resolved_at, "undo_until": undo_until(review), "name": name_of(decision.source_path),
        "library_name": None if matched is None else name_of(matched.path), "person_id": decision.person_id,
        "kept_item_id": None if kept is None else kept.id, "kept_person_id": None if kept is None else kept.person_id,
    }


def listing(opened: Session, status: str | None) -> list[dict[str, object]]:
    query = select(ReviewItem, DedupeDecision).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
    if status is not None:
        query = query.where(ReviewItem.status == status)
    order = (ReviewItem.id,) if status == "pending" else (ReviewItem.resolved_at.desc(), ReviewItem.id.desc())
    rows = opened.execute(query.order_by(*order).limit(LIST_LIMIT)).all()
    return [summary(opened, review, decision) for review, decision in rows]


def pending_count(opened: Session) -> int:
    return int(opened.execute(select(func.count(ReviewItem.id)).where(ReviewItem.status == "pending")).scalar_one())


def library_view(item: MediaItem | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "item_id": item.id, "person_id": item.person_id, "source": item.source, "name": name_of(item.path), "path": person_relative(item.path),
        "bytes": item.bytes, "captured_utc": item.captured_utc, "captured_local": item.captured_local, "offset_minutes": item.offset_minutes,
        "make": item.make, "model": item.model, "width": item.width, "height": item.height, "duration_ms": item.duration_ms,
        "is_video": Path(item.path).suffix.lower() in media.VIDEO_SUFFIXES,
    }


def read_tags(read: xmp.Reader, path: Path) -> dict[str, object]:
    try:
        return read([path]).get(path, {})
    except (exiftool.Missing, exiftool.Unreadable, OSError, ValueError):
        return {}



def export_view(photos: Photos, hit: Found, read: xmp.Reader) -> tuple[dict[str, object], str | None]:
    source = hit.decision.source_path
    base: dict[str, object] = {
        "name": name_of(source), "path": source, "held": False, "bytes": None, "captured_utc": None, "captured_local": None,
        "offset_minutes": None, "make": None, "model": None, "width": None, "height": None, "duration_ms": None,
        "is_video": source is not None and PurePosixPath(source).suffix.lower() in media.VIDEO_SUFFIXES,
    }
    hold = cleanup.hold_file(photos, hit.export, hit.review)
    if hold is None:
        return base, None
    shot = media.fingerprint(hold, read_tags(read, hold))
    base.update(
        held=True, bytes=shot.bytes, captured_utc=shot.captured_utc, captured_local=shot.captured_local, offset_minutes=shot.offset_minutes,
        make=shot.make, model=shot.model, width=shot.width, height=shot.height, duration_ms=shot.duration_ms,
    )
    return base, shot.phash


def people_of(opened: Session, account_id: int) -> list[dict[str, object]]:
    served = accounts.served_people(opened, account_id)
    rows = opened.execute(select(Person).where(Person.id.in_(served)).order_by(Person.name, Person.id)).scalars()
    return [{"id": person.id, "name": person.name} for person in rows]


def detail(opened: Session, photos: Photos, review_id: int, read: xmp.Reader = exiftool.read) -> dict[str, object]:
    hit = found(opened, review_id)
    matched = None if hit.decision.matched_item_id is None else opened.get(MediaItem, hit.decision.matched_item_id)
    exported, phash = export_view(photos, hit, read)
    account = opened.get(GoogleAccount, hit.decision.account_id)
    return {
        "review": summary(opened, hit.review, hit.decision), "library": library_view(matched), "export": exported,
        "phash_distance": None if matched is None else dedupe.phash_distance(phash, matched.phash),
        "served_people": people_of(opened, hit.decision.account_id), "default_person_id": None if account is None else account.default_person_id,
    }


def library_file(opened: Session, photos: Photos, item: MediaItem) -> tuple[str, Path]:
    person = opened.get(Person, item.person_id)
    if person is None:
        raise thumbs.NotAPhoto("the item has no person")
    try:
        path = photos.original(person.slug, item.path) if item.source == "phone" else photos.inside(item.path)
    except paths.OutsideRoot as error:
        raise thumbs.NotAPhoto("the file is outside the photo folder") from error
    if not path.is_relative_to(photos.root / person.slug):
        raise thumbs.NotAPhoto("the file is outside the person's folder")
    return person.slug, path


def thumb(opened: Session, photos: Photos, review_id: int, side: str, preview: thumbs.Preview = exiftool.embedded_preview) -> bytes:
    hit = found(opened, review_id)
    if side == "export":
        hold = cleanup.hold_file(photos, hit.export, hit.review)
        if hold is None or hit.review.hold_path is None:
            raise thumbs.NotAPhoto("the held copy is gone")
        return thumbs.thumbnail(photos, PurePosixPath(hit.review.hold_path).parts[1], hold, preview)
    if side != "library":
        raise thumbs.NotAPhoto("no such side")
    matched = None if hit.decision.matched_item_id is None else opened.get(MediaItem, hit.decision.matched_item_id)
    if matched is None:
        raise thumbs.NotAPhoto("the pair has no library item")
    slug, path = library_file(opened, photos, matched)
    return thumbs.thumbnail(photos, slug, path, preview)


def staged_sidecar(opened: Session, photos: Photos, export: Export, source_path: str) -> tuple[Path | None, Path | None]:
    staged = cleanup.staged_copy(opened, photos, export, source_path)
    if staged is None:
        return None, None
    for item in pairing.pair(staged.parent).items:
        if item.path == staged:
            return staged, item.sidecar
    return staged, None


def kept_fields(opened: Session, photos: Photos, hit: Found, hold: Path, person_id: int, now: float, read: xmp.Reader) -> tuple[TakeoutItem, MediaItem]:
    staged, found_sidecar = (None, None) if hit.decision.source_path is None else staged_sidecar(opened, photos, hit.export, hit.decision.source_path)
    raw = None if found_sidecar is None else found_sidecar.read_text(encoding="utf-8", errors="replace")
    parsed = sidecar.parse(raw) if raw else None
    item = TakeoutItem(path=hold, sha1="", copies=(), sidecar=found_sidecar)
    fields = metadata.candidate(item, read_tags(read, hold), parsed, person_id, UNPLACED, now, hit.export.id)
    if staged is not None and media.sha1_of(staged)[0] != fields.sha1:
        raise WrongState("the held copy no longer matches the file in the export, so it was not kept")
    return item, fields


def keep_both(opened: Session, photos: Photos, hit: Found, now: float, read: xmp.Reader, write: xmp.Writer) -> set[int]:
    hold = cleanup.hold_file(photos, hit.export, hit.review)
    account = opened.get(GoogleAccount, hit.export.account_id)
    if hold is None or account is None:
        raise WrongState("the held copy from the export is gone, so it cannot be kept")
    item, fields = kept_fields(opened, photos, hit, hold, account.default_person_id, now, read)
    try:
        row = place.place_google_only(opened, photos, hit.export, place.Entry(item, fields, hit.decision), place.PlaceReport(), read, write)
    except place.PlaceFailed as error:
        raise WrongState("the held copy could not be placed in google-only") from error
    return {row.person_id}


def resolve(opened: Session, photos: Photos, review_id: int, action: str, now: float,
            read: xmp.Reader = exiftool.read, write: xmp.Writer = exiftool.write_xmp) -> Changed:
    if action not in ACTIONS:
        raise WrongState("the action is keep_both or skip")
    hit = found(opened, review_id)
    if hit.review.status != "pending":
        raise WrongState("this pair is already decided; undo that decision first")
    if hit.decision.reversed_at is not None:
        raise WrongState("this pair's dedupe decision was reversed")
    changed = Changed(hit.export)
    if action == "keep_both":
        changed.people |= keep_both(opened, photos, hit, now, read, write)
    hit.review.status, hit.review.resolved_at = ACTIONS[action], now
    opened.flush()
    events.record(opened, now, "review_resolved", f"review {hit.review.id}", action)
    return changed


def still_referenced(opened: Session, item: MediaItem, decision: DedupeDecision) -> tuple[bool, bool]:
    matched = opened.execute(
        select(DedupeDecision.id).where(DedupeDecision.matched_item_id == item.id, DedupeDecision.reversed_at.is_(None)).limit(1)
    ).first() is not None
    shared = opened.execute(
        select(DedupeDecision.id).where(DedupeDecision.takeout_item_id == item.id, DedupeDecision.id != decision.id).limit(1)
    ).first() is not None
    return matched, shared


def take_back(opened: Session, photos: Photos, hit: Found, hold: Path) -> set[int]:
    item = None if hit.decision.takeout_item_id is None else opened.get(MediaItem, hit.decision.takeout_item_id)
    if item is None:
        return set()
    matched, shared = still_referenced(opened, item, hit.decision)
    if matched:
        raise WrongState("a later import matched the kept copy; reverse that decision before undoing this one")
    hit.decision.takeout_item_id = None
    if shared:
        return set()
    try:
        paths.drop_library_copy(photos, item.path, hold)
    except paths.NotALibraryCopy as error:
        raise WrongState("the kept copy is not where it was placed, so it was left alone") from error
    owner = item.person_id
    opened.delete(item)
    return {owner}


def undo(opened: Session, photos: Photos, review_id: int, now: float) -> Changed:
    hit = found(opened, review_id)
    if hit.review.status not in RESOLVED or hit.review.resolved_at is None:
        raise WrongState("only a kept or skipped pair can be undone")
    if hit.decision.reversed_at is not None:
        raise WrongState("this pair's dedupe decision was reversed, so the pair can no longer go back to the queue")
    if now - hit.review.resolved_at >= UNDO_WINDOW:
        raise TooLate("this decision is more than 14 days old and its held copy is cleaned up, so it can no longer be undone")
    hold = cleanup.hold_file(photos, hit.export, hit.review)
    if hold is None:
        raise WrongState("the held copy from the export is gone, so this decision can no longer be undone")
    changed = Changed(hit.export)
    if hit.review.status == "kept_both":
        changed.people |= take_back(opened, photos, hit, hold)
    hit.review.status, hit.review.resolved_at = "pending", None
    opened.flush()
    events.record(opened, now, "review_undone", f"review {hit.review.id}")
    return changed


def decision_view(opened: Session, row: DedupeDecision) -> dict[str, object]:
    review = opened.execute(select(ReviewItem).where(ReviewItem.decision_id == row.id)).scalar_one_or_none()
    return {
        "id": row.id, "export_id": row.export_id, "account_id": row.account_id, "name": name_of(row.source_path), "path": row.source_path,
        "person_id": row.person_id, "tier": row.tier, "reason": row.reason, "decided_by": row.decided_by, "decided_at": row.decided_at,
        "reversed_at": row.reversed_at, "matched_item_id": row.matched_item_id, "takeout_item_id": row.takeout_item_id,
        "review_status": None if review is None else review.status,
    }


def decisions(opened: Session, person_id: int | None, tier: int | None, export_id: int | None, page: int) -> tuple[list[dict[str, object]], int]:
    query: Select[DedupeDecision] = select(DedupeDecision)
    if person_id is not None:
        query = query.where(DedupeDecision.person_id == person_id)
    if tier is not None:
        query = query.where(DedupeDecision.tier == tier)
    if export_id is not None:
        query = query.where(DedupeDecision.export_id == export_id)
    total = int(opened.execute(select(func.count()).select_from(query.subquery())).scalar_one())
    rows = opened.execute(query.order_by(DedupeDecision.decided_at.desc(), DedupeDecision.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE)).scalars()
    return [decision_view(opened, row) for row in rows], total


def reverse(opened: Session, decision_id: int, now: float) -> DedupeDecision:
    row = opened.get(DedupeDecision, decision_id)
    if row is None:
        raise NoSuchDecision("no such dedupe decision")
    if row.reversed_at is not None:
        raise WrongState("this decision is already reversed")
    review = opened.execute(select(ReviewItem).where(ReviewItem.decision_id == row.id)).scalar_one_or_none()
    if review is not None and review.status == "pending":
        raise WrongState("this pair is waiting in the review queue; keep both or skip it there instead")
    return dedupe.reverse(opened, row.id, now, "owner")


def reassign_checks(opened: Session, item: MediaItem, person_id: int) -> tuple[Export, Person, Person]:
    if item.source != "takeout" or item.export_id is None:
        raise WrongState("only a photo from a Takeout export can be reassigned")
    export = opened.get(Export, item.export_id)
    current, target = opened.get(Person, item.person_id), opened.get(Person, person_id)
    if export is None or current is None:
        raise WrongState("only a photo from a Takeout export can be reassigned")
    if target is None or person_id not in accounts.served_people(opened, export.account_id):
        raise NotServed("the Google account this photo came from doesn't serve that person")
    return export, current, target


def reassign(opened: Session, photos: Photos, item_id: int, person_id: int, now: float,
             read: xmp.Reader = exiftool.read, write: xmp.Writer = exiftool.write_xmp) -> tuple[MediaItem, Changed]:
    item = opened.get(MediaItem, item_id)
    if item is None:
        raise NoSuchItem("no such photo")
    export, current, target = reassign_checks(opened, item, person_id)
    if target.id == current.id:
        return item, Changed(export)
    try:
        source = photos.google_only_file(item.path)
    except (paths.NotALibraryCopy, paths.OutsideRoot) as error:
        raise WrongState("only a photo in a google-only folder can be reassigned") from error
    if not source.is_relative_to(photos.root / current.slug):
        raise WrongState("only a photo in its person's google-only folder can be reassigned")
    if opened.execute(select(DedupeDecision.id).where(DedupeDecision.matched_item_id == item.id, DedupeDecision.reversed_at.is_(None)).limit(1)).first():
        raise WrongState("a later import matched this photo; reverse that decision before reassigning it")
    if opened.execute(select(MediaItem.id).where(MediaItem.person_id == target.id, MediaItem.sha1 == item.sha1).limit(1)).first():
        raise WrongState(f"{target.name} already has this photo")
    try:
        placed = place.copy_into(place.google_only_folder(photos, target.slug, item), source, item.sha1)
    except place.PlaceFailed as error:
        raise WrongState("the photo could not be moved; it was left where it was") from error
    had_sidecar = paths.sidecar_of(placed.path).exists()
    try:
        if not paths.carry_sidecar(source, placed.path):
            xmp.fill(placed.path, xmp.GoogleMetadata.of(item.sidecar_json), True, read, write)
        paths.drop_library_copy(photos, item.path, placed.path)
    except Exception as error:
        if placed.copied:
            paths.drop_library_copy(photos, place.relative(photos, placed.path), source, with_sidecar=not had_sidecar)
        if isinstance(error, (paths.NotALibraryCopy, paths.NotASidecar, paths.AlreadyExists)):
            raise WrongState("the photo could not be moved; it was left where it was") from error
        raise
    item.person_id, item.path = target.id, place.relative(photos, placed.path)
    for row in opened.execute(select(DedupeDecision).where(DedupeDecision.takeout_item_id == item.id)).scalars():
        row.person_id = target.id
    opened.flush()
    events.record(opened, now, "media_reassigned", f"item {item.id}", f"person {current.id} to person {target.id}")
    return item, Changed(export, {current.id, target.id})


def media_view(item: MediaItem) -> dict[str, object]:
    view = library_view(item)
    assert view is not None
    return {**view, "id": item.id, "export_id": item.export_id, "lat": item.lat, "lon": item.lon}


def missing_gps(opened: Session, person_id: int | None, start: float | None, end: float | None, page: int) -> tuple[list[dict[str, object]], int]:
    query: Select[MediaItem] = select(MediaItem).where(or_(MediaItem.lat.is_(None), MediaItem.lon.is_(None)))
    if person_id is not None:
        query = query.where(MediaItem.person_id == person_id)
    if start is not None:
        query = query.where(MediaItem.captured_utc >= start)
    if end is not None:
        query = query.where(MediaItem.captured_utc < end)
    total = int(opened.execute(select(func.count()).select_from(query.subquery())).scalar_one())
    ordered = query.order_by(MediaItem.captured_utc.is_(None), MediaItem.captured_utc.desc(), MediaItem.id.desc())
    rows = opened.execute(ordered.offset(page * PAGE_SIZE).limit(PAGE_SIZE)).scalars()
    return [media_view(item) for item in rows], total
