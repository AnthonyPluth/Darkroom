from __future__ import annotations

import json
from dataclasses import dataclass

ORIGINS = {"mobileUpload": "mobile_upload", "webUpload": "web_upload", "fromSharedAlbum": "shared_album", "composition": "composition", "driveUpload": "drive_upload"}


@dataclass(frozen=True)
class Sidecar:
    photo_taken_utc: float | None
    lat: float | None
    lon: float | None
    description: str | None
    title: str | None
    origin: str
    raw: str


def whole_seconds(value: object) -> float | None:
    try:
        seconds = float(str(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def place(block: object) -> tuple[float, float] | None:
    if not isinstance(block, dict):
        return None
    lat, lon = block.get("latitude"), block.get("longitude")
    if isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    if (lat == 0.0 and lon == 0.0) or abs(lat) > 90 or abs(lon) > 180:
        return None
    return float(lat), float(lon)


def text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def origin_of(data: dict[str, object]) -> str:
    block = data.get("googlePhotosOrigin")
    if not isinstance(block, dict):
        return "none"
    for key, name in ORIGINS.items():
        if key in block:
            return name
    return "other"


def parse(raw: str) -> Sidecar | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    taken = data.get("photoTakenTime")
    seconds = whole_seconds(taken.get("timestamp")) if isinstance(taken, dict) else None
    found = place(data.get("geoDataExif")) or place(data.get("geoData"))
    return Sidecar(seconds, found[0] if found else None, found[1] if found else None, text(data.get("description")), text(data.get("title")), origin_of(data), raw)
