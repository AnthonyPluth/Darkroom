from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
MODES = ("ok", "down", "slow", "401", "500")
VERSION = "v1.130.0"
SLOW_SECONDS = 2.0


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

    def json(self) -> dict[str, object]:
        checksum = self.checksum or base64.b64encode(hashlib.sha1(self.data, usedforsecurity=False).digest()).decode()
        return {
            "id": self.id, "ownerId": self.owner_id, "libraryId": self.library_id, "checksum": checksum, "type": self.kind,
            "originalFileName": self.name, "visibility": self.visibility, "livePhotoVideoId": self.live_video_id,
            "isTrashed": self.trashed, "isOffline": False, "updatedAt": self.updated_at,
        }


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
    page_size: int = 250
    searches: int = 0
    on_search: Callable[[int], None] | None = None
    version: str = VERSION
    permissions: tuple[str, ...] = ("user.read", "server.about", "asset.read", "asset.download")


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
            found = [a for a in state.assets if a.owner_id == person.user_id and not a.trashed
                     and window.get("gte", "") <= a.updated_at <= window.get("lte", "9999")]
            start = int(asked.get("cursor") or 0)
            size = min(int(asked.get("size", 250)), state.page_size)
            end = start + size
            self.reply(200, {"assets": {"items": [a.json() for a in found[start:end]], "nextCursor": str(end) if end < len(found) else None}})

        def asset(self, person: FakePerson, parts: list[str]) -> None:
            found = next((a for a in state.assets if a.id == parts[3] and a.owner_id == person.user_id), None)
            if found is None:
                self.reply(404, {"message": "Not found"})
            elif len(parts) == 4:
                self.reply(200, found.json())
            elif parts[4] == "metadata":
                self.reply(200, [{"key": key, "value": {}} for key in found.metadata])
            elif parts[4] == "original":
                self.raw(found.data)
            else:
                self.reply(404, {"message": "Not found"})

        def route(self, path: str) -> None:
            with state.lock:
                state.requests.append((self.command, path))
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
            if self.command == "GET" and path == "/api/users/me":
                self.users_me(role, person)
            elif self.command == "GET" and path == "/api/api-keys/me" and person is not None:
                self.reply(200, {"permissions": list(state.permissions)})
            elif self.command == "GET" and path == "/api/libraries":
                visible = state.people if person is None else [person]
                self.reply(200, [library_json(p) for p in visible])
            elif self.command == "POST" and path == "/api/search/metadata" and person is not None:
                self.search(person, self.body)
            elif self.command == "GET" and path.startswith("/api/assets/") and person is not None:
                self.asset(person, path.split("/"))
            elif self.command == "POST" and path.startswith("/api/libraries/") and path.endswith("/scan"):
                self.scan(role, path.split("/")[3])
            else:
                self.reply(404, {"message": "Cannot " + self.command + " " + path})

        def users_me(self, role: str, person: FakePerson | None) -> None:
            if person is None:
                self.reply(200, {"id": "admin", "name": "Admin", "email": "admin@fake.invalid", "isAdmin": True})
            else:
                self.reply(200, {"id": person.user_id, "name": person.name or person.slug, "email": person.email or f"{person.slug}@fake.invalid", "isAdmin": False})

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
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

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
