from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.storage.models import GoogleAccount, Pixel, PixelPerson


def served_people(opened: Session, account_id: int) -> set[int]:
    account = opened.get(GoogleAccount, account_id)
    if account is None:
        return set()
    people = {account.default_person_id}
    rows = opened.execute(
        select(PixelPerson.person_id).join(Pixel, Pixel.id == PixelPerson.pixel_id).where(Pixel.google_account_id == account_id)
    ).scalars()
    people.update(rows)
    return people


def accounts_serving(opened: Session, person_id: int) -> set[int]:
    by_default = set(opened.execute(select(GoogleAccount.id).where(GoogleAccount.default_person_id == person_id)).scalars())
    by_pixel = set(
        opened.execute(
            select(Pixel.google_account_id).join(PixelPerson, PixelPerson.pixel_id == Pixel.id).where(PixelPerson.person_id == person_id)
        ).scalars()
    )
    return by_default | by_pixel
