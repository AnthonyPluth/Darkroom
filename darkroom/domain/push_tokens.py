from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from darkroom import oidc
from darkroom.config import Settings
from darkroom.domain import events
from darkroom.storage import secretbox
from darkroom.storage.models import Device, Person

TOKEN = re.compile(r"[0-9a-f]{64,200}")
MAX_RAW = 256


class BadToken(ValueError):
    pass


class HeldByAnotherPerson(ValueError):
    pass


class DeviceGone(LookupError):
    pass


@dataclass(frozen=True)
class Target:
    device_id: int
    digest: str = field(repr=False)
    token: str = field(repr=False)


def normalized(raw: str) -> str:
    token = raw.strip().lower() if len(raw) <= MAX_RAW else ""
    if not TOKEN.fullmatch(token):
        raise BadToken("token must be the APNs device token, 64 to 200 hexadecimal characters")
    return token


def digest_of(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def subject_of(opened: Session, device: Device) -> str:
    person = opened.get(Person, device.person_id)
    return person.slug if person is not None else str(device.person_id)


def forget(opened: Session, device: Device, now: float) -> bool:
    if device.push_token_sealed is None and device.push_token_digest is None:
        return False
    device.push_token_sealed, device.push_token_digest, device.push_token_at = None, None, now
    events.record(opened, now, "push_token_cleared", subject_of(opened, device), device.name)
    return True


def hold_active(opened: Session, device: Device) -> None:
    held = opened.execute(update(Device).where(Device.id == device.id, Device.revoked_at.is_(None)).values(revoked_at=None))
    assert isinstance(held, CursorResult)
    if held.rowcount != 1:
        raise DeviceGone("no such device")


def forget_own(opened: Session, device_id: int, now: float) -> bool:
    device = opened.get(Device, device_id)
    if device is None or device.revoked_at is not None:
        raise DeviceGone("no such device")
    hold_active(opened, device)
    return forget(opened, device, now)


def holders(opened: Session, digest: str, device: Device) -> list[Device]:
    return list(opened.execute(select(Device).where(Device.push_token_digest == digest, Device.id != device.id)).scalars())


def register(opened: Session, settings: Settings, device_id: int, raw: str, now: float) -> bool:
    token = normalized(raw)
    device = opened.get(Device, device_id)
    if device is None or device.revoked_at is not None:
        raise DeviceGone("no such device")
    digest = digest_of(token)
    sealed = secretbox.seal(token, settings.secret_key)
    hold_active(opened, device)
    if device.push_token_digest == digest and unsealed(device, settings.secret_key) == token:
        device.push_token_at = now
        return False
    for other in holders(opened, digest, device):
        if other.person_id != device.person_id and not access_lapsed(opened, settings, other, now):
            raise HeldByAnotherPerson("another person's iPhone has this push token; sign that iPhone out first")
        forget(opened, other, now)
    opened.flush()
    device.push_token_sealed, device.push_token_digest, device.push_token_at = sealed, digest, now
    opened.flush()
    events.record(opened, now, "push_token_registered", subject_of(opened, device), device.name)
    return True


def access_lapsed(opened: Session, settings: Settings, device: Device, now: float) -> bool:
    if device.revoked_at is not None:
        return True
    person = opened.get(Person, device.person_id)
    return person is None or oidc.device_access_lapsed(opened, settings, device.sub, person.email, now) is not None


def token_for(opened: Session, settings: Settings, device: Device, now: float) -> str | None:
    if access_lapsed(opened, settings, device, now):
        return None
    return unsealed(device, settings.secret_key)


def unsealed(device: Device, secret: str) -> str | None:
    if device.push_token_sealed is None:
        return None
    try:
        token = secretbox.unseal(device.push_token_sealed, secret)
    except secretbox.Unreadable:
        return None
    return token if TOKEN.fullmatch(token) and digest_of(token) == device.push_token_digest else None


def targets(opened: Session, settings: Settings, person_ids: set[int], now: float) -> list[Target]:
    if not person_ids:
        return []
    query = select(Device).where(Device.person_id.in_(person_ids), Device.revoked_at.is_(None), Device.push_token_digest.is_not(None)).order_by(Device.id)
    found = []
    for device in opened.execute(query).scalars():
        token = token_for(opened, settings, device, now)
        if token is not None and device.push_token_digest is not None:
            found.append(Target(device.id, device.push_token_digest, token))
    return found


def forget_reported(opened: Session, target: Target, unregistered_at: float | None, now: float) -> bool:
    device = opened.get(Device, target.device_id)
    if device is None or device.push_token_digest != target.digest:
        return False
    if unregistered_at is not None and device.push_token_at is not None and device.push_token_at > unregistered_at:
        return False
    return forget(opened, device, now)
