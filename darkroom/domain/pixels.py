from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom.domain import accounts, dedupe, events, pixel_folder
from darkroom.storage.models import GoogleAccount, Person, Pixel, PixelCopy, PixelPerson


class NoSuchPixel(LookupError):
    pass


class NoPeople(ValueError):
    pass


class StillHasCopies(ValueError):
    pass


def get(opened: Session, pixel_id: int) -> Pixel:
    pixel = opened.get(Pixel, pixel_id)
    if pixel is None:
        raise NoSuchPixel(f"no Pixel {pixel_id}")
    return pixel


def listing(opened: Session) -> list[Pixel]:
    return list(opened.execute(select(Pixel).order_by(Pixel.id)).scalars())


def people_of(opened: Session, pixel_id: int) -> list[int]:
    return sorted(opened.execute(select(PixelPerson.person_id).where(PixelPerson.pixel_id == pixel_id)).scalars())


def as_dict(opened: Session, pixel: Pixel) -> dict[str, object]:
    return {
        "id": pixel.id,
        "name": pixel.name,
        "syncthing_device_id": pixel.syncthing_device_id,
        "google_account_id": pixel.google_account_id,
        "people_ids": people_of(opened, pixel.id),
        "last_sync": pixel.last_sync,
        "created": pixel.created,
    }


def require_account(opened: Session, account_id: int) -> GoogleAccount:
    return accounts.get(opened, account_id)


def require_people(opened: Session, people_ids: list[int]) -> None:
    if not people_ids:
        raise NoPeople("a Pixel serves at least one person")
    for person_id in people_ids:
        if opened.get(Person, person_id) is None:
            raise accounts.NoSuchPerson(f"no person {person_id}")


def set_people(opened: Session, pixel_id: int, people_ids: list[int]) -> None:
    opened.execute(sql_delete(PixelPerson).where(PixelPerson.pixel_id == pixel_id))
    for person_id in people_ids:
        opened.add(PixelPerson(pixel_id=pixel_id, person_id=person_id))
    opened.flush()


def create(opened: Session, name: str, syncthing_device_id: str | None, account_id: int, people_ids: list[int] | None, now: float) -> Pixel:
    account = require_account(opened, account_id)
    chosen = people_ids if people_ids is not None else [account.default_person_id]
    require_people(opened, chosen)
    before = accounts.served_people(opened, account_id)
    pixel = Pixel(name=name, syncthing_device_id=syncthing_device_id, google_account_id=account_id, created=now)
    opened.add(pixel)
    opened.flush()
    set_people(opened, pixel.id, chosen)
    dedupe.confirm_newly_served(opened, account_id, before)
    events.record(opened, now, "pixel_created", str(pixel.id))
    return pixel


def lock(opened: Session, pixel_id: int) -> None:
    try:
        opened.execute(sql_update(Pixel).where(Pixel.id == pixel_id).values(id=Pixel.id))
    except OperationalError as error:
        if "database is locked" not in str(error.orig):
            raise
        raise accounts.Busy("Darkroom is busy copying or pruning this Pixel's photos; try again in a moment") from None


def update(opened: Session, pixel_id: int, fields: dict[str, object], now: float) -> Pixel:
    lock(opened, pixel_id)
    pixel = get(opened, pixel_id)
    account_id = fields.get("google_account_id", pixel.google_account_id)
    assert isinstance(account_id, int)
    require_account(opened, account_id)
    chosen = fields.get("people_ids", people_of(opened, pixel.id))
    assert isinstance(chosen, list)
    require_people(opened, chosen)
    require_still_served(opened, pixel, set(chosen) if account_id == pixel.google_account_id else set())
    earlier = set(people_of(opened, pixel.id))
    leaving = earlier - set(chosen)
    moving = account_id != pixel.google_account_id
    require_no_placed_copies(opened, pixel, None if moving else leaving)
    drop_queued_copies(opened, pixel, leaving, now)
    before = accounts.served_people(opened, account_id)
    pixel.google_account_id = account_id
    if "people_ids" in fields:
        set_people(opened, pixel.id, chosen)
    if "name" in fields:
        pixel.name = str(fields["name"])
    if "syncthing_device_id" in fields:
        value = fields["syncthing_device_id"]
        pixel.syncthing_device_id = value if isinstance(value, str) else None
    opened.flush()
    dedupe.confirm_newly_served(opened, account_id, before)
    if moving or set(chosen) - earlier:
        pixel_folder.requeue_unconfirmed(opened, pixel, None if moving else set(chosen) - earlier, now)
    events.record(opened, now, "pixel_updated", str(pixel.id))
    return pixel


def require_no_placed_copies(opened: Session, pixel: Pixel, people: set[int] | None) -> None:
    query = select(PixelCopy.id).where(PixelCopy.pixel_id == pixel.id, PixelCopy.state == "placed")
    if people is not None:
        if not people:
            return
        query = query.where(PixelCopy.person_id.in_(people))
    if opened.execute(query.limit(1)).first() is not None:
        raise StillHasCopies(f"{pixel.name} still has photos in its folder for the Google account and people it serves now; they leave only once "
                             "Google Photos has them and Darkroom prunes them, so change the Pixel's account or people after that")


def drop_queued_copies(opened: Session, pixel: Pixel, people: set[int], now: float) -> None:
    if not people:
        return
    waiting = [PixelCopy.pixel_id == pixel.id, PixelCopy.state == "queued", PixelCopy.person_id.in_(people)]
    dropped = opened.execute(select(func.count(PixelCopy.id)).where(*waiting)).scalar_one()
    opened.execute(sql_delete(PixelCopy).where(*waiting))
    if dropped:
        events.record(opened, now, "pixel_copies_dropped", f"pixel {pixel.id}", f"{dropped} queued photos of people it no longer serves")


def require_still_served(opened: Session, pixel: Pixel, kept: set[int]) -> None:
    account = require_account(opened, pixel.google_account_id)
    still = {account.default_person_id} | accounts.served_by_pixels(opened, account.id, except_pixel_id=pixel.id) | kept
    accounts.require_confirmations_kept(opened, account.id, still)


def delete(opened: Session, pixel_id: int, now: float) -> None:
    lock(opened, pixel_id)
    pixel = get(opened, pixel_id)
    require_still_served(opened, pixel, set())
    waiting = opened.execute(select(PixelCopy.id).where(PixelCopy.pixel_id == pixel.id, PixelCopy.state.in_(("queued", "placed"))).limit(1)).first()
    if waiting is not None:
        raise StillHasCopies(f"{pixel.name} still has photos in its folder or waiting for it; they leave only once Google Photos has them and Darkroom prunes them, "
                             "so delete the Pixel after that")
    opened.delete(pixel)
    opened.flush()
    events.record(opened, now, "pixel_deleted", str(pixel_id))
