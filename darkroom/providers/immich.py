from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from darkroom import config, tls

TIMEOUT = 15.0
MAX_REPLY = 4 * 1024 * 1024


class ImmichFailed(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotConfigured(ImmichFailed):
    pass


@dataclass(frozen=True)
class Library:
    id: str
    owner_id: str


@dataclass(frozen=True)
class Client:
    url: str
    admin_key: str
    timeout: float = TIMEOUT

    @classmethod
    def of(cls, settings: config.Settings) -> Client:
        if not settings.immich_url or not settings.immich_admin_key:
            raise NotConfigured("IMMICH_URL and IMMICH_ADMIN_KEY are not both set")
        return cls(settings.immich_url, settings.immich_admin_key)

    def call(self, method: str, path: str, keyed: bool = True) -> object:
        host = urlsplit(self.url).hostname or ""
        headers = {"Accept": "application/json"}
        if keyed:
            headers["x-api-key"] = self.admin_key
        try:
            with tls.urlopen(f"{self.url}{path}", method=method, headers=headers, data=b"" if method == "POST" else None,
                             timeout=self.timeout, allow_http=config.local_host(host)) as response:
                raw = response.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                raise ImmichFailed(f"Immich refused the admin key ({error.code})", error.code) from None
            raise ImmichFailed(f"Immich answered {error.code}", error.code) from None
        except tls.Refused as error:
            raise ImmichFailed(str(error)) from None
        except tls.CONNECTION_PROBLEMS as error:
            raise ImmichFailed(f"Immich could not be reached ({type(error).__name__})") from None
        if len(raw) > MAX_REPLY:
            raise ImmichFailed("Immich's answer was too large")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ImmichFailed("Immich's answer was not JSON") from None

    def ping(self) -> bool:
        found = self.call("GET", "/api/server/ping", keyed=False)
        return isinstance(found, dict) and found.get("res") == "pong"

    def list_libraries(self) -> list[Library]:
        found = self.call("GET", "/api/libraries")
        if not isinstance(found, list):
            raise ImmichFailed("Immich's library list was not a list")
        libraries = []
        for entry in found:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("ownerId"), str):
                libraries.append(Library(entry["id"], entry["ownerId"]))
        return libraries

    def find_library(self, owner_id: str) -> str | None:
        owned = sorted(library.id for library in self.list_libraries() if library.owner_id == owner_id)
        if len(owned) > 1:
            raise ImmichFailed("the Immich user owns several libraries")
        return owned[0] if owned else None

    def rescan_library(self, library_id: str) -> None:
        if not library_id or "/" in library_id:
            raise ImmichFailed("not a library id")
        self.call("POST", f"/api/libraries/{quote(library_id, safe='')}/scan")
