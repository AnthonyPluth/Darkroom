from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, and_, case, func, literal, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from darkroom import dates
from darkroom import config, monitoring
from darkroom.domain import accounts, alerts, events, pixel_source
from darkroom.providers import immich
from darkroom.storage import db, paths
from darkroom.storage.models import (Alert, DedupeDecision, DuplicateRemoval, Event, Export, ManagedAssetVersion, MediaItem, Person, Pixel, PixelCopy, PixelPerson, UNKNOWN_SHA1,
                                     known_contents)
from darkroom.storage.paths import Photos

GIGABYTE = 1024**3
DEFAULT_CAP_BYTES = 50 * GIGABYTE
Readers = Callable[[Person], immich.ManagedReader]
Placed = Literal["placed", "held", "moved", "failed"]
HoldReason = Literal["source_off", "source_unrecorded", "source_unavailable", "source_not_custody", "source_changed"]
HOLD_REASONS: dict[str, str] = {
    "source_off": "Pixel copies from Immich are turned off",
    "source_unrecorded": "no verified Immich original is recorded for the copy",
    "source_unavailable": "the person's Immich original could not be read",
    "source_not_custody": "the Immich original is missing, trashed, offline or not the person's own",
    "source_changed": "the Immich original holds other bytes than the copy",
}
HELD_ALERT_SECONDS = 24 * 3600.0


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
    blocked: int = 0

    def add(self, other: Tended) -> None:
        for name in ("placed", "pruned", "skipped", "deferred", "failed", "refused", "blocked"):
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


def withheld(opened: Session, pixel: Pixel, row: PixelCopy) -> bool:
    return confirms_copy(opened, pixel, row) or pixel_source.sent_home(opened, row, pixel.google_account_id)


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


def copy_of(opened: Session, pixel: Pixel, item: MediaItem) -> PixelCopy | None:
    return opened.execute(select(PixelCopy).where(PixelCopy.pixel_id == pixel.id, PixelCopy.item_id == item.id)).scalar_one_or_none()


def enqueue(opened: Session, pixel: Pixel, item: MediaItem, now: float, always_send: bool) -> tuple[PixelCopy, int]:
    existing = copy_of(opened, pixel, item)
    if existing is None:
        held_back = not always_send and confirmed_for(opened, pixel.google_account_id, item.id, item.person_id)
        row = PixelCopy(pixel_id=pixel.id, item_id=item.id, person_id=item.person_id, state="pruned" if held_back else "queued",
                        bytes=item.bytes, sha1=item.sha1, queued_at=now, pruned_at=now if held_back else None, always_send=always_send)
        opened.add(row)
        opened.flush()
        return row, 0 if held_back else 1
    queued = 0
    if always_send and not existing.always_send and existing.state == "pruned":
        existing.state, existing.queued_at, existing.placed_at, existing.pruned_at = "queued", now, None, None
        queued = 1
    elif existing.state == "pruned" and not withheld(opened, pixel, existing):
        revive(opened, existing, now)
        queued = 1
    existing.always_send = existing.always_send or always_send
    return existing, queued


def queue(opened: Session, item: MediaItem, now: float, always_send: bool = False) -> int:
    db.write_lock(opened)
    in_originals(opened, item)
    queued = sum(enqueue(opened, pixel, item, now, always_send)[1] for pixel in pixels_serving(opened, item.person_id))
    opened.flush()
    return queued


def queue_managed(opened: Session, item: MediaItem, version_id: int, now: float, existing_only: bool = False) -> int:
    db.write_lock(opened)
    version = opened.get(ManagedAssetVersion, version_id, populate_existing=True)
    if version is None or item.sha1 == UNKNOWN_SHA1 or not pixel_source.usable(opened, version, item.person_id, item.sha1, item.bytes):
        raise pixel_source.NotManaged("only a verified Immich-managed original of the person goes to a Pixel")
    if opened.execute(select(DuplicateRemoval.id).where(DuplicateRemoval.removed_item_id == item.id, DuplicateRemoval.state == "pending").limit(1)).first():
        raise pixel_source.NotManaged("a photo whose duplicate removal is pending does not go to a Pixel")
    always_send = pixel_source.phone_origin(opened, version.id)
    queued = 0
    for pixel in pixels_serving(opened, item.person_id):
        existing = copy_of(opened, pixel, item)
        changed = existing is not None and (existing.sha1, existing.bytes) != (item.sha1, item.bytes)
        if changed or (existing_only and existing is None) or pixel_source.returns_home(opened, version.id, pixel.google_account_id):
            continue
        row, added = enqueue(opened, pixel, item, now, always_send)
        pixel_source.bind(opened, row, pixel, version, now)
        queued += added
    opened.flush()
    return queued


def replacing() -> ColumnElement[bool]:
    return and_(PixelCopy.state == "queued", PixelCopy.placed_at.is_not(None))


def used_bytes(opened: Session, pixel_id: int) -> int:
    counted = or_(PixelCopy.state == "placed", replacing())
    return int(opened.execute(select(func.coalesce(func.sum(PixelCopy.bytes), 0)).where(PixelCopy.pixel_id == pixel_id, counted)).scalar_one())


def ever_sent() -> ColumnElement[bool]:
    return or_(PixelCopy.state != "queued", PixelCopy.placed_at.is_not(None))


def still_pending(opened: Session, row: PixelCopy, now: float) -> None:
    if row.placed_at is None:
        return
    subject, detail = f"pixel {row.pixel_id}", f"copy {row.id}"
    last = opened.execute(
        select(Event.type).where(Event.subject == subject, Event.detail == detail, Event.type.in_(("pixel_copy_missing", "pixel_copy_replacement_pending")))
        .order_by(Event.id.desc()).limit(1)
    ).scalar_one_or_none()
    if last != "pixel_copy_replacement_pending":
        events.record(opened, now, "pixel_copy_replacement_pending", subject, detail)


def about_copy(row: PixelCopy) -> ColumnElement[bool]:
    return and_(Event.subject == f"pixel {row.pixel_id}", or_(Event.detail == f"copy {row.id}", Event.detail.startswith(f"copy {row.id}: ")))


def held(opened: Session, row: PixelCopy, now: float, reason: HoldReason) -> None:
    detail = f"copy {row.id}: {reason}"
    last = opened.execute(select(Event.type, Event.detail).where(about_copy(row)).order_by(Event.id.desc()).limit(1)).first()
    if last is None or tuple(last) != ("pixel_copy_source_held", detail):
        events.record(opened, now, "pixel_copy_source_held", f"pixel {row.pixel_id}", detail)
        monitoring.log("pixel copy held for its Immich source", copy=row.id, reason=reason)


def hold(opened: Session, copy_id: int, now: float, reason: HoldReason) -> Placed:
    lock_copy(opened, copy_id)
    row = opened.get(PixelCopy, copy_id, populate_existing=True)
    if row is not None:
        held(opened, row, now, reason)
    opened.commit()
    return "held"


def fail(opened: Session, copy_id: int, now: float) -> Placed:
    lock_copy(opened, copy_id)
    row = opened.get(PixelCopy, copy_id, populate_existing=True)
    if row is not None and row.placed_at is not None:
        still_pending(opened, row, now)
    elif row is not None:
        last = opened.execute(select(Event.type).where(about_copy(row)).order_by(Event.id.desc()).limit(1)).scalar_one_or_none()
        if last != "pixel_copy_place_failed":
            events.record(opened, now, "pixel_copy_place_failed", f"pixel {row.pixel_id}", f"copy {row.id}")
    opened.commit()
    return "failed"


def deferred(opened: Session, row: PixelCopy | None, now: float) -> None:
    if row is not None and held_since(opened, row) is not None:
        events.record(opened, now, "pixel_copy_deferred", f"pixel {row.pixel_id}", f"copy {row.id}")


def held_since(opened: Session, row: PixelCopy) -> tuple[float, str] | None:
    last = opened.execute(select(Event.type, Event.time, Event.detail).where(about_copy(row)).order_by(Event.id.desc()).limit(1)).first()
    if last is None or last.type != "pixel_copy_source_held" or (row.placed_at is not None and row.placed_at >= last.time):
        return None
    return last.time, str(last.detail).rpartition(": ")[2]


def alert_held(opened: Session, settings: config.Settings, now: float, notifier: alerts.Notifier | None = None) -> int:
    waiting: dict[int, list[tuple[float, str]]] = {}
    for row in opened.execute(select(PixelCopy).where(PixelCopy.state.in_(("queued", "placed"))).order_by(PixelCopy.id)).scalars().all():
        if pixel_source.converted(opened, row.id) and (found := held_since(opened, row)) is not None and now - found[0] >= HELD_ALERT_SECONDS:
            waiting.setdefault(row.person_id, []).append(found)
    opened.commit()
    opened_alerts = 0
    for person_id, held_copies in sorted(waiting.items()):
        key = f"pixel:source-held:{person_id}:{int(min(since for since, _ in held_copies))}"
        db.write_lock(opened)
        if opened.execute(select(Alert.id).where(Alert.key == key).limit(1)).first() is None:
            reasons = "; ".join(HOLD_REASONS.get(reason, HOLD_REASONS["source_unrecorded"]) for reason in sorted({reason for _, reason in held_copies}))
            counted = "1 Pixel copy has" if len(held_copies) == 1 else f"{len(held_copies)} Pixel copies have"
            message = (f"{counted} waited more than a day for the person's Immich original "
                       f"({reasons}). Darkroom keeps them as they are and tries again.")
            alerts.open_alert(opened, settings, key, "failure", f"person {person_id}", alerts.Wording("Darkroom needs a look", message), None, now,
                              notifier=notifier, about=alerts.person(person_id))
            opened_alerts += 1
        opened.commit()
    return opened_alerts


def placing(row: PixelCopy) -> tuple[object, ...]:
    return row.state, row.sha1, row.bytes, row.requeued, row.placed_at, row.name, row.person_id


def place_managed(opened: Session, photos: Photos, readers: Readers, row: PixelCopy, pixel: Pixel, person: Person, item_path: str, now: float) -> Placed:
    source = pixel_source.carried(opened, row, pixel, now)
    if source is None:
        held(opened, row, now, "source_unrecorded")
        opened.commit()
        return "held"
    copy_id, pixel_id, slug, expected = row.id, pixel.id, person.slug, placing(row)
    name = row.name or pixel_source.copy_name(photos, slug, item_path, source.asset_id)
    asset_id, sha1, size = source.asset_id, source.sha1, source.bytes
    opened.commit()
    try:
        chunks = readers(person).original_chunks(asset_id, sha1)
        with paths.staged_pixel_copy(photos, chunks, sha1, size, now) as staged:
            lock_copy(opened, copy_id)
            current = opened.get(Pixel, pixel_id, populate_existing=True)
            found = opened.get(PixelCopy, copy_id, populate_existing=True)
            serving = found is not None and opened.get(PixelPerson, (pixel_id, found.person_id), populate_existing=True) is not None
            if found is None or current is None or not serving or placing(found) != expected or pixel_source.current(opened, found, current) is None:
                opened.commit()
                return "moved"
            try:
                found.name = paths.publish_pixel_copy(photos, pixel_id, slug, name, staged, sha1, size)
            except (paths.OutsideRoot, paths.NotAPixelCopy, OSError):
                return fail(opened, copy_id, now)
    except (immich.NotCustody, immich.NotVisible):
        return hold(opened, copy_id, now, "source_not_custody")
    except (immich.ChecksumMismatch, paths.NotAPixelCopy):
        return hold(opened, copy_id, now, "source_changed")
    except immich.ImmichFailed:
        return hold(opened, copy_id, now, "source_unavailable")
    except (paths.OutsideRoot, OSError):
        return fail(opened, copy_id, now)
    again = found.placed_at is not None
    if again:
        events.record(opened, now, "pixel_copy_replaced", f"pixel {pixel_id}", f"copy {copy_id}")
    found.state, found.placed_at, found.pruned_at = "placed", found.placed_at if again else now, None
    opened.commit()
    return "placed"


def fill(opened: Session, photos: Photos, pixel: Pixel, cap_bytes: int, now: float, readers: Readers | None = None) -> Tended:
    tended = Tended()
    pixel_id = pixel.id
    used = used_bytes(opened, pixel_id)
    waiting = opened.execute(
        select(PixelCopy.id, PixelCopy.placed_at.is_not(None)).where(PixelCopy.pixel_id == pixel_id, PixelCopy.state == "queued")
        .order_by(PixelCopy.queued_at, PixelCopy.id)
    ).all()
    deferring = False
    for copy_id, was_placed in waiting:
        if deferring and not was_placed:
            tended.deferred += 1
            waiting_row = opened.get(PixelCopy, copy_id, populate_existing=True)
            if waiting_row is not None and pixel_source.converted(opened, copy_id) and held_since(opened, waiting_row) is not None:
                lock_copy(opened, copy_id)
                deferred(opened, opened.get(PixelCopy, copy_id, populate_existing=True), now)
                opened.commit()
            continue
        lock_copy(opened, copy_id)
        row = opened.get(PixelCopy, copy_id, populate_existing=True)
        current = opened.get(Pixel, pixel_id, populate_existing=True)
        if row is None or current is None or row.state != "queued":
            opened.commit()
            continue
        managed = pixel_source.converted(opened, row.id)
        if managed and readers is None:
            tended.blocked += 1
            held(opened, row, now, "source_off")
            opened.commit()
            continue
        again = row.placed_at is not None
        if not again and not row.always_send and withheld(opened, current, row):
            row.state, row.pruned_at = "pruned", now
            tended.skipped += 1
            opened.commit()
            continue
        if not again and (deferring or (used > 0 and used + row.bytes > cap_bytes)):
            deferring = True
            tended.deferred += 1
            if managed:
                deferred(opened, row, now)
            opened.commit()
            continue
        item, person = opened.get(MediaItem, row.item_id, populate_existing=True), opened.get(Person, row.person_id)
        if item is None or person is None or item.person_id != row.person_id or item.sha1 != row.sha1 or not serves(opened, pixel_id, row.person_id):
            tended.failed += 1
            still_pending(opened, row, now)
            opened.commit()
            continue
        if readers is not None and managed:
            outcome = place_managed(opened, photos, readers, row, current, person, item.path, now)
            tended.placed += outcome == "placed"
            tended.blocked += outcome == "held"
            tended.failed += outcome == "failed"
            used += row.bytes if outcome == "placed" and not again else 0
            continue
        try:
            row.name = paths.place_pixel_copy(photos, pixel_id, person.slug, item.path, row.sha1, now)
            if again and not paths.pixel_copy_intact(photos, pixel_id, person.slug, row.name, row.sha1, row.bytes):
                raise paths.NotAPixelCopy("the copy read back is not the one Darkroom recorded")
        except (paths.OutsideRoot, paths.NotAPixelCopy, OSError):
            tended.failed += 1
            still_pending(opened, row, now)
            opened.commit()
            continue
        if again:
            events.record(opened, now, "pixel_copy_replaced", f"pixel {pixel_id}", f"copy {row.id}")
        row.state, row.placed_at, row.pruned_at = "placed", row.placed_at if again else now, None
        used += 0 if again else row.bytes
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


def prune(opened: Session, photos: Photos, pixel: Pixel, now: float, readers: Readers | None = None) -> Tended:
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
        if pixel_source.converted(opened, row.id) and (readers is None or pixel_source.current(opened, row, current) is None):
            tended.blocked += 1
            held(opened, row, now, "source_off" if readers is None else "source_unrecorded")
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


def tend(opened: Session, photos: Photos, cap_bytes: int, now: float, readers: Readers | None = None) -> Tended:
    tended = Tended()
    for pixel in opened.execute(select(Pixel).order_by(Pixel.id)).scalars().all():
        tended.add(prune(opened, photos, pixel, now, readers))
        tended.add(fill(opened, photos, pixel, cap_bytes, now, readers))
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
        if pixel is None or not serves(opened, pixel.id, row.person_id) or withheld(opened, pixel, row):
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
    rows = opened.execute(select(PixelCopy).where(PixelCopy.item_id == item.id, PixelCopy.state.in_(("queued", "pruned")), ~replacing())).scalars().all()
    for row in rows:
        if row.state == "pruned":
            pixel = opened.get(Pixel, row.pixel_id)
            if pixel is None or not serves(opened, pixel.id, row.person_id) or withheld(opened, pixel, row):
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
        if not serves(opened, pixel.id, row.person_id) or withheld(opened, pixel, row):
            continue
        revive(opened, row, now)
        requeued += 1
    opened.flush()
    return requeued
