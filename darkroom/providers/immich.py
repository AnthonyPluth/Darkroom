from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import math
import re
import urllib.error
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from darkroom import config, tls

TIMEOUT = 15.0
MAX_REPLY = 4 * 1024 * 1024
PAGE_SIZE = 250
MAX_PASSES = 5
DOWNLOAD_CHUNK = 1024 * 1024
PHONE_KEY = "mobile-app"
PING_PATH = "/api/server/ping"
CHECK_TIMEOUT = 5.0
CHECK_LIMIT = 4096
USER_AGENT = "Darkroom"
NOT_READABLE = "Not found or no asset.read access"
AddressCheck = Literal["answered", "no_answer", "not_immich", "tls_failed"]
MANAGED_VERSIONS = frozenset({"v3.3.6", "v3.4.1"})
MANAGED_READ_PERMISSIONS = frozenset({"user.read", "server.about", "asset.read", "asset.download"})
MANAGED_IMPORT_PERMISSIONS = MANAGED_READ_PERMISSIONS | {"asset.upload"}
ManagedAccess = Literal["read", "import"]
MANAGED_GRANTS: dict[str, tuple[frozenset[str], str]] = {
    "read": (MANAGED_READ_PERMISSIONS, "managed Immich reads require only the approved read permissions"),
    "import": (MANAGED_IMPORT_PERMISSIONS, "managed Immich imports require only the approved import permissions"),
}
READ_ROUTES = frozenset({("GET", "/api/server/ping"), ("GET", "/api/server/about"), ("GET", "/api/users/me"),
                         ("GET", "/api/api-keys/me"), ("GET", "/api/libraries"), ("POST", "/api/search/metadata")})
UPLOAD_PATH = "/api/assets"
BULK_CHECK_PATH = "/api/assets/bulk-upload-check"
IMPORT_ROUTES = frozenset({("POST", UPLOAD_PATH), ("POST", BULK_CHECK_PATH)})
UPLOAD_TIMEOUT = 300.0
BULK_CHECK_ID = "darkroom-source"
CHECKSUM_PAGE = 100


def require_allowed_request(method: str, path: str, *, importing: bool = False) -> None:
    if (method, path) in READ_ROUTES or (importing is True and (method, path) in IMPORT_ROUTES):
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


class NotVisible(ImmichFailed):
    pass


class NotCustody(ImmichFailed):
    pass


class UploadUncertain(ImmichFailed):
    pass


def checksum_of(sha1: str) -> str:
    if not isinstance(sha1, str) or not re.fullmatch(r"[a-f0-9]{40}", sha1):
        raise ImmichFailed("not a source checksum")
    return base64.b64encode(bytes.fromhex(sha1)).decode("ascii")


@dataclass(frozen=True)
class BulkMatch:
    asset_id: str = field(repr=False)
    trashed: bool


def not_readable(raw: bytes) -> bool:
    if len(raw) > CHECK_LIMIT:
        return False
    try:
        found = json.loads(raw)
    except ValueError:
        return False
    return (isinstance(found, dict) and found.get("message") == NOT_READABLE
            and found.get("error", "Bad Request") == "Bad Request" and type(found.get("statusCode", 400)) is int and found.get("statusCode", 400) == 400)


def denial_of(error: urllib.error.HTTPError) -> bytes:
    try:
        return error.read(CHECK_LIMIT + 1) if error.code == 400 else b""
    except (OSError, ValueError):
        return b""
    finally:
        error.close()


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
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value)
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
        self.original = original
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
    if not isinstance(status, str) or type(http_status) is not int or (http_status, status) not in ((201, "created"), (200, "duplicate")):
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


@dataclass(frozen=True)
class Extracted:
    taken: str | None = field(repr=False)
    zone: str | None = field(repr=False)
    latitude: float | None = field(repr=False)
    longitude: float | None = field(repr=False)
    description: str | None = field(repr=False)


def extracted_of(info: object) -> Extracted | None:
    if not isinstance(info, dict):
        return None

    def text(name: str) -> str | None:
        value = info.get(name)
        return value if isinstance(value, str) else None

    def degrees(name: str) -> float | None:
        value = info.get(name)
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) else None
    return Extracted(text("dateTimeOriginal"), text("timeZone"), degrees("latitude"), degrees("longitude"), text("description"))


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


def check_address(url: str, timeout: float = CHECK_TIMEOUT) -> AddressCheck:
    require_allowed_request("GET", PING_PATH)
    host = urlsplit(url).hostname or ""
    try:
        with tls.urlopen(f"{url}{PING_PATH}", headers={"Accept": "application/json", "User-Agent": USER_AGENT}, timeout=timeout, allow_http=config.local_host(host)) as response:
            raw = response.read(CHECK_LIMIT + 1)
    except urllib.error.HTTPError as error:
        error.close()
        return "not_immich"
    except tls.Refused:
        return "not_immich"
    except tls.NOT_HTTP_REPLIES as error:
        return "no_answer" if isinstance(error, ConnectionError) else "not_immich"
    except tls.CONNECTION_PROBLEMS as error:
        return "tls_failed" if tls.tls_failure(error) else "no_answer"
    if len(raw) > CHECK_LIMIT:
        return "not_immich"
    try:
        found = json.loads(raw)
    except ValueError:
        return "not_immich"
    return "answered" if isinstance(found, dict) and found.get("res") == "pong" else "not_immich"


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

    def open(self, method: str, path: str, key: str, body: object | None = None, *, importing: bool = False,
             upload: MultipartUpload | None = None) -> contextlib.AbstractContextManager[Any]:
        require_allowed_request(method, path, importing=importing)
        host = urlsplit(self.url).hostname or ""
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if key:
            headers["x-api-key"] = key
        data: bytes | MultipartUpload | None = b"" if method == "POST" else None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if upload is not None:
            headers |= upload.headers
            data = upload
        timeout = self.timeout if upload is None else max(self.timeout, UPLOAD_TIMEOUT)
        return tls.urlopen(f"{self.url}{path}", method=method, headers=headers, data=data, timeout=timeout, allow_http=config.local_host(host))

    def call(self, method: str, path: str, keyed: bool = True, key: str | None = None, body: object | None = None, *, importing: bool = False) -> object:
        try:
            with self.open(method, path, (key or self.admin_key) if keyed else "", body, importing=importing) as response:
                raw = response.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            denial = denial_of(error)
            if error.code in (401, 403):
                raise ImmichFailed(f"Immich refused the {'person key' if key else 'admin key'} ({error.code})", error.code) from None
            if error.code == 400 and not_readable(denial):
                raise NotVisible("Immich answered 400: not found or not readable with this key", 400) from None
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

    def person_key(self, key: str) -> str:
        if not isinstance(key, str) or not key or key == self.admin_key:
            raise ImmichFailed("an upload needs the person's own key")
        return key

    def upload(self, key: str, body: MultipartUpload) -> UploadResult:
        try:
            with self.open("POST", UPLOAD_PATH, self.person_key(key), importing=True, upload=body) as response:
                status, raw = response.status, response.read(CHECK_LIMIT + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code >= 500:
                raise UploadUncertain(f"Immich answered {error.code}", error.code) from None
            raise ImmichFailed(f"Immich refused the upload ({error.code})", error.code) from None
        except tls.Refused as error:
            raise UploadUncertain(str(error)) from None
        except tls.CONNECTION_PROBLEMS as error:
            raise UploadUncertain(f"the upload's answer was lost ({type(error).__name__})") from None
        try:
            return upload_result(status, json.loads(raw) if len(raw) <= CHECK_LIMIT else None)
        except (ValueError, ImmichFailed):
            raise UploadUncertain("the upload's answer was not understood") from None

    def bulk_check(self, key: str, sha1: str) -> BulkMatch | None:
        found = self.call("POST", BULK_CHECK_PATH, key=self.person_key(key), importing=True, body={"assets": [{"id": BULK_CHECK_ID, "checksum": checksum_of(sha1)}]})
        results = found.get("results") if isinstance(found, dict) else None
        entry = results[0] if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict) else {}
        if entry.get("id") == BULK_CHECK_ID and entry.get("action") == "accept":
            return None
        if entry.get("id") == BULK_CHECK_ID and entry.get("action") == "reject" and entry.get("reason") == "duplicate" and is_canonical_id(entry.get("assetId")):
            return BulkMatch(entry["assetId"], entry.get("isTrashed") is not False)
        raise ImmichFailed("Immich's upload check answer was not understood")

    def search_checksum(self, key: str, sha1: str) -> list[Asset]:
        found = self.call("POST", "/api/search/metadata", key=key, body={"checksum": checksum_of(sha1), "size": CHECKSUM_PAGE})
        page = found.get("assets") if isinstance(found, dict) else None
        if not isinstance(page, dict) or not isinstance(page.get("items"), list) or page.get("nextCursor"):
            raise ImmichFailed("Immich's checksum search answer was not understood")
        return [asset_of(entry) for entry in page["items"]]

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
    access: ManagedAccess = "read"

    def __post_init__(self) -> None:
        if not isinstance(self.access, str) or self.access not in MANAGED_GRANTS:
            raise ImmichFailed("managed Immich access must be read or import")
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
        if identity.get("isAdmin") is not False:
            raise ImmichFailed("managed Immich access refuses an administrator's key")
        about = self.client.call("GET", "/api/server/about", key=self.key)
        version = about.get("version") if isinstance(about, dict) else None
        if not isinstance(version, str) or version not in MANAGED_VERSIONS:
            raise ImmichFailed("this Immich version is not supported for managed access")
        found = self.client.call("GET", "/api/api-keys/me", key=self.key)
        permissions = found.get("permissions") if isinstance(found, dict) else None
        required, refusal = MANAGED_GRANTS[self.access]
        if (not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions)
                or frozenset(permissions) != required):
            raise ImmichFailed(refusal)

    def custody(self, asset_id: str) -> dict[str, Any]:
        if not is_canonical_id(asset_id):
            raise ImmichFailed("not a managed asset id")
        self.check_access()
        found = self.client.call("GET", asset_path(asset_id), key=self.key)
        if (not isinstance(found, dict) or found.get("id") != asset_id or found.get("ownerId") != self.owner_id
                or "libraryId" not in found or found["libraryId"] is not None
                or found.get("isTrashed") is not False or found.get("isOffline") is not False
                or found.get("type") not in ("IMAGE", "VIDEO")
                or (found.get("livePhotoVideoId") is not None and not is_canonical_id(found["livePhotoVideoId"]))):
            raise NotCustody("the asset is not an available managed original of this person")
        return found

    def get_asset(self, asset_id: str) -> Asset:
        return asset_of(self.custody(asset_id))

    def extracted(self, asset_id: str) -> tuple[Asset, Extracted | None]:
        found = self.custody(asset_id)
        return asset_of(found), extracted_of(found.get("exifInfo"))

    def upload(self, body: MultipartUpload, sending: Callable[[], None]) -> UploadResult:
        if self.access != "import":
            raise ImmichFailed("only a managed import may upload")
        self.check_access()
        sending()
        return self.client.upload(self.key, body)

    def bulk_check(self, sha1: str) -> BulkMatch | None:
        if self.access != "import":
            raise ImmichFailed("only a managed import may check an upload")
        self.check_access()
        return self.client.bulk_check(self.key, sha1)

    def managed_with_checksum(self, sha1: str) -> list[str]:
        self.check_access()
        return sorted(asset.id for asset in self.client.search_checksum(self.key, sha1) if asset.owner_id == self.owner_id
                      and asset.library_id is None and not asset.trashed and not asset.offline and asset.sha1 == sha1)

    def verify_original(self, asset_id: str, sha1: str, size: int) -> None:
        if not isinstance(sha1, str) or not re.fullmatch(r"[a-f0-9]{40}", sha1) or type(size) is not int or size <= 0:
            raise ImmichFailed("not a recorded source")
        asset = self.get_asset(asset_id)
        if asset.sha1 != sha1:
            raise NotCustody("the asset holds other bytes than the source")
        if sum(len(chunk) for chunk in self.client.original_chunks(self.key, asset)) != size:
            raise ChecksumMismatch("the downloaded original's size differs from the source")

    def original_chunks(self, asset_id: str, expected_sha1: str) -> Iterator[bytes]:
        if not re.fullmatch(r"[a-f0-9]{40}", expected_sha1):
            raise ImmichFailed("not an original checksum")
        asset = self.get_asset(asset_id)
        if asset.sha1 != expected_sha1:
            raise ChecksumMismatch("the managed original changed since it was recorded")
        yield from self.client.original_chunks(self.key, asset)
