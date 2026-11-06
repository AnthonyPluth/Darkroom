from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom.domain import alerts, browser_jobs, events, exports
from darkroom.storage.paths import Photos
from darkroom.storage.models import Alert, DedupeDecision, Export, GoogleAccount, MediaItem, Person, Pixel, PixelPerson

CONFIRMING_TIERS = (1, 2)


class NoSuchAccount(LookupError):
    pass


class NoSuchPerson(LookupError):
    pass


class EmailTaken(ValueError):
    pass


class InUse(ValueError):
    pass


class Busy(RuntimeError):
    pass


class StillConfirming(ValueError):
    pass


class JobOpen(ValueError):
    pass


def served_people(opened: Session, account_id: int) -> set[int]:
    return served_people_of(opened, {account_id}).get(account_id, set())


def served_people_of(opened: Session, account_ids: set[int]) -> dict[int, set[int]]:
    if not account_ids:
        return {}
    served = {row.id: {row.default_person_id} for row in opened.execute(select(GoogleAccount.id, GoogleAccount.default_person_id).where(GoogleAccount.id.in_(account_ids)))}
    pixels = select(Pixel.google_account_id, PixelPerson.person_id).join(Pixel, Pixel.id == PixelPerson.pixel_id).where(Pixel.google_account_id.in_(list(served)))
    for account_id, person_id in opened.execute(pixels):
        served[account_id].add(person_id)
    return served


def served_by_pixels(opened: Session, account_id: int, except_pixel_id: int | None = None) -> set[int]:
    query = select(PixelPerson.person_id).join(Pixel, Pixel.id == PixelPerson.pixel_id).where(Pixel.google_account_id == account_id)
    if except_pixel_id is not None:
        query = query.where(Pixel.id != except_pixel_id)
    return set(opened.execute(query).scalars())


def confirmed_people(opened: Session, account_id: int) -> set[int]:
    rows = opened.execute(
        select(DedupeDecision.person_id, MediaItem.person_id).join(MediaItem, MediaItem.id == DedupeDecision.matched_item_id).where(
            DedupeDecision.account_id == account_id, DedupeDecision.reversed_at.is_(None), DedupeDecision.decided_by == "auto",
            DedupeDecision.tier.in_(CONFIRMING_TIERS), MediaItem.source == "phone",
        )
    ).all()
    return {person_id for row in rows for person_id in row}


def require_confirmations_kept(opened: Session, account_id: int, still_served: set[int]) -> None:
    lost = (served_people(opened, account_id) - still_served) & confirmed_people(opened, account_id)
    if not lost:
        return
    account = get(opened, account_id)
    who = sorted(opened.execute(select(Person.name).where(Person.id.in_(lost))).scalars())
    raise StillConfirming(
        f"{account.email} has matched photos of {' and '.join(who)} in Google Photos, which count as backed up; "
        "it has to keep serving them until those matches are reversed"
    )


def accounts_serving(opened: Session, person_id: int) -> set[int]:
    by_default = set(opened.execute(select(GoogleAccount.id).where(GoogleAccount.default_person_id == person_id)).scalars())
    by_pixel = set(
        opened.execute(
            select(Pixel.google_account_id).join(PixelPerson, PixelPerson.pixel_id == Pixel.id).where(PixelPerson.person_id == person_id)
        ).scalars()
    )
    return by_default | by_pixel


def get(opened: Session, account_id: int) -> GoogleAccount:
    account = opened.get(GoogleAccount, account_id)
    if account is None:
        raise NoSuchAccount(f"no Google account {account_id}")
    return account


def listing(opened: Session) -> list[GoogleAccount]:
    return list(opened.execute(select(GoogleAccount).order_by(GoogleAccount.id)).scalars())


def as_dict(opened: Session, account: GoogleAccount) -> dict[str, object]:
    return {
        "id": account.id,
        "email": account.email,
        "default_person_id": account.default_person_id,
        "session_state": account.session_state,
        "session_checked": account.session_checked,
        "schedule_created": account.schedule_created,
        "schedule_end": account.schedule_end,
        "served_people": sorted(served_people(opened, account.id)),
        "created": account.created,
    }


def require_person(opened: Session, person_id: int) -> None:
    if opened.get(Person, person_id) is None:
        raise NoSuchPerson(f"no person {person_id}")


def require_free_email(opened: Session, email: str, except_id: int | None = None) -> None:
    wanted = email.casefold()
    rows = opened.execute(select(GoogleAccount.id, GoogleAccount.email)).all()
    if any(account_id != except_id and existing.casefold() == wanted for account_id, existing in rows):
        raise EmailTaken("there is already a Google account with that email")


def create(opened: Session, email: str, default_person_id: int, now: float) -> GoogleAccount:
    require_person(opened, default_person_id)
    require_free_email(opened, email)
    account = GoogleAccount(email=email, default_person_id=default_person_id, created=now)
    opened.add(account)
    opened.flush()
    events.record(opened, now, "account_created", str(account.id))
    return account


def follow_alerts(opened: Session, old: str, new: str) -> None:
    for alert in opened.execute(select(Alert).where(Alert.closed.is_(None), Alert.subject == old)).scalars():
        alert.subject = new
        alert.message = alert.message.replace(old, new)


def lock(opened: Session, account_id: int) -> GoogleAccount:
    try:
        return browser_jobs.lock_account(opened, account_id)
    except browser_jobs.UnknownAccount:
        raise NoSuchAccount(f"no Google account {account_id}") from None


def update(opened: Session, account_id: int, email: str | None, default_person_id: int | None, now: float) -> GoogleAccount:
    account = lock(opened, account_id)
    if email is not None:
        require_free_email(opened, email, account.id)
        if email != account.email and browser_jobs.has_open_onboarding_job(opened, account.id):
            raise JobOpen("a connect, schedule or renewal job is still open for this account; change the email once it has finished")
        if email != account.email and browser_jobs.unchecked_schedule(opened, account.id) is not None:
            raise JobOpen("the last schedule or renewal attempt may have made a schedule in this Google account; answer the Takeout check on the Exports page before changing the email")
        if email != account.email and exports.download_underway(opened, account.id):
            raise JobOpen("a Takeout export is still being downloaded for this account; change the email once it has finished")
    if default_person_id is not None:
        require_person(opened, default_person_id)
        require_confirmations_kept(opened, account.id, {default_person_id} | served_by_pixels(opened, account.id))
        if default_person_id != account.default_person_id and exports.download_underway(opened, account.id):
            raise JobOpen("a Takeout export is still being downloaded for this account; change the default person once it has finished")
    if email is not None:
        if email != account.email:
            follow_alerts(opened, account.email, email)
        account.email = email
    if default_person_id is not None:
        if default_person_id != account.default_person_id:
            exports.pin_owners(opened, account)
        account.default_person_id = default_person_id
    opened.flush()
    events.record(opened, now, "account_updated", str(account.id))
    return account


def delete(opened: Session, photos: Photos, account_id: int, now: float) -> None:
    try:
        opened.execute(sql_update(GoogleAccount).where(GoogleAccount.id == account_id).values(id=GoogleAccount.id))
    except OperationalError as error:
        if "database is locked" not in str(error.orig):
            raise
        raise Busy("Darkroom is busy with another change; try deleting the account again in a moment") from None
    account = get(opened, account_id)
    found = []
    if opened.execute(select(func.count(Pixel.id)).where(Pixel.google_account_id == account_id)).scalar_one():
        found.append("Pixels")
    if opened.execute(select(func.count(Export.id)).where(Export.account_id == account_id)).scalar_one():
        found.append("exports")
    if found:
        raise InUse(f"{account.email} still has {' and '.join(found)}; remove those first")
    hold = browser_jobs.holds_profile(opened, account_id, now)
    if hold == "open":
        raise InUse(f"{account.email} has a browser job that has not finished; wait for it to finish, then delete the account")
    if hold == "lease_expired":
        raise InUse(f"{account.email} had a browser job that failed when its worker stopped reporting less than {int(browser_jobs.LEASE_SECONDS // 60)} minutes ago, "
                    "and that worker may still be closing its browser; try again in a few minutes")
    email = account.email
    opened.delete(account)
    opened.flush()
    alerts.close(opened, browser_jobs.sign_in_key(account_id), now)
    for alert in opened.execute(select(Alert).where(Alert.closed.is_(None), Alert.subject == email)).scalars():
        alert.closed = now
    photos.retire_account_downloads(str(account_id))
    photos.retire_browser_account(str(account_id))
    events.record(opened, now, "account_deleted", str(account_id))
