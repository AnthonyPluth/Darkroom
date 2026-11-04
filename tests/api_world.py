from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from darkroom.storage import db
from darkroom.storage.models import Device
from tests.shared import fetch, own_database, settings_from, start_server

SECRET_KEY = "api-world-secret-key-0123456789"


class ApiCase(unittest.TestCase):
    extra_env: dict[str, str] = {}

    def setUp(self):
        self.engine = own_database(self)
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        self.photos = Path(photos.name)
        env = {"DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_PHOTOS_ROOT": photos.name, "DARKROOM_SECRET_KEY": SECRET_KEY,
               "DARKROOM_PUBLIC_URL": "http://127.0.0.1", **self.extra_env}
        self.base, self.settings = start_server(self, settings_from(**env), self.engine)
        self.replies: list[bytes] = []

    def call(self, method, path, body=None, raw=None):
        headers = {"Content-Type": "application/json", "Origin": self.base} if method != "GET" else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None))
        status, _, reply = fetch(self.base, method, path, headers, data)
        self.replies.append(reply)
        return status, json.loads(reply) if reply else None

    def person(self, name="Jo Example", **extra):
        status, reply = self.call("POST", "/api/people", {"name": name, **extra})
        self.assertEqual(status, 201, reply)
        return reply["person"]

    def account(self, person_id, email="jo.photos@example.test"):
        status, reply = self.call("POST", "/api/accounts", {"email": email, "default_person_id": person_id})
        self.assertEqual(status, 201, reply)
        return reply["account"]

    def pixel(self, account_id, name="Pixel A", **extra):
        status, reply = self.call("POST", "/api/pixels", {"name": name, "google_account_id": account_id, **extra})
        self.assertEqual(status, 201, reply)
        return reply["pixel"]

    def add_device(self, person_id, name="Jo's iPhone", last_checkin=100.0, pending=3):
        with db.session(self.engine) as opened:
            device = Device(person_id=person_id, name=name, token_hash=f"hash-{person_id}-{name}", last_checkin=last_checkin, pending_uploads=pending, created=1.0)
            opened.add(device)
            opened.flush()
            return device.id
