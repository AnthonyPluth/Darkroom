from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote, urlsplit

from darkroom import oidc
from darkroom.storage import db
from darkroom.storage.models import Device, Person, User
from darkroom.storage.paths import Photos
from tests.shared import fetch, own_database, settings_from, start_server

TOKEN = "device-token-canary-5d4c3b2a1f0e9d8c7b6a"
OTHER_TOKEN = "device-token-canary-other-0f1e2d3c4b5a69788"
UPLOAD_ROUTE = "/api/device/assets/{id}/resources/{kind}"
ASSET = "6F1E2D3C-AAAA-BBBB-CCCC-0123456789AB/L0/001"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def resource(kind: str, data: bytes, filename: str) -> dict[str, object]:
    return {"kind": kind, "sha1": sha1(data), "bytes": len(data), "filename": filename}


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
        self.originals = self.photos.originals("jo")
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
            device = Device(person_id=person.id, name=f"{slug} iPhone", token_hash=oidc.hash_token(token), created=1.0)
            opened.add(device)
            opened.flush()
            return person.id, device.id

    def call(self, method, path, body=None, token=TOKEN, headers=None):
        sent = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        status, _, reply = fetch(self.base, method, path, sent, data)
        self.replies.append(reply)
        return status, json.loads(reply) if reply else None

    def check(self, assets, token=TOKEN):
        return self.call("POST", "/api/device/assets/check", {"assets": assets}, token)

    def put(self, data, kind="photo", filename="IMG_0001.JPG", asset=ASSET, digest=None, token=TOKEN, length=None):
        headers = {"X-Darkroom-SHA1": digest or sha1(data if isinstance(data, bytes) else b""), "X-Darkroom-Filename": filename,
                   "Content-Type": "application/octet-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if length is not None:
            headers["Content-Length"] = str(length)
        status, _, reply = fetch(self.base, "PUT", UPLOAD_ROUTE.format(id=quote(asset, safe=""), kind=kind), headers, data)
        self.replies.append(reply)
        return status, json.loads(reply) if reply else None

    def files_under_originals(self) -> list[Path]:
        return sorted(path for path in self.originals.rglob("*") if path.is_file())

    def raw_put(self, head_length, sent, wait=0.0, chunked=False):
        address = urlsplit(self.base)
        path = UPLOAD_ROUTE.format(id=quote(ASSET, safe=""), kind="photo")
        length = "Transfer-Encoding: chunked" if chunked else f"Content-Length: {head_length}"
        head = (f"PUT {path} HTTP/1.1\r\nHost: {address.netloc}\r\nAuthorization: Bearer {TOKEN}\r\nX-Darkroom-SHA1: {sha1(b'x' * head_length)}\r\n"
                f"X-Darkroom-Filename: IMG_0001.JPG\r\n{length}\r\n\r\n").encode()
        with socket.create_connection((address.hostname, address.port), timeout=10) as connection:
            connection.sendall(head + sent)
            if wait:
                time.sleep(wait)
            else:
                connection.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := connection.recv(65536):
                reply += chunk
        return int(reply.split(b" ", 2)[1])
