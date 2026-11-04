from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.config import Settings, demo_allowed
from darkroom.domain import events
from darkroom.storage.models import Alert

HOUR = 3600.0
DAY = 24 * HOUR
REPEAT_AFTER = (0.0, DAY, 4 * DAY)
ONCE = (0.0,)
SENT = "sent"
DISABLED = "disabled"
FAILED = "failed"
ONE_SHOT_KINDS = frozenset({"export_imported", "schedule_renewed", "failure"})
TITLES = {
    "sign_in_needed": "Google sign-in needed",
    "wrong_account": "Wrong Google account",
    "export_imported": "Takeout import finished",
    "originals_unmatched": "Some originals are not in Google Photos",
    "schedule_ending": "Takeout schedule ending",
    "schedule_renewed": "Takeout schedule renewed",
    "export_expiring": "A Takeout export is about to expire",
    "failure": "Darkroom needs a look",
}

Notifier = Callable[[Settings, str, str, str | None], str]


@dataclass(frozen=True)
class Wording:
    title: str
    message: str


def sign_in_needed(account: str) -> Wording:
    return Wording("Google sign-in needed", f"{account} needs someone to sign in to Google again. Open Darkroom's browser view to do it.")


def wrong_account(account: str) -> Wording:
    return Wording("Wrong Google account", f"The browser is signed in to a different Google account than {account}. Open Darkroom's browser view and switch to {account}.")


def export_imported(summary: str) -> Wording:
    return Wording("Takeout import finished", summary)


def originals_unmatched(person: str, pixel: str | None, count: int) -> Wording:
    where = f"on {pixel}" if pixel else "in Google Photos on the Pixel that serves them"
    return Wording("Some originals are not in Google Photos", f"{count} of {person}'s originals have no match in Google yet. Check that their folder is turned on for backup {where}.")


def schedule_ending(account: str, end: str) -> Wording:
    return Wording("Takeout schedule ending", f"The export schedule for {account} ends on {end}. It will be renewed shortly.")


def schedule_renewed(account: str, end: str) -> Wording:
    return Wording("Takeout schedule renewed", f"The export schedule for {account} now runs until {end}.")


def export_expiring(export: str, expires: str) -> Wording:
    return Wording("A Takeout export is about to expire", f"Export {export} expires on {expires} and has not been downloaded.")


def failure(subject: str) -> Wording:
    return Wording("Darkroom needs a look", f"{subject} failed. Open Darkroom for details.")


def undelivered(settings: Settings, title: str, message: str, link: str | None = None) -> str:
    return DISABLED


RECORDED: list[tuple[str, str, str | None]] = []
RECORDED_LIMIT = 100


def recorded(settings: Settings, title: str, message: str, link: str | None = None) -> str:
    RECORDED.append((title, message, link))
    del RECORDED[:-RECORDED_LIMIT]
    return SENT


def notifier_for(settings: Settings) -> Notifier:
    return recorded if demo_allowed(settings) else undelivered


def deliver(opened: Session, settings: Settings, alert: Alert, wording: Wording, now: float, notifier: Notifier | None) -> str:
    outcome = (notifier or notifier_for(settings))(settings, wording.title, wording.message, alert.link)
    alert.last_sent = now
    alert.sent_count += 1
    events.record(opened, now, "alert_sent" if outcome == SENT else f"alert_{outcome}", alert.subject, alert.kind)
    return outcome


def open_alert(opened: Session, settings: Settings, key: str, kind: str, subject: str, wording: Wording, link: str | None, now: float,
               repeats: tuple[float, ...] = REPEAT_AFTER, notifier: Notifier | None = None) -> Alert:
    existing = opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars().first()
    if existing is not None:
        return existing
    if kind in ONE_SHOT_KINDS:
        repeats = ONCE
    alert = Alert(key=key, kind=kind, subject=subject, message=wording.message, link=link, opened=now)
    opened.add(alert)
    opened.flush()
    deliver(opened, settings, alert, wording, now, notifier)
    if len(repeats) == 1:
        alert.closed = now
    return alert


def open_alert_committed(opened: Session, settings: Settings, key: str, kind: str, subject: str, wording: Wording, link: str | None, now: float,
                         notifier: Notifier | None = None) -> Alert:
    existing = opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars().first()
    if existing is not None:
        return existing
    alert = Alert(key=key, kind=kind, subject=subject, message=wording.message, link=link, opened=now, last_sent=now, sent_count=1,
                  closed=now if kind in ONE_SHOT_KINDS else None)
    opened.add(alert)
    opened.commit()
    outcome = (notifier or notifier_for(settings))(settings, wording.title, wording.message, link)
    events.record(opened, now, "alert_sent" if outcome == SENT else f"alert_{outcome}", alert.subject, alert.kind)
    return alert


def tick(opened: Session, settings: Settings, now: float, notifier: Notifier | None = None) -> int:
    sent = 0
    for alert in opened.execute(select(Alert).where(Alert.closed.is_(None))).scalars():
        if alert.sent_count >= len(REPEAT_AFTER):
            continue
        if now >= alert.opened + REPEAT_AFTER[alert.sent_count]:
            deliver(opened, settings, alert, Wording(TITLES.get(alert.kind, alert.subject), alert.message), now, notifier)
            sent += 1
    return sent


def close_other_kinds(opened: Session, key: str, kind: str, now: float) -> int:
    closed = 0
    for alert in opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None), Alert.kind != kind)).scalars():
        alert.closed = now
        closed += 1
    return closed


def close(opened: Session, key: str, now: float) -> int:
    closed = 0
    for alert in opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars():
        alert.closed = now
        closed += 1
    return closed
