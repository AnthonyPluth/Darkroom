from __future__ import annotations

import argparse
import base64
import email.parser
import email.policy
import hashlib
import html
import io
import json
import re
import uuid
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import ExifTags, Image

HOST = "127.0.0.1"
MODES = ("ok", "down", "slow", "401", "500")
VERSION = "v1.130.0"
SLOW_SECONDS = 2.0
NOT_READABLE = {"message": "Not found or no asset.read access", "error": "Bad Request", "statusCode": 400}


@dataclass(frozen=True)
class FakePerson:
    slug: str
    user_id: str
    library_id: str
    key: str
    name: str = ""
    email: str = ""
    import_paths: tuple[str, ...] = ()
    exclusion_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scan:
    library_id: str
    at: float
    key_role: str


@dataclass
class FakeAsset:
    id: str
    owner_id: str
    data: bytes
    name: str = "IMG_0001.JPG"
    kind: str = "IMAGE"
    updated_at: str = "2026-01-01T00:00:00.000Z"
    visibility: str = "timeline"
    live_video_id: str | None = None
    library_id: str | None = None
    trashed: bool = False
    metadata: tuple[str, ...] = ("mobile-app",)
    checksum: str | None = None
    exif: dict[str, object] | None = None
    ready_at: float = 0.0
    offline: bool = False
    sidecar: bytes | None = None

    def json(self) -> dict[str, object]:
        checksum = self.checksum or base64.b64encode(hashlib.sha1(self.data, usedforsecurity=False).digest()).decode()
        found: dict[str, object] = {
            "id": self.id, "ownerId": self.owner_id, "libraryId": self.library_id, "checksum": checksum, "type": self.kind,
            "originalFileName": self.name, "visibility": self.visibility, "livePhotoVideoId": self.live_video_id,
            "isTrashed": self.trashed, "isOffline": self.offline, "updatedAt": self.updated_at,
        }
        if self.exif is not None and time.monotonic() >= self.ready_at:
            found["exifInfo"] = self.exif
            found["duration"] = 300 if self.kind == "VIDEO" else None
        return found


def extract(data: bytes, sidecar: bytes | None) -> dict[str, object]:
    found: dict[str, object] = {"dateTimeOriginal": None, "timeZone": None, "latitude": None, "longitude": None, "description": None}
    try:
        exif = Image.open(io.BytesIO(data)).getexif()
    except OSError:
        exif = Image.Exif()
    tags, gps = exif.get_ifd(ExifTags.IFD.Exif), exif.get_ifd(ExifTags.IFD.GPSInfo)
    if exif.get(ExifTags.Base.ImageDescription):
        found["description"] = str(exif[ExifTags.Base.ImageDescription])
    if ExifTags.Base.DateTimeOriginal in tags:
        offset = tags.get(ExifTags.Base.OffsetTimeOriginal, "+00:00")
        taken = datetime.strptime(tags[ExifTags.Base.DateTimeOriginal] + offset, "%Y:%m:%d %H:%M:%S%z")
        found.update(dateTimeOriginal=taken.astimezone(UTC).isoformat().replace("+00:00", "Z"), timeZone=f"UTC{offset}")
    if 2 in gps and 4 in gps:
        lat, lon = (sum(float(p) / 60 ** n for n, p in enumerate(gps[i])) for i in (2, 4))
        found.update(latitude=-lat if gps.get(1) == "S" else lat, longitude=-lon if gps.get(3) == "W" else lon)
    text = (sidecar or b"").decode("utf-8", "replace")
    if taken_text := re.search(r'exif:DateTimeOriginal="([^"]+)"', text):
        taken = datetime.fromisoformat(taken_text.group(1))
        found.update(dateTimeOriginal=taken.astimezone(UTC).isoformat().replace("+00:00", "Z"), timeZone=f"UTC{taken.strftime('%z')}")
    for name, tag in (("latitude", "GPSLatitude"), ("longitude", "GPSLongitude")):
        if value := re.search(rf'exif:{tag}="(\d+),([\d.]+)([NSEW])"', text):
            found[name] = (int(value.group(1)) + float(value.group(2)) / 60) * (-1 if value.group(3) in "SW" else 1)
    if description := re.search(r'<rdf:li xml:lang="x-default">([^<]*)</rdf:li>', text):
        found["description"] = html.unescape(description.group(1))
    return found


@dataclass
class State:
    admin_key: str
    people: list[FakePerson]
    mode: str = "ok"
    recorded: list[Scan] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    slow_seconds: float = SLOW_SECONDS
    assets: list[FakeAsset] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    user_agents: list[str] = field(default_factory=list)
    page_size: int = 250
    searches: int = 0
    on_search: Callable[[int], None] | None = None
    version: str = VERSION
    permissions: tuple[str, ...] = ("user.read", "server.about", "asset.read", "asset.download")
    admins: tuple[str, ...] = ()
    quotas: dict[str, int] = field(default_factory=dict)
    extraction_delay: float = 0.0
    intercept: Callable[[str, str, str], int | None] | None = None
    drop_fields: tuple[str, ...] = ()
    refused_agents: tuple[str, ...] = ()
    missing_status: int = 400
    shared: set[str] = field(default_factory=set)
    search_shared: bool = False
    lose_uploads: int = 0
    upload_answer: dict[str, object] | None = None


def library_json(person: FakePerson) -> dict[str, object]:
    return {
        "id": person.library_id, "ownerId": person.user_id, "name": person.slug,
        "importPaths": list(person.import_paths), "exclusionPatterns": list(person.exclusion_patterns),
        "assetCount": 0, "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z", "refreshedAt": None,
    }


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, body: object | None = None) -> None:
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if payload:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def who(self) -> tuple[str, FakePerson | None]:
            key = self.headers.get("x-api-key", "")
            if key and key == state.admin_key:
                return "admin", None
            for person in state.people:
                if key and key == person.key:
                    return f"person:{person.slug}", person
            return "", None

        def drop(self) -> None:
            self.close_connection = True
            self.connection.close()

        def handle_any(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self.body = body
            path = self.path.split("?", 1)[0]
            if path == "/__fake/mode" and self.command == "POST":
                wanted = json.loads(body or b"{}").get("mode")
                if wanted not in MODES:
                    self.reply(400, {"error": "unknown mode"})
                    return
                state.mode = wanted
                self.reply(200, {"mode": wanted})
                return
            if state.mode == "down":
                self.drop()
                return
            if state.mode == "slow":
                time.sleep(state.slow_seconds)
            if state.mode == "500":
                self.reply(500, {"message": "Internal server error"})
                return
            if state.mode == "401":
                self.reply(401, {"message": "Invalid API key"})
                return
            self.route(path)

        def raw(self, data: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def search(self, person: FakePerson, body: bytes) -> None:
            asked = json.loads(body or b"{}")
            state.searches += 1
            if state.on_search is not None:
                state.on_search(state.searches)
            window = asked.get("filter", {}).get("updatedAt", {})
            found = [a for a in state.assets if (a.owner_id == person.user_id or (state.search_shared and a.id in state.shared)) and not a.trashed
                     and window.get("gte", "") <= a.updated_at <= window.get("lte", "9999")
                     and asked.get("checksum") in (None, a.json()["checksum"])]
            start = int(asked.get("cursor") or 0)
            size = min(int(asked.get("size", 250)), state.page_size)
            end = start + size
            self.reply(200, {"assets": {"items": [a.json() for a in found[start:end]], "nextCursor": str(end) if end < len(found) else None}})

        def asset(self, person: FakePerson, parts: list[str]) -> None:
            found = next((a for a in state.assets if a.id == parts[3] and (a.owner_id == person.user_id or a.id in state.shared)), None)
            if found is None and state.missing_status == 404 and not any(a.id == parts[3] for a in state.assets):
                self.reply(404, {"message": "Not found"})
            elif found is None:
                self.reply(400, NOT_READABLE)
            elif len(parts) == 4:
                self.reply(200, {k: v for k, v in found.json().items() if k not in state.drop_fields})
            elif parts[4] == "metadata":
                self.reply(200, [{"key": key, "value": {}} for key in found.metadata])
            elif parts[4] == "original":
                self.raw(found.data)
            else:
                self.reply(404, {"message": "Not found"})

        def route(self, path: str) -> None:
            with state.lock:
                state.requests.append((self.command, path))
                state.user_agents.append(self.headers.get("User-Agent", ""))
            if any(self.headers.get("User-Agent", "").startswith(refused) for refused in state.refused_agents):
                self.reply(403, {"message": "error code: 1010"})
                return
            if self.command == "GET" and path == "/api/server/ping":
                self.reply(200, {"res": "pong"})
                return
            if self.command == "GET" and path == "/api/server/about":
                self.reply(200, {"version": state.version})
                return
            role, person = self.who()
            if not role:
                self.reply(401, {"message": "Invalid API key"})
                return
            if person is not None and state.intercept is not None and (forced := state.intercept(self.command, path, person.slug)):
                self.reply(forced, {"message": "Injected failure"})
            elif self.command == "GET" and path == "/api/users/me":
                self.users_me(role, person)
            elif self.command == "GET" and path == "/api/api-keys/me" and person is not None:
                self.reply(200, {"permissions": list(state.permissions)})
            elif self.command == "GET" and path == "/api/libraries":
                visible = state.people if person is None else [person]
                self.reply(200, [library_json(p) for p in visible])
            elif self.command == "POST" and path == "/api/assets" and person is not None:
                self.upload(person)
            elif self.command == "POST" and path == "/api/assets/bulk-upload-check" and person is not None:
                self.bulk_check(person)
            elif self.command == "POST" and path == "/api/search/metadata" and person is not None:
                self.search(person, self.body)
            elif self.command == "GET" and path.startswith("/api/assets/") and person is not None:
                self.asset(person, path.split("/"))
            elif self.command == "POST" and path.startswith("/api/libraries/") and path.endswith("/scan"):
                self.scan(role, path.split("/")[3])
            else:
                self.reply(404, {"message": "Cannot " + self.command + " " + path})

        def upload(self, person: FakePerson) -> None:
            if "asset.upload" not in state.permissions:
                self.reply(403, {"message": "Missing required permission: asset.upload"})
                return
            if state.upload_answer is not None:
                self.reply(200, state.upload_answer)
                return
            message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + self.body)
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.iter_parts()}
            data, sidecar = fields.get("assetData"), fields.get("sidecarData")
            if not data or not fields.get("fileCreatedAt"):
                self.reply(400, {"message": "assetData and fileCreatedAt are required"})
                return
            checksum = base64.b64encode(hashlib.sha1(data, usedforsecurity=False).digest()).decode()
            with state.lock:
                existing = next((a for a in state.assets if a.owner_id == person.user_id and a.library_id is None and a.json()["checksum"] == checksum), None)
                if existing is not None:
                    self.reply(200, {"id": existing.id, "status": "duplicate"})
                    return
                used = sum(len(a.data) for a in state.assets if a.owner_id == person.user_id)
                if person.user_id in state.quotas and used + len(data) > state.quotas[person.user_id]:
                    self.reply(400, {"message": "Quota has been exceeded!"})
                    return
                name = (fields.get("filename") or b"").decode()
                kind = "VIDEO" if name.lower().endswith((".avi", ".mp4", ".mov")) else "IMAGE"
                asset = FakeAsset(str(uuid.uuid4()), person.user_id, data, name=name, kind=kind, exif=extract(data, sidecar),
                                  ready_at=time.monotonic() + state.extraction_delay, metadata=(), sidecar=sidecar)
                video_id = (fields.get("livePhotoVideoId") or b"").decode()
                video = next((a for a in state.assets if a.id == video_id and a.owner_id == person.user_id and a.kind == "VIDEO"), None)
                if video_id and video is None:
                    self.reply(400, {"message": "Live photo video not found"})
                    return
                if video is not None:
                    asset.live_video_id, video.visibility = video.id, "hidden"
                state.assets.append(asset)
                lost, state.lose_uploads = state.lose_uploads > 0, max(state.lose_uploads - 1, 0)
            if lost:
                self.drop()
                return
            self.reply(201, {"id": asset.id, "status": "created"})

        def bulk_check(self, person: FakePerson) -> None:
            results = []
            for item in json.loads(self.body or b"{}").get("assets", []):
                found = next((a for a in state.assets if a.owner_id == person.user_id and a.json()["checksum"] == item.get("checksum")), None)
                results.append({"id": item.get("id"), "action": "reject", "reason": "duplicate", "assetId": found.id, "isTrashed": found.trashed}
                               if found else {"id": item.get("id"), "action": "accept"})
            self.reply(200, {"results": results})

        def users_me(self, role: str, person: FakePerson | None) -> None:
            if person is None:
                self.reply(200, {"id": "admin", "name": "Admin", "email": "admin@fake.invalid", "isAdmin": True})
            else:
                self.reply(200, {"id": person.user_id, "name": person.name or person.slug, "email": person.email or f"{person.slug}@fake.invalid",
                             "isAdmin": person.slug in state.admins, "quotaSizeInBytes": state.quotas.get(person.user_id),
                             "quotaUsageInBytes": sum(len(a.data) for a in state.assets if a.owner_id == person.user_id)})

        def scan(self, role: str, library_id: str) -> None:
            if role != "admin":
                self.reply(403, {"message": "Forbidden"})
                return
            if not any(p.library_id == library_id for p in state.people):
                self.reply(404, {"message": "Library not found"})
                return
            with state.lock:
                state.recorded.append(Scan(library_id, time.time(), role))
            self.reply(204)

        do_GET = do_POST = do_PUT = do_DELETE = handle_any

        def log_message(self, format: str, *args: object) -> None:
            return None

    return Handler


class FakeImmich:
    def __init__(self, people: list[FakePerson], admin_key: str = "admin-key", port: int = 0, host: str = HOST) -> None:
        if host != HOST:
            raise ValueError("the fake Immich only binds to 127.0.0.1")
        self.state = State(admin_key=admin_key, people=list(people))
        self.server = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def start(self) -> FakeImmich:
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()

    def mode(self, name: str) -> None:
        if name not in MODES:
            raise ValueError(f"unknown mode {name}")
        self.state.mode = name

    def add_asset(self, asset: FakeAsset) -> FakeAsset:
        with self.state.lock:
            self.state.assets.append(asset)
        return asset

    def requests(self) -> list[tuple[str, str]]:
        with self.state.lock:
            return list(self.state.requests)

    def user_agents(self) -> list[str]:
        with self.state.lock:
            return list(self.state.user_agents)

    def scans(self) -> list[Scan]:
        with self.state.lock:
            return list(self.state.recorded)

    def __enter__(self) -> FakeImmich:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def load_state(path: Path) -> tuple[str, list[FakePerson]]:
    data = json.loads(path.read_text())
    people = [
        FakePerson(
            slug=item["slug"], user_id=item["user_id"], library_id=item["library_id"], key=item["key"],
            name=item.get("name", ""), email=item.get("email", ""),
            import_paths=tuple(item.get("import_paths", ())), exclusion_patterns=tuple(item.get("exclusion_patterns", ())),
        )
        for item in data["people"]
    ]
    return str(data["admin_key"]), people


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.fakes.immich")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    admin_key, people = load_state(args.state)
    fake = FakeImmich(people, admin_key, args.port)
    print(f"fake Immich on {fake.url}", flush=True)
    try:
        fake.server.serve_forever()
    except KeyboardInterrupt:
        fake.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
