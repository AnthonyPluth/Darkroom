from __future__ import annotations

import contextlib
import json
import threading
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from darkroom.providers.syncthing import is_device_id

HOST = "127.0.0.1"
FOLDER_TYPES = frozenset({"sendreceive", "sendonly", "receiveonly", "receiveencrypted"})


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


class FakeSyncthing:
    def __init__(self, test: unittest.TestCase, key: str, my_id: str):
        self.key = key
        self.my_id = my_id
        self.requests: list[Received] = []
        self.folders: dict[str, dict[str, object]] = {}
        self.devices: dict[str, dict[str, object]] = {my_id: {"deviceID": my_id, "name": "darkroom", "paused": False}}
        self.options: dict[str, object] = {"globalAnnounceEnabled": False, "localAnnounceEnabled": False, "relaysEnabled": False}
        self.gui_unix_mode = "0600"
        self.ignores: dict[str, list[str]] = {}
        self.scans: list[str] = []
        self.errors: set[str] = set()
        self.completions: dict[tuple[str, str], dict[str, object]] = {}
        self.connections: dict[str, dict[str, object]] = {}
        self.stats: dict[str, dict[str, object]] = {}
        self.pending: dict[str, dict[str, object]] = {}
        self.forgetful = False
        self.forced_type: str | None = None
        self.status = 0
        self.raw: bytes | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self) -> None:
                address = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received = Received(self.command, address.path, parse_qs(address.query), {name.lower(): value for name, value in self.headers.items()}, body)
                fake.requests.append(received)
                status, reply = fake.answer(received)
                raw = fake.raw if fake.raw is not None and status == 200 else (b"CSRF Error\n" if status == 403 else json.dumps(reply).encode())
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = handle_any

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = f"http://{HOST}:{self.server.server_address[1]}"

    def configuration(self) -> dict[str, object]:
        return {"version": 37, "folders": list(self.folders.values()), "devices": list(self.devices.values()), "options": dict(self.options),
                "gui": self.gui()}

    def gui(self) -> dict[str, object]:
        return {"enabled": True, "address": "/run/st/gui.sock", "unixSocketPermissions": self.gui_unix_mode, "apiKey": self.key, "password": "", "theme": "default"}

    def stored_folder(self, entry: dict[str, object]) -> dict[str, object]:
        kind = entry.get("type")
        listed = entry.get("devices")
        devices = [device for device in listed if isinstance(device, dict)] if isinstance(listed, list) else []
        if not any(device.get("deviceID") == self.my_id for device in devices):
            devices.append({"deviceID": self.my_id})
        return entry | {"type": self.forced_type or (kind if kind in FOLDER_TYPES else "sendreceive"), "devices": devices}

    def answer(self, received: Received) -> tuple[int, object]:
        if self.status:
            return self.status, {}
        if received.headers.get("x-api-key") != self.key and received.headers.get("authorization") != f"Bearer {self.key}":
            return 403, {}
        incoming = json.loads(received.body) if received.body else None
        folder = (received.query.get("folder") or [""])[0]
        route = (received.method, received.path)
        parts = received.path.split("/")
        item = unquote(parts[4]) if len(parts) == 5 and parts[2] == "config" else ""
        if route == ("GET", "/rest/system/status"):
            return 200, {"myID": self.my_id, "uptime": 10}
        if route == ("GET", "/rest/system/version"):
            return 200, {"version": "v2.1.7", "longVersion": "syncthing v2.1.7", "isRelease": True, "isBeta": False, "os": "linux", "arch": "amd64"}
        if route == ("GET", "/rest/config"):
            return 200, self.configuration()
        if route == ("GET", "/rest/config/folders"):
            return 200, list(self.folders.values())
        if route == ("GET", "/rest/config/gui"):
            return 200, self.gui()
        if route == ("GET", "/rest/system/connections"):
            return 200, {"connections": self.connections, "total": {}}
        if route == ("GET", "/rest/stats/device"):
            return 200, self.stats
        if route == ("GET", "/rest/cluster/pending/devices"):
            return 200, self.pending
        if received.path.startswith("/rest/db/") and folder not in self.folders:
            return 404, {}
        if route == ("GET", "/rest/db/ignores"):
            return 200, {"ignore": self.ignores.get(folder), "expanded": self.ignores.get(folder)}
        if route == ("GET", "/rest/db/completion"):
            return 200, self.completions.get((folder, (received.query.get("device") or [""])[0]), {"completion": 0})
        if route == ("POST", "/rest/db/scan"):
            self.scans.append(folder)
            return (500, {}) if folder in self.errors else (200, {})
        if self.forgetful and received.method != "GET":
            return 200, {}
        if route == ("POST", "/rest/db/ignores") and isinstance(incoming, dict):
            self.ignores[folder] = list(incoming.get("ignore") or [])
            return 200, {"ignore": self.ignores[folder]}
        if route == ("POST", "/rest/config/folders") and isinstance(incoming, dict):
            self.folders[str(incoming["id"])] = self.stored_folder(incoming)
            return 200, {}
        if route == ("POST", "/rest/config/devices") and isinstance(incoming, dict):
            if not is_device_id(incoming.get("deviceID")):
                return 400, {}
            self.devices[str(incoming["deviceID"])] = incoming
            return 200, {}
        if route == ("PATCH", "/rest/config/options") and isinstance(incoming, dict):
            self.options |= incoming
            return 200, {}
        if route == ("PATCH", "/rest/config/gui") and isinstance(incoming, dict):
            self.key = str(incoming.get("apiKey", self.key))
            self.gui_unix_mode = str(incoming.get("unixSocketPermissions", self.gui_unix_mode))
            return 200, {}
        for kind, table in (("folders", self.folders), ("devices", self.devices)):
            if item and parts[3] == kind and item in table:
                if received.method == "GET":
                    return 200, table[item]
                if received.method == "DELETE":
                    del table[item]
                    return 200, {}
                if received.method == "PATCH" and isinstance(incoming, dict):
                    table[item] = self.stored_folder(table[item] | incoming) if kind == "folders" else table[item] | incoming
                    return 200, {}
        return 404, {}
