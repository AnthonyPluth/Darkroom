from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit

from darkroom import config, tls

TIMEOUT = 15.0
MAX_REPLY = 4 * 1024 * 1024
SEND_ONLY = "sendonly"
NO_VERSIONING = {"type": ""}
ON_SOCKET = "http://localhost"
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
SECRET_FIELDS = frozenset({"apiKey", "password", "encryptionPassword"})
FOLDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ROUTES = frozenset({
    ("GET", "/rest/system/status"), ("GET", "/rest/system/version"), ("GET", "/rest/config"),
    ("GET", "/rest/config/folders"), ("POST", "/rest/config/folders"), ("POST", "/rest/config/devices"),
    ("PATCH", "/rest/config/options"), ("GET", "/rest/config/gui"), ("PATCH", "/rest/config/gui"),
    ("GET", "/rest/db/ignores"), ("POST", "/rest/db/ignores"), ("POST", "/rest/db/scan"), ("GET", "/rest/db/completion"),
    ("GET", "/rest/system/connections"), ("GET", "/rest/stats/device"), ("GET", "/rest/cluster/pending/devices"),
})
ITEM_ROUTES = ((frozenset({"GET", "PATCH", "DELETE"}), re.compile(r"/rest/config/folders/[A-Za-z0-9._-]+")),
               (frozenset({"PATCH", "DELETE"}), re.compile(r"/rest/config/devices/[A-Z2-7-]+")))


class SyncthingFailed(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class KeyRefused(SyncthingFailed):
    pass


class InvalidDeviceId(SyncthingFailed):
    pass


NOT_A_DEVICE_ID = "not a Syncthing device ID"


class ReadBackFailed(SyncthingFailed):
    pass


class NotSendOnly(ReadBackFailed):
    pass


def check_character(data: str) -> str:
    factor, total = 1, 0
    for character in data:
        addend = factor * ALPHABET.index(character)
        factor = 3 - factor
        total += addend // 32 + addend % 32
    return ALPHABET[(32 - total % 32) % 32]


def grouped(characters: str) -> str:
    return "-".join(characters[start:start + 7] for start in range(0, 56, 7))


def device_id(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidDeviceId(NOT_A_DEVICE_ID)
    characters = value.strip().upper().replace("-", "")
    if len(characters) != 56 or any(character not in ALPHABET for character in characters):
        raise InvalidDeviceId(NOT_A_DEVICE_ID)
    for start in range(0, 56, 14):
        if check_character(characters[start:start + 13]) != characters[start + 13]:
            raise InvalidDeviceId("a check character of the Syncthing device ID is wrong")
    return grouped(characters)


def is_device_id(value: object) -> bool:
    try:
        device_id(value)
    except InvalidDeviceId:
        return False
    return True


def device_id_of_certificate(der: bytes) -> str:
    data = base64.b32encode(hashlib.sha256(der).digest()).decode("ascii").rstrip("=")
    return grouped("".join(data[start:start + 13] + check_character(data[start:start + 13]) for start in range(0, 52, 13)))


def folder_id(value: object) -> str:
    if not isinstance(value, str) or not FOLDER_ID.fullmatch(value):
        raise SyncthingFailed("not a Syncthing folder id")
    return value


def require_allowed_request(method: str, path: str) -> None:
    if (method, path) in ROUTES or any(method in methods and pattern.fullmatch(path) for methods, pattern in ITEM_ROUTES):
        return
    raise SyncthingFailed("the Syncthing operation is not allowed")


def without_secrets(value: object) -> object:
    if isinstance(value, dict):
        return {name: without_secrets(item) for name, item in value.items() if name not in SECRET_FIELDS}
    if isinstance(value, list):
        return [without_secrets(item) for item in value]
    return value


def mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SyncthingFailed(f"Syncthing's {what} answer was not an object")
    return value


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


@dataclass(frozen=True)
class Version:
    version: str
    is_release: bool


@dataclass(frozen=True)
class Folder:
    id: str
    path: str = field(repr=False)
    type: str
    devices: frozenset[str] = field(repr=False)
    paused: bool
    versioning: str = ""


@dataclass(frozen=True)
class Device:
    device_id: str = field(repr=False)
    name: str = field(repr=False)
    paused: bool


@dataclass(frozen=True)
class Completion:
    completion: float
    global_bytes: int
    global_items: int
    need_bytes: int
    need_items: int
    need_deletes: int
    remote_state: str


@dataclass(frozen=True)
class Connection:
    connected: bool
    type: str


@dataclass(frozen=True)
class DeviceStats:
    last_seen: str
    last_connection_duration_s: float


@dataclass(frozen=True)
class PendingDevice:
    device_id: str = field(repr=False)
    name: str = field(repr=False)
    address: str = field(repr=False)
    time: str


def folder_of(entry: object) -> Folder:
    found = mapping(entry, "folder")
    identity, path, kind, paused, devices = found.get("id"), found.get("path"), found.get("type"), found.get("paused"), found.get("devices")
    if not isinstance(identity, str) or not isinstance(path, str) or not isinstance(kind, str) or not isinstance(paused, bool) or not isinstance(devices, list):
        raise SyncthingFailed("Syncthing sent a folder without its id, path, type, devices or paused state")
    versioning = found.get("versioning")
    kept = text(versioning.get("type")) if isinstance(versioning, dict) else ""
    return Folder(identity, path, kind, frozenset(device_id(mapping(device, "folder device").get("deviceID")) for device in devices), paused, kept)


def device_of(entry: object) -> Device:
    found = mapping(entry, "device")
    paused = found.get("paused")
    if not isinstance(paused, bool):
        raise SyncthingFailed("Syncthing sent a device without its paused state")
    return Device(device_id(found.get("deviceID")), text(found.get("name")), paused)


@dataclass(frozen=True)
class Client:
    url: str
    api_key: str = field(repr=False)
    timeout: float = TIMEOUT
    socket: str = ""

    @classmethod
    def on_socket(cls, socket_path: str, api_key: str, timeout: float = TIMEOUT) -> Client:
        return cls(ON_SOCKET, api_key, timeout, socket_path)

    def __post_init__(self) -> None:
        try:
            address = urlsplit(self.url)
            valid = (address.scheme in ("http", "https") and bool(address.hostname) and address.username is None and address.password is None
                     and address.path in ("", "/") and not address.query and not address.fragment)
        except ValueError:
            valid = False
        if self.socket and (self.url != ON_SOCKET or not self.socket.startswith("/") or "\x00" in self.socket):
            valid = False
        if not valid:
            raise SyncthingFailed("the Syncthing address is not valid")
        if not isinstance(self.api_key, str) or not self.api_key or any(not 33 <= ord(character) <= 126 for character in self.api_key):
            raise SyncthingFailed("the Syncthing API key is not usable")

    def exchange(self, method: str, target: str, data: bytes | None, headers: Mapping[str, str]) -> tuple[int, bytes]:
        try:
            if self.socket:
                return tls.unix_request(self.socket, method, target, data=data, headers=headers, timeout=self.timeout, limit=MAX_REPLY + 1)
            allow_http = config.local_host(urlsplit(self.url).hostname or "")
            with tls.urlopen(f"{self.url.rstrip('/')}{target}", data=data, headers=headers, method=method, timeout=self.timeout, allow_http=allow_http) as reply:
                return reply.status, reply.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            return status, b""
        except tls.Refused:
            raise SyncthingFailed("the request to Syncthing was refused before it was sent") from None
        except tls.CONNECTION_PROBLEMS as error:
            raise SyncthingFailed(f"Syncthing could not be reached ({type(error).__name__})") from None

    def call(self, method: str, path: str, query: Mapping[str, str] | None = None, body: object | None = None) -> object:
        require_allowed_request(method, path)
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        data = b"" if method in ("POST", "PATCH") else None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, raw = self.exchange(method, f"{path}{'?' + urlencode(query) if query else ''}", data, headers)
        if status in (401, 403):
            raise KeyRefused("Syncthing refused the API key", status)
        if not 200 <= status < 300:
            raise SyncthingFailed(f"Syncthing answered {status}", status)
        if len(raw) > MAX_REPLY:
            raise SyncthingFailed("Syncthing's answer was too large")
        if not raw.strip():
            return None
        try:
            return without_secrets(json.loads(raw))
        except ValueError:
            raise SyncthingFailed("Syncthing's answer was not JSON") from None

    def own_device_id(self) -> str:
        return device_id(mapping(self.call("GET", "/rest/system/status"), "status").get("myID"))

    def version(self) -> Version:
        found = mapping(self.call("GET", "/rest/system/version"), "version")
        if not isinstance(found.get("version"), str):
            raise SyncthingFailed("Syncthing's version answer had no version")
        return Version(text(found["version"]), found.get("isRelease") is True)

    def configuration(self) -> dict[str, object]:
        return mapping(self.call("GET", "/rest/config"), "configuration")

    def folders(self) -> list[Folder]:
        found = self.call("GET", "/rest/config/folders")
        if not isinstance(found, list):
            raise SyncthingFailed("Syncthing's folder list was not a list")
        return [folder_of(entry) for entry in found]

    def folder(self, identity: str) -> Folder:
        return folder_of(self.call("GET", f"/rest/config/folders/{quote(folder_id(identity), safe='')}"))

    def checked_folder(self, identity: str, devices: frozenset[str] | None, paused: bool | None, path: str | None = None) -> Folder:
        try:
            found = self.folder(identity)
        except SyncthingFailed as error:
            if error.status != 404:
                raise
            raise ReadBackFailed("Syncthing did not keep the folder as written") from None
        if found.type != SEND_ONLY:
            raise NotSendOnly("Syncthing did not keep the folder Send Only")
        if found.versioning:
            raise ReadBackFailed("Syncthing kept versioning on the folder")
        own = self.own_device_id()
        if (found.id != identity or (path is not None and found.path != path) or (paused is not None and found.paused != paused)
                or (devices is not None and found.devices - {own} != devices - {own})):
            raise ReadBackFailed("Syncthing did not keep the folder as written")
        return found

    def add_folder(self, identity: str, path: str, devices: Iterable[str], paused: bool = True) -> Folder:
        wanted = frozenset(device_id(device) for device in devices)
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise SyncthingFailed("the folder path is not absolute")
        identity = folder_id(identity)
        if any(found.id == identity for found in self.folders()):
            raise SyncthingFailed("the folder is already configured")
        body = {"id": identity, "path": path, "type": SEND_ONLY, "paused": paused, "devices": [{"deviceID": device} for device in sorted(wanted)],
                "versioning": NO_VERSIONING}
        self.call("POST", "/rest/config/folders", body=body)
        return self.checked_folder(identity, wanted, paused, path)

    def update_folder(self, identity: str, devices: Iterable[str] | None = None, paused: bool | None = None) -> Folder:
        wanted = None if devices is None else frozenset(device_id(device) for device in devices)
        body: dict[str, object] = {"type": SEND_ONLY, "versioning": NO_VERSIONING}
        if wanted is not None:
            body["devices"] = [{"deviceID": device} for device in sorted(wanted)]
        if paused is not None:
            body["paused"] = paused
        self.call("PATCH", f"/rest/config/folders/{quote(folder_id(identity), safe='')}", body=body)
        return self.checked_folder(identity, wanted, paused)

    def remove_folder(self, identity: str) -> None:
        self.call("DELETE", f"/rest/config/folders/{quote(folder_id(identity), safe='')}")
        if any(found.id == identity for found in self.folders()):
            raise ReadBackFailed("Syncthing still has the folder")

    def devices(self) -> list[Device]:
        found = self.configuration().get("devices")
        if not isinstance(found, list):
            raise SyncthingFailed("Syncthing's device list was not a list")
        return [device_of(entry) for entry in found]

    def checked_device(self, identity: str, paused: bool, name: str | None = None) -> Device:
        found = [device for device in self.devices() if device.device_id == identity]
        if len(found) != 1 or found[0].paused != paused or (name is not None and found[0].name != name):
            raise ReadBackFailed("Syncthing did not keep the device as written")
        return found[0]

    def add_device(self, identity: str, name: str, paused: bool = True) -> Device:
        canonical = device_id(identity)
        if not isinstance(name, str) or len(name) > 64 or not name.isprintable():
            raise SyncthingFailed("the device name is not usable")
        self.call("POST", "/rest/config/devices", body={"deviceID": canonical, "name": name, "paused": paused})
        return self.checked_device(canonical, paused, name)

    def update_device(self, identity: str, paused: bool) -> Device:
        canonical = device_id(identity)
        self.call("PATCH", f"/rest/config/devices/{canonical}", body={"paused": paused})
        return self.checked_device(canonical, paused)

    def remove_device(self, identity: str) -> None:
        canonical = device_id(identity)
        self.call("DELETE", f"/rest/config/devices/{canonical}")
        if any(found.device_id == canonical for found in self.devices()):
            raise ReadBackFailed("Syncthing still has the device")

    def options(self) -> dict[str, object]:
        return mapping(self.configuration().get("options"), "options")

    def set_options(self, changes: Mapping[str, object]) -> dict[str, object]:
        self.call("PATCH", "/rest/config/options", body=dict(changes))
        found = self.options()
        if any(found.get(name) != value for name, value in changes.items()):
            raise ReadBackFailed("Syncthing did not keep the options as written")
        return found

    def gui(self) -> dict[str, object]:
        return mapping(self.call("GET", "/rest/config/gui"), "GUI")

    def set_gui(self, changes: Mapping[str, object]) -> dict[str, object]:
        if SECRET_FIELDS & set(changes):
            raise SyncthingFailed("the provider does not change Syncthing's API key or password")
        self.call("PATCH", "/rest/config/gui", body=dict(changes))
        found = self.gui()
        if any(found.get(name) != value for name, value in changes.items()):
            raise ReadBackFailed("Syncthing did not keep the GUI settings as written")
        return found

    def ignores(self, identity: str) -> tuple[str, ...]:
        found = mapping(self.call("GET", "/rest/db/ignores", {"folder": folder_id(identity)}), "ignores").get("ignore")
        if found is None:
            return ()
        if not isinstance(found, list) or not all(isinstance(pattern, str) for pattern in found):
            raise SyncthingFailed("Syncthing's ignore patterns were not a list of patterns")
        return tuple(found)

    def set_ignores(self, identity: str, patterns: Iterable[str]) -> tuple[str, ...]:
        wanted = tuple(patterns)
        if not all(isinstance(pattern, str) and pattern and "\n" not in pattern and "\r" not in pattern for pattern in wanted):
            raise SyncthingFailed("an ignore pattern is not usable")
        self.call("POST", "/rest/db/ignores", {"folder": folder_id(identity)}, body={"ignore": list(wanted)})
        if self.ignores(identity) != wanted:
            raise ReadBackFailed("Syncthing did not keep the ignore patterns as written")
        return wanted

    def scan(self, identity: str) -> None:
        self.call("POST", "/rest/db/scan", {"folder": folder_id(identity)})

    def completion(self, identity: str, device: str) -> Completion:
        found = mapping(self.call("GET", "/rest/db/completion", {"folder": folder_id(identity), "device": device_id(device)}), "completion")
        return Completion(number(found.get("completion")), int(number(found.get("globalBytes"))), int(number(found.get("globalItems"))),
                          int(number(found.get("needBytes"))), int(number(found.get("needItems"))), int(number(found.get("needDeletes"))),
                          text(found.get("remoteState")))

    def connections(self) -> dict[str, Connection]:
        found = mapping(mapping(self.call("GET", "/rest/system/connections"), "connections").get("connections"), "connections")
        out = {}
        for identity, entry in found.items():
            if is_device_id(identity) and isinstance(entry, dict):
                out[device_id(identity)] = Connection(entry.get("connected") is True, text(entry.get("type")))
        return out

    def device_stats(self) -> dict[str, DeviceStats]:
        found = mapping(self.call("GET", "/rest/stats/device"), "device stats")
        out = {}
        for identity, entry in found.items():
            if is_device_id(identity) and isinstance(entry, dict):
                out[device_id(identity)] = DeviceStats(text(entry.get("lastSeen")), number(entry.get("lastConnectionDurationS")))
        return out

    def pending_devices(self) -> list[PendingDevice]:
        found = mapping(self.call("GET", "/rest/cluster/pending/devices"), "pending devices")
        return [PendingDevice(device_id(identity), text(entry.get("name")), text(entry.get("address")), text(entry.get("time")))
                for identity, entry in sorted(found.items()) if is_device_id(identity) and isinstance(entry, dict)]
