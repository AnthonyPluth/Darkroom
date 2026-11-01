from __future__ import annotations

import argparse
import json
import threading
import time
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
class State:
    admin_key: str
    people: list[FakePerson]
    mode: str = "ok"
    recorded: list[Scan] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    slow_seconds: float = SLOW_SECONDS


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

        def route(self, path: str) -> None:
            if self.command == "GET" and path == "/api/server/ping":
                self.reply(200, {"res": "pong"})
                return
            if self.command == "GET" and path == "/api/server/about":
                self.reply(200, {"version": VERSION})
                return
            role, person = self.who()
            if not role:
                self.reply(401, {"message": "Invalid API key"})
                return
            if self.command == "GET" and path == "/api/users/me":
                self.users_me(role, person)
            elif self.command == "GET" and path == "/api/libraries":
                visible = state.people if person is None else [person]
                self.reply(200, [library_json(p) for p in visible])
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
