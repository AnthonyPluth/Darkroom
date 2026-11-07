from __future__ import annotations


from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.storage.models import Device, Person

class NoSuchDevice(LookupError):
    pass


class NoPerson(LookupError):
    pass


def person_for(opened: Session, email: str | None, verified: bool) -> Person:
    if not email or not verified:
        raise NoPerson("Darkroom has no person with this email")
    found = list(opened.execute(select(Person).where(func.lower(Person.email) == email.strip().lower())).scalars())
    if len(found) != 1:
        raise NoPerson("Darkroom has no person with this email" if not found else "more than one person has this email")
    return found[0]


def get(opened: Session, device_id: int) -> Device:
    device = opened.get(Device, device_id)
    if device is None:
        raise NoSuchDevice(f"no device {device_id}")
    return device


def of_person(opened: Session, person_id: int) -> list[Device]:
    return list(opened.execute(select(Device).where(Device.person_id == person_id).order_by(Device.id)).scalars())


def as_dict(device: Device) -> dict[str, object]:
    return {
        "id": device.id,
        "person_id": device.person_id,
        "name": device.name,
        "cleanup_enabled": device.cleanup_enabled,
        "last_checkin": device.last_checkin,
        "app_version": device.app_version,
        "pending_uploads": device.pending_uploads,
        "created": device.created,
        "revoked_at": device.revoked_at,
    }


def revoke(opened: Session, device_id: int, now: float) -> Device:
    device = get(opened, device_id)
    if device.revoked_at is None:
        device.revoked_at = now
        device.cleanup_enabled = False
        slug = opened.get(Person, device.person_id)
        events.record(opened, now, "device_revoked", slug.slug if slug else str(device.person_id), device.name)
    return device
