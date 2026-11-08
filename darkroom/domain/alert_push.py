from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import monitoring
from darkroom.config import Settings
from darkroom.domain import alerts, events
from darkroom.providers.push_note import Note, Pusher
from darkroom.storage.models import Alert

RETRY_AFTER = 300.0
RETRY_FOR = 3600.0
FALLBACK_TITLE = "Darkroom"
OPEN_TEXT = "Darkroom needs a look. Open its Status page for details."
CLOSED_TEXT = "This no longer needs a look."


def closing(alert: Alert) -> bool:
    return alert.closed is not None and alert.kind not in alerts.ONE_SHOT_KINDS


def note_for(settings: Settings, alert: Alert) -> Note:
    title = alerts.TITLES.get(alert.kind, FALLBACK_TITLE)
    click = settings.public_url if settings.public_url.startswith(("https://", "http://")) else None
    if closing(alert):
        return Note(f"Resolved: {title}", CLOSED_TEXT, click)
    return Note(title, OPEN_TEXT, click)


def detail_of(alert: Alert) -> str:
    return f"{alert.kind}, resolved" if closing(alert) else alert.kind


def due(opened: Session, now: float) -> list[tuple[int, float]]:
    query = select(Alert.id, Alert.push_due).where(Alert.push_due.is_not(None), Alert.push_due <= now).order_by(Alert.push_due, Alert.id)
    return [(alert_id, due_at) for alert_id, due_at in opened.execute(query).tuples() if due_at is not None]


def deliver_due(opened: Session, settings: Settings, pusher: Pusher | None, now: float) -> int:
    sent = 0
    for alert_id, due_at in due(opened, now):
        try:
            sent += deliver_one(opened, settings, pusher, alert_id, now)
        except Exception as error:
            opened.rollback()
            monitoring.log("alert push failed", error=type(error).__name__)
            give_up(opened, alert_id, due_at, now)
    return sent


def give_up(opened: Session, alert_id: int, due_at: float, now: float) -> None:
    opened.expire_all()
    alert = opened.get(Alert, alert_id)
    if alert is None or alert.push_due != due_at:
        return
    alert.push_due = None
    events.record(opened, now, "alert_failed", alert.subject, detail_of(alert))
    opened.commit()


def deliver_one(opened: Session, settings: Settings, pusher: Pusher | None, alert_id: int, now: float) -> int:
    alert = opened.get(Alert, alert_id)
    if alert is None or alert.push_due is None:
        return 0
    due_at, subject, detail = alert.push_due, alert.subject, detail_of(alert)
    if pusher is None:
        alert.push_due = None
        if not closing(alert):
            events.record(opened, now, "alert_disabled", subject, detail)
        opened.commit()
        return 0
    note = note_for(settings, alert)
    started = alert.closed if closing(alert) and alert.closed is not None else alert.last_sent or alert.opened
    opened.commit()
    outcome = pusher.send(note)
    opened.expire_all()
    fresh = opened.get(Alert, alert_id)
    current = fresh is not None and fresh.push_due == due_at
    if outcome == "retry" and now + RETRY_AFTER - started <= RETRY_FOR:
        if fresh is not None and current:
            fresh.push_due = now + RETRY_AFTER
        opened.commit()
        return 0
    if fresh is not None and current:
        fresh.push_due = None
    events.record(opened, now, "alert_sent" if outcome == "sent" else "alert_failed", subject, detail)
    opened.commit()
    return 1 if outcome == "sent" else 0
