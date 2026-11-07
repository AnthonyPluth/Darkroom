from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from darkroom import oidc
from darkroom.storage import db
from darkroom.storage.models import Device, Person, User
from darkroom.storage.paths import Photos
from tests.shared import fetch, own_database, settings_from, start_server

TOKEN = "device-token-canary-5d4c3b2a1f0e9d8c7b6a"
OTHER_TOKEN = "device-token-canary-other-0f1e2d3c4b5a69788"


class DeviceCase(unittest.TestCase):
    extra_env: dict[str, str] = {}

    def setUp(self):
        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        env = {"OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom", "DARKROOM_PUBLIC_URL": "https://darkroom.example",
               "DARKROOM_PHOTOS_ROOT": folder.name, **self.extra_env}
        self.base, self.settings = start_server(self, settings_from(**env), self.engine)
        self.photos = Photos(self.root)
        self.person_id, self.device_id = self.enrol("jo", "jo@example.test", TOKEN)
        self.replies: list[bytes] = []

    def enrol(self, slug: str, email: str, token: str, groups: tuple[str, ...] = ("family",)) -> tuple[int, int]:
        self.photos.ensure_person_tree(slug)
        with db.session(self.engine) as opened:
            person = opened.query(Person).filter_by(slug=slug).one_or_none()
            if person is None:
                person = Person(name=slug.title(), slug=slug, email=email, originals_path=f"{slug}/originals", google_only_path=f"{slug}/google-only", created=1.0)
                opened.add(person)
                opened.flush()
            if opened.get(User, f"sub-{slug}") is None:
                opened.add(User(sub=f"sub-{slug}", email=email, email_verified=True, groups=json.dumps(list(groups)), last_seen=time.time()))
            device = Device(person_id=person.id, name=f"{slug} iPhone", token_hash=oidc.hash_token(token), sub=f"sub-{slug}", created=1.0)
            opened.add(device)
            opened.flush()
            return person.id, device.id

    def call(self, method, path, body=None, token=TOKEN, headers=None):
        sent = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        status, _, reply = fetch(self.base, method, path, sent, data)
        self.replies.append(reply)
        return status, json.loads(reply) if reply else None
