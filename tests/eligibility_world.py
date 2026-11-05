from __future__ import annotations

import hashlib
import itertools
import json
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import MetaData, select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, eligibility
from darkroom.storage.models import DedupeDecision, Device, Export, GoogleAccount, MediaItem, Person, PhoneAsset, PhoneResource, Pixel, PixelPerson, User
from darkroom.storage.paths import Photos
from tests.shared import own_database, settings_from

FILENAMES = {"photo": "IMG_{n:04d}.HEIC", "live_video": "IMG_{n:04d}.MOV", "adjusted": "IMG_{n:04d}_adjusted.HEIC"}
IGNORED = {("devices", "eligibility_checked_at"), ("devices", "last_checkin")}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


@dataclass
class Photo:
    asset: PhoneAsset
    items: dict[str, MediaItem]
    rows: dict[str, PhoneResource]
    decisions: list[DedupeDecision] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.asset.photokit_id


def reported_of(opened: Session, asset: PhoneAsset) -> eligibility.Reported:
    rows = opened.execute(select(PhoneResource).where(PhoneResource.asset_id == asset.id).order_by(PhoneResource.id)).scalars()
    return eligibility.Reported(asset.photokit_id, tuple(eligibility.ReportedResource(row.kind, row.sha1, row.bytes, row.filename) for row in rows))


class Household:
    def __init__(self, test: unittest.TestCase, opened: Session | None = None, root: Path | None = None) -> None:
        if opened is None:
            self.engine = own_database(test)
            opened = Session(self.engine, expire_on_commit=False)
            test.addCleanup(opened.close)
        self.opened = opened
        if root is None:
            folder = tempfile.TemporaryDirectory()
            test.addCleanup(folder.cleanup)
            root = Path(folder.name)
        self.photos = Photos(root)
        self.settings = settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom", DARKROOM_PUBLIC_URL="https://darkroom.example",
                                      DARKROOM_PHOTOS_ROOT=str(root))
        self.now = time.time()
        self.counter = itertools.count(1)

    def person(self, slug: str) -> Person:
        self.photos.ensure_person_tree(slug)
        person = self.opened.execute(select(Person).where(Person.slug == slug)).scalar_one_or_none()
        if person is None:
            person = Person(name=slug.title(), slug=slug, email=f"{slug}@example.test", originals_path=f"{slug}/originals",
                            google_only_path=f"{slug}/google-only", created=1.0)
            self.opened.add(person)
            self.opened.flush()
        if self.opened.get(User, f"sub-{slug}") is None:
            self.opened.add(User(sub=f"sub-{slug}", email=person.email, email_verified=True, groups=json.dumps(["family"]), last_seen=self.now))
            self.opened.flush()
        return person

    def device(self, person: Person, cleanup: bool = True) -> Device:
        device = Device(person_id=person.id, name=f"{person.slug} iPhone", token_hash=f"hash-{next(self.counter)}", sub=f"sub-{person.slug}",
                        cleanup_enabled=cleanup, created=1.0)
        self.opened.add(device)
        self.opened.flush()
        return device

    def account(self, default: Person, pixel_people: tuple[Person, ...] = ()) -> GoogleAccount:
        account = GoogleAccount(email=f"account{next(self.counter)}@example.test", default_person_id=default.id, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        if pixel_people:
            pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
            self.opened.add(pixel)
            self.opened.flush()
            self.opened.add_all(PixelPerson(pixel_id=pixel.id, person_id=person.id) for person in pixel_people)
            self.opened.flush()
        return account

    def export(self, account: GoogleAccount) -> Export:
        export = Export(account_id=account.id, kind="manual", status="imported")
        self.opened.add(export)
        self.opened.flush()
        return export

    def item(self, person: Person, kind: str, number: int) -> MediaItem:
        name = FILENAMES[kind].format(n=number)
        data = f"{kind}-{person.slug}-{number}-made-up-bytes".encode()
        path = self.photos.originals(person.slug) / name
        path.write_bytes(data)
        item = MediaItem(person_id=person.id, source="phone", path=f"{person.slug}/originals/{name}", sha1=sha1(data), bytes=len(data), indexed_at=1.0)
        self.opened.add(item)
        self.opened.flush()
        return item

    def photo(self, device: Device, kinds: tuple[str, ...] = ("photo",), through: GoogleAccount | None = None, tier: int = 1,
              decided_by: dedupe.DecidedBy = "auto", unmatched: tuple[str, ...] = ()) -> Photo:
        person = self.opened.get(Person, device.person_id)
        assert person is not None
        number = next(self.counter)
        asset = PhoneAsset(device_id=device.id, photokit_id=f"ASSET-{number:04d}/L0/001", created=1.0)
        self.opened.add(asset)
        self.opened.flush()
        photo = Photo(asset, {}, {})
        for kind in kinds:
            item = self.item(person, kind, number)
            row = PhoneResource(asset_id=asset.id, kind=kind, sha1=item.sha1, bytes=item.bytes, filename=Path(item.path).name, uploaded_at=2.0,
                                media_item_id=item.id)
            self.opened.add(row)
            self.opened.flush()
            photo.items[kind], photo.rows[kind] = item, row
            if through is not None and kind not in unmatched:
                photo.decisions.append(self.match(item, through, tier, decided_by))
        return photo

    def match(self, item: MediaItem, account: GoogleAccount, tier: int = 1, decided_by: dedupe.DecidedBy = "auto") -> DedupeDecision:
        export = self.export(account)
        action: dedupe.Action = "skip" if tier in (1, 2, 3) else "review" if tier == 4 else "google_only"
        decision = dedupe.Decision(source_path=f"Takeout/Google Photos/item-{next(self.counter)}", account_id=account.id, person_id=item.person_id,
                                   tier=tier, action=action, reason="made up", matched_item_id=item.id if tier < 5 else None,
                                   matched_source="phone" if tier < 5 else None, decided_by=decided_by)
        return dedupe.apply(self.opened, decision, export, self.now)

    def reported(self, photo: Photo) -> eligibility.Reported:
        return reported_of(self.opened, photo.asset)

    def eligible(self, device: Device, *photos: Photo) -> eligibility.Evaluation:
        return eligibility.eligible_assets(self.opened, self.settings, self.photos, device.id, [self.reported(photo) for photo in photos], self.now)

    def confirm(self, device: Device, *photos: Photo) -> tuple[list[eligibility.Eligible], list[eligibility.Rejected]]:
        return eligibility.confirm(self.opened, self.settings, self.photos, device.id, [self.reported(photo) for photo in photos], self.now)

    def state(self) -> dict[str, list[tuple[object, ...]]]:
        self.opened.flush()
        meta = MetaData()
        connection = self.opened.connection()
        meta.reflect(connection)
        out: dict[str, list[tuple[object, ...]]] = {}
        for table in meta.sorted_tables:
            if table.name == "events":
                continue
            columns = [column for column in table.columns if (table.name, column.name) not in IGNORED]
            out[table.name] = sorted((tuple(row) for row in connection.execute(select(*columns))), key=repr)
        return out

    def files(self) -> dict[str, str]:
        return {str(path.relative_to(self.photos.root)): sha1(path.read_bytes()) for path in sorted(self.photos.root.rglob("*")) if path.is_file()}
