from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.config import Settings, demo_allowed
from darkroom.domain import events
from darkroom.providers import resend
from darkroom.storage.models import Alert

HOUR = 3600.0
DAY = 24 * HOUR
REPEAT_AFTER = (0.0, DAY, 4 * DAY)
ONCE = (0.0,)
SENT = "sent"
DISABLED = "disabled"
FAILED = "failed"
QUEUED = "queued"
ONE_SHOT_KINDS = frozenset({"export_imported", "schedule_renewed", "failure"})
ANSWERED_IN_THE_BROWSER = frozenset({"sign_in_needed", "wrong_account"})
TITLES = {
    "sign_in_needed": "Google sign-in needed",
    "wrong_account": "Wrong Google account",
    "export_imported": "Takeout import finished",
    "originals_unmatched": "Some originals are not in Google Photos",
    "schedule_ending": "Takeout schedule ending",
    "schedule_renewed": "Takeout schedule renewed",
    "schedule_renewal_failed": "Takeout schedule not renewed",
    "schedule_renewal_unconfirmed": "Takeout schedule renewal not confirmed",
    "export_expiring": "A Takeout export is about to expire",
    "failure": "Darkroom needs a look",
    "syncthing_down": "Syncthing not answering",
    "syncthing_settings_failed": "Syncthing settings not applied",
    "syncthing_version": "Syncthing version not tested",
    "share_check_failed": "Photo share check failed",
    "pixel_held": "Pixel folder held",
    "part_stalled": "Takeout import stalled",
    "managed_pausing": "Immich-managed switch waiting",
}

Notifier = Callable[[Settings, str, str, str | None], str]


@dataclass(frozen=True)
class About:
    person_id: int | None = None
    account_id: int | None = None


NOBODY = About()


def person(person_id: int) -> About:
    return About(person_id=person_id)


def account(account_id: int) -> About:
    return About(account_id=account_id)


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


def schedule_ending(account: str, end: str, ended: bool = False) -> Wording:
    if ended:
        return Wording("Takeout schedule ending", f"The export schedule for {account} ended on {end}. It will be renewed shortly.")
    return Wording("Takeout schedule ending", f"The export schedule for {account} ends on {end}. It will be renewed shortly.")


def schedule_renewed(account: str, end: str) -> Wording:
    return Wording("Takeout schedule renewed", f"The export schedule for {account} now runs until {end}.")


def schedule_renewal_failed(account: str, end: str, ended: bool = False) -> Wording:
    if ended:
        return Wording(
            "Takeout schedule not renewed",
            f"The export schedule for {account} was not renewed, and it ended on {end}, so scheduled exports have stopped. Darkroom does not retry a renewal on its own. Press Renew now on the Exports page to create a new schedule.",
        )
    return Wording(
        "Takeout schedule not renewed",
        f"The export schedule for {account} was not renewed, and it ends on {end}. Darkroom does not retry a renewal on its own. Press Renew now on the Exports page to ask for another.",
    )


def schedule_renewal_unconfirmed(account: str, end: str) -> Wording:
    return Wording(
        "Takeout schedule renewal not confirmed",
        f"Darkroom could not confirm whether the export schedule for {account} was renewed; the current one ends on {end}. Open Takeout's export list, then say on the Exports page whether a new schedule is there: Darkroom records it if it is, and reports the renewal as not done if it is not. Darkroom does not retry a renewal, so it will not create a second schedule.",
    )


def export_expiring(export: str, expires: str) -> Wording:
    return Wording("A Takeout export is about to expire", f"Export {export} expires on {expires} and has not been downloaded.")


def failure(subject: str) -> Wording:
    return Wording("Darkroom needs a look", f"{subject} failed. Open Darkroom for details.")


def part_stalled(part: int, export: int, minutes: int) -> Wording:
    return Wording(TITLES["part_stalled"], f"Part {part} of export {export} has imported nothing for {minutes} minutes although Darkroom is still "
                   "working on it. Nothing was lost; the logs say which step it is in. The alert closes once the count moves again.")


def export_unfinished(export: str, held: int) -> Wording:
    return Wording("Darkroom needs a look", f"Export {export} did not download in full, so {counted(held, 'item')} from the parts that did arrive "
                   "still wait for the rest. Darkroom keeps them and the downloaded files.")


def syncthing_down() -> Wording:
    return Wording(TITLES["syncthing_down"],
                   "Darkroom's Syncthing has not answered for over an hour, so nothing is reaching the Pixels. Darkroom keeps restarting it, and placing and pruning carry on; "
                   "the Pixels catch up once it is back. If it stays down, restart the container.")


def syncthing_settings_failed() -> Wording:
    return Wording(TITLES["syncthing_settings_failed"],
                   "Darkroom could not apply its settings to Syncthing or read them back, so it has resumed nothing and the Pixels are not syncing. "
                   "It tries again before the next hourly run. The Status page gives the reason.")


def syncthing_version() -> Wording:
    return Wording(TITLES["syncthing_version"],
                   "The Syncthing in this container is not a version Darkroom was tested with, so Darkroom has not started it and nothing reaches the Pixels. "
                   "Run the published Darkroom image, which carries a tested Syncthing.")


def share_check_failed() -> Wording:
    return Wording(TITLES["share_check_failed"],
                   "The photo share did not pass Darkroom's check, so placing, pruning and syncing to the Pixels are paused, and nothing on the share is changed. "
                   "If the share is not mounted, fix the mapping and restart the container. If it really was replaced, bind the new one as the runbook says. "
                   "The Status page gives the reason.")


def counted(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def pixel_held(pixel: str, copies: int, files: int) -> Wording:
    if copies:
        message = (f"{counted(copies, 'photo')} could not be written to {pixel}'s folder again, so Darkroom is not sharing the folder with it. Nothing is deleted from the Pixel. "
                   "Something else is in their place, or Darkroom can't read it; the runbook says what to look for.")
        if files:
            message += f" It also has {counted(files, 'file')} Darkroom did not put there."
        return Wording(TITLES["pixel_held"], message)
    return Wording(TITLES["pixel_held"],
                   f"{pixel}'s folder has {counted(files, 'file')} Darkroom did not put there, so Darkroom is not sharing the folder with it. "
                   "Darkroom never removes or moves them: remove them, or move them out of the folder, by hand.")


def undelivered(settings: Settings, title: str, message: str, link: str | None = None) -> str:
    return DISABLED


RECORDED: list[tuple[str, str, str | None]] = []
RECORDED_LIMIT = 100


def recorded(settings: Settings, title: str, message: str, link: str | None = None) -> str:
    RECORDED.append((title, message, link))
    del RECORDED[:-RECORDED_LIMIT]
    return SENT


def queued(settings: Settings, title: str, message: str, link: str | None = None) -> str:
    return QUEUED


def notifier_for(settings: Settings) -> Notifier:
    if demo_allowed(settings):
        return recorded
    return queued if resend.of(settings) is not None else undelivered


def deliver(opened: Session, settings: Settings, alert: Alert, wording: Wording, now: float, notifier: Notifier | None) -> str:
    outcome = (notifier or notifier_for(settings))(settings, wording.title, wording.message, alert.link)
    alert.last_sent = now
    alert.sent_count += 1
    if outcome == QUEUED:
        alert.push_due = now
        return outcome
    events.record(opened, now, "alert_sent" if outcome == SENT else f"alert_{outcome}", alert.subject, alert.kind)
    return outcome


def open_alert(opened: Session, settings: Settings, key: str, kind: str, subject: str, wording: Wording, link: str | None, now: float,
               repeats: tuple[float, ...] = REPEAT_AFTER, notifier: Notifier | None = None, *, about: About) -> Alert:
    existing = opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars().first()
    if existing is not None:
        return existing
    if kind in ONE_SHOT_KINDS:
        repeats = ONCE
    alert = Alert(key=key, kind=kind, subject=subject, message=wording.message, link=link, opened=now, person_id=about.person_id, account_id=about.account_id)
    opened.add(alert)
    opened.flush()
    deliver(opened, settings, alert, wording, now, notifier)
    if len(repeats) == 1:
        alert.closed = now
    return alert


def open_alert_committed(opened: Session, settings: Settings, key: str, kind: str, subject: str, wording: Wording, link: str | None, now: float,
                         notifier: Notifier | None = None, *, about: About) -> Alert:
    existing = opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars().first()
    if existing is not None:
        return existing
    chosen = notifier or notifier_for(settings)
    queue = chosen is queued
    alert = Alert(key=key, kind=kind, subject=subject, message=wording.message, link=link, opened=now, last_sent=now, sent_count=1,
                  closed=now if kind in ONE_SHOT_KINDS else None, person_id=about.person_id, account_id=about.account_id,
                  push_due=now if queue else None)
    opened.add(alert)
    opened.commit()
    if queue:
        return alert
    outcome = chosen(settings, wording.title, wording.message, link)
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
        alert.push_due = None
        closed += 1
    return closed


def close(opened: Session, key: str, now: float) -> int:
    closed = 0
    for alert in opened.execute(select(Alert).where(Alert.key == key, Alert.closed.is_(None))).scalars():
        alert.closed = now
        if alert.sent_count and alert.kind not in ONE_SHOT_KINDS:
            alert.push_due = now
        closed += 1
    return closed
