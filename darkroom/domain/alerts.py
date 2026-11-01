from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import events
from darkroom.providers import homeassistant
from darkroom.storage.models import Alert

HOUR = 3600.0
DAY = 24 * HOUR
REPEAT_AFTER = (0.0, DAY, 4 * DAY)
ONCE = (0.0,)

Notifier = Callable[[Settings, str, str, str | None], str]


@dataclass(frozen=True)
class Wording:
    title: str
    message: str


def sign_in_needed(account: str) -> Wording:
    return Wording("Google sign-in needed", f"{account} needs someone to sign in to Google again. Open Darkroom's browser view to do it.")


def export_imported(summary: str) -> Wording:
    return Wording("Takeout import finished", summary)


def originals_unmatched(person: str, pixel: str, count: int) -> Wording:
    return Wording("Some originals are not in Google Photos", f"{count} of {person}'s originals have no match in Google yet. Check that their folder is turned on for backup on {pixel}.")


def schedule_ending(account: str, end: str) -> Wording:
    return Wording("Takeout schedule ending", f"The export schedule for {account} ends on {end}. It will be renewed shortly.")


def schedule_renewed(account: str, end: str) -> Wording:
    return Wording("Takeout schedule renewed", f"The export schedule for {account} now runs until {end}.")


def export_expiring(export: str, expires: str) -> Wording:
    return Wording("A Takeout export is about to expire", f"Export {export} expires on {expires} and has not been downloaded.")


def failure(subject: str) -> Wording:
    return Wording("Darkroom needs a look", f"{subject} failed. Open Darkroom for details.")


def deliver(opened: Session, settings: Settings, alert: Alert, wording: Wording, now: float, notifier: Notifier) -> str:
    outcome = notifier(settings, wording.title, wording.message, alert.link)
    alert.last_sent = now
    alert.sent_count += 1
    events.record(opened, now, "alert_sent" if outcome == homeassistant.SENT else f"alert_{outcome}", alert.subject, alert.kind)
    return outcome


def open_alert(opened: Session, settings: Settings, key: str, kind: str, subject: str, wording: Wording, link: str | None, now: float,
               repeats: tuple[float, ...] = REPEAT_AFTER, notifier: Notifier = homeassistant.notify) -> Alert:
    existing = opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars().first()
    if existing is not None:
        return existing
    alert = Alert(key=key, kind=kind, subject=subject, message=wording.message, link=link, opened=now)
    opened.add(alert)
    opened.flush()
    deliver(opened, settings, alert, wording, now, notifier)
    if len(repeats) == 1:
        alert.closed = now
    return alert


def tick(opened: Session, settings: Settings, now: float, notifier: Notifier = homeassistant.notify) -> int:
    sent = 0
    for alert in opened.execute(select(Alert).where(Alert.closed.is_(None))).scalars():
        if alert.sent_count >= len(REPEAT_AFTER):
            continue
        if now >= alert.opened + REPEAT_AFTER[alert.sent_count]:
            deliver(opened, settings, alert, Wording(alert.subject, alert.message), now, notifier)
            sent += 1
    return sent


def close(opened: Session, key: str, now: float) -> int:
    closed = 0
    for alert in opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars():
        alert.closed = now
        closed += 1
    return closed
