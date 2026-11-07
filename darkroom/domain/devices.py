from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom import oidc
from darkroom.config import Settings
from darkroom.domain import events
from darkroom.storage.models import Device, Person

MAX_TOKEN = 200
DEVICE_REVOKED = "device_revoked"
SIGN_IN_LAPSED = "sign_in_lapsed"
SIGN_IN_UNAVAILABLE = "sign_in_unavailable"
SIGN_IN_REFUSED = "sign_in_refused"
REFUSALS = (DEVICE_REVOKED, SIGN_IN_LAPSED, SIGN_IN_UNAVAILABLE, SIGN_IN_REFUSED)


class NoSuchDevice(LookupError):
    pass


class NoPerson(LookupError):
    pass


@dataclass(frozen=True)
class Refused:
    reason: str | None


@dataclass(frozen=True)
class SignedInDevice:
    device_id: int
    person_id: int
    name: str
    email: str | None


def person_for(opened: Session, email: str | None, verified: bool) -> Person:
    if not email or not verified:
        raise NoPerson("Darkroom has no person with this email")
    found = list(opened.execute(select(Person).where(func.lower(Person.email) == email.strip().lower())).scalars())
    if len(found) != 1:
        raise NoPerson("Darkroom has no person with this email" if not found else "more than one person has this email")
    return found[0]


def enrol(opened: Session, sub: str, email: str | None, verified: bool, name: str, now: float) -> tuple[Device, str]:
    person = person_for(opened, email, verified)
    for other in of_person(opened, person.id):
        if other.revoked_at is None and other.sub != sub:
            revoke(opened, other.id, now)
    token = secrets.token_urlsafe(32)
    device = Device(person_id=person.id, name=name, token_hash=oidc.hash_token(token), sub=sub, created=now, last_checkin=now)
    opened.add(device)
    opened.flush()
    events.record(opened, now, "device_signed_in", person.slug, name)
    return device, token


def authenticate(opened: Session, settings: Settings, token: str | None, now: float) -> SignedInDevice | Refused:
    if not token or len(token) > MAX_TOKEN:
        return Refused(None)
    device = opened.execute(select(Device).where(Device.token_hash == oidc.hash_token(token))).scalar_one_or_none()
    if device is None:
        return Refused(None)
    if device.revoked_at is not None:
        return Refused(DEVICE_REVOKED)
    person = opened.get(Person, device.person_id)
    if person is None:
        return Refused(None)
    lapsed = oidc.device_access_lapsed(opened, settings, device.sub, person.email, now)
    if lapsed is not None:
        return Refused(lapsed if lapsed in (SIGN_IN_LAPSED, SIGN_IN_UNAVAILABLE) else SIGN_IN_REFUSED)
    device.last_checkin = now
    opened.flush()
    return SignedInDevice(device.id, person.id, device.name, person.email)


def check_in(opened: Session, device_id: int, pending_uploads: int, app_version: str | None, now: float) -> Device:
    device = get(opened, device_id)
    device.pending_uploads, device.app_version, device.last_checkin = pending_uploads, app_version, now
    opened.flush()
    return device


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
