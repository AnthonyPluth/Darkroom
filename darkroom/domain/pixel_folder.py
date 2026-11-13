from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, case, func, literal, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from darkroom import dates
from darkroom.domain import accounts, events
from darkroom.storage import db, paths
from darkroom.storage.models import DedupeDecision, Export, MediaItem, Person, Pixel, PixelCopy, PixelPerson, UNKNOWN_SHA1, known_contents
from darkroom.storage.paths import Photos

GIGABYTE = 1024**3
DEFAULT_CAP_BYTES = 50 * GIGABYTE


class NotInOriginals(ValueError):
    pass


@dataclass
class Tended:
    placed: int = 0
    pruned: int = 0
    skipped: int = 0
    deferred: int = 0
    failed: int = 0
    refused: int = 0

    def add(self, other: Tended) -> None:
        for name in ("placed", "pruned", "skipped", "deferred", "failed", "refused"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


NEVER_AFTER_PLACING = 0.0


def export_plausibly_created() -> ColumnElement[bool]:
    return and_(Export.created_at.between(1, dates.LATEST_INSTANT), Export.created_at <= DedupeDecision.decided_at)


def observed_at() -> ColumnElement[float]:
    return case(
        (Export.created_at.is_(None), DedupeDecision.decided_at),
        (export_plausibly_created(), Export.created_at),
        else_=literal(NEVER_AFTER_PLACING),
    )


def confirming_matches(opened: Session, account_id: int, item_id: int, person_id: int) -> list[float]:
    return list(opened.execute(
        select(observed_at()).select_from(DedupeDecision).join(MediaItem, MediaItem.id == DedupeDecision.matched_item_id)
        .join(Export, Export.id == DedupeDecision.export_id).where(
            DedupeDecision.matched_item_id == item_id, DedupeDecision.account_id == account_id, DedupeDecision.person_id == person_id,
            DedupeDecision.reversed_at.is_(None), DedupeDecision.decided_by == "auto", DedupeDecision.tier.in_(accounts.CONFIRMING_TIERS),
            MediaItem.person_id == person_id, known_contents(),
        )
    ).scalars())


def confirmed_for(opened: Session, account_id: int, item_id: int, person_id: int, since: float | None = None) -> bool:
    return any(since is None or observed_at >= since for observed_at in confirming_matches(opened, account_id, item_id, person_id))


def holds_current_contents(opened: Session, row: PixelCopy) -> bool:
    item = opened.get(MediaItem, row.item_id, populate_existing=True)
    return item is not None and item.sha1 != UNKNOWN_SHA1 and item.sha1 == row.sha1


def confirms_copy(opened: Session, pixel: Pixel, row: PixelCopy) -> bool:
    if row.state == "placed" and not holds_current_contents(opened, row):
        return False
    if not confirmed_for(opened, pixel.google_account_id, row.item_id, row.person_id):
        return False
    if not row.always_send or row.placed_at is None:
        return True
    seen_after_placing = row.seen_again_at is not None and row.seen_again_at >= row.placed_at
    return seen_after_placing or confirmed_for(opened, pixel.google_account_id, row.item_id, row.person_id, since=row.placed_at)


def seen_again(opened: Session, account_id: int, item_id: int, person_id: int, at: float) -> None:
    rows = opened.execute(
        select(PixelCopy).join(Pixel, Pixel.id == PixelCopy.pixel_id).where(
            Pixel.google_account_id == account_id, PixelCopy.item_id == item_id, PixelCopy.person_id == person_id,
            PixelCopy.state == "placed", PixelCopy.always_send.is_(True),
        )
    ).scalars()
    for row in rows:
        row.seen_again_at = at if row.seen_again_at is None else max(row.seen_again_at, at)
    opened.flush()


def pixels_serving(opened: Session, person_id: int) -> list[Pixel]:
    return list(opened.execute(
        select(Pixel).join(PixelPerson, PixelPerson.pixel_id == Pixel.id).where(PixelPerson.person_id == person_id).order_by(Pixel.id)
    ).scalars())


def serves(opened: Session, pixel_id: int, person_id: int) -> bool:
    return opened.get(PixelPerson, (pixel_id, person_id)) is not None


def in_originals(opened: Session, item: MediaItem) -> Person:
    person = opened.get(Person, item.person_id)
    if person is None or item.source != "phone" or item.sha1 == UNKNOWN_SHA1 or not item.path.startswith(f"{person.slug}/originals/"):
        raise NotInOriginals("only a photo in the person's originals folder goes to a Pixel")
    return person


def queue(opened: Session, item: MediaItem, now: float, always_send: bool = False) -> int:
    db.write_lock(opened)
    in_originals(opened, item)
    queued = 0
    for pixel in pixels_serving(opened, item.person_id):
        existing = opened.execute(select(PixelCopy).where(PixelCopy.pixel_id == pixel.id, PixelCopy.item_id == item.id)).scalar_one_or_none()
        if existing is not None:
            if always_send and not existing.always_send and existing.state == "pruned":
                existing.state, existing.queued_at, existing.placed_at, existing.pruned_at = "queued", now, None, None
                queued += 1
            elif existing.state == "pruned" and not confirms_copy(opened, pixel, existing):
                revive(opened, existing, now)
                queued += 1
            existing.always_send = existing.always_send or always_send
            continue
        withheld = not always_send and confirmed_for(opened, pixel.google_account_id, item.id, item.person_id)
        opened.add(PixelCopy(pixel_id=pixel.id, item_id=item.id, person_id=item.person_id, state="pruned" if withheld else "queued",
                             bytes=item.bytes, sha1=item.sha1, queued_at=now, pruned_at=now if withheld else None, always_send=always_send))
        queued += 0 if withheld else 1
    opened.flush()
    return queued


def used_bytes(opened: Session, pixel_id: int) -> int:
    return int(opened.execute(select(func.coalesce(func.sum(PixelCopy.bytes), 0)).where(PixelCopy.pixel_id == pixel_id, PixelCopy.state == "placed")).scalar_one())


def fill(opened: Session, photos: Photos, pixel: Pixel, cap_bytes: int, now: float) -> Tended:
    tended = Tended()
    pixel_id = pixel.id
    used = used_bytes(opened, pixel_id)
    waiting = opened.execute(
        select(PixelCopy.id).where(PixelCopy.pixel_id == pixel_id, PixelCopy.state == "queued").order_by(PixelCopy.queued_at, PixelCopy.id)
    ).scalars().all()
    for position, copy_id in enumerate(waiting):
        lock_copy(opened, copy_id)
        row = opened.get(PixelCopy, copy_id, populate_existing=True)
        current = opened.get(Pixel, pixel_id, populate_existing=True)
        if row is None or current is None or row.state != "queued":
            opened.commit()
            continue
        if not row.always_send and confirms_copy(opened, current, row):
            row.state, row.pruned_at = "pruned", now
            tended.skipped += 1
            opened.commit()
            continue
        if used > 0 and used + row.bytes > cap_bytes:
            opened.commit()
            tended.deferred = len(waiting) - position
            break
        item, person = opened.get(MediaItem, row.item_id, populate_existing=True), opened.get(Person, row.person_id)
        if item is None or person is None or item.person_id != row.person_id or item.sha1 != row.sha1 or not serves(opened, pixel_id, row.person_id):
            tended.failed += 1
            opened.commit()
            continue
        try:
            row.name = paths.place_pixel_copy(photos, pixel_id, person.slug, item.path, row.sha1, now)
        except (paths.OutsideRoot, paths.NotAPixelCopy, OSError):
            tended.failed += 1
            opened.commit()
            continue
        row.state, row.placed_at, row.pruned_at = "placed", now, None
        used += row.bytes
        tended.placed += 1
        opened.commit()
    return tended


def prune_copy(photos: Photos, row: PixelCopy, slug: str) -> bool:
    if row.name is None:
        raise paths.NotAPixelCopy("the copy was never placed")
    return paths.remove_pixel_copy(photos, row.pixel_id, slug, row.name, row.sha1)


def lock_copy(opened: Session, copy_id: int) -> None:
    opened.commit()
    db.write_lock(opened)
    opened.execute(sql_update(PixelCopy).where(PixelCopy.id == copy_id).values(id=PixelCopy.id))


def prune(opened: Session, photos: Photos, pixel: Pixel, now: float) -> Tended:
    tended = Tended()
    pixel_id = pixel.id
    placed = opened.execute(select(PixelCopy.id).where(PixelCopy.pixel_id == pixel_id, PixelCopy.state == "placed").order_by(PixelCopy.id)).scalars().all()
    for copy_id in placed:
        lock_copy(opened, copy_id)
        row = opened.get(PixelCopy, copy_id, populate_existing=True)
        current = opened.get(Pixel, pixel_id, populate_existing=True)
        person = None if row is None else opened.get(Person, row.person_id)
        if row is None or current is None or person is None or row.state != "placed" or not confirms_copy(opened, current, row):
            opened.commit()
            continue
        try:
            prune_copy(photos, row, person.slug)
        except (paths.OutsideRoot, paths.NotAPixelCopy, OSError):
            tended.refused += 1
            events.record(opened, now, "pixel_prune_refused", f"pixel {pixel_id}", f"copy {row.id}")
            opened.commit()
            continue
        row.state, row.pruned_at = "pruned", now
        tended.pruned += 1
        opened.commit()
    return tended


def tend(opened: Session, photos: Photos, cap_bytes: int, now: float) -> Tended:
    tended = Tended()
    for pixel in opened.execute(select(Pixel).order_by(Pixel.id)).scalars().all():
        tended.add(prune(opened, photos, pixel, now))
        tended.add(fill(opened, photos, pixel, cap_bytes, now))
    return tended


def revive(opened: Session, row: PixelCopy, now: float) -> None:
    row.state, row.queued_at, row.placed_at, row.pruned_at = "queued", now, None, None
    row.requeued += 1
    events.record(opened, now, "pixel_copy_requeued", f"pixel {row.pixel_id}", f"copy {row.id}")


def requeue(opened: Session, item_id: int, now: float) -> int:
    db.write_lock(opened)
    rows = opened.execute(select(PixelCopy).where(PixelCopy.item_id == item_id, PixelCopy.state == "pruned")).scalars().all()
    requeued = 0
    for row in rows:
        pixel = opened.get(Pixel, row.pixel_id)
        if pixel is None or not serves(opened, pixel.id, row.person_id) or confirms_copy(opened, pixel, row):
            continue
        revive(opened, row, now)
        requeued += 1
    opened.flush()
    return requeued


def requeue_new_contents(opened: Session, item: MediaItem, now: float) -> int:
    db.write_lock(opened)
    if item.sha1 == UNKNOWN_SHA1:
        return 0
    requeued = 0
    rows = opened.execute(select(PixelCopy).where(PixelCopy.item_id == item.id, PixelCopy.state.in_(("queued", "pruned")))).scalars().all()
    for row in rows:
        if row.state == "pruned":
            pixel = opened.get(Pixel, row.pixel_id)
            if pixel is None or not serves(opened, pixel.id, row.person_id) or confirms_copy(opened, pixel, row):
                continue
            revive(opened, row, now)
            requeued += 1
        row.sha1, row.bytes = item.sha1, item.bytes
    opened.flush()
    return requeued


def requeue_unconfirmed(opened: Session, pixel: Pixel, people: set[int] | None, now: float) -> int:
    db.write_lock(opened)
    query = select(PixelCopy).where(PixelCopy.pixel_id == pixel.id, PixelCopy.state == "pruned").order_by(PixelCopy.id)
    if people is not None:
        query = query.where(PixelCopy.person_id.in_(people))
    requeued = 0
    for row in opened.execute(query).scalars().all():
        if not serves(opened, pixel.id, row.person_id) or confirms_copy(opened, pixel, row):
            continue
        revive(opened, row, now)
        requeued += 1
    opened.flush()
    return requeued
