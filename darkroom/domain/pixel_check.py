from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events, pixel_folder
from darkroom.storage import db, paths
from darkroom.storage.models import Event, MediaItem, Person, Pixel, PixelCopy
from darkroom.storage.paths import Photos

UNACCOUNTED = "pixel_files_unaccounted"
RECHECK_SECONDS = 24 * 3600.0


@dataclass
class Checked:
    checked: int = 0
    intact: int = 0
    requeued: int = 0
    adopted: int = 0
    unaccounted: dict[int, int] = field(default_factory=dict)


def intact(photos: Photos, pixel_id: int, slug: str | None, name: str | None, sha1: str, size: int, single_name: bool = False) -> bool:
    if slug is None or name is None:
        return False
    try:
        return paths.pixel_copy_intact(photos, pixel_id, slug, name, sha1, size, single_name=single_name)
    except (paths.OutsideRoot, OSError):
        return False


def mark_for_replacement(opened: Session, row: PixelCopy, now: float) -> None:
    row.state = "queued"
    events.record(opened, now, "pixel_copy_missing", f"pixel {row.pixel_id}", f"copy {row.id}")


def recorded(row: PixelCopy) -> tuple[str, str | None, str, int]:
    return row.state, row.name, row.sha1, row.bytes


def check(opened: Session, photos: Photos, now: float, only: frozenset[int] | None = None) -> Checked:
    checked = Checked()
    accounted: set[tuple[int, str, str]] = set()
    placed_rows = select(PixelCopy.id).where(PixelCopy.state == "placed").order_by(PixelCopy.pixel_id, PixelCopy.id)
    pixel_rows = select(Pixel.id).order_by(Pixel.id)
    if only is not None:
        placed_rows, pixel_rows = placed_rows.where(PixelCopy.pixel_id.in_(only)), pixel_rows.where(Pixel.id.in_(only))
    placed = opened.execute(placed_rows).scalars().all()
    for copy_id in placed:
        row = opened.get(PixelCopy, copy_id, populate_existing=True)
        if row is None or row.state != "placed":
            continue
        person = opened.get(Person, row.person_id)
        hashed, pixel_id, slug = recorded(row), row.pixel_id, None if person is None else person.slug
        opened.commit()
        verdict = intact(photos, pixel_id, slug, hashed[1], hashed[2], hashed[3])
        pixel_folder.lock_copy(opened, copy_id)
        row = opened.get(PixelCopy, copy_id, populate_existing=True)
        if row is None or recorded(row) != hashed:
            opened.commit()
            continue
        checked.checked += 1
        if verdict and slug is not None and hashed[1] is not None:
            checked.intact += 1
            accounted.add((pixel_id, slug, hashed[1]))
        else:
            mark_for_replacement(opened, row, now)
            checked.requeued += 1
        opened.commit()
    for pixel_id in opened.execute(pixel_rows).scalars().all():
        account_for_files(opened, photos, pixel_id, accounted, checked, now)
    opened.commit()
    return checked


def expected_name(photos: Photos, slug: str, relative: str) -> str | None:
    try:
        return paths.pixel_copy_name(photos, slug, relative)
    except (paths.OutsideRoot, ValueError, OSError):
        return None


def adoptable(opened: Session, photos: Photos, pixel_id: int) -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    rows = opened.execute(
        select(PixelCopy.id, Person.slug, MediaItem.path).join(Person, Person.id == PixelCopy.person_id).join(MediaItem, MediaItem.id == PixelCopy.item_id)
        .where(PixelCopy.pixel_id == pixel_id, PixelCopy.state == "queued", PixelCopy.placed_at.is_(None)).order_by(PixelCopy.id)
    ).all()
    for copy_id, slug, relative in rows:
        name = expected_name(photos, slug, relative)
        if name is not None:
            found.setdefault((slug, name), copy_id)
    return found


def still_adoptable(opened: Session, photos: Photos, row: PixelCopy, slug: str, name: str) -> bool:
    item, person = opened.get(MediaItem, row.item_id, populate_existing=True), opened.get(Person, row.person_id, populate_existing=True)
    if item is None or person is None or person.slug != slug or item.person_id != row.person_id or item.sha1 != row.sha1:
        return False
    return row.placed_at is None and pixel_folder.serves(opened, row.pixel_id, row.person_id) and expected_name(photos, slug, item.path) == name


def adopt(opened: Session, photos: Photos, copy_id: int, slug: str, name: str, now: float) -> bool:
    row = opened.get(PixelCopy, copy_id, populate_existing=True)
    if row is None or row.state != "queued":
        opened.commit()
        return False
    hashed, pixel_id = recorded(row), row.pixel_id
    opened.commit()
    if not intact(photos, pixel_id, slug, name, hashed[2], hashed[3], single_name=True):
        return False
    pixel_folder.lock_copy(opened, copy_id)
    row = opened.get(PixelCopy, copy_id, populate_existing=True)
    if row is None or recorded(row) != hashed or not still_adoptable(opened, photos, row, slug, name):
        opened.commit()
        return False
    row.state, row.name, row.placed_at, row.pruned_at = "placed", name, now, None
    events.record(opened, now, "pixel_copy_adopted", f"pixel {pixel_id}", f"copy {copy_id}")
    opened.commit()
    return True


def awaiting_replacement(opened: Session, pixel_id: int) -> set[tuple[str, str]]:
    rows = opened.execute(
        select(Person.slug, PixelCopy.name).join(Person, Person.id == PixelCopy.person_id)
        .where(PixelCopy.pixel_id == pixel_id, pixel_folder.replacing(), PixelCopy.name.is_not(None))
    ).all()
    return {(slug, name) for slug, name in rows if name is not None}


def account_for_files(opened: Session, photos: Photos, pixel_id: int, accounted: set[tuple[int, str, str]], checked: Checked, now: float) -> None:
    waiting, replacing = adoptable(opened, photos, pixel_id), awaiting_replacement(opened, pixel_id)
    opened.commit()
    unaccounted = 0
    for parts in paths.pixel_folder_entries(photos, pixel_id):
        if len(parts) > 1:
            slug, name = parts[0], "/".join(parts[1:])
            if (pixel_id, slug, name) in accounted or (slug, name) in replacing:
                continue
            copy_id = waiting.get((slug, name))
            if copy_id is not None and adopt(opened, photos, copy_id, slug, name, now):
                checked.adopted += 1
                continue
        unaccounted += 1
    if unaccounted:
        checked.unaccounted[pixel_id] = unaccounted
    report_unaccounted(opened, pixel_id, unaccounted, now)


def report_unaccounted(opened: Session, pixel_id: int, count: int, now: float) -> None:
    db.write_lock(opened)
    subject, detail = f"pixel {pixel_id}", f"files {count}"
    last = opened.execute(select(Event.detail).where(Event.type == UNACCOUNTED, Event.subject == subject).order_by(Event.id.desc()).limit(1)).scalar_one_or_none()
    if (last or "files 0") != detail:
        events.record(opened, now, UNACCOUNTED, subject, detail)
    opened.commit()


def replacing_pixels(opened: Session) -> list[int]:
    return list(opened.execute(select(PixelCopy.pixel_id).where(pixel_folder.replacing()).distinct().order_by(PixelCopy.pixel_id)).scalars())


def held(opened: Session, pixel_id: int, checked_since_start: bool) -> bool:
    if not checked_since_start:
        return True
    return opened.execute(select(PixelCopy.id).where(PixelCopy.pixel_id == pixel_id, pixel_folder.replacing()).limit(1)).first() is not None


def listing(photos: Photos, pixel_ids: Iterable[int]) -> dict[int, tuple[tuple[str, ...], ...]]:
    return {pixel_id: tuple(paths.pixel_folder_entries(photos, pixel_id)) for pixel_id in sorted(pixel_ids)}


@dataclass
class PlacedCopyCheck:
    ran: bool = False
    unaccounted: dict[int, int] = field(default_factory=dict)
    ran_at: float = 0.0
    listed: dict[int, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    def every_pixel_due(self, now: float) -> bool:
        return not self.ran or not self.ran_at <= now < self.ran_at + RECHECK_SECONDS

    def changed_unaccounted(self, photos: Photos) -> set[int]:
        found = listing(photos, self.unaccounted)
        return {pixel_id for pixel_id in self.unaccounted if found.get(pixel_id) != self.listed.get(pixel_id)}

    def scope(self, opened: Session, photos: Photos, now: float) -> frozenset[int] | None:
        if self.every_pixel_due(now):
            return None
        return frozenset(set(replacing_pixels(opened)) | self.changed_unaccounted(photos))

    def due(self, opened: Session, photos: Photos, now: float) -> bool:
        found = self.scope(opened, photos, now)
        return found is None or bool(found)

    def run(self, opened: Session, photos: Photos, now: float) -> Checked:
        only = self.scope(opened, photos, now) or None
        pixel_ids = opened.execute(select(Pixel.id)).scalars().all() if only is None else sorted(only)
        before = listing(photos, pixel_ids)
        opened.commit()
        checked = check(opened, photos, now, only)
        kept = {} if only is None else {pixel_id: count for pixel_id, count in self.unaccounted.items() if pixel_id not in only}
        self.unaccounted = kept | dict(checked.unaccounted)
        self.listed = {pixel_id: before.get(pixel_id, self.listed.get(pixel_id, ())) for pixel_id in sorted(self.unaccounted)}
        if only is None:
            self.ran, self.ran_at = True, now
        return checked

    def held(self, opened: Session, pixel_id: int) -> bool:
        return pixel_id in self.unaccounted or held(opened, pixel_id, self.ran)

    def held_pixels(self, opened: Session) -> list[int]:
        return sorted(set(replacing_pixels(opened)) | set(self.unaccounted))
