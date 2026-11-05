from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import accounts, dedupe, events
from darkroom.storage.models import GoogleAccount, Person, Pixel, PixelPerson


class NoSuchPixel(LookupError):
    pass


class NoPeople(ValueError):
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


def update(opened: Session, pixel_id: int, fields: dict[str, object], now: float) -> Pixel:
    pixel = get(opened, pixel_id)
    account_id = fields.get("google_account_id", pixel.google_account_id)
    assert isinstance(account_id, int)
    require_account(opened, account_id)
    chosen = fields.get("people_ids", people_of(opened, pixel.id))
    assert isinstance(chosen, list)
    require_people(opened, chosen)
    require_still_served(opened, pixel, set(chosen) if account_id == pixel.google_account_id else set())
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
    events.record(opened, now, "pixel_updated", str(pixel.id))
    return pixel


def require_still_served(opened: Session, pixel: Pixel, kept: set[int]) -> None:
    account = require_account(opened, pixel.google_account_id)
    still = {account.default_person_id} | accounts.served_by_pixels(opened, account.id, except_pixel_id=pixel.id) | kept
    accounts.require_confirmations_kept(opened, account.id, still)


def delete(opened: Session, pixel_id: int, now: float) -> None:
    pixel = get(opened, pixel_id)
    require_still_served(opened, pixel, set())
    opened.delete(pixel)
    opened.flush()
    events.record(opened, now, "pixel_deleted", str(pixel_id))
