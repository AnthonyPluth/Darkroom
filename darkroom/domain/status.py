from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import accounts, browser_jobs, eligibility
from darkroom.domain.ingest import run
from darkroom.storage.models import Alert, Device, Export, GoogleAccount, ImportRun, MediaItem, Person, Pixel, PixelPerson
from darkroom.storage.paths import Photos


@dataclass(frozen=True)
class Viewing:
    settings: Settings
    photos: Photos
    now: float


def open_alerts(opened: Session) -> list[Alert]:
    return list(opened.execute(select(Alert).where(Alert.closed.is_(None)).order_by(Alert.opened, Alert.id)).scalars())


def alert_dict(alert: Alert) -> dict[str, object]:
    return {"id": alert.id, "kind": alert.kind, "subject": alert.subject, "message": alert.message, "link": alert.link, "opened": alert.opened}


def photo_counts(opened: Session) -> dict[int, tuple[int, int, int]]:
    phone = MediaItem.source == "phone"
    query = (select(MediaItem.person_id, func.count(MediaItem.id),
                    func.sum(func.iif(MediaItem.gps_source != "none", 1, 0)),
                    func.sum(func.iif(MediaItem.time_source == "unknown", 1, 0)))
             .where(phone).group_by(MediaItem.person_id))
    return {person_id: (int(total), int(with_gps or 0), int(unknown or 0)) for person_id, total, with_gps, unknown in opened.execute(query).all()}


def last_import(opened: Session, account_ids: set[int]) -> float | None:
    if not account_ids:
        return None
    query = (select(func.max(ImportRun.finished)).join(Export, Export.id == ImportRun.export_id)
             .where(Export.account_id.in_(account_ids), ImportRun.finished.is_not(None)))
    return opened.execute(query).scalar_one()


def account_state(account: GoogleAccount) -> dict[str, object]:
    return {"id": account.id, "email": account.email, "session_state": account.session_state, "session_checked": account.session_checked,
            "schedule_end": account.schedule_end}


def device_state(opened: Session, view: Viewing, device: Device) -> dict[str, object]:
    eligible, size = eligibility.counts(opened, view.settings, view.photos, device, view.now)
    return {"id": device.id, "name": device.name, "last_checkin": device.last_checkin, "pending_uploads": device.pending_uploads,
            "cleanup_enabled": device.cleanup_enabled, "eligible": eligible, "eligible_bytes": size, "eligible_checked": device.eligibility_checked_at}


def person_status(opened: Session, view: Viewing, person: Person, counts: tuple[int, int, int], alerts_open: list[Alert], unmatched: Mapping[str, int | None]
                  ) -> dict[str, object]:
    serving = accounts.accounts_serving(opened, person.id)
    people_accounts = [account for account in accounts.listing(opened) if account.id in serving]
    emails = {account.email for account in people_accounts}
    served_pixels = list(opened.execute(select(Pixel).join(PixelPerson, PixelPerson.pixel_id == Pixel.id).where(PixelPerson.person_id == person.id).order_by(Pixel.id)).scalars())
    devices = list(opened.execute(select(Device).where(Device.person_id == person.id, Device.revoked_at.is_(None)).order_by(Device.id)).scalars())
    concerning = [alert_dict(alert) for alert in alerts_open if alert.subject == person.slug or alert.subject in emails]
    return {
        "id": person.id,
        "slug": person.slug,
        "name": person.name,
        "indexed": counts[0],
        "with_gps": counts[1],
        "unknown_time": counts[2],
        "accounts": [account_state(account) for account in people_accounts],
        "pixels": [{"id": pixel.id, "name": pixel.name, "google_account_id": pixel.google_account_id} for pixel in served_pixels],
        "devices": [device_state(opened, view, device) for device in devices],
        "last_import": last_import(opened, serving),
        "unmatched": unmatched.get(person.slug),
        "alerts": concerning,
    }


def overview(opened: Session, settings: Settings, photos: Photos, now: float) -> dict[str, object]:
    view = Viewing(settings, photos, now)
    counts = photo_counts(opened)
    alerts_open = open_alerts(opened)
    unmatched = run.unmatched_by_person(opened)
    people = [person_status(opened, view, person, counts.get(person.id, (0, 0, 0)), alerts_open, unmatched) for person in opened.execute(select(Person).order_by(Person.id)).scalars()]
    return {"worker_last_seen": browser_jobs.worker_last_seen(opened), "people": people, "alerts": [alert_dict(alert) for alert in alerts_open]}
