from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import alerts, pixel_check, pixel_folder, share_check, syncthing_version, transport
from darkroom.providers import syncthing
from darkroom.storage import db
from darkroom.storage.models import Alert, Event, Pixel, PixelCopy, PixelTransport

DOWN_ALERT_AFTER = alerts.HOUR
NOT_SEEN_AFTER = 3 * alerts.DAY
SUBJECT = transport.SUBJECT
DOWN_KEY = "syncthing:down"
SETTINGS_KEY = "syncthing:settings"
VERSION_KEY = "syncthing:version"
SHARE_KEY = "share:check"
HELD_PREFIX = "syncthing:held:"
KEY_REASONS = (syncthing_version.NO_KEY, syncthing_version.KEY_REFUSED)

CardState = Literal["starting", "running", "down", "settings_failed", "version", "key", "share_check_failed"]
PixelState = Literal["not_syncing", "not_paired", "device_id_invalid", "device_id_shared", "folder_removed", "folder_path_refused", "checking", "held", "waiting", "not_seen", "sending", "connected", "not_connected"]


@dataclass(frozen=True)
class Card:
    state: CardState
    reason: str | None = None
    since: float | None = None


@dataclass(frozen=True)
class PixelView:
    state: PixelState
    copies: int = 0
    files: int = 0
    days: int = 0
    completion: float | None = None
    last_seen: float | None = None
    others: tuple[str, ...] = ()

    def as_dict(self, pixel_id: int, name: str) -> dict[str, object]:
        return {"pixel_id": pixel_id, "name": name, "state": self.state, "copies": self.copies, "files": self.files, "days": self.days, "completion": self.completion,
                "last_seen": self.last_seen, "others": list(self.others)}


def latest(opened: Session, types: tuple[str, ...], subject: str) -> Event | None:
    return opened.execute(select(Event).where(Event.type.in_(types), Event.subject == subject).order_by(Event.id.desc()).limit(1)).scalars().first()


def card(opened: Session) -> Card:
    share = latest(opened, share_check.SHARE_EVENTS, share_check.SUBJECT)
    if share is not None and share.type == share_check.FAILED:
        return Card("share_check_failed", share.detail, share.time)
    found = latest(opened, transport.OUTCOMES, SUBJECT)
    if found is None:
        return Card("starting")
    if found.type == transport.PAUSED:
        return Card("share_check_failed", found.detail, found.time)
    if found.type == syncthing_version.EVENT:
        if found.detail == syncthing_version.NOT_ANSWERING:
            return Card("down", None, found.time)
        return Card("key" if found.detail in KEY_REASONS else "version", found.detail, found.time)
    if found.type == transport.FAILED:
        if found.detail == transport.UNREACHABLE:
            return Card("down", None, found.time)
        return Card("settings_failed", found.detail, found.time)
    return Card("running")


def unwritten(opened: Session) -> dict[int, int]:
    rows = opened.execute(select(PixelCopy.pixel_id, func.count(PixelCopy.id)).where(pixel_folder.replacing()).group_by(PixelCopy.pixel_id)).all()
    return {int(pixel_id): int(number) for pixel_id, number in rows}


def files_of(detail: str | None) -> int:
    number = (detail or "").removeprefix("files ")
    return int(number) if number.isascii() and number.isdigit() and len(number) <= 9 else 0


def unaccounted(opened: Session) -> dict[int, int]:
    last = (select(Event.subject, func.max(Event.id).label("last")).where(Event.type == pixel_check.UNACCOUNTED).group_by(Event.subject).subquery())
    rows = opened.execute(select(Event.subject, Event.detail).join(last, Event.id == last.c.last)).all()
    found = {}
    for subject, detail in rows:
        number = subject.removeprefix("pixel ")
        if number.isascii() and number.isdigit() and len(number) <= 12:
            found[int(number)] = files_of(detail)
    return found


def canonical(value: str | None) -> str | None:
    return syncthing.device_id(value) if value and syncthing.is_device_id(value) else None


def same_device(pixels: list[Pixel]) -> dict[int, tuple[str, ...]]:
    devices = {pixel.id: canonical(pixel.syncthing_device_id) for pixel in pixels}
    return {pixel.id: tuple(other.name for other in pixels if other.id != pixel.id and devices[other.id] == devices[pixel.id])
            for pixel in pixels if devices[pixel.id] is not None}


def unusable(pixel: Pixel, row: PixelTransport | None, others: tuple[str, ...], released: bool) -> PixelView | None:
    error = None if row is None else row.error
    if error == transport.INVALID_DEVICE:
        return PixelView("device_id_invalid")
    if error == transport.SHARED_DEVICE:
        return PixelView("device_id_shared", others=others) if others else PixelView("checking")
    if error == transport.REMOVED:
        return PixelView("checking") if released else PixelView("folder_removed")
    if error == transport.PATH_REFUSED:
        return PixelView("folder_path_refused")
    return None


def pixel_view(pixel: Pixel, row: PixelTransport | None, running: bool, copies: int, files: int, now: float, others: tuple[str, ...] = (),
               released: bool = False) -> PixelView:
    if not running:
        return PixelView("not_syncing")
    if not pixel.syncthing_device_id:
        return PixelView("not_paired")
    found = unusable(pixel, row, others, released)
    if found is not None:
        return found
    if row is not None and row.error == transport.HELD and (copies or files):
        return PixelView("held", copies=copies, files=files)
    if row is not None and row.error in (transport.HELD, transport.CHECKING):
        return PixelView("checking")
    if row is None or (row.last_seen is None and not row.connected):
        return PixelView("waiting")
    if row.connected:
        if row.completion is not None and row.completion < 100.0:
            return PixelView("sending", completion=row.completion, last_seen=row.last_seen)
        return PixelView("connected", completion=row.completion, last_seen=row.last_seen)
    seen = row.last_seen if row.last_seen is not None else now
    if now - seen >= NOT_SEEN_AFTER:
        return PixelView("not_seen", days=int((now - seen) // alerts.DAY), last_seen=seen)
    return PixelView("not_connected", last_seen=seen)


@dataclass(frozen=True)
class Overview:
    card: Card
    pixels: dict[int, PixelView]


def overview(opened: Session, now: float) -> Overview:
    found = card(opened)
    copies, files = unwritten(opened), unaccounted(opened)
    rows = {row.pixel_id: row for row in opened.execute(select(PixelTransport)).scalars()}
    pixels = list(opened.execute(select(Pixel).order_by(Pixel.id)).scalars())
    shared = same_device(pixels)
    released = {pixel_id for pixel_id, row in rows.items() if row.error == transport.REMOVED and not transport.removed_before(opened, pixel_id)}
    views = {pixel.id: pixel_view(pixel, rows.get(pixel.id), found.state == "running", copies.get(pixel.id, 0), files.get(pixel.id, 0), now, shared.get(pixel.id, ()),
                                  pixel.id in released)
             for pixel in pixels}
    return Overview(found, views)


def pixel_dicts(opened: Session, seen: Overview) -> dict[int, dict[str, object]]:
    return {pixel.id: seen.pixels[pixel.id].as_dict(pixel.id, pixel.name) for pixel in opened.execute(select(Pixel).order_by(Pixel.id)).scalars() if pixel.id in seen.pixels}


def any_paired(opened: Session) -> bool:
    return opened.execute(select(Pixel.id).where(Pixel.syncthing_device_id.is_not(None), Pixel.syncthing_device_id != "").limit(1)).first() is not None


def card_dict(opened: Session, seen: Overview) -> dict[str, object]:
    listed = list(pixel_dicts(opened, seen).values()) if any_paired(opened) else []
    return {"state": seen.card.state, "reason": seen.card.reason, "since": seen.card.since, "pixels": listed}


def raise_or_close(opened: Session, settings: Settings, key: str, kind: str, subject: str, wording: alerts.Wording | None, link: str | None, now: float,
                   notifier: alerts.Notifier | None) -> None:
    if wording is None or kind in settings.alerts_off:
        alerts.close(opened, key, now)
        return
    alerts.open_alert(opened, settings, key, kind, subject, wording, link, now, notifier=notifier, about=alerts.NOBODY)


def link_to(settings: Settings, path: str) -> str | None:
    return f"{settings.public_url}{path}" if settings.public_url else None


def down(found: Card, now: float) -> bool:
    if found.state == "key":
        return True
    return found.state == "down" and found.since is not None and now - found.since >= DOWN_ALERT_AFTER


def review_held(opened: Session, settings: Settings, seen: Overview, now: float, notifier: alerts.Notifier | None) -> None:
    if seen.card.state != "running":
        return
    names = dict(opened.execute(select(Pixel.id, Pixel.name)).all())
    held = {pixel_id: view for pixel_id, view in seen.pixels.items() if view.state == "held"}
    for pixel_id, view in sorted(held.items()):
        raise_or_close(opened, settings, f"{HELD_PREFIX}{pixel_id}", "pixel_held", f"pixel {pixel_id}", alerts.pixel_held(names[pixel_id], view.copies, view.files),
                       link_to(settings, "/people"), now, notifier)
    for alert in opened.execute(select(Alert).where(Alert.kind == "pixel_held", Alert.closed.is_(None))).scalars().all():
        number = alert.key.removeprefix(HELD_PREFIX)
        if not (number.isdigit() and int(number) in held):
            alerts.close(opened, alert.key, now)


SYNCTHING_KINDS = ((DOWN_KEY, "syncthing_down"), (SETTINGS_KEY, "syncthing_settings_failed"), (VERSION_KEY, "syncthing_version"))


def close_switched_off(opened: Session, settings: Settings, now: float) -> None:
    for key, kind in SYNCTHING_KINDS:
        if kind in settings.alerts_off:
            alerts.close(opened, key, now)
    if "pixel_held" in settings.alerts_off:
        for alert in opened.execute(select(Alert).where(Alert.kind == "pixel_held", Alert.closed.is_(None))).scalars().all():
            alerts.close(opened, alert.key, now)


def review(opened: Session, settings: Settings, now: float, notifier: alerts.Notifier | None = None) -> None:
    db.write_lock(opened)
    review_unlocked(opened, settings, now, notifier)
    opened.commit()


def review_unlocked(opened: Session, settings: Settings, now: float, notifier: alerts.Notifier | None) -> None:
    share = latest(opened, share_check.SHARE_EVENTS, share_check.SUBJECT)
    failed = share is not None and share.type == share_check.FAILED
    home = link_to(settings, "/")
    raise_or_close(opened, settings, SHARE_KEY, "share_check_failed", share_check.SUBJECT, alerts.share_check_failed(settings.syncthing) if failed else None, home, now,
                   notifier)
    if not settings.syncthing:
        for key in (DOWN_KEY, SETTINGS_KEY, VERSION_KEY):
            alerts.close(opened, key, now)
        return
    close_switched_off(opened, settings, now)
    seen = overview(opened, now)
    if seen.card.state in ("share_check_failed", "starting"):
        return
    raise_or_close(opened, settings, DOWN_KEY, "syncthing_down", SUBJECT, alerts.syncthing_down() if down(seen.card, now) else None, home, now, notifier)
    raise_or_close(opened, settings, SETTINGS_KEY, "syncthing_settings_failed", SUBJECT,
                   alerts.syncthing_settings_failed() if seen.card.state == "settings_failed" else None, home, now, notifier)
    raise_or_close(opened, settings, VERSION_KEY, "syncthing_version", SUBJECT, alerts.syncthing_version() if seen.card.state == "version" else None, home, now,
                   notifier)
    review_held(opened, settings, seen, now, notifier)
