from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events, pixel_folder
from darkroom.storage import paths
from darkroom.storage.models import Person, PixelCopy
from darkroom.storage.paths import Photos


@dataclass
class Checked:
    checked: int = 0
    intact: int = 0
    requeued: int = 0


def intact(photos: Photos, row: PixelCopy, person: Person | None) -> bool:
    if person is None or row.name is None:
        return False
    try:
        return paths.pixel_copy_intact(photos, row.pixel_id, person.slug, row.name, row.sha1, row.bytes)
    except paths.OutsideRoot:
        return False


def mark_for_replacement(opened: Session, row: PixelCopy, now: float) -> None:
    row.state = "queued"
    events.record(opened, now, "pixel_copy_missing", f"pixel {row.pixel_id}", f"copy {row.id}")


def check(opened: Session, photos: Photos, now: float) -> Checked:
    checked = Checked()
    placed = opened.execute(select(PixelCopy.id).where(PixelCopy.state == "placed").order_by(PixelCopy.pixel_id, PixelCopy.id)).scalars().all()
    for copy_id in placed:
        pixel_folder.lock_copy(opened, copy_id)
        row = opened.get(PixelCopy, copy_id, populate_existing=True)
        if row is None or row.state != "placed":
            opened.commit()
            continue
        checked.checked += 1
        if intact(photos, row, opened.get(Person, row.person_id)):
            checked.intact += 1
        else:
            mark_for_replacement(opened, row, now)
            checked.requeued += 1
        opened.commit()
    return checked


def replacing_pixels(opened: Session) -> list[int]:
    return list(opened.execute(select(PixelCopy.pixel_id).where(pixel_folder.replacing()).distinct().order_by(PixelCopy.pixel_id)).scalars())


def held(opened: Session, pixel_id: int, checked_since_start: bool) -> bool:
    if not checked_since_start:
        return True
    return opened.execute(select(PixelCopy.id).where(PixelCopy.pixel_id == pixel_id, pixel_folder.replacing()).limit(1)).first() is not None


@dataclass
class PlacedCopyCheck:
    ran: bool = False

    def due(self, opened: Session) -> bool:
        return not self.ran or bool(replacing_pixels(opened))

    def run(self, opened: Session, photos: Photos, now: float) -> Checked:
        checked = check(opened, photos, now)
        self.ran = True
        return checked

    def held(self, opened: Session, pixel_id: int) -> bool:
        return held(opened, pixel_id, self.ran)
