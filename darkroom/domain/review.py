from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from darkroom import monitoring
from darkroom.domain import accounts, dedupe, devices, events, exports, media, thumbs, xmp
from darkroom.domain.ingest import cleanup, metadata, pairing, place, sidecar
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, Export, GoogleAccount, MediaItem, Person, ReviewItem, User
from darkroom.storage.paths import Photos

UNDO_WINDOW = cleanup.KEEP_FOR
STATUSES = ("pending", "kept_both", "skipped", "undone")
RESOLVED = ("kept_both",)
ACTIONS = {"keep_both": "kept_both"}
SIDES = ("library", "export")
UNPLACED = "unplaced"
PAGE_SIZE = 50
LIST_LIMIT = 500
ADOPTED = "; adopted what was already there: "
BY_PERSON = re.compile(r"(?:^|, )by person (\d+)$|^by person (\d+): ")
BY_PERSON_SUFFIX = re.compile(r", by person \d+$")
FOUND_AT = "; found at "
FOUND = re.compile(r"; found at person (\d+), place ([0-9a-f]{16})$")
CARRIES_FOUND = ", carrying what keep both found"
MOVED_BACK = "moved the photo back to where keep both found it"


MOVE_FAILURES = (paths.NotALibraryCopy, paths.NotASidecar, OSError, exiftool.Missing, exiftool.Unreadable, exiftool.WriteFailed)


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


class NotSaved(WrongState):
    pass


@dataclass
class Changed:
    export: Export
    people: set[int] = field(default_factory=set)


@dataclass
class FileEffects:
    undo: list[Callable[[], None]] = field(default_factory=list)
    done: list[Callable[[], None]] = field(default_factory=list)

    def made(self, photos: Photos, placed: place.Placed, kept: Path, had_sidecar: bool) -> None:
        relative, sidecar = place.relative(photos, placed.path), paths.sidecar_of(placed.path)
        if placed.copied:
            self.undo.append(lambda: paths.drop_library_copy(photos, relative, kept, with_sidecar=not had_sidecar))
        elif not had_sidecar:
            self.undo.append(lambda: paths.drop_new_sidecar(photos, relative))
        if had_sidecar and sidecar.is_file() and not sidecar.is_symlink():
            before = sidecar.read_bytes()
            self.undo.append(lambda: paths.restore_sidecar(sidecar, before))

    def placing(self, destination: Path, token: str) -> None:
        self.undo.append(lambda: paths.clear_placing(destination, token, committed=False))
        self.done.append(lambda: paths.clear_placing(destination, token, committed=True))

    def set_aside(self, aside: paths.SetAside) -> None:
        self.undo.append(aside.restore)
        self.done.append(aside.finish)

    def undone(self) -> int:
        left = 0
        for step in reversed(self.undo):
            try:
                step()
            except MOVE_FAILURES:
                left += 1
        if left:
            monitoring.log("file changes not undone", left=left)
        return left

    def finished(self) -> None:
        for step in self.done:
            step()


@contextmanager
def saving(opened: Session) -> Iterator[FileEffects]:
    effects = FileEffects()
    try:
        yield effects
        try:
            opened.commit()
        except Exception as error:
            raise NotSaved("the change could not be saved, so it was undone") from error
    except BaseException as error:
        opened.rollback()
        left = effects.undone()
        if left and isinstance(error, WrongState):
            raise WrongState(f"the change failed, and {left} file{'' if left == 1 else 's'} it made could not be removed") from error
        raise
    effects.finished()


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
        "decided_by_person_id": decided_by(opened, review, kept),
    }


def undoable_count(opened: Session, status: str | None, now: float) -> int:
    query = select(func.count(ReviewItem.id)).where(ReviewItem.status.in_(RESOLVED), ReviewItem.resolved_at > now - UNDO_WINDOW)
    if status is not None:
        query = query.where(ReviewItem.status == status)
    return int(opened.execute(query).scalar_one())


def listing(opened: Session, status: str | None, now: float, after: int | None = None) -> list[dict[str, object]]:
    query = select(ReviewItem, DedupeDecision).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
    if status is not None:
        query = query.where(ReviewItem.status == status)
    if after is not None:
        query = query.where(ReviewItem.id > after)
    order = (ReviewItem.id,) if status == "pending" else (ReviewItem.resolved_at.desc(), ReviewItem.id.desc())
    limit = LIST_LIMIT if status == "pending" else max(LIST_LIMIT, undoable_count(opened, status, now))
    rows = opened.execute(query.order_by(*order).limit(limit)).all()
    return [summary(opened, review, decision) for review, decision in rows]


def pending_count(opened: Session, up_to: int | None = None) -> int:
    query = select(func.count(ReviewItem.id)).where(ReviewItem.status == "pending")
    if up_to is not None:
        query = query.where(ReviewItem.id <= up_to)
    return int(opened.execute(query).scalar_one())


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
    recorded = photos.root.joinpath(*PurePosixPath(item.path).parts)
    if recorded != path:
        raise thumbs.NotAPhoto("the recorded path goes through a link")
    return person.slug, recorded


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
    raw = hit.review.sidecar_json if found_sidecar is None else found_sidecar.read_text(encoding="utf-8", errors="replace")
    parsed = sidecar.parse(raw) if raw else None
    item = TakeoutItem(path=hold, sha1="", copies=(), sidecar=found_sidecar)
    fields = metadata.candidate(item, read_tags(read, hold), parsed, person_id, UNPLACED, now, hit.export.id)
    expected = media.sha1_of(staged)[0] if staged is not None else hit.review.hold_sha1 or (cleanup.saved_hashes(opened, hit.export) or {}).get(str(hit.review.id))
    if expected is None:
        raise WrongState("the held copy can no longer be checked against the export, so it was not kept")
    if expected != fields.sha1:
        raise WrongState("the held copy no longer matches the file in the export, so it was not kept")
    return item, fields


def place_digest(relative: str) -> str:
    return hashlib.sha256(relative.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def keep_both(opened: Session, photos: Photos, hit: Found, now: float, effects: FileEffects, read: xmp.Reader,
              write: xmp.Writer) -> tuple[set[int], list[str], str]:
    hold = cleanup.hold_file(photos, hit.export, hit.review)
    person = exports.owner_of(opened, hit.export)
    if hold is None:
        raise WrongState("the held copy from the export is gone, so it cannot be kept")
    if person is None:
        raise WrongState("the export's account has no default person")
    item, fields = kept_fields(opened, photos, hit, hold, person.id, now, read)
    adopted: list[str] = []
    found: list[str] = []

    def copied(placed: place.Placed) -> None:
        had_sidecar = paths.sidecar_of(placed.path).exists() or paths.sidecar_of(placed.path).is_symlink()
        effects.made(photos, placed, hold, had_sidecar)
        recorded = opened.execute(
            select(MediaItem.sha1).where(MediaItem.person_id == person.id, MediaItem.path == place.relative(photos, placed.path))
        ).scalar_one_or_none()
        if recorded is not None and recorded != fields.sha1:
            raise WrongState("another photo is recorded at that place in google-only, so the pair was not kept")
        adopted.extend(name for name, kept in (("photo", not placed.copied), ("sidecar", had_sidecar), ("row", recorded is not None)) if kept)
        if not placed.copied:
            found.append(f"{FOUND_AT}person {person.id}, place {place_digest(place.relative(photos, placed.path))}")

    try:
        row = place.place_google_only(opened, photos, hit.export, place.Entry(item, fields, hit.decision), place.PlaceReport(), read, write, copied)
    except (place.PlaceFailed, *MOVE_FAILURES) as error:
        raise WrongState("the held copy could not be placed in google-only; nothing was changed") from error
    return {row.person_id}, adopted, "".join(found)


def by_prefix(by: int | None) -> str:
    return "" if by is None else f"by person {by}: "


def person_of_session(opened: Session, sub: str, email: str | None) -> int | None:
    verified = opened.execute(select(User.email_verified).where(User.sub == sub)).scalar_one_or_none() or False
    try:
        return devices.person_for(opened, email, verified).id
    except devices.NoPerson:
        return None


def actor_of(detail: str | None) -> int | None:
    found_by = None if detail is None else BY_PERSON.search(detail)
    return None if found_by is None else int(found_by.group(1) or found_by.group(2))


def decided_by(opened: Session, review: ReviewItem, kept: MediaItem | None) -> int | None:
    if review.status != "kept_both":
        return None
    kept_event = opened.execute(
        select(Event.id, Event.detail).where(Event.type == "review_resolved", Event.subject == f"review {review.id}").order_by(Event.id.desc()).limit(1)
    ).first()
    if kept_event is None:
        return None
    moved = None if kept is None else opened.execute(
        select(Event.detail).where(Event.type == "media_reassigned", Event.subject == f"item {kept.id}", Event.id > kept_event.id).order_by(Event.id.desc()).limit(1)
    ).scalar_one_or_none()
    return actor_of(moved) if moved is not None else actor_of(kept_event.detail)


def adopted_in(detail: str | None) -> set[str]:
    if detail is None or ADOPTED not in detail:
        return set()
    return set(BY_PERSON_SUFFIX.sub("", detail.split(ADOPTED, 1)[1].split(FOUND_AT, 1)[0]).split(", "))


def kept_and_moved(opened: Session, review: ReviewItem, item: MediaItem) -> tuple[str, str | None] | None:
    kept = opened.execute(
        select(Event.id, Event.detail).where(Event.type == "review_resolved", Event.subject == f"review {review.id}").order_by(Event.id.desc()).limit(1)
    ).first()
    if kept is None:
        return None
    moved = opened.execute(
        select(Event.detail).where(Event.type == "media_reassigned", Event.subject == f"item {item.id}", Event.id > kept.id).order_by(Event.id.desc()).limit(1)
    ).scalar_one_or_none()
    return kept.detail or "", moved


def adopted_by(opened: Session, review: ReviewItem, item: MediaItem) -> set[str]:
    events_of = kept_and_moved(opened, review, item)
    if events_of is None:
        return set()
    kept, moved = events_of
    adopted = adopted_in(kept)
    return adopted if moved is None else (adopted & {"row"}) | (adopted_in(moved) & {"photo", "sidecar"})


def found_by(opened: Session, review: ReviewItem, item: MediaItem) -> tuple[int, str] | None:
    events_of = kept_and_moved(opened, review, item)
    match = None if events_of is None else FOUND.search(events_of[0])
    if events_of is None or match is None or {"photo", "row"} & adopted_in(events_of[0]) != {"photo"}:
        return None
    moved = events_of[1]
    if moved is not None and CARRIES_FOUND not in moved.split(ADOPTED, 1)[0]:
        return None
    return int(match.group(1)), match.group(2)


def kept_review(opened: Session, item: MediaItem) -> ReviewItem | None:
    return opened.execute(
        select(ReviewItem).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id)
        .where(DedupeDecision.takeout_item_id == item.id, ReviewItem.status == "kept_both").order_by(ReviewItem.id.desc()).limit(1)
    ).scalar_one_or_none()


def adopted_lineage(opened: Session, item: MediaItem) -> set[str]:
    kept = kept_review(opened, item)
    adopted = set() if kept is None else adopted_by(opened, kept, item)
    found = set() if kept is None or found_by(opened, kept, item) is None else {"found"}
    return (adopted & {"photo", "sidecar"}) | ({"photo"} if "row" in adopted else set()) | found


def resolve(opened: Session, photos: Photos, review_id: int, action: str, now: float, effects: FileEffects,
            read: xmp.Reader = exiftool.read, write: xmp.Writer = exiftool.write_xmp, by: int | None = None) -> Changed:
    if action not in ACTIONS:
        raise WrongState("the only action is keep_both; skip leaves the pair for later and saves nothing")
    hit = found(opened, review_id)
    if hit.review.status != "pending":
        raise WrongState("this pair is already decided; undo that decision first")
    if hit.decision.reversed_at is not None:
        raise WrongState("this pair's dedupe decision was reversed")
    changed = Changed(hit.export)
    people, adopted, found_at = keep_both(opened, photos, hit, now, effects, read, write)
    changed.people |= people
    hit.review.status, hit.review.resolved_at = ACTIONS[action], now
    opened.flush()
    events.record(opened, now, "review_resolved", f"review {hit.review.id}",
                  by_prefix(by) + action + (f"{ADOPTED}{', '.join(adopted)}" if adopted else "") + found_at)
    return changed


def still_referenced(opened: Session, item: MediaItem, decision: DedupeDecision) -> tuple[bool, bool]:
    matched = opened.execute(
        select(DedupeDecision.id).where(DedupeDecision.matched_item_id == item.id, DedupeDecision.reversed_at.is_(None)).limit(1)
    ).first() is not None
    shared = opened.execute(
        select(DedupeDecision.id).where(DedupeDecision.takeout_item_id == item.id, DedupeDecision.id != decision.id).limit(1)
    ).first() is not None
    return matched, shared


def found_place(opened: Session, photos: Photos, item: MediaItem, hold: Path, found: tuple[int, str]) -> tuple[int, Path] | None:
    person = opened.get(Person, found[0])
    if person is None:
        return None
    folder = place.google_only_folder(photos, person.slug, item)
    if not folder.is_relative_to(photos.root / person.slug / "google-only"):
        return None
    for name in place.names(hold, item.sha1):
        if place_digest(place.relative(photos, folder / name)) == found[1]:
            return person.id, folder / name
    return None


def move_back(opened: Session, photos: Photos, review: ReviewItem, item: MediaItem, hold: Path, with_sidecar: bool,
              effects: FileEffects) -> tuple[int, str] | None:
    found = found_by(opened, review, item)
    back = None if found is None else found_place(opened, photos, item, hold, found)
    source = photos.google_only_file(item.path)
    if back is None or back[1] == source or any(path.exists() or path.is_symlink() for path in (back[1], paths.sidecar_of(back[1]))):
        return None
    token = paths.placing_token()
    try:
        placed = place.copy_exactly(back[1], source, item.sha1, token)
    except paths.AlreadyExists:
        return None
    effects.placing(placed.path, token)
    effects.made(photos, placed, source, False)
    if with_sidecar:
        paths.mark_placing(placed.path, token, paths.Placing(placed.path.name, item.sha1, True, True, paths.regular_sha1(paths.sidecar_of(source))))
        paths.carry_sidecar(source, placed.path)
        paths.mark_placing(placed.path, token, paths.Placing(placed.path.name, item.sha1, True, True,
                                                             paths.regular_sha1(paths.sidecar_of(placed.path))))
    effects.set_aside(paths.set_library_copy_aside(photos, item.path, placed.path))
    return back[0], token


def take_back(opened: Session, photos: Photos, hit: Found, hold: Path, effects: FileEffects) -> tuple[set[int], str | None]:
    item = None if hit.decision.takeout_item_id is None else opened.get(MediaItem, hit.decision.takeout_item_id)
    if item is None:
        return set(), None
    matched, shared = still_referenced(opened, item, hit.decision)
    if matched:
        raise WrongState("a later import matched the kept copy; reverse that decision before undoing this one")
    if shared:
        hit.decision.takeout_item_id = None
        return set(), None
    adopted = adopted_by(opened, hit.review, item)
    moved = None
    try:
        if "photo" not in adopted:
            effects.set_aside(paths.set_library_copy_aside(photos, item.path, hold, with_sidecar="sidecar" not in adopted))
        elif "row" not in adopted:
            moved = move_back(opened, photos, hit.review, item, hold, "sidecar" in adopted, effects)
        if moved is None and "photo" in adopted and "sidecar" not in adopted:
            effects.set_aside(paths.set_sidecar_aside(photos, item.path, hit.review.id, cleanup.undone_count(opened, hit.review.id)))
    except paths.NotALibraryCopy as error:
        raise WrongState("the kept copy is not where it was placed, so it was left alone") from error
    except (OSError, place.PlaceFailed) as error:
        raise WrongState("the kept copy could not be removed, so nothing was changed") from error
    hit.decision.takeout_item_id = None
    owner = item.person_id
    if "row" not in adopted:
        opened.delete(item)
    if moved is not None:
        return {owner, moved[0]}, moved[1]
    return set() if {"row", "photo", "sidecar"} <= adopted else {owner}, None


def undo(opened: Session, photos: Photos, review_id: int, now: float, effects: FileEffects, by: int | None = None) -> Changed:
    hit = found(opened, review_id)
    if hit.review.status not in RESOLVED or hit.review.resolved_at is None:
        raise WrongState("only a kept pair can be undone")
    if hit.decision.reversed_at is not None:
        raise WrongState("this pair's dedupe decision was reversed, so the pair can no longer go back to the queue")
    if now - hit.review.resolved_at >= UNDO_WINDOW:
        raise TooLate("this decision is more than 14 days old and its held copy is cleaned up, so it can no longer be undone")
    hold = cleanup.hold_file(photos, hit.export, hit.review)
    if hold is None:
        raise WrongState("the held copy from the export is gone, so this decision can no longer be undone")
    people, token = take_back(opened, photos, hit, hold, effects)
    changed = Changed(hit.export, people)
    hit.review.status, hit.review.resolved_at = "pending", None
    opened.flush()
    said = ([] if token is None else [f"{MOVED_BACK}{cleanup.REASSIGN_COPY}{token}"]) + ([] if by is None else [by_prefix(by).rstrip(": ")])
    events.record(opened, now, "review_undone", f"review {hit.review.id}", ", ".join(said) or None)
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
        raise WrongState("this pair is waiting in the review queue; decide it there instead")
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


def reassign(opened: Session, photos: Photos, item_id: int, person_id: int, now: float, effects: FileEffects,
             read: xmp.Reader = exiftool.read, write: xmp.Writer = exiftool.write_xmp, by: int | None = None) -> tuple[MediaItem, Changed]:
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
    lineage, token = adopted_lineage(opened, item), paths.placing_token()
    try:
        placed = place.copy_into(place.google_only_folder(photos, target.slug, item), source, item.sha1,
                                 lambda destination, made: paths.mark_placing(destination, token, paths.Placing(destination.name, item.sha1, made, False)))
        effects.placing(placed.path, token)
        had_sidecar = paths.present(paths.sidecar_of(placed.path))
        effects.made(photos, placed, source, had_sidecar)
        paths.mark_placing(placed.path, token, paths.Placing(placed.path.name, item.sha1, placed.copied, not had_sidecar))
        old_path, new_path = item.path, place.relative(photos, placed.path)
        if opened.execute(select(MediaItem.id).where(MediaItem.person_id == target.id, MediaItem.path == new_path, MediaItem.id != item.id).limit(1)).first():
            raise WrongState(f"another photo of {target.name}'s is recorded at that place, so the photo was left where it was")
        def recording(sidecar_sha1: str | None) -> None:
            paths.mark_placing(placed.path, token, paths.Placing(placed.path.name, item.sha1, placed.copied, True, sidecar_sha1))

        if not had_sidecar:
            recording(paths.regular_sha1(paths.sidecar_of(source)))
        carried = paths.carry_sidecar(source, placed.path)
        if not carried:
            if had_sidecar:
                raise paths.AlreadyExists("a different sidecar is already at the target")
            xmp.fill(placed.path, xmp.GoogleMetadata.of(item.sidecar_json), True, read,
                     lambda sidecar, tags: paths.write_new_sidecar(sidecar, lambda temp: write(temp, tags), recording))
        if not had_sidecar:
            recording(paths.regular_sha1(paths.sidecar_of(placed.path)))
        carries = CARRIES_FOUND if "found" in lineage and placed.copied and not had_sidecar else ""
        adopted = [name for name, kept in (("photo", not placed.copied or "photo" in lineage),
                                           ("sidecar", had_sidecar or (carried and "sidecar" in lineage))) if kept]
        item.person_id, item.path = target.id, new_path
        opened.flush()
        events.record(opened, now, "media_reassigned", f"item {item.id}", f"person {current.id} to person {target.id}{cleanup.REASSIGN_COPY}{token}{carries}"
                      + (f"{ADOPTED}{', '.join(adopted)}" if adopted else "") + ("" if by is None else f", by person {by}"))
        opened.flush()
        effects.set_aside(paths.set_library_copy_aside(photos, old_path, placed.path))
    except (place.PlaceFailed, *MOVE_FAILURES) as error:
        raise WrongState("the photo could not be moved; it was left where it was") from error
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
