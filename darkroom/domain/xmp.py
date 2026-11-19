from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from darkroom.domain import media, timezones
from darkroom.domain.ingest import sidecar as takeout_sidecar
from darkroom.providers import exiftool
from darkroom.storage import paths

LATITUDE = "XMP-exif:GPSLatitude"
LONGITUDE = "XMP-exif:GPSLongitude"
CREATED = "XMP-photoshop:DateCreated"
DESCRIPTION = "XMP-dc:Description"
DATE_TAGS = ("DateTimeOriginal", "DateCreated", "CreationDate", "CreateDate")
STILL_DATE_TAGS = ("DateTimeOriginal", "DateCreated", "CreationDate")
DESCRIPTION_TAGS = ("ImageDescription", "Description", "Caption-Abstract")

Reader = Callable[[list[Path]], dict[Path, dict[str, object]]]
Writer = Callable[[Path, Mapping[str, str]], bool]


@dataclass(frozen=True)
class GoogleMetadata:
    lat: float | None
    lon: float | None
    taken_utc: float | None
    description: str | None

    @classmethod
    def of(cls, raw: str | None) -> GoogleMetadata:
        parsed = None if raw is None else takeout_sidecar.parse(raw)
        if parsed is None:
            return cls(None, None, None, None)
        return cls(parsed.lat, parsed.lon, parsed.photo_taken_utc, parsed.description)


def has_gps(tags: dict[str, object]) -> bool:
    return media.location(tags)[2] != "none"


def has_date(tags: dict[str, object], still: bool = False) -> bool:
    return media.parse_local(media.tag(tags, *(STILL_DATE_TAGS if still else DATE_TAGS))) is not None


def has_description(tags: dict[str, object]) -> bool:
    return media.text(media.tag(tags, *DESCRIPTION_TAGS)) is not None


def has_tag(tags: dict[str, object], name: str) -> bool:
    return media.text(media.tag(tags, name.rsplit(":", 1)[-1])) is not None


def offset_text(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def created_text(utc: float, lat: float | None, lon: float | None, own_offset: int | None = None) -> str | None:
    try:
        moment = datetime.fromtimestamp(utc, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    offset = timezones.offset_at(lat, lon, moment) if lat is not None and lon is not None else own_offset
    if offset is None:
        return f"{moment:%Y:%m:%d %H:%M:%S}"
    local = moment + timedelta(minutes=offset)
    return f"{local:%Y:%m:%d %H:%M:%S}{offset_text(offset)}"


def place_set_by_google(own: dict[str, object], lat: float, lon: float) -> bool:
    own_lat, own_lon, _ = media.location(own)
    return own_lat is None or not media.same_place(own_lat, own_lon, lat, lon)


def date_kind(own: dict[str, object], google: GoogleMetadata, with_date: bool, still: bool) -> str | None:
    if google.taken_utc is None:
        return None
    if not has_date(own, still):
        return "missing" if with_date else None
    return "corrected" if media.date_corrected_by(own, google.taken_utc) else None


def gaps(own: dict[str, object], existing: dict[str, object], google: GoogleMetadata, with_date: bool, still: bool = False) -> dict[str, str]:
    fields: dict[str, str] = {}
    if google.lat is not None and google.lon is not None and place_set_by_google(own, google.lat, google.lon) and not (has_tag(existing, LATITUDE) or has_tag(existing, LONGITUDE)):
        fields[LATITUDE], fields[LONGITUDE] = f"{google.lat:.7f}", f"{google.lon:.7f}"
    kind = date_kind(own, google, with_date, still)
    if google.taken_utc is not None and kind is not None and not has_tag(existing, CREATED):
        own_lat, own_lon, _ = media.location(own)
        lat, lon = (google.lat, google.lon) if google.lat is not None and google.lon is not None else (own_lat, own_lon)
        created = created_text(google.taken_utc, lat, lon, None if kind == "missing" else media.capture(own, own_lat, own_lon)[2])
        if created is not None:
            fields[CREATED] = created
    if google.description is not None and not has_description(own) and not has_tag(existing, DESCRIPTION):
        fields[DESCRIPTION] = google.description
    return fields


def fill(media_path: Path, google: GoogleMetadata, with_date: bool, read: Reader = exiftool.read, write: Writer = exiftool.write_xmp) -> bool:
    sidecar = paths.sidecar_of(media_path)
    present = sidecar.exists() or sidecar.is_symlink()
    if present:
        paths.checked_sidecar(sidecar)
    tags = read([media_path, sidecar] if present else [media_path])
    own = tags.get(media_path)
    existing = tags.get(sidecar, {}) if present else {}
    if own is None or "Error" in own or (present and (not existing or "Error" in existing)):
        return False
    return write(sidecar, gaps(own, existing, google, with_date, still=media_path.suffix.lower() not in media.VIDEO_SUFFIXES))


def place_refreshable(own: dict[str, object], old: GoogleMetadata, new: GoogleMetadata) -> bool:
    if new.lat is None or new.lon is None or media.same_place(new.lat, new.lon, old.lat, old.lon):
        return False
    return old.lat is not None or place_set_by_google(own, new.lat, new.lon)


def nothing_written_for(own: dict[str, object], existing: dict[str, object], old: GoogleMetadata) -> bool:
    if old.lat is None or old.lon is None or has_tag(existing, LATITUDE) or has_tag(existing, LONGITUDE):
        return False
    own_lat, own_lon, _ = media.location(own)
    return own_lat is not None and media.same_place(own_lat, own_lon, old.lat, old.lon)


def changes(own: dict[str, object], existing: dict[str, object], old: GoogleMetadata, new: GoogleMetadata) -> dict[str, str]:
    fields: dict[str, str] = {}
    described = new.description is not None and new.description != old.description and not has_description(own)
    if described and new.description is not None and media.text(media.tag(existing, "Description")) == old.description:
        fields[DESCRIPTION] = new.description
    if place_refreshable(own, old, new):
        lat, lon, _ = media.location(existing)
        if new.lat is not None and new.lon is not None and (media.same_place(lat, lon, old.lat, old.lon) or nothing_written_for(own, existing, old)):
            fields[LATITUDE], fields[LONGITUDE] = f"{new.lat:.7f}", f"{new.lon:.7f}"
    return fields


LOCATION_KEYS = ("geoDataExif", "geoData")


def removes(old: GoogleMetadata, new: GoogleMetadata) -> frozenset[str]:
    removed: set[str] = set()
    if old.lat is not None and old.lon is not None and (new.lat is None or new.lon is None):
        removed.add(LATITUDE)
    if old.description is not None and new.description is None:
        removed.add(DESCRIPTION)
    return frozenset(removed)


def json_object(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def baseline(old_raw: str | None, new_raw: str | None) -> str | None:
    old_data, new_data = json_object(old_raw), json_object(new_raw)
    if old_data is None:
        return new_raw
    if new_data is None:
        return old_raw
    removed = removes(GoogleMetadata.of(old_raw), GoogleMetadata.of(new_raw))
    if not removed:
        return new_raw
    kept = (["description"] if DESCRIPTION in removed else []) + (list(LOCATION_KEYS) if LATITUDE in removed else [])
    for key in kept:
        if key in old_data:
            new_data[key] = old_data[key]
        else:
            new_data.pop(key, None)
    return json.dumps(new_data)


def applied(own: dict[str, object], existing: dict[str, object], old: GoogleMetadata, new: GoogleMetadata) -> frozenset[str]:
    done: set[str] = set()
    described = new.description is not None and new.description != old.description and not has_description(own)
    if described and media.text(media.tag(existing, "Description")) == new.description:
        done.add(DESCRIPTION)
    if place_refreshable(own, old, new):
        lat, lon, _ = media.location(existing)
        if new.lat is not None and new.lon is not None and media.same_place(lat, lon, new.lat, new.lon):
            done.update((LATITUDE, LONGITUDE))
    return frozenset(done)


def refresh(media_path: Path, old: GoogleMetadata, new: GoogleMetadata, read: Reader = exiftool.read,
            write: Writer = exiftool.write_xmp) -> frozenset[str] | None:
    sidecar = paths.sidecar_of(media_path)
    present = sidecar.exists() or sidecar.is_symlink()
    if present:
        paths.checked_sidecar(sidecar)
    tags = read([media_path, sidecar] if present else [media_path])
    own = tags.get(media_path)
    existing = tags.get(sidecar, {}) if present else {}
    if own is None or "Error" in own or (present and (not existing or "Error" in existing)):
        return None
    fields = changes(own, existing, old, new)
    write(sidecar, fields)
    return frozenset(fields) | applied(own, existing, old, new)
