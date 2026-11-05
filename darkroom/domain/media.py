from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import imagehash
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from darkroom.domain import timezones

register_heif_opener()

CHUNK = 1024 * 1024
DATE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$")
OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
PHASH_SIZE = 8
DHASH_SIZE = 8
VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}


@dataclass(frozen=True)
class PerceptualHashes:
    phash: str | None
    dhash: str | None


@dataclass(frozen=True)
class Fingerprint:
    sha1: str
    bytes: int
    phash: str | None
    dhash: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    make: str | None
    model: str | None
    captured_utc: float | None
    captured_local: str | None
    offset_minutes: int | None
    time_source: str
    lat: float | None
    lon: float | None
    gps_source: str


def sha1_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def tag(tags: dict[str, object], *names: str) -> object | None:
    for name in names:
        for key, value in tags.items():
            if key.rsplit(":", 1)[-1] == name and value not in (None, ""):
                return value
    return None


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def text(value: object) -> str | None:
    return None if value is None or str(value).strip() == "" else str(value).strip()


def parse_offset(value: str) -> int | None:
    if value == "Z":
        return 0
    found = OFFSET.match(value)
    if not found:
        return None
    minutes = int(found.group(2)) * 60 + int(found.group(3))
    return minutes if found.group(1) == "+" else -minutes


def parse_local(value: object) -> tuple[datetime, int | None] | None:
    if not isinstance(value, str):
        return None
    found = DATE.match(value.strip())
    if not found:
        return None
    year, month, day, hour, minute, second = (int(part) for part in found.groups()[:6])
    try:
        local = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    offset = parse_offset(found.group(7)) if found.group(7) else None
    return local, offset


def seconds_fraction(tags: dict[str, object]) -> float:
    value = tag(tags, "SubSecTimeOriginal", "SubSecCreateDate", "SubSecTime")
    if value is None:
        return 0.0
    digits = "".join(c for c in str(value) if c.isdigit())
    return float(f"0.{digits}") if digits else 0.0


def capture(tags: dict[str, object], lat: float | None, lon: float | None) -> tuple[float | None, str | None, int | None, str]:
    stamp = tag(tags, "DateTimeOriginal", "CreationDate", "CreateDate")
    parsed = parse_local(stamp)
    if parsed is None:
        return None, None, None, "unknown"
    local, embedded = parsed
    local_text = local.isoformat(timespec="seconds")
    offset = embedded
    if offset is None:
        separate = tag(tags, "OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime")
        offset = parse_offset(str(separate)) if separate is not None else None
    if offset is not None:
        return local_to_utc(local, offset) + seconds_fraction(tags), local_text, offset, "exif_offset"
    if lat is not None and lon is not None:
        looked_up = timezones.offset_minutes(lat, lon, local)
        if looked_up is not None:
            return local_to_utc(local, looked_up) + seconds_fraction(tags), local_text, looked_up, "gps_lookup"
    return None, local_text, None, "unknown"


def local_to_utc(local: datetime, offset_minutes: int) -> float:
    return local.replace(tzinfo=timezone(timedelta(minutes=offset_minutes))).astimezone(UTC).timestamp()


def signed(tags: dict[str, object], name: str, negative: str) -> float | None:
    value = number(tags.get(f"Composite:{name}"))
    if value is not None:
        return value
    value = number(tag(tags, name))
    if value is None:
        return None
    reference = text(tag(tags, f"{name}Ref"))
    return -abs(value) if reference is not None and reference.upper().startswith(negative) else value


def location(tags: dict[str, object]) -> tuple[float | None, float | None, str]:
    lat, lon = signed(tags, "GPSLatitude", "S"), signed(tags, "GPSLongitude", "W")
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0) or abs(lat) > 90 or abs(lon) > 180:
        return None, None, "none"
    return lat, lon, "exif"


def perceptual_hashes(path: Path) -> PerceptualHashes:
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return PerceptualHashes(None, None)
    try:
        with Image.open(path) as image:
            return PerceptualHashes(str(imagehash.phash(image, hash_size=PHASH_SIZE)), str(imagehash.dhash(image, hash_size=DHASH_SIZE)))
    except (UnidentifiedImageError, OSError, ValueError):
        return PerceptualHashes(None, None)


def dimension(tags: dict[str, object], *names: str) -> int | None:
    value = number(tag(tags, *names))
    return None if value is None else int(value)


def fingerprint(path: Path, tags: dict[str, object]) -> Fingerprint:
    digest, size = sha1_of(path)
    lat, lon, gps_source = location(tags)
    utc, local, offset, time_source = capture(tags, lat, lon)
    hashes = perceptual_hashes(path)
    duration = number(tag(tags, "Duration", "MediaDuration"))
    return Fingerprint(
        sha1=digest, bytes=size, phash=hashes.phash, dhash=hashes.dhash,
        width=dimension(tags, "ImageWidth", "ExifImageWidth", "SourceImageWidth"),
        height=dimension(tags, "ImageHeight", "ExifImageHeight", "SourceImageHeight"),
        duration_ms=None if duration is None else round(duration * 1000),
        make=text(tag(tags, "Make")), model=text(tag(tags, "Model")),
        captured_utc=utc, captured_local=local, offset_minutes=offset, time_source=time_source,
        lat=lat, lon=lon, gps_source=gps_source,
    )
