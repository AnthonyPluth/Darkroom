from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import monitoring
from darkroom.config import Settings
from darkroom.domain import accounts, alerts, events, push_tokens
from darkroom.providers import apns
from darkroom.storage.models import Alert, Device

RETRY_AFTER = 300.0
RETRY_FOR = 3600.0
TRANSPORT_REASONS = frozenset({"Unreachable", "KeyUnusable"})
FALLBACK_TITLE = "Darkroom"


def recipients(opened: Session, alert: Alert) -> set[int]:
    if alert.person_id is not None:
        return {alert.person_id}
    if alert.account_id is not None:
        return accounts.served_people(opened, alert.account_id)
    return set()


def due(opened: Session, now: float) -> list[int]:
    query = select(Alert.id).where(Alert.push_due.is_not(None), Alert.push_due <= now).order_by(Alert.push_due, Alert.id)
    return list(opened.execute(query).scalars())


def deliver_due(opened: Session, settings: Settings, pusher: apns.Pusher | None, now: float) -> int:
    pushed = 0
    for alert_id in due(opened, now):
        try:
            pushed += deliver_one(opened, settings, pusher, alert_id, now)
        except Exception as error:
            opened.rollback()
            monitoring.log("alert push failed", error=type(error).__name__)
            give_up(opened, alert_id, now)
    return pushed


def give_up(opened: Session, alert_id: int, now: float) -> None:
    alert = opened.get(Alert, alert_id)
    if alert is not None and alert.push_due is not None:
        settle(opened, alert, alert.push_due, "alert_failed", now)


def settle(opened: Session, alert: Alert, due_at: float, outcome: str, now: float) -> None:
    opened.refresh(alert)
    if alert.push_due == due_at:
        alert.push_due = None
    if outcome:
        events.record(opened, now, outcome, alert.subject, alert.kind)
    opened.commit()


def deliver_one(opened: Session, settings: Settings, pusher: apns.Pusher | None, alert_id: int, now: float) -> int:
    alert = opened.get(Alert, alert_id)
    if alert is None or alert.push_due is None:
        return 0
    due_at = alert.push_due
    if alert.closed is not None and alert.kind not in alerts.ONE_SHOT_KINDS:
        settle(opened, alert, due_at, "", now)
        return 0
    if pusher is None:
        settle(opened, alert, due_at, "alert_disabled", now)
        return 0
    targets = push_tokens.targets(opened, settings, recipients(opened, alert), now)
    if not targets:
        settle(opened, alert, due_at, "alert_no_device", now)
        return 0
    push = apns.Push(alert.kind, alert.id, alerts.TITLES.get(alert.kind, FALLBACK_TITLE))
    opened.commit()
    opened.expire_all()
    held = [target for target in targets if push_tokens.still_held(opened, settings, target, now)]
    if not held:
        skipped(opened, targets, now)
        settle(opened, alert, due_at, "alert_no_device", now)
        return 0
    results = pusher.send([target.token for target in held], push)
    skipped(opened, [target for target in targets if target not in held], now)
    for target, result in zip(held, results, strict=True):
        record(opened, target, result, now)
    sent = sum(result.outcome == "sent" for result in results)
    failed = [result for result in results if result.outcome == "failed"]
    if not sent and failed and all(result.reason in TRANSPORT_REASONS for result in failed) and retry_left(alert, now):
        opened.refresh(alert)
        if alert.push_due == due_at:
            alert.push_due = now + RETRY_AFTER
        opened.commit()
        return 0
    settle(opened, alert, due_at, "alert_sent" if sent else "alert_failed" if failed else "alert_no_device", now)
    return sent


def retry_left(alert: Alert, now: float) -> bool:
    return now + RETRY_AFTER - (alert.last_sent if alert.last_sent is not None else alert.opened) <= RETRY_FOR


def skipped(opened: Session, targets: list[push_tokens.Target], now: float) -> None:
    for target in targets:
        device = opened.get(Device, target.device_id)
        subject = push_tokens.subject_of(opened, device) if device is not None else str(target.device_id)
        events.record(opened, now, "push_skipped", subject, device.name if device is not None else None)


def record(opened: Session, target: push_tokens.Target, result: apns.Result, now: float) -> None:
    device = opened.get(Device, target.device_id)
    if device is None:
        return
    subject = push_tokens.subject_of(opened, device)
    if result.outcome == "sent":
        events.record(opened, now, "push_sent", subject, device.name)
    elif result.outcome == "invalid_token":
        events.record(opened, now, "push_token_invalid", subject, f"{device.name}: {result.reason}")
        push_tokens.forget_reported(opened, target, result.unregistered_at, now)
    else:
        events.record(opened, now, "push_failed", subject, f"{device.name}: {result.reason}")
