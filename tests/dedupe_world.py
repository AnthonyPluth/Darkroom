from __future__ import annotations

import dataclasses
import itertools
import unittest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe
from darkroom.storage.models import DedupeDecision, Export, GoogleAccount, MediaItem, Pixel, PixelPerson, ReviewItem
from tests.shared import add_person, own_database

MOMENT = 1_700_000_000.0
LOCAL = "2023-11-14T22:13:20"
PHASH = "f0f0f0f0f0f0f0f0"


def flip(phash: str, bits: int) -> str:
    return f"{int(phash, 16) ^ ((1 << bits) - 1):016x}"


class World:
    def __init__(self, test: unittest.TestCase) -> None:
        engine = own_database(test)
        self.opened = Session(engine, expire_on_commit=False)
        test.addCleanup(self.opened.close)
        self.counter = itertools.count(1)

    def person(self, slug: str) -> int:
        return add_person(self.opened, slug)

    def account(self, email: str, default: int, pixel_people: tuple[int, ...] = ()) -> int:
        account = GoogleAccount(email=email, default_person_id=default, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        if pixel_people:
            pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
            self.opened.add(pixel)
            self.opened.flush()
            self.opened.add_all(PixelPerson(pixel_id=pixel.id, person_id=person) for person in pixel_people)
            self.opened.flush()
        return account.id

    def export(self, account_id: int) -> Export:
        export = Export(account_id=account_id, kind="manual")
        self.opened.add(export)
        self.opened.flush()
        return export

    def item(self, person_id: int, source: str = "phone", suffix: str = ".HEIC", export_id: int | None = None, **fields: object) -> MediaItem:
        number = next(self.counter)
        values: dict[str, object] = dict(
            person_id=person_id, source=source, path=f"p{person_id}/IMG_{number:04d}{suffix}", sha1=f"{number:040x}", bytes=100,
            phash=PHASH, captured_utc=MOMENT, captured_local=LOCAL, offset_minutes=0, time_source="exif_offset",
            width=4032, height=3024, duration_ms=None, make="Apple", model="iPhone 15 Pro", export_id=export_id, indexed_at=1.0,
        )
        values.update(fields)
        item = MediaItem(**values)
        self.opened.add(item)
        self.opened.flush()
        return item

    def candidate(self, **fields: object) -> dedupe.TakeoutCandidate:
        number = next(self.counter)
        base = dedupe.TakeoutCandidate(
            source_path=f"Takeout/Google Photos/Photos from 2023/IMG_{number:04d}.HEIC", sha1=f"{number + 10**9:040x}", phash=PHASH,
            captured_utc=MOMENT, captured_local=LOCAL, width=4032, height=3024, duration_ms=None,
            make="Apple", model="iPhone 15 Pro", is_video=False,
        )
        return dataclasses.replace(base, **fields)

    def index(self, account_id: int) -> dedupe.PersonIndex:
        return dedupe.PersonIndex.load(self.opened, account_id)

    def decisions(self) -> list[DedupeDecision]:
        return list(self.opened.execute(select(DedupeDecision).order_by(DedupeDecision.id)).scalars())

    def reviews(self) -> list[ReviewItem]:
        return list(self.opened.execute(select(ReviewItem).order_by(ReviewItem.id)).scalars())

    def confirmed(self) -> set[int]:
        return set(self.opened.execute(select(MediaItem.id).where(MediaItem.confirmed_in_google.is_(True))).scalars())

    def count(self, model: type[object]) -> int:
        return int(self.opened.execute(select(func.count()).select_from(model)).scalar_one())
