from __future__ import annotations

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from darkroom import monitoring
from darkroom.config import Settings
from darkroom.domain import events, push_tokens
from darkroom.providers import apns
from darkroom.storage.models import Device

QUIET_AFTER = 24 * 3600.0
WAKE_INTERVAL = 24 * 3600.0


def candidates(opened: Session, now: float) -> list[Device]:
    query = (select(Device)
             .where(Device.revoked_at.is_(None), Device.last_checkin.is_not(None),
                    Device.last_checkin <= now - QUIET_AFTER,
                    Device.wake_pushed_at.is_(None) | (Device.wake_pushed_at <= now - WAKE_INTERVAL))
             .order_by(Device.id))
    return list(opened.execute(query).scalars())


def claim(opened: Session, device_id: int, now: float) -> bool:
    claimed = opened.execute(update(Device).where(
        Device.id == device_id, Device.revoked_at.is_(None), Device.last_checkin.is_not(None),
        Device.last_checkin <= now - QUIET_AFTER,
        Device.wake_pushed_at.is_(None) | (Device.wake_pushed_at <= now - WAKE_INTERVAL)).values(wake_pushed_at=now))
    assert isinstance(claimed, CursorResult)
    opened.commit()
    return claimed.rowcount == 1


def still_quiet(opened: Session, device_id: int, now: float) -> bool:
    device = opened.get(Device, device_id, populate_existing=True)
    return device is not None and device.last_checkin is not None and device.last_checkin <= now - QUIET_AFTER


def wake_due(opened: Session, settings: Settings, pusher: apns.Pusher | None, now: float) -> int:
    if pusher is None:
        return 0
    woken = 0
    for device_id, person_id in [(each.id, each.person_id) for each in candidates(opened, now)]:
        try:
            woken += wake_one(opened, settings, pusher, device_id, person_id, now)
        except Exception as error:
            opened.rollback()
            monitoring.log("wake push failed", error=type(error).__name__)
    return woken


def wake_one(opened: Session, settings: Settings, pusher: apns.Pusher, device_id: int, person_id: int, now: float) -> int:
    if not push_tokens.targets(opened, settings, {person_id}, now, only={device_id}):
        return 0
    if not claim(opened, device_id, now):
        return 0
    opened.expire_all()
    found = push_tokens.targets(opened, settings, {person_id}, now, only={device_id})
    if not found:
        return 0
    target = found[0]
    opened.commit()
    opened.expire_all()
    if not push_tokens.still_held(opened, settings, target, {person_id}, now) or not still_quiet(opened, device_id, now):
        return 0
    opened.rollback()
    result = pusher.send([target.token], apns.Wake())[0]
    opened.rollback()
    device = opened.get(Device, device_id)
    if device is None:
        return 0
    subject = push_tokens.subject_of(opened, device)
    if result.outcome == "sent":
        events.record(opened, now, "wake_sent", subject, device.name)
    elif result.outcome == "invalid_token":
        events.record(opened, now, "wake_token_invalid", subject, f"{device.name}: {result.reason}")
        push_tokens.forget_reported(opened, target, result.unregistered_at, now)
    else:
        events.record(opened, now, "wake_failed", subject, f"{device.name}: {result.reason}")
    opened.commit()
    return 1 if result.outcome == "sent" else 0
