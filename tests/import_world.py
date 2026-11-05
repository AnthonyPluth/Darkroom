from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import alerts, index
from darkroom.domain.ingest import run, unpack
from darkroom.providers import immich
from darkroom.storage.models import (
    Alert, DedupeDecision, Export, ExportPart, GoogleAccount, ImportRun, MediaItem, Person, Pixel, PixelPerson, ReviewItem,
)
from darkroom.storage.paths import Photos
from tests.fakes.immich import FakeImmich, FakePerson
from tests.fixtures import media as fixtures
from tests.shared import add_person, own_database, settings_from
from tests.takeout_fixtures import TakeoutBuilder

NOW = 1_800_000_000.0
DAY = 24 * 3600.0
INDEXED = NOW - 30 * DAY
ADMIN_KEY = "fake-immich-admin-key-canary"


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()


def tree(root: Path, *skipped: str) -> dict[str, str]:
    return {str(p.relative_to(root)): sha1_of(p) for p in sorted(root.rglob("*"))
            if p.is_file() and not any(part in skipped for part in p.relative_to(root).parts)}


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, settings: object, title: str, message: str, link: str | None = None) -> str:
        self.sent.append((title, message))
        return alerts.SENT


class World:
    def __init__(self, test: unittest.TestCase, people: tuple[str, ...] = ("jo", "sam"), pixel_people: tuple[str, ...] = ("jo", "sam"),
                 immich_configured: bool = True) -> None:
        self.test = test
        self.people = people
        folder = tempfile.TemporaryDirectory()
        test.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.root = self.base / "photos"
        self.root.mkdir()
        self.photos = Photos(self.root)
        self.engine = own_database(test)
        self.opened = Session(self.engine, expire_on_commit=False)
        test.addCleanup(lambda: self.opened.close())
        self.ids = {slug: add_person(self.opened, slug) for slug in self.people}
        for slug in self.people:
            person = self.opened.get(Person, self.ids[slug])
            assert person is not None
            person.immich_user_id = f"u-{slug}"
            self.photos.ensure_person_tree(slug)
        account = GoogleAccount(email="family@x.test", default_person_id=self.ids[self.people[0]], created=1.0)
        self.opened.add(account)
        self.opened.flush()
        self.account = account.id
        self.pixel: int | None = None
        if pixel_people:
            pixel = Pixel(name="Hall Pixel", google_account_id=account.id, created=1.0)
            self.opened.add(pixel)
            self.opened.flush()
            self.pixel = pixel.id
            self.opened.add_all(PixelPerson(pixel_id=pixel.id, person_id=self.ids[slug]) for slug in pixel_people)
        self.opened.commit()
        self.fake = FakeImmich([FakePerson(slug, f"u-{slug}", f"lib-{slug}", f"key-{slug}") for slug in self.people], ADMIN_KEY).start()
        test.addCleanup(self.fake.stop)
        immich_env = {"IMMICH_URL": self.fake.url, "IMMICH_ADMIN_KEY": ADMIN_KEY} if immich_configured else {}
        self.settings = settings_from(DARKROOM_PHOTOS_ROOT=str(self.root), DARKROOM_DATA_DIR=str(self.base / "data"), **immich_env)
        self.recorder = Recorder()
        self.tools = run.Tools(notifier=self.recorder)

    def reopen(self) -> Session:
        self.opened.rollback()
        self.opened.close()
        self.opened = Session(self.engine, expire_on_commit=False)
        return self.opened

    def original(self, slug: str, relative: str, data: bytes) -> Path:
        path = self.root / slug / "originals" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def index_all(self, at: float = INDEXED) -> None:
        for slug in self.people:
            person = self.opened.get(Person, self.ids[slug])
            assert person is not None
            index.index_person(self.opened, self.photos, person, at)
        self.opened.commit()

    def scratch_jpeg(self, name: str, **tags: object) -> bytes:
        path = fixtures.make_jpeg(self.base / "scratch" / name, **tags)
        data = path.read_bytes()
        path.unlink()
        return data

    def tiered_export(self, folder: Path) -> list[Path]:
        builder = TakeoutBuilder(folder, seed=3, parts=2)
        exact = builder.image_bytes(".JPG")
        self.original("jo", "IMG_1001.JPG", exact)
        builder.photo("IMG_1001.JPG", data=exact, gps=None)
        shared = builder.image_bytes(".JPG")
        self.original("sam", "2019/IMG_1002.JPG", shared)
        builder.photo("IMG_1002.JPG", data=shared, part=1)
        moment = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
        self.original("jo", "IMG_1003.JPG", self.scratch_jpeg("t2a.jpg", colour=(10, 120, 30), **moment))
        builder.photo("IMG_1003.JPG", data=self.scratch_jpeg("t2b.jpg", colour=(12, 118, 33), **moment), gps=None)
        edited_of = builder.image_bytes(".JPG")
        self.original("sam", "IMG_1004.JPG", edited_of)
        builder.photo("IMG_1004.JPG", data=edited_of)
        builder.add("IMG_1004-edited.JPG", builder.image_bytes(".JPG"), "Photos from 2019", 1, None, None, None, "")
        near = {"offset": "+02:00", "make": "Apple", "model": "iPhone 11", "colour": (90, 90, 200)}
        self.original("jo", "IMG_1005.JPG", self.scratch_jpeg("t4a.jpg", taken="2019:08:01 10:00:00", **near))
        builder.photo("IMG_1005.JPG", data=self.scratch_jpeg("t4b.jpg", taken="2019:08:01 10:00:01", **near), gps=None)
        builder.photo("IMG_2001.JPG", gps=(45.4, 12.3), description="A made-up canal")
        builder.photo("IMG_2002.JPG", gps=None, part=1)
        self.original("jo", "IMG_3001.JPG", builder.image_bytes(".JPG"))
        return builder.build().parts

    def hand_dropped(self) -> Export:
        self.tiered_export(self.photos.manual_inbox(self.people[0]))
        self.index_all()
        person = self.opened.get(Person, self.ids[self.people[0]])
        assert person is not None
        exports = unpack.inspect_manual_inbox(self.opened, self.photos, person, NOW)
        self.opened.commit()
        self.test.assertEqual(len(exports), 1)
        return exports[0]

    def downloaded(self, owner: str | None = None) -> Export:
        inbox = self.photos.inbox(self.people[0]) / "google-123"
        parts = self.tiered_export(inbox)
        self.index_all()
        export = Export(account_id=self.account, kind="initial", google_id="google-123", created_at=NOW - DAY, ready_at=NOW,
                        expires_at=NOW + 7 * DAY, part_count=len(parts), status="downloaded", inbox_dir=str(inbox),
                        owner_person_id=None if owner is None else self.ids[owner])
        self.opened.add(export)
        self.opened.flush()
        self.opened.add_all(ExportPart(export_id=export.id, number=number, filename=part.name, bytes=part.stat().st_size, status="done")
                            for number, part in enumerate(parts, start=1))
        self.opened.commit()
        return export

    def run_import(self, export: Export | int, at: float = NOW) -> ImportRun:
        found = self.opened.get(Export, export if isinstance(export, int) else export.id)
        assert found is not None
        return run.import_export(self.opened, self.settings, found, at, self.tools)

    def scanned(self) -> list[str]:
        return [scan.library_id for scan in self.fake.scans()]

    def sent(self, title: str) -> int:
        return sum(1 for sent_title, _ in self.recorder.sent if sent_title == title)

    def state(self) -> dict[str, object]:
        def rows(query: Any) -> list[tuple[object, ...]]:
            return [tuple(row) for row in self.opened.execute(query).all()]

        return {
            "tree": tree(self.root, "manual", "google-123", "parts"),
            "decisions": rows(select(DedupeDecision.source_path, DedupeDecision.tier, DedupeDecision.person_id, DedupeDecision.matched_item_id,
                                     DedupeDecision.takeout_item_id, DedupeDecision.decided_by).order_by(DedupeDecision.source_path)),
            "items": rows(select(MediaItem.person_id, MediaItem.source, MediaItem.path, MediaItem.sha1, MediaItem.confirmed_in_google).order_by(MediaItem.path)),
            "reviews": rows(select(ReviewItem.status, ReviewItem.hold_path).order_by(ReviewItem.id)),
            "runs": rows(select(ImportRun.status, ImportRun.stage, ImportRun.added, ImportRun.skipped_t1, ImportRun.skipped_t2, ImportRun.skipped_t3,
                                ImportRun.review, ImportRun.missing_gps, ImportRun.failed, ImportRun.unmatched_by_person, ImportRun.changed_people)),
            "alerts": rows(select(Alert.kind, Alert.key, Alert.message, Alert.closed.is_(None)).order_by(Alert.key)),
            "exports": rows(select(Export.status, Export.stage)),
            "scans": sorted(self.scanned()),
            "summaries": self.sent("Takeout import finished"),
        }

    def immich_tools(self) -> run.Tools:
        return run.Tools(immich=immich.Client(self.fake.url, ADMIN_KEY, timeout=5.0), notifier=self.recorder)

    def open_alerts(self, kind: str) -> list[Alert]:
        return list(self.opened.execute(select(Alert).where(Alert.kind == kind, Alert.closed.is_(None)).order_by(Alert.id)).scalars())

    def count(self, model: type[object]) -> int:
        return int(self.opened.execute(select(func.count()).select_from(model)).scalar_one())
