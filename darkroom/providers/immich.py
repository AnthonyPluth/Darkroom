from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import re
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from darkroom import config, tls

TIMEOUT = 15.0
MAX_REPLY = 4 * 1024 * 1024
PAGE_SIZE = 250
MAX_PASSES = 5
DOWNLOAD_CHUNK = 1024 * 1024
PHONE_KEY = "mobile-app"
MANAGED_VERSION = "v3.3.6"
MANAGED_READ_PERMISSIONS = frozenset({"user.read", "server.about", "asset.read", "asset.download"})
READ_ROUTES = frozenset({("GET", "/api/server/ping"), ("GET", "/api/server/about"), ("GET", "/api/users/me"),
                         ("GET", "/api/api-keys/me"), ("GET", "/api/libraries"), ("POST", "/api/search/metadata")})


def require_allowed_request(method: str, path: str) -> None:
    if (method, path) in READ_ROUTES:
        return
    if method == "GET" and re.fullmatch(r"/api/assets/[A-Za-z0-9_-]+(?:/metadata|/original(?:\?edited=false)?)?", path):
        return
    if method == "POST" and re.fullmatch(r"/api/libraries/[A-Za-z0-9_-]+/scan", path):
        return
    raise ImmichFailed("the Immich operation is not allowed")


def is_canonical_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


class ImmichFailed(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotConfigured(ImmichFailed):
    pass


class ChecksumMismatch(ImmichFailed):
    pass


@dataclass(frozen=True)
class UploadSource:
    reader: BinaryIO = field(repr=False)
    size: int
    sha1: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.size) is not int or self.size <= 0 or not isinstance(self.sha1, str) or not re.fullmatch(r"[a-f0-9]{40}", self.sha1)
                or not callable(getattr(self.reader, "read", None))):
            raise ImmichFailed("an upload source needs a positive size and recorded checksum")


def upload_timestamp(value: str) -> bytes:
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value)
            or value.endswith("-00:00")):
        raise ImmichFailed("upload timestamps require an explicit known UTC offset")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ImmichFailed("the upload timestamp is not valid") from None
    return value.encode("ascii")


def upload_filename(value: str) -> tuple[bytes, str]:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00/\\"):
        raise ImmichFailed("the upload filename is not a supported basename")
    stem, dot, suffix = value.rpartition(".")
    if not stem or not dot or not re.fullmatch(r"[A-Za-z0-9]{1,10}", suffix):
        raise ImmichFailed("the upload filename is not a supported basename")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ImmichFailed("the upload filename is not valid UTF-8") from None
    if len(encoded) > 4096:
        raise ImmichFailed("the upload filename is too long")
    return encoded, suffix


def upload_source_chunks(source: UploadSource) -> Iterator[bytes]:
    remaining = source.size
    digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    try:
        while remaining:
            chunk = source.reader.read(min(DOWNLOAD_CHUNK, remaining))
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > min(DOWNLOAD_CHUNK, remaining):
                raise ImmichFailed("the upload source does not match its recorded size")
            remaining -= len(chunk)
            digest.update(chunk)
            yield chunk
        if source.reader.read(1) != b"":
            raise ImmichFailed("the upload source does not match its recorded size")
    except (OSError, ValueError):
        raise ImmichFailed("the upload source could not be read") from None
    if digest.hexdigest() != source.sha1:
        raise ChecksumMismatch("the upload source does not match its recorded checksum")


def is_upload_id(value: object) -> bool:
    return is_canonical_id(value) and UUID(str(value)).version == 4


class MultipartUpload:
    def __init__(self, original: UploadSource, filename: str, file_created_at: str, file_modified_at: str,
                 sidecar: UploadSource | None = None, live_video_id: str | None = None) -> None:
        if not isinstance(original, UploadSource) or (sidecar is not None and not isinstance(sidecar, UploadSource)):
            raise ImmichFailed("an upload body requires recorded binary sources")
        encoded_name, suffix = upload_filename(filename)
        created, modified = upload_timestamp(file_created_at), upload_timestamp(file_modified_at)
        if live_video_id is not None and not is_upload_id(live_video_id):
            raise ImmichFailed("not a Live Photo video id")
        if sidecar is not None and sidecar.reader is original.reader:
            raise ImmichFailed("the original and sidecar need independent upload sources")
        boundary = f"darkroom-{uuid4().hex}"
        self._content_type = f"multipart/form-data; boundary={boundary}"
        marker = boundary.encode("ascii")
        parts: list[bytes | UploadSource] = []
        for name, value in (("fileCreatedAt", created), ("fileModifiedAt", modified)):
            parts.append(b"--" + marker + b'\r\nContent-Disposition: form-data; name="' + name.encode("ascii") + b'"\r\n\r\n' + value + b"\r\n")
        if live_video_id is not None:
            parts.append(b"--" + marker + b'\r\nContent-Disposition: form-data; name="livePhotoVideoId"\r\n\r\n' + live_video_id.encode("ascii") + b"\r\n")
        files = [("assetData", f"original.{suffix}", original)]
        if sidecar is not None:
            files.append(("sidecarData", "sidecar.xmp", sidecar))
        for name, header_filename, source in files:
            parts.extend((b"--" + marker + b'\r\nContent-Disposition: form-data; name="' + name.encode("ascii") + b'"; filename="'
                          + header_filename.encode("ascii") + b'"\r\nContent-Type: application/octet-stream\r\n\r\n', source, b"\r\n"))
        parts.append(b"--" + marker + b'\r\nContent-Disposition: form-data; name="filename"\r\n\r\n' + encoded_name + b"\r\n")
        parts.append(b"--" + marker + b"--\r\n")
        self._parts = tuple(parts)
        self._length = sum(len(part) if isinstance(part, bytes) else part.size for part in self._parts)
        self._used = False
        self._complete = False

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": self._content_type, "Content-Length": str(self._length)}

    @property
    def complete(self) -> bool:
        return self._complete

    def __iter__(self) -> Iterator[bytes]:
        if self._used:
            raise ImmichFailed("an upload body can only be consumed once")
        self._used = True
        for part in self._parts:
            if isinstance(part, bytes):
                yield part
            else:
                yield from upload_source_chunks(part)
        self._complete = True


@dataclass(frozen=True)
class UploadResult:
    asset_id: str = field(repr=False)
    status: str


def upload_result(http_status: int, found: object) -> UploadResult:
    if not isinstance(found, dict) or not is_upload_id(found.get("id")):
        raise ImmichFailed("the upload response has no valid asset id")
    status = found.get("status")
    if type(http_status) is not int or (http_status, status) not in ((201, "created"), (200, "duplicate")):
        raise ImmichFailed("the upload response has an unrecognized outcome")
    return UploadResult(found["id"], status)


@dataclass(frozen=True)
class Asset:
    id: str
    owner_id: str
    library_id: str | None
    sha1: str
    file_name: str
    kind: str
    visibility: str
    live_video_id: str | None
    trashed: bool
    offline: bool


def sha1_of_checksum(checksum: object) -> str:
    try:
        raw = base64.b64decode(str(checksum), validate=True)
    except (binascii.Error, ValueError):
        raise ImmichFailed("Immich sent a checksum that is not base64") from None
    if len(raw) != 20:
        raise ImmichFailed("Immich sent a checksum that is not a SHA-1")
    return raw.hex()


def asset_of(entry: object) -> Asset:
    if not isinstance(entry, dict) or not all(isinstance(entry.get(name), str) and entry[name] for name in ("id", "ownerId", "checksum", "type")):
        raise ImmichFailed("Immich sent an asset without its id, owner, type or checksum")
    library, video, name = entry.get("libraryId"), entry.get("livePhotoVideoId"), entry.get("originalFileName")
    return Asset(entry["id"], entry["ownerId"], library if isinstance(library, str) else None, sha1_of_checksum(entry["checksum"]),
                 name if isinstance(name, str) else "", entry["type"], str(entry.get("visibility") or "timeline"),
                 video if isinstance(video, str) and video else None, entry.get("isTrashed") is True, entry.get("isOffline") is True)


def asset_path(asset_id: str) -> str:
    if not asset_id or "/" in asset_id or "?" in asset_id:
        raise ImmichFailed("not an asset id")
    return f"/api/assets/{quote(asset_id, safe='')}"


@dataclass(frozen=True)
class Library:
    id: str
    owner_id: str


@dataclass(frozen=True)
class Client:
    url: str
    admin_key: str = field(repr=False)
    timeout: float = TIMEOUT

    @classmethod
    def of(cls, settings: config.Settings) -> Client:
        if not settings.immich_url or not settings.immich_admin_key:
            raise NotConfigured("IMMICH_URL and IMMICH_ADMIN_KEY are not both set")
        return cls(settings.immich_url, settings.immich_admin_key)

    @classmethod
    def at(cls, settings: config.Settings) -> Client:
        if not settings.immich_url:
            raise NotConfigured("IMMICH_URL is not set")
        return cls(settings.immich_url, settings.immich_admin_key)

    def open(self, method: str, path: str, key: str, body: object | None = None) -> contextlib.AbstractContextManager[Any]:
        require_allowed_request(method, path)
        host = urlsplit(self.url).hostname or ""
        headers = {"Accept": "application/json"}
        if key:
            headers["x-api-key"] = key
        data = b"" if method == "POST" else None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        return tls.urlopen(f"{self.url}{path}", method=method, headers=headers, data=data, timeout=self.timeout, allow_http=config.local_host(host))

    def call(self, method: str, path: str, keyed: bool = True, key: str | None = None, body: object | None = None) -> object:
        try:
            with self.open(method, path, (key or self.admin_key) if keyed else "", body) as response:
                raw = response.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                raise ImmichFailed(f"Immich refused the {'person key' if key else 'admin key'} ({error.code})", error.code) from None
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

    def search_stable(self, key: str, since: str | None, until: str) -> list[Asset]:
        previous = list(self.search_updated(key, since, until))
        for _ in range(MAX_PASSES):
            current = list(self.search_updated(key, since, until))
            if {asset.id for asset in current} == {asset.id for asset in previous}:
                return current
            previous = current
        raise ImmichFailed("Immich's assets kept changing while they were listed")

    def user_id(self, key: str) -> str:
        found = self.call("GET", "/api/users/me", key=key)
        identity = found.get("id") if isinstance(found, dict) else None
        if not isinstance(identity, str):
            raise ImmichFailed("Immich's answer about the key's user had no id")
        return identity

    def search_updated(self, key: str, since: str | None, until: str) -> Iterator[Asset]:
        window = {"lte": until} if since is None else {"gte": since, "lte": until}
        cursor: object = None
        while True:
            body: dict[str, object] = {"filter": {"updatedAt": window}, "size": PAGE_SIZE, "orderBy": {"field": "fileCreatedAt", "direction": "asc"}}
            if cursor is not None:
                body["cursor"] = cursor
            found = self.call("POST", "/api/search/metadata", key=key, body=body)
            page = found.get("assets") if isinstance(found, dict) else None
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ImmichFailed("Immich's search answer had no assets")
            yield from (asset_of(entry) for entry in page["items"])
            cursor = page.get("nextCursor")
            if not cursor:
                return

    def get_asset(self, key: str, asset_id: str) -> Asset:
        return asset_of(self.call("GET", asset_path(asset_id), key=key))

    def metadata_keys(self, key: str, asset_id: str) -> set[str]:
        found = self.call("GET", f"{asset_path(asset_id)}/metadata", key=key)
        if not isinstance(found, list):
            raise ImmichFailed("Immich's metadata answer was not a list")
        return {entry["key"] for entry in found if isinstance(entry, dict) and isinstance(entry.get("key"), str)}

    def original_chunks(self, key: str, asset: Asset) -> Iterator[bytes]:
        digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
        total = 0
        try:
            with self.open("GET", f"{asset_path(asset.id)}/original?edited=false", key) as response:
                expected = response.headers.get("Content-Length")
                while chunk := response.read(DOWNLOAD_CHUNK):
                    digest.update(chunk)
                    total += len(chunk)
                    yield chunk
        except urllib.error.HTTPError as error:
            error.close()
            raise ImmichFailed(f"Immich answered {error.code}", error.code) from None
        except tls.Refused as error:
            raise ImmichFailed(str(error)) from None
        except tls.CONNECTION_PROBLEMS as error:
            raise ImmichFailed(f"Immich download broke off ({type(error).__name__})") from None
        if (expected is not None and expected.isdigit() and int(expected) != total) or digest.hexdigest() != asset.sha1:
            raise ChecksumMismatch("the downloaded original does not match Immich's checksum")


@dataclass(frozen=True)
class ManagedReader:
    client: Client = field(repr=False)
    key: str = field(repr=False)
    owner_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.key, str) or not self.key or any(not 33 <= ord(c) <= 126 for c in self.key)
                or self.key == self.client.admin_key or not is_canonical_id(self.owner_id)):
            raise ImmichFailed("managed Immich access requires a person key and owner")
        try:
            address = urlsplit(self.client.url)
            valid = (address.hostname and address.username is None and address.password is None
                     and "?" not in self.client.url and "#" not in self.client.url)
        except ValueError:
            valid = False
        if not valid:
            raise ImmichFailed("the managed Immich address is not valid")

    def check_access(self) -> None:
        identity = self.client.call("GET", "/api/users/me", key=self.key)
        if not isinstance(identity, dict) or identity.get("id") != self.owner_id:
            raise ImmichFailed("the Immich key does not belong to the expected person")
        about = self.client.call("GET", "/api/server/about", key=self.key)
        if not isinstance(about, dict) or about.get("version") != MANAGED_VERSION:
            raise ImmichFailed("this Immich version is not supported for managed access")
        found = self.client.call("GET", "/api/api-keys/me", key=self.key)
        permissions = found.get("permissions") if isinstance(found, dict) else None
        if (not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions)
                or frozenset(permissions) != MANAGED_READ_PERMISSIONS):
            raise ImmichFailed("managed Immich reads require only the approved read permissions")

    def get_asset(self, asset_id: str) -> Asset:
        if not is_canonical_id(asset_id):
            raise ImmichFailed("not a managed asset id")
        self.check_access()
        found = self.client.call("GET", asset_path(asset_id), key=self.key)
        if (not isinstance(found, dict) or found.get("id") != asset_id or found.get("ownerId") != self.owner_id
                or "libraryId" not in found or found["libraryId"] is not None
                or found.get("isTrashed") is not False or found.get("isOffline") is not False
                or found.get("type") not in ("IMAGE", "VIDEO")
                or (found.get("livePhotoVideoId") is not None and not is_canonical_id(found["livePhotoVideoId"]))):
            raise ImmichFailed("the asset is not an available managed original of this person")
        return asset_of(found)

    def original_chunks(self, asset_id: str, expected_sha1: str) -> Iterator[bytes]:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_sha1):
            raise ImmichFailed("not an original checksum")
        asset = self.get_asset(asset_id)
        if asset.sha1 != expected_sha1:
            raise ChecksumMismatch("the managed original changed since it was recorded")
        yield from self.client.original_chunks(self.key, asset)
