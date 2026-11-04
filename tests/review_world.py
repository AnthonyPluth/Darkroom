from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path

from sqlalchemy import select

from darkroom.storage.models import DedupeDecision, MediaItem, Person, ReviewItem
from tests.import_world import World, tree
from tests.shared import add_person, fetch, settings_from, start_server

PAIR_NAME = "IMG_1005.JPG"
GOOGLE_ONLY_NAME = "IMG_2001.JPG"


class ReviewCase(unittest.TestCase):
    people: tuple[str, ...] = ("jo", "sam")
    pixel_people: tuple[str, ...] = ("jo", "sam")

    def setUp(self):
        self.world = World(self, people=self.people, pixel_people=self.pixel_people)
        settings = dataclasses.replace(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_PUBLIC_URL="http://127.0.0.1"),
                                       photos_root=self.world.settings.photos_root, data_dir=self.world.settings.data_dir,
                                       immich_url=self.world.settings.immich_url, immich_admin_key=self.world.settings.immich_admin_key)
        self.base, self.settings = start_server(self, settings, self.world.engine)
        self.export = self.world.hand_dropped()
        self.world.run_import(self.export)
        self.replies: list[bytes] = []

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json", "Origin": self.base} if method != "GET" else {}
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        status, received, reply = fetch(self.base, method, path, headers, data)
        self.replies.append(reply)
        if received.get("Content-Type", "").startswith("image/"):
            return status, reply
        return status, json.loads(reply) if reply else None

    @property
    def opened(self):
        self.world.opened.rollback()
        self.world.opened.expire_all()
        return self.world.opened

    def pending(self) -> ReviewItem:
        (found,) = self.opened.execute(select(ReviewItem).where(ReviewItem.status == "pending")).scalars()
        return found

    def decision_of(self, name: str) -> DedupeDecision:
        rows = self.opened.execute(select(DedupeDecision)).scalars()
        (found,) = [row for row in rows if row.source_path is not None and Path(row.source_path).name == name]
        return found

    def item_named(self, name: str, source: str = "takeout") -> MediaItem:
        rows = self.opened.execute(select(MediaItem).where(MediaItem.source == source)).scalars()
        (found,) = [row for row in rows if Path(row.path).name == name]
        return found

    def confirmed(self) -> dict[int, bool]:
        return {row.id: row.confirmed_in_google for row in self.opened.execute(select(MediaItem).order_by(MediaItem.id)).scalars()}

    def tree(self) -> dict[str, str]:
        return tree(self.world.root, ".thumbs")

    def hold(self) -> Path:
        return self.world.root / self.pending().hold_path

    def stranger(self, slug: str = "kim") -> int:
        person_id = add_person(self.world.opened, slug)
        self.world.opened.commit()
        self.world.photos.ensure_person_tree(slug)
        return person_id

    def person(self, slug: str) -> Person:
        found = self.opened.execute(select(Person).where(Person.slug == slug)).scalar_one()
        return found
