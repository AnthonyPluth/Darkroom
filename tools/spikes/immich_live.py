from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import random
import re
import struct
import sys
import threading
import time
import urllib.error
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from PIL import ExifTags, Image, ImageDraw

from darkroom import config, tls
from darkroom.providers import immich

MARKER = "darkroom-spike"
USER_AGENT = "immich-spike/1.0"
GRANTS = frozenset({"user.read", "server.about", "asset.read", "asset.download", "asset.upload"})
EXACT_ROUTES = frozenset({("GET", "/api/server/about"), ("GET", "/api/users/me"), ("GET", "/api/api-keys/me"),
                          ("POST", "/api/assets"), ("POST", "/api/assets/bulk-upload-check"), ("POST", "/api/search/metadata")})
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ASSET_ROUTE = re.compile(rf"/api/assets/{UUID}(?:/original\?edited=false|/thumbnail)?")
ALBUM_ROUTE = re.compile(rf"/api/albums\?assetId={UUID}")
UPDATE_ROUTE = re.compile(rf"/api/assets/{UUID}")
MAX_VIDEO_MB = 256
OPT_IN = "not run: opt in with --enrich"
READ_ONLY_REFUSED = frozenset({"all", "asset.update"})
CHUNK = 1024 * 1024
DENIED = (400, "Not found or no asset.read access")
QUOTA = (400, "Quota has been exceeded!")
DATA_START, DATA_END = "```json immich-live-data", "```"
ROWS = (
    ("grants", "Server version and the key's exact grants (refused before any upload if broader or an admin user)", ""),
    ("new", "New original plus sidecar: created ID, owner, `libraryId: null`, SHA-1/size of the unedited download", ""),
    ("extract", "Extraction completion; EXIF-first and XMP missing-field values incl. UTC offset and GPS", ""),
    ("dup_sidecar", "Duplicate upload with a new sidecar: existing ID and what metadata survives", ""),
    ("concurrent", "Concurrent duplicate uploads reconcile to one asset by owner/checksum", ""),
    ("lost", "Lost response: reconcile by owner/checksum before retry, resume without deleting", ""),
    ("two_users", "Same bytes owned by two users", "not run: needs second key"),
    ("binding", "Owner binding: each key's user, asset owners, the other user's asset refused", "not run: needs second key"),
    ("partner", "Partner-visible assets", "not run: the disposable users have no partner sharing by design"),
    ("trashed", "Trashed duplicate", "not run: needs the after-trash phase (runbook, steps 4 and 5)"),
    ("offline", "Offline / external-library duplicate versus managed upload", "not run: needs an external library, which needs an admin key this harness refuses"),
    ("live", "Generated Live Photo pairing, interrupted between uploads", ""),
    ("raw", "RAW/JPEG pair", ""),
    ("edited", "Edited representations", "not run: needs an edit in Immich's editor and `asset.update`, which this harness refuses"),
    ("video", "Large video and async extraction delay", "not run: opt in with --large-video-mb"),
    ("quota", "Quota error", "not run: no IMMICH_LIVE_QUOTA_KEY"),
    ("revoked", "Revoked-key error", "not run: no IMMICH_LIVE_REVOKED_KEY"),
    ("e1", "E1 Enrichment key: whoami, grants as the server lists them, version", OPT_IN),
    ("e2", "E2 Description fill on an asset with none", OPT_IN),
    ("e3", "E3 Place fill (latitude and longitude together)", OPT_IN),
    ("e4", "E4 Capture time with an offset on a fallback date, and an offsetless edit", OPT_IN),
    ("e5", "E5 Capture time with an offset on a wall clock with none", OPT_IN),
    ("e6", "E6 Update onto a field that has a value", OPT_IN),
    ("e7", "E7 Edit in Immich between a read and an update", "not run: needs an edit in Immich's own interface, timed by hand"),
    ("e8", "E8 Sidecar Immich writes after an update, and re-extraction", "not run: needs the owner to look at Immich's storage and an admin to run extraction"),
    ("e9", "E9 Null date, null place and empty description", OPT_IN),
    ("e10", "E10 Update with a read-only key; reads with the all-permission key", "not run: no IMMICH_LIVE_ENRICH_READ_KEY"),
    ("e11", "E11 Update of an asset in the locked folder", "not run: needs an asset moved to the locked folder by hand"),
    ("e12", "E12 Update of a trashed asset", "not run: needs an asset trashed by hand"),
    ("e13", "E13 Update of another user's asset", "not run: no IMMICH_LIVE_ENRICH_KEY_2"),
    ("e14", "E14 Update with a revoked key", "not run: no IMMICH_LIVE_REVOKED_KEY"),
    ("e15", "E15 The same update twice, then the reconcile's window and checksum search", OPT_IN),
    ("e16", "E16 Capture-time search for an enriched date", OPT_IN),
    ("e17", "E17 Update while Immich's sidecar job queue is paused", "not run: needs an admin to pause the queue"),
    ("e18", "E18 Album list before and after an update", OPT_IN),
    ("no_delete", "Managed imports need no `asset.delete` or admin grant", ""),
)
NOT_RUN = {key: reason for key, _, reason in ROWS if reason}


class Refused(Exception):
    pass


class Failed(Exception):
    def __init__(self, text: str, status: int | None = None, reply: str = "") -> None:
        super().__init__(text)
        self.status, self.reply = status, reply


def allowed(method: str, path: str, enriching: bool = False) -> None:
    if (method, path) in EXACT_ROUTES or (method == "GET" and (ASSET_ROUTE.fullmatch(path) or ALBUM_ROUTE.fullmatch(path))):
        return
    if enriching and method == "PUT" and UPDATE_ROUTE.fullmatch(path):
        return
    raise Refused("the harness does not send this request")


def check_address(url: str, allow_public: bool) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise Refused("IMMICH_URL is not a valid address") from None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise Refused("IMMICH_URL must be scheme://host[:port] with nothing else")
    if config.local_host(host):
        return "private or loopback address"
    if not allow_public:
        raise Refused("IMMICH_URL is not a private or loopback address; see --allow-public-address in the runbook")
    if parts.scheme != "https":
        raise Refused("a public IMMICH_URL must use https")
    return "public https address, allowed by --allow-public-address"


@dataclass
class Api:
    url: str
    key: str = field(repr=False)
    timeout: float = 300.0
    enriching: bool = False

    def open(self, method: str, path: str, data: bytes | None, headers: dict[str, str] | None) -> Any:
        allowed(method, path, self.enriching)
        sent = {"Accept": "application/json", "User-Agent": USER_AGENT, "x-api-key": self.key, **(headers or {})}
        host = urlsplit(self.url).hostname or ""
        return tls.urlopen(self.url.rstrip("/") + path, method=method, data=data, headers=sent, timeout=self.timeout, allow_http=config.local_host(host))

    def call(self, method: str, path: str, body: object = None, raw: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        if body is not None:
            raw, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
        try:
            with self.open(method, path, raw, headers) as response:
                status, payload = response.status, response.read(immich.MAX_REPLY + 1)
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read(4096)
            error.close()
        except tls.Refused:
            raise Failed("the server answered with a redirect, which is not followed") from None
        except tls.CONNECTION_PROBLEMS as error:
            raise Failed(f"Immich could not be reached ({type(error).__name__})") from None
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, None

    def download(self, asset_id: str) -> tuple[str, int]:
        digest, total = hashlib.sha1(usedforsecurity=False), 0
        try:
            with self.open("GET", f"/api/assets/{asset_id}/original?edited=false", None, None) as response:
                while chunk := response.read(CHUNK):
                    digest.update(chunk)
                    total += len(chunk)
        except urllib.error.HTTPError as error:
            error.close()
            raise Failed(f"the original download answered {error.code}") from None
        except tls.CONNECTION_PROBLEMS as error:
            raise Failed(f"the original download broke off ({type(error).__name__})") from None
        return digest.hexdigest(), total


def message(found: object) -> str:
    text = found.get("message") if isinstance(found, dict) else None
    return re.sub(r"[^\w .,:!'()-]", "", str(text))[:120] if text else "no message"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(bytes.fromhex(sha1(data))).decode()


def owner_hash(user_id: str) -> str:
    return hashlib.sha256(f"{MARKER}:{user_id}".encode()).hexdigest()[:16]


def jpeg(seed: str, exif: bytes = b"", size: tuple[int, int] = (64, 48), quality: int = 90) -> bytes:
    rng = random.Random(seed)
    image = Image.new("RGB", size, (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        draw.rectangle((x, y, x + rng.randrange(1, size[0]), y + rng.randrange(1, size[1])), fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, exif=exif)
    return out.getvalue()


def exif_of(taken: datetime, lat: float, lon: float) -> bytes:
    exif = Image.Exif()
    tags = exif.get_ifd(ExifTags.IFD.Exif)
    tags[ExifTags.Base.DateTimeOriginal] = taken.strftime("%Y:%m:%d %H:%M:%S")
    tags[ExifTags.Base.OffsetTimeOriginal] = taken.strftime("%z")[:3] + ":" + taken.strftime("%z")[3:]
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    dms = lambda value: (float(int(abs(value))), float(int(abs(value) * 60) % 60), round(abs(value) * 3600 % 60, 4))
    gps.update({1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)})
    return exif.tobytes()


def xmp(taken: datetime | None, lat: float | None, lon: float | None, description: str) -> bytes:
    coordinate = lambda value, refs: f"{int(abs(value))},{abs(value) * 60 % 60:.6f}{refs[0] if value >= 0 else refs[1]}"
    fields = f' exif:DateTimeOriginal="{taken.isoformat()}"' if taken else ""
    if lat is not None and lon is not None:
        fields += f' exif:GPSLatitude="{coordinate(lat, "NS")}" exif:GPSLongitude="{coordinate(lon, "EW")}"'
    return (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description rdf:about="" xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"{fields}>'
            f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{description}</rdf:li></rdf:Alt></dc:description>'
            f'</rdf:Description></rdf:RDF></x:xmpmeta>').encode()


def dng(seed: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 24), tuple(random.Random(seed).randbytes(3))).save(out, "TIFF", tiffinfo={50706: b"\x01\x04\x00\x00", 271: "Darkroom spike", 272: "Generated"})
    return out.getvalue()


def avi(seed: str, megabytes: float, frame_size: tuple[int, int] = (320, 240)) -> bytes:
    rng = random.Random(seed)
    frames = [jpeg(f"{seed}-{n}", size=frame_size, quality=95) for n in range(4)]
    image = Image.frombytes("RGB", frame_size, rng.randbytes(frame_size[0] * frame_size[1] * 3))
    noise = io.BytesIO()
    image.save(noise, "JPEG", quality=95)
    frames.append(noise.getvalue())
    count = max(3, int(megabytes * CHUNK * len(frames)) // sum(len(f) for f in frames) + 1)
    chosen = [frames[n % len(frames)] for n in range(count)]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return tag + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) % 2 else b"")

    def listed(kind: bytes, body: bytes) -> bytes:
        return chunk(b"LIST", kind + body)
    width, height = frame_size
    largest = max(len(f) for f in chosen)
    avih = struct.pack("<10I4x4x4x4x", 100000, largest * 10, 0, 0x10, count, 0, 1, largest, width, height)
    strh = struct.pack("<4s4sIHHIIIIIIIIhhhh", b"vids", b"MJPG", 0, 0, 0, 0, 1, 10, 0, count, largest, 0xFFFFFFFF, 0, 0, 0, width, height)
    strf = struct.pack("<IiiHH4sIiiII", 40, width, height, 1, 24, b"MJPG", width * height * 3, 0, 0, 0, 0)
    movi, index, offset = b"", b"", 4
    for frame in chosen:
        piece = chunk(b"00dc", frame)
        index += b"00dc" + struct.pack("<III", 0x10, offset, len(frame))
        movi += piece
        offset += len(piece)
    body = b"AVI " + listed(b"hdrl", chunk(b"avih", avih) + listed(b"strl", chunk(b"strh", strh) + chunk(b"strf", strf))) + listed(b"movi", movi) + chunk(b"idx1", index)
    return b"RIFF" + struct.pack("<I", len(body)) + body


@dataclass
class Account:
    label: str
    api: Api
    user_id: str = field(default="", repr=False)
    grants: list[str] = field(default_factory=list)
    quota: str = ""


@dataclass
class Run:
    run_id: str
    poll_seconds: float = 2.0
    extraction_timeout: float = 180.0
    rows: dict[str, tuple[str, str]] = field(default_factory=dict)
    created: list[dict[str, str]] = field(default_factory=list)
    version: str = ""
    address: str = ""
    accounts: dict[str, dict[str, object]] = field(default_factory=dict)
    shared: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def name(self, label: str, suffix: str) -> str:
        return f"{MARKER}-{self.run_id}-{label}.{suffix}"

    def upload(self, account: Account, label: str, suffix: str, data: bytes, sidecar: bytes | None = None,
               live: str | None = None, taken: str = "2024-01-02T03:04:05+00:00") -> immich.UploadResult:
        name = self.name(label, suffix)
        side = immich.UploadSource(io.BytesIO(sidecar), len(sidecar), sha1(sidecar)) if sidecar else None
        body = immich.MultipartUpload(immich.UploadSource(io.BytesIO(data), len(data), sha1(data)), name, taken, taken, side, live)
        status, found = account.api.call("POST", "/api/assets", raw=b"".join(body), headers=body.headers)
        if status not in (200, 201):
            raise Failed(f"upload of {label} answered {status} ({message(found)})", status, message(found))
        try:
            result = immich.upload_result(status, found)
        except immich.ImmichFailed as error:
            raise Failed(f"upload of {label}: {error}") from None
        with self.lock:
            if not any(c["id"] == result.asset_id for c in self.created):
                self.created.append({"label": label, "file": name, "id": result.asset_id, "account": account.label})
        return result

    def asset(self, account: Account, asset_id: str) -> dict[str, Any]:
        status, found = account.api.call("GET", f"/api/assets/{asset_id}")
        if status != 200 or not isinstance(found, dict):
            raise Failed(f"reading asset answered {status}")
        return found

    def verify(self, account: Account, asset_id: str, data: bytes) -> dict[str, Any]:
        found = self.asset(account, asset_id)
        problems = [what for what, bad in (
            ("id", found.get("id") != asset_id), ("owner", found.get("ownerId") != account.user_id),
            ("libraryId not null", "libraryId" not in found or found["libraryId"] is not None),
            ("trashed", found.get("isTrashed") is not False), ("offline", found.get("isOffline") is not False),
            ("checksum", found.get("checksum") != b64(data))) if bad]
        digest, size = account.api.download(asset_id)
        if digest != sha1(data) or size != len(data):
            problems.append("downloaded original differs")
        if problems:
            raise Failed("asset check failed: " + ", ".join(problems))
        return found

    def bulk_check(self, account: Account, data: bytes) -> dict[str, Any]:
        status, found = account.api.call("POST", "/api/assets/bulk-upload-check", {"assets": [{"id": "probe", "checksum": b64(data)}]})
        results = found.get("results") if isinstance(found, dict) else None
        if status != 200 or not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise Failed(f"bulk upload check answered {status}")
        return results[0]

    def by_checksum(self, account: Account, data: bytes) -> list[str]:
        status, found = account.api.call("POST", "/api/search/metadata", {"checksum": b64(data), "size": 10})
        items = found.get("assets", {}).get("items") if isinstance(found, dict) else None
        if status != 200 or not isinstance(items, list):
            raise Failed(f"checksum search answered {status}")
        return sorted(str(item.get("id")) for item in items if isinstance(item, dict))

    def wait(self, account: Account, asset_id: str, ready: Callable[[dict[str, Any]], bool], timeout: float | None = None) -> tuple[dict[str, Any], float | None]:
        started = time.monotonic()
        limit = self.extraction_timeout if timeout is None else timeout
        while True:
            found = self.asset(account, asset_id)
            if ready(found):
                return found, round(time.monotonic() - started, 1)
            if time.monotonic() - started >= limit:
                return found, None
            time.sleep(self.poll_seconds)

    def waited(self, delay: float | None) -> str:
        return f"after {delay} s" if delay is not None else f"extraction timed out after {self.extraction_timeout:g} s"

    def step(self, row: str, action: Callable[[], tuple[str, str]]) -> None:
        try:
            self.rows[row] = action()
        except (Failed, Refused) as error:
            self.rows[row] = ("fail", str(error))
        except (immich.ImmichFailed, OSError, ValueError) as error:
            self.rows[row] = ("fail", f"unexpected {type(error).__name__}")


def exif_values(found: dict[str, Any]) -> dict[str, object]:
    info = found.get("exifInfo") if isinstance(found.get("exifInfo"), dict) else {}
    return {name: info.get(name) for name in ("dateTimeOriginal", "timeZone", "latitude", "longitude", "description")}


def instant(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def near(value: object, wanted: float) -> bool:
    return isinstance(value, (int, float)) and abs(float(value) - wanted) < 1e-3


def matches(found: dict[str, Any], taken: datetime, lat: float, lon: float, description: str | None = None) -> bool:
    values = exif_values(found)
    return (instant(values["dateTimeOriginal"]) == taken and bool(values["timeZone"]) and near(values["latitude"], lat) and near(values["longitude"], lon)
            and (description is None or values["description"] == description))


def preflight(run: Run, account: Account) -> None:
    status, me = account.api.call("GET", "/api/users/me")
    if status != 200 or not isinstance(me, dict) or not immich.is_canonical_id(me.get("id")):
        raise Refused(f"account {account.label}: the key was not accepted ({status})")
    if me.get("isAdmin") is not False:
        raise Refused(f"account {account.label}: the key belongs to an admin user; use a disposable non-admin user")
    status, key = account.api.call("GET", "/api/api-keys/me")
    grants = key.get("permissions") if isinstance(key, dict) else None
    if status != 200 or not isinstance(grants, list) or not all(isinstance(g, str) for g in grants):
        raise Refused(f"account {account.label}: the server did not list the key's grants ({status})")
    if frozenset(grants) != GRANTS:
        extra, missing = sorted(set(grants) - GRANTS), sorted(GRANTS - set(grants))
        raise Refused(f"account {account.label}: the key must have exactly {sorted(GRANTS)}; extra {extra}, missing {missing}")
    status, about = account.api.call("GET", "/api/server/about")
    version = about.get("version") if isinstance(about, dict) else None
    if status != 200 or not isinstance(version, str):
        raise Refused("the server version could not be read")
    if run.version and version != run.version:
        raise Refused("the server version differs from the earlier result's; start a new result")
    status, listed = account.api.call("POST", "/api/search/metadata", {"size": 100})
    items = listed.get("assets", {}).get("items") if isinstance(listed, dict) else None
    if status != 200 or not isinstance(items, list):
        raise Refused(f"account {account.label}: its assets could not be listed ({status})")
    if any(not str(item.get("originalFileName", "")).startswith(MARKER) for item in items if isinstance(item, dict)):
        raise Refused(f"account {account.label}: the user has assets this harness did not create; use an empty disposable user")
    account.user_id, account.grants, run.version = me["id"], sorted(grants), version
    if isinstance(me.get("quotaSizeInBytes"), int):
        account.quota = f"quota {me['quotaSizeInBytes']} bytes, used {me.get('quotaUsageInBytes')}"
    run.accounts[account.label] = {"owner": owner_hash(account.user_id), "grants": account.grants}


def single_account(run: Run, a: Account, large_video_mb: float) -> None:
    exif_taken = datetime(2021, 6, 15, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    xmp_taken = datetime(2019, 3, 2, 8, 15, tzinfo=timezone(timedelta(hours=-5)))
    exif_bytes = jpeg(f"{run.run_id}-exif", exif_of(exif_taken, 10.5, -20.25))
    bare_bytes = jpeg(f"{run.run_id}-xmp")
    described = f"{MARKER} {run.run_id} sidecar"
    ids: dict[str, str] = {}

    def new() -> tuple[str, str]:
        result = run.upload(a, "exif", "jpg", exif_bytes, xmp(None, None, None, described))
        bare = run.upload(a, "xmp", "jpg", bare_bytes, xmp(xmp_taken, 12.25, 45.75, described))
        if (result.status, bare.status) != ("created", "created"):
            raise Failed(f"expected two new assets, got {result.status} and {bare.status}")
        ids.update(exif=result.asset_id, xmp=bare.asset_id)
        run.verify(a, result.asset_id, exif_bytes)
        run.verify(a, bare.asset_id, bare_bytes)
        run.verify(a, run.shared["asset"], jpeg(f"{run.run_id}-shared"))
        return "pass", f"201 created `{result.asset_id}` and `{bare.asset_id}`; owner = key's user, `libraryId: null`; unedited download SHA-1 {sha1(exif_bytes)[:12]}… / {len(exif_bytes)} bytes and {sha1(bare_bytes)[:12]}… / {len(bare_bytes)} bytes equal the input"
    run.step("new", new)

    def extract() -> tuple[str, str]:
        if not ids:
            raise Failed("no assets from the previous step")
        found_exif, exif_delay = run.wait(a, ids["exif"], lambda f: matches(f, exif_taken, 10.5, -20.25))
        found_xmp, xmp_delay = run.wait(a, ids["xmp"], lambda f: matches(f, xmp_taken, 12.25, 45.75, described))
        conflict_taken = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
        conflict = run.upload(a, "conflict", "jpg", jpeg(f"{run.run_id}-conflict", exif_of(datetime(2020, 1, 1, 10, 0, tzinfo=UTC), 1.0, 1.0)), xmp(conflict_taken, 2.0, 2.0, described))
        found_conflict, conflict_delay = run.wait(a, conflict.asset_id, lambda f: exif_values(f)["dateTimeOriginal"] is not None)
        winner = {conflict_taken: "XMP", datetime(2020, 1, 1, 10, 0, tzinfo=UTC): "EXIF"}.get(instant(exif_values(found_conflict)["dateTimeOriginal"]), "neither")
        delays = [run.waited(d) for d in (exif_delay, xmp_delay, conflict_delay)]
        detail = (f"EXIF-only: {exif_values(found_exif)} {delays[0]}; XMP-only: {exif_values(found_xmp)} {delays[1]}; "
                  f"conflicting EXIF 10:00Z vs XMP 12:00Z: {winner} date won ({exif_values(found_conflict)['dateTimeOriginal']}) {delays[2]}")
        return ("pass" if None not in (exif_delay, xmp_delay, conflict_delay) and winner != "neither" else "fail"), detail
    run.step("extract", extract)

    def dup_sidecar() -> tuple[str, str]:
        if "exif" not in ids:
            raise Failed("no asset from the new-original step")
        first = run.asset(a, ids["exif"])
        if not matches(first, exif_taken, 10.5, -20.25):
            return "not verified", "the first asset's metadata had not been extracted, so survival cannot be judged"
        before = exif_values(first)
        again = run.upload(a, "exif-again", "jpg", exif_bytes, xmp(None, 33.0, 33.0, f"{MARKER} {run.run_id} second sidecar"))
        if (again.status, again.asset_id) != ("duplicate", ids["exif"]):
            raise Failed(f"expected 200 duplicate of the first asset, got {again.status}")
        after, _ = run.wait(a, ids["exif"], lambda f: exif_values(f) != before, timeout=min(30.0, run.extraction_timeout))
        survived = "unchanged" if exif_values(after) == before else f"changed to {exif_values(after)}"
        return "pass", f"200 duplicate with the existing ID; metadata after the new sidecar: {survived}"
    run.step("dup_sidecar", dup_sidecar)

    def concurrent() -> tuple[str, str]:
        data = jpeg(f"{run.run_id}-concurrent")
        barrier, results, errors = threading.Barrier(3), [], []

        def one(_: int) -> None:
            barrier.wait()
            try:
                results.append(run.upload(a, "concurrent", "jpg", data))
            except Failed as error:
                errors.append(str(error))
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(one, range(3)))
        found = run.by_checksum(a, data)
        statuses = sorted(r.status for r in results)
        if len(found) != 1 or errors or statuses != ["created", "duplicate", "duplicate"] or any(r.asset_id != found[0] for r in results):
            raise Failed(f"expected one created and two duplicates of one asset; found {len(found)}, statuses {statuses}, errors {errors}")
        check = run.bulk_check(a, data)
        if (check.get("action"), check.get("reason"), check.get("assetId")) != ("reject", "duplicate", found[0]):
            raise Failed(f"the bulk check answered {check.get('action')}/{check.get('reason')} without that ID")
        run.verify(a, found[0], data)
        return "pass", f"3 simultaneous uploads: {statuses}, errors {errors}; checksum search finds one asset; bulk check {check.get('action')}/{check.get('reason')} with that ID"
    run.step("concurrent", concurrent)

    def lost() -> tuple[str, str]:
        data = jpeg(f"{run.run_id}-lost")
        run.upload(a, "lost", "jpg", data)
        check = run.bulk_check(a, data)
        found = run.by_checksum(a, data)
        if check.get("action") != "reject" or check.get("reason") != "duplicate" or len(found) != 1 or check.get("assetId") != found[0]:
            raise Failed(f"reconcile did not find exactly one asset (bulk check {check.get('action')}, search {len(found)})")
        run.verify(a, found[0], data)
        retry = run.upload(a, "lost", "jpg", data)
        if (retry.status, retry.asset_id) != ("duplicate", found[0]):
            raise Failed(f"retry gave {retry.status}")
        return "pass", "response discarded on purpose; bulk check and checksum search found the one asset, verified it, and the retry was a 200 duplicate of it"
    run.step("lost", lost)

    def live() -> tuple[str, str]:
        video_bytes, still_bytes = avi(f"{run.run_id}-live", 0.05), jpeg(f"{run.run_id}-live-still")
        video = run.upload(a, "live-video", "avi", video_bytes)
        resumed = run.bulk_check(a, video_bytes)
        if resumed.get("assetId") != video.asset_id:
            raise Failed("after the interruption the video was not found by checksum")
        still = run.upload(a, "live-still", "jpg", still_bytes, live=video.asset_id)
        found = run.verify(a, still.asset_id, still_bytes)
        video_found = run.verify(a, video.asset_id, video_bytes)
        linked = found.get("livePhotoVideoId") == video.asset_id
        return ("pass" if linked else "fail"), f"video uploaded, then found again by checksum before the still; still `livePhotoVideoId` {'is' if linked else 'is not'} the video; video visibility `{video_found.get('visibility')}`"
    run.step("live", live)

    def raw() -> tuple[str, str]:
        raw_bytes, jpg_bytes = dng(f"{run.run_id}-raw"), jpeg(f"{run.run_id}-raw-jpg")
        raw_up, jpg_up = run.upload(a, "pair", "dng", raw_bytes), run.upload(a, "pair", "jpg", jpg_bytes)
        found_raw, found_jpg = run.verify(a, raw_up.asset_id, raw_bytes), run.verify(a, jpg_up.asset_id, jpg_bytes)
        if (found_raw.get("type"), found_jpg.get("type")) != ("IMAGE", "IMAGE") or raw_up.asset_id == jpg_up.asset_id:
            raise Failed(f"expected two distinct IMAGE assets, got types {found_raw.get('type')}/{found_jpg.get('type')}")
        return "pass", f"two assets, bytes verified; types {found_raw.get('type')}/{found_jpg.get('type')}; stack {bool(found_raw.get('stack') or found_jpg.get('stack'))}"
    run.step("raw", raw)

    if large_video_mb > 0:
        def video() -> tuple[str, str]:
            data = avi(f"{run.run_id}-large", large_video_mb, (640, 480))
            started = time.monotonic()
            up = run.upload(a, "large-video", "avi", data)
            took = round(time.monotonic() - started, 1)
            run.verify(a, up.asset_id, data)
            found, delay = run.wait(a, up.asset_id, lambda f: type(f.get("duration")) is int and f["duration"] > 0)
            return ("pass" if delay is not None else "fail"), f"{len(data)} bytes uploaded in {took}s, bytes verified; duration `{found.get('duration')}` ms {run.waited(delay)}"
        run.step("video", video)


def two_accounts(run: Run, b: Account, data: bytes, a_asset: str, a_owner: str) -> None:
    def two_users() -> tuple[str, str]:
        up = run.upload(b, "shared", "jpg", data)
        if up.status != "created" or up.asset_id == a_asset:
            raise Failed(f"the second user's upload gave {up.status} with {'the same' if up.asset_id == a_asset else 'a new'} ID")
        run.verify(b, up.asset_id, data)
        return "pass", "the second user's upload of the same bytes was 201 created with its own ID and owner"
    run.step("two_users", two_users)

    def binding() -> tuple[str, str]:
        if owner_hash(b.user_id) == a_owner:
            raise Failed("both keys belong to the same user")
        status, found = b.api.call("GET", f"/api/assets/{a_asset}")
        if status == 200:
            raise Failed("the second user could read the first user's asset")
        if (status, message(found)) != DENIED:
            return "not verified", f"the other user's asset answered {status} ({message(found)}), not Immich's 400 access denial"
        return "pass", f"keys belong to different non-admin users; each asset's owner is its uploader; the other user's asset answered 400 ({DENIED[1]})"
    run.step("binding", binding)


def optional_keys(run: Run, url: str, account: Account | None, revoked_key: str) -> None:
    if account is not None:
        def quota() -> tuple[str, str]:
            data = jpeg(f"{run.run_id}-quota", size=(256, 256))
            try:
                run.upload(account, "quota", "jpg", data)
            except Failed as error:
                if (error.status, error.reply) != QUOTA:
                    return "not verified", f"{account.quota}; the upload failed without Immich's quota refusal: {error}"
                stored = run.bulk_check(account, data).get("action") != "accept"
                return ("fail" if stored else "pass"), f"{account.quota}; {error}; the bytes were {'stored anyway' if stored else 'not stored'}"
            return "fail", f"{account.quota}; the upload was accepted"
        run.step("quota", quota)
    if revoked_key:
        def revoked() -> tuple[str, str]:
            api = Api(url, revoked_key)
            me, _ = api.call("GET", "/api/users/me")
            check, _ = api.call("POST", "/api/assets/bulk-upload-check", {"assets": [{"id": "probe", "checksum": b64(b"x")}]})
            ok = me == 401 and check == 401
            return ("pass" if ok else "fail"), f"`/api/users/me` answered {me}, bulk upload check answered {check}"
        run.step("revoked", revoked)


def after_trash(run: Run, a: Account, previous: dict[str, Any]) -> None:
    def trashed() -> tuple[str, str]:
        data = jpeg(f"{previous['run_id']}-trash-me")
        asset_id = previous["trash_me"]
        before = run.asset(a, asset_id)
        if before.get("isTrashed") is not True:
            raise Failed("the trash-me asset is not in the trash; trash it in the web UI first")
        check = run.bulk_check(a, data)
        up = run.upload(a, "trash-me-again", "jpg", data)
        after = run.asset(a, asset_id)
        if check.get("action") not in ("accept", "reject") or not isinstance(after.get("isTrashed"), bool):
            raise Failed("the bulk check or the trashed asset's state was missing from Immich's answers")
        return "pass", (f"bulk check {check.get('action')}/{check.get('reason')}, isTrashed {check.get('isTrashed')}; upload {up.status} "
                        f"{'with the trashed ID' if up.asset_id == asset_id else 'with a new ID'}; trashed asset still trashed: {after.get('isTrashed') is True}")
    run.step("trashed", trashed)


def wall_exif(wall: datetime) -> bytes:
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = wall.strftime("%Y:%m:%d %H:%M:%S")
    return exif.tobytes()


def preflight_enrich(run: Run, account: Account, owner: Account, same_user: bool, wants_all: bool) -> None:
    status, me = account.api.call("GET", "/api/users/me")
    if status != 200 or not isinstance(me, dict) or not immich.is_canonical_id(me.get("id")):
        raise Refused(f"account {account.label}: the key was not accepted ({status})")
    if me.get("isAdmin") is not False:
        raise Refused(f"account {account.label}: the key belongs to an admin user; use a disposable non-admin user")
    status, key = account.api.call("GET", "/api/api-keys/me")
    grants = key.get("permissions") if isinstance(key, dict) else None
    if status != 200 or not isinstance(grants, list) or not all(isinstance(g, str) for g in grants):
        raise Refused(f"account {account.label}: the server did not list the key's grants ({status})")
    if not wants_all and READ_ONLY_REFUSED & set(grants):
        raise Refused(f"account {account.label}: the read-only key must hold neither `all` nor `asset.update`")
    if (me["id"] == owner.user_id) != same_user:
        raise Refused(f"account {account.label}: the key must belong to {'the same user as the import key' if same_user else 'a second user'}")
    account.user_id, account.grants = me["id"], sorted(grants)
    run.accounts[account.label] = {"owner": owner_hash(account.user_id), "grants": account.grants}


def changed(before: dict[str, Any], after: dict[str, Any], expected: tuple[str, ...]) -> list[str]:
    return [name for name in sorted(set(before) | set(after)) if name not in expected and before.get(name) != after.get(name)]


def shown(found: dict[str, Any]) -> dict[str, Any]:
    info = found.get("exifInfo") if isinstance(found.get("exifInfo"), dict) else {}
    return {**exif_values(found), **{n: found.get(n) for n in ("ownerId", "libraryId", "checksum", "visibility", "isFavorite", "isTrashed", "isOffline", "type", "livePhotoVideoId")},
            "rating": info.get("rating"), "city": info.get("city"), "fileCreatedAt": found.get("fileCreatedAt"), "localDateTime": found.get("localDateTime")}


def enrich(run: Run, a: Account, e: Account, e2: Account | None, read: Account | None, revoked: Account | None, settle: float) -> None:
    ids: dict[str, str] = {}
    blobs: dict[str, bytes] = {}
    sent = datetime(2019, 7, 4, 12, 30, 15, tzinfo=timezone(timedelta(hours=2)))

    def fixture(label: str, data: bytes) -> str:
        up = run.upload(a, f"e-{label}", "jpg", data)
        blobs[label] = data
        ids[label] = up.asset_id
        if run.wait(a, up.asset_id, lambda f: isinstance(f.get("exifInfo"), dict))[1] is None:
            raise Failed(f"the fixture {label} was not extracted in time")
        return up.asset_id

    def put(who: Account, asset_id: str, body: dict[str, object]) -> tuple[int, Any]:
        return who.api.call("PUT", f"/api/assets/{asset_id}", body)

    def view(label: str) -> dict[str, Any]:
        return shown(run.asset(a, ids[label]))

    def intact(label: str, data: bytes | None = None) -> bool:
        return a.api.download(ids[label]) == (sha1(data or blobs[label]), len(data or blobs[label]))

    def updated(label: str, body: dict[str, object], expect: tuple[str, ...]) -> tuple[int, dict[str, Any], dict[str, Any], list[str]]:
        before = view(label)
        status, answer = put(e, ids[label], body)
        if status != 200:
            raise Failed(f"the update answered {status} ({message(answer)})")
        after = view(label)
        return status, before, after, changed(before, after, expect)

    def plain(label: str) -> str:
        return fixture(label, jpeg(f"{run.run_id}-e-{label}"))

    def e1() -> tuple[str, str]:
        listed = ", ".join(f"`{g}`" for g in e.grants)
        return "pass", f"`users/me` is the harness user (owner {owner_hash(e.user_id)}), non-admin, the same as the import key's; `api-keys/me` lists {listed}; server `{run.version}`"
    run.step("e1", e1)

    def e2_row() -> tuple[str, str]:
        plain("desc")
        text = f"{MARKER} {run.run_id} description"
        status, before, after, other = updated("desc", {"description": text}, ("description",))
        ok = after["description"] == text and not other and intact("desc")
        return ("pass" if ok else "fail"), f"{status}; description {before['description']!r} to {after['description']!r}; other fields changed: {other or 'none'}; unedited download with the import key {'equals' if ok else 'differs from'} the input"
    run.step("e2", e2_row)

    def e3() -> tuple[str, str]:
        plain("place")
        lonely = put(e, ids["place"], {"latitude": 12.5})[0]
        status, before, after, other = updated("place", {"latitude": 12.5, "longitude": -20.25}, ("latitude", "longitude", "city", "state", "country"))
        ok = lonely == 400 and near(after["latitude"], 12.5) and near(after["longitude"], -20.25) and not other
        return ("pass" if ok else "fail"), f"latitude alone answered {lonely}; both answered {status}; place {before['latitude']},{before['longitude']} to {after['latitude']},{after['longitude']}; `city` {before['city']!r} to {after['city']!r}; other fields changed: {other or 'none'}"
    run.step("e3", e3)

    def wait_state(label: str, wanted: datetime) -> dict[str, Any]:
        found, _ = run.wait(a, ids[label], lambda f: instant(exif_values(f)["dateTimeOriginal"]) == wanted, timeout=min(30.0, run.extraction_timeout))
        return shown(found)

    def e4() -> tuple[str, str]:
        plain("fallback")
        plain("naive")
        fresh = view("fallback")
        status, _, now, _ = updated("fallback", {"dateTimeOriginal": sent.isoformat()}, ("dateTimeOriginal", "timeZone"))
        naive_status = put(e, ids["naive"], {"dateTimeOriginal": "2019-07-04T12:30:15"})[0]
        naive = wait_state("naive", datetime(2019, 7, 4, 12, 30, 15, tzinfo=UTC)) if naive_status == 200 else {}
        time.sleep(settle)
        later, naive_later = view("fallback"), view("naive")
        ok = instant(now["dateTimeOriginal"]) == sent and bool(now["timeZone"])
        fields = lambda v: f"dateTimeOriginal {v['dateTimeOriginal']}, timeZone {v['timeZone']}, fileCreatedAt {v['fileCreatedAt']}, localDateTime {v['localDateTime']}"
        return ("pass" if ok else "fail"), (f"fresh fallback: {fields(fresh)}; offset edit answered {status}: now {fields(now)}; after {settle:g} s {fields(later)}; "
                                            f"offsetless edit answered {naive_status}: {fields(naive) if naive else 'not read'}; after {settle:g} s {fields(naive_later)}; "
                                            f"`dateTimeOriginal` equals `fileCreatedAt` on the fresh fallback: {instant(fresh['dateTimeOriginal']) == instant(fresh['fileCreatedAt'])}, after the offset edit: {instant(later['dateTimeOriginal']) == instant(later['fileCreatedAt'])}")
    run.step("e4", e4)

    def e5() -> tuple[str, str]:
        fixture("wall", jpeg(f"{run.run_id}-e-wall", wall_exif(datetime(2021, 6, 15, 14, 30))))
        status, before, after, _ = updated("wall", {"dateTimeOriginal": "2021-06-15T14:30:00+02:00"}, ("dateTimeOriginal", "timeZone"))
        moved = instant(after["dateTimeOriginal"]) == datetime(2021, 6, 15, 12, 30, tzinfo=UTC)
        return ("pass" if moved and after["timeZone"] else "fail"), (f"{status}; instant {before['dateTimeOriginal']} (zone {before['timeZone']}) to {after['dateTimeOriginal']} (zone {after['timeZone']}); "
                                                                       f"wall clock 14:30 at +02:00 {'is' if moved else 'is not'} the stored instant 12:30Z; `localDateTime` {before['localDateTime']} to {after['localDateTime']}")
    run.step("e5", e5)

    def e6() -> tuple[str, str]:
        if "desc" not in ids:
            raise Failed("no asset from the description row")
        text = f"{MARKER} {run.run_id} overwritten"
        status, before, after, _ = updated("desc", {"description": text}, ("description",))
        return ("pass" if after["description"] == text else "fail"), f"{status}; description {before['description']!r} to {after['description']!r}: the server {'overwrites a present value without an error' if after['description'] == text else 'kept the value'}"
    run.step("e6", e6)

    def e9() -> tuple[str, str]:
        plain("nulls")
        got = [put(e, ids["nulls"], body)[0] for body in ({"dateTimeOriginal": None}, {"latitude": None, "longitude": None}, {"description": ""})]
        ok = got == [400, 400, 200]
        return ("pass" if ok else "fail"), f"null date answered {got[0]}, null place {got[1]}, empty description {got[2]}"
    run.step("e9", e9)

    if read is not None:
        def e10() -> tuple[str, str]:
            plain("readonly")
            before = view("readonly")
            status, answer = put(read, ids["readonly"], {"description": f"{MARKER} refused"})
            digest, size = e.api.download(ids["readonly"])
            thumb = e.api.call("GET", f"/api/assets/{ids['readonly']}/thumbnail")[0]
            ok = status == 403 and "Missing required permission" in message(answer) and view("readonly") == before and (digest, size) == (sha1(blobs["readonly"]), len(blobs["readonly"])) and thumb == 200
            return ("pass" if ok else "fail"), f"read-only key answered {status} ({message(answer)}), asset unchanged; the all-permission key's original read matched the input and its thumbnail answered {thumb}"
        run.step("e10", e10)

    if e2 is not None:
        def e13() -> tuple[str, str]:
            plain("foreign")
            before = view("foreign")
            status, answer = put(e2, ids["foreign"], {"description": f"{MARKER} refused"})
            ok = status == 400 and message(answer) == "Not found or no asset.update access" and view("foreign") == before
            return ("pass" if ok else "fail"), f"the second user's all-permission key answered {status} ({message(answer)}) and the asset is unchanged; the partner-shared case is not run (it needs partner sharing, runbook row 1)"
        run.step("e13", e13)

    if revoked is not None:
        def e14() -> tuple[str, str]:
            plain("revoked")
            before = view("revoked")
            status = put(revoked, ids["revoked"], {"description": f"{MARKER} refused"})[0]
            return ("pass" if status == 401 and view("revoked") == before else "fail"), f"the revoked key answered {status} and the asset is unchanged"
        run.step("e14", e14)

    def e15() -> tuple[str, str]:
        plain("twice")
        first = run.asset(a, ids["twice"])
        body = {"description": f"{MARKER} {run.run_id} twice"}
        put(e, ids["twice"], body)
        once = run.asset(a, ids["twice"])
        statuses = [put(e, ids["twice"], body)[0], put(e, ids["twice"], body)[0]]
        twice = run.asset(a, ids["twice"])
        window = {"gte": first.get("updatedAt"), "lte": (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        listed = a.api.call("POST", "/api/search/metadata", {"filter": {"updatedAt": window}, "size": 100, "orderBy": {"field": "fileCreatedAt", "direction": "asc"}})[1]
        items = listed.get("assets", {}).get("items", []) if isinstance(listed, dict) else []
        count = sum(1 for item in items if isinstance(item, dict) and item.get("id") == ids["twice"])
        by_sum = run.by_checksum(a, blobs["twice"])
        ok = shown(once) == shown(twice) and count == 1 and by_sum == [ids["twice"]] and twice.get("checksum") == first.get("checksum")
        return ("pass" if ok else "fail"), f"the repeated updates answered {statuses}; the end state {'equals' if shown(once) == shown(twice) else 'differs from'} the first update's; the asset appears {count} time(s) in the `updatedAt` window and {len(by_sum)} time(s) by checksum; the checksum is unchanged: {twice.get('checksum') == first.get('checksum')}; `updatedAt` moved after the repeat: {twice.get('updatedAt') != once.get('updatedAt')}"
    run.step("e15", e15)

    def e16() -> tuple[str, str]:
        if "fallback" not in ids:
            raise Failed("no asset from the capture-time row")
        window = {"gte": (sent - timedelta(seconds=1)).isoformat(), "lte": (sent + timedelta(seconds=1)).isoformat()}
        found = a.api.call("POST", "/api/search/metadata", {"filter": {"takenAt": window}, "size": 10, "orderBy": {"field": "fileCreatedAt", "direction": "asc"}})
        items = found[1].get("assets", {}).get("items") if isinstance(found[1], dict) else None
        if found[0] != 200 or not isinstance(items, list):
            raise Failed(f"the capture-time search answered {found[0]}")
        hit = any(isinstance(item, dict) and item.get("id") == ids["fallback"] for item in items)
        return "pass", f"the search for the edited date {'finds' if hit else 'does not find'} the asset ({len(items)} result(s)), so same-shot matching {'sees' if hit else 'does not see'} an enriched date"
    run.step("e16", e16)

    def e18() -> tuple[str, str]:
        if "twice" not in ids:
            raise Failed("no asset from the repeat row")
        route = f"/api/albums?assetId={ids['twice']}"
        before = e.api.call("GET", route)
        put(e, ids["twice"], {"description": f"{MARKER} {run.run_id} albums"})
        after = e.api.call("GET", route)
        ok = before[0] == after[0] == 200 and isinstance(before[1], list) and before[1] == after[1]
        return ("pass" if ok else "fail"), f"the asset's album list answered {before[0]} and {after[0]} with {len(before[1]) if isinstance(before[1], list) else 'no'} and {len(after[1]) if isinstance(after[1], list) else 'no'} album(s); the one-album case is not run (it needs an album made by hand)"
    run.step("e18", e18)


def enrich_run(run: Run, url: str, a: Account, keys: list[str], revoked_key: str, settle: float) -> None:
    first, second, only_read = (Account(label, Api(url, key, enriching=True)) if key else None for label, key in zip(("A-all", "B-all", "A-read"), keys, strict=True))
    if first is None:
        raise Refused("set IMMICH_LIVE_ENRICH_KEY")
    preflight_enrich(run, first, a, True, True)
    if second is not None:
        preflight_enrich(run, second, a, False, True)
    if only_read is not None:
        preflight_enrich(run, only_read, a, True, False)
    enrich(run, a, first, second, only_read, Account("revoked", Api(url, revoked_key, enriching=True)) if revoked_key else None, settle)


def load_data(path: Path) -> dict[str, Any]:
    block = re.findall(re.escape(DATA_START) + r"\n(.*?)\n" + re.escape(DATA_END), path.read_text(), re.S)
    try:
        found = json.loads(block[-1]) if block else None
    except ValueError:
        found = None
    if not isinstance(found, dict) or not isinstance(found.get("run_id"), str) or not isinstance(found.get("version"), str):
        raise Refused("the earlier result's data block is not valid")
    return found


def merged(previous: dict[str, Any], rows: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    result = {k: (v[0], v[1]) for k, v in previous.get("rows", {}).items()}
    for key, (status, detail) in rows.items():
        if status == "not run" and key in result:
            continue
        old = result.get(key)
        if old and old[0] != "not run":
            status = next((s for s in ("fail", "not verified") if s in (status, old[0])), status)
            detail = f"{old[1]}; then {detail}"
        result[key] = (status, detail)
    return result


def report(run: Run, previous: dict[str, Any] | None) -> tuple[str, int]:
    rows = {key: run.rows.get(key, ("not run", NOT_RUN.get(key, "not run"))) for key, _, _ in ROWS}
    if previous:
        rows = merged(previous, rows)
    created = (previous or {}).get("created", []) + run.created
    accounts = {**(previous or {}).get("accounts", {}), **run.accounts}
    data = {"run_id": (previous or {}).get("run_id", run.run_id), "version": run.version, "rows": rows, "created": created,
            "accounts": accounts, "shared": (previous or {}).get("shared") or run.shared,
            "trash_me": (previous or {}).get("trash_me") or next((c["id"] for c in run.created if c["label"] == "trash-me"), "")}
    lines = [f"### Immich live spike result (#266), run `{data['run_id']}`", "",
             f"- Harness: `tools/spikes/immich_live.py`; finished {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC; {run.address}",
             f"- Server version: `{run.version}`"]
    lines += [f"- Account {label} grants: {', '.join(f'`{g}`' for g in info['grants'])}" for label, info in accounts.items()]
    lines += ["- Media: generated in code by the harness; no real photos", "", "| Evidence (#266) | Result | Details |", "| --- | --- | --- |"]
    lines += [f"| {title} | {rows[key][0]} | {rows[key][1].replace('|', '/')} |" for key, title, _ in ROWS]
    lines += ["", f"Created assets ({len(created)}; remove them by hand in the web UI, the harness never deletes):", "",
              "| Account | File name | Asset ID |", "| --- | --- | --- |"]
    lines += [f"| {c['account']} | `{c['file']}` | `{c['id']}` |" for c in created]
    lines += ["", DATA_START, json.dumps(data, indent=1), DATA_END, ""]
    return "\n".join(lines), sum(1 for status, _ in rows.values() if status in ("fail", "not verified"))


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def main(argv: list[str] | None = None, out: TextIO = sys.stdout, environ: dict[str, str] | None = None, poll_seconds: float = 2.0) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.spikes.immich_live")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--append-results", type=Path)
    parser.add_argument("--phase", choices=("main", "second-account", "after-trash"), default="main")
    parser.add_argument("--allow-public-address", action="store_true")
    parser.add_argument("--large-video-mb", type=float, default=0.0)
    parser.add_argument("--extraction-timeout", type=float, default=180.0)
    parser.add_argument("--enrich", action="store_true", help="run only the E rows of section 16 of the enrichment design, with IMMICH_LIVE_ENRICH_KEY")
    parser.add_argument("--settle-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    url, key_a, key_b = env.get("IMMICH_URL", ""), env.get("IMMICH_LIVE_KEY_A", ""), env.get("IMMICH_LIVE_KEY_B", "")
    quota_key, revoked_key = env.get("IMMICH_LIVE_QUOTA_KEY", ""), env.get("IMMICH_LIVE_REVOKED_KEY", "")
    host = urlsplit(url).hostname or ""
    enrich_keys = [env.get(name, "") for name in ("IMMICH_LIVE_ENRICH_KEY", "IMMICH_LIVE_ENRICH_KEY_2", "IMMICH_LIVE_ENRICH_READ_KEY")]
    secrets = [key_a, key_b, quota_key, revoked_key, *enrich_keys, url, host if len(host) >= 4 else ""]
    run: Run | None = None
    try:
        if not url or not key_a:
            raise Refused("set IMMICH_URL and IMMICH_LIVE_KEY_A")
        if any(any(not 33 <= ord(c) <= 126 for c in k) for k in (key_a, key_b, quota_key, revoked_key, *enrich_keys)):
            raise Refused("a key holds characters a header cannot carry")
        if not 0 <= args.large_video_mb <= MAX_VIDEO_MB:
            raise Refused(f"--large-video-mb is limited to {MAX_VIDEO_MB}")
        if args.enrich and (args.phase != "main" or args.large_video_mb or not 0 <= args.settle_seconds <= 3600 or not enrich_keys[0]):
            raise Refused("--enrich runs alone (no other phase, no large video), needs IMMICH_LIVE_ENRICH_KEY and a --settle-seconds of at most 3600")
        if not args.enrich and (args.phase == "main") != (args.append_results is None):
            raise Refused("--append-results goes with --phase second-account or after-trash, and only with them")
        address = check_address(url, args.allow_public_address)
        if not os.environ.get("SSL_CERT_FILE") and (bundle := os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")):
            os.environ["SSL_CERT_FILE"] = bundle
        previous = load_data(args.append_results) if args.append_results else None
        run = Run(previous["run_id"] if previous else uuid.uuid4().hex[:8], poll_seconds=poll_seconds,
                  extraction_timeout=args.extraction_timeout, address=address, version=previous["version"] if previous else "")
        a = Account("B" if args.phase == "second-account" else "A", Api(url, key_a))
        preflight(run, a)
        b = quota = None
        if key_b and args.phase == "main" and not args.enrich:
            b = Account("B", Api(url, key_b))
            preflight(run, b)
        if quota_key and args.phase == "main" and not args.enrich:
            quota = Account("quota", Api(url, quota_key))
            preflight(run, quota)
        run.rows["grants"] = ("pass", f"exactly {', '.join(a.grants)}; non-admin user{'s' if b else ''}")
        if args.enrich:
            enrich_run(run, url, a, enrich_keys, revoked_key, args.settle_seconds)
        elif args.phase == "main":
            shared = jpeg(f"{run.run_id}-shared")
            first = run.upload(a, "shared", "jpg", shared)
            run.shared = {"asset": first.asset_id, "owner": owner_hash(a.user_id)}
            run.upload(a, "trash-me", "jpg", jpeg(f"{run.run_id}-trash-me"))
            single_account(run, a, args.large_video_mb)
            if b and owner_hash(b.user_id) != owner_hash(a.user_id):
                two_accounts(run, b, shared, first.asset_id, owner_hash(a.user_id))
            elif b:
                run.rows["two_users"] = run.rows["binding"] = ("not run", "not run: the second key resolves to the same user")
            optional_keys(run, url, quota, revoked_key)
            uploads = [k for k in ("new", "concurrent", "lost", "live", "raw") if run.rows.get(k, ("",))[0] == "pass"]
            run.rows["no_delete"] = (("pass" if len(uploads) == 5 else "fail"), f"{len(uploads)} of 5 upload steps passed with only {', '.join(a.grants)}; the harness has no delete, trash, restore or update request")
        elif previous and args.phase == "second-account":
            two_accounts(run, a, jpeg(f"{run.run_id}-shared"), previous["shared"]["asset"], previous["shared"]["owner"])
        elif previous:
            after_trash(run, a, previous)
        text, failed = report(run, previous)
        args.out.write_text(redact(text, secrets))
        print(redact(f"wrote {args.out}: {len(run.created)} assets created, {failed} failed or not verified", secrets), file=out)
        code = 1 if failed else 0
    except Refused as error:
        print(redact(f"refused: {error}", secrets), file=out)
        code = 2
    except Failed as error:
        print(redact(f"stopped: {error}", secrets), file=out)
        code = 1
    for created in run.created if run else []:
        print(f"created {created['account']} {created['id']} {created['file']}", file=out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
