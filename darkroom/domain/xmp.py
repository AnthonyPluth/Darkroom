from __future__ import annotations

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


def has_date(tags: dict[str, object]) -> bool:
    return media.parse_local(media.tag(tags, *DATE_TAGS)) is not None


def has_description(tags: dict[str, object]) -> bool:
    return media.text(media.tag(tags, *DESCRIPTION_TAGS)) is not None


def has_tag(tags: dict[str, object], name: str) -> bool:
    return media.text(media.tag(tags, name.rsplit(":", 1)[-1])) is not None


def offset_text(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def created_text(utc: float, lat: float | None, lon: float | None) -> str | None:
    try:
        moment = datetime.fromtimestamp(utc, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    offset = timezones.offset_at(lat, lon, moment) if lat is not None and lon is not None else None
    minutes = 0 if offset is None else offset
    local = moment + timedelta(minutes=minutes)
    return f"{local:%Y:%m:%d %H:%M:%S}{offset_text(minutes)}"


def gaps(own: dict[str, object], existing: dict[str, object], google: GoogleMetadata, with_date: bool) -> dict[str, str]:
    fields: dict[str, str] = {}
    if google.lat is not None and google.lon is not None and not has_gps(own) and not (has_tag(existing, LATITUDE) or has_tag(existing, LONGITUDE)):
        fields[LATITUDE], fields[LONGITUDE] = f"{google.lat:.7f}", f"{google.lon:.7f}"
    if with_date and google.taken_utc is not None and not has_date(own) and not has_tag(existing, CREATED):
        own_lat, own_lon, _ = media.location(own)
        lat, lon = (google.lat, google.lon) if google.lat is not None and google.lon is not None else (own_lat, own_lon)
        created = created_text(google.taken_utc, lat, lon)
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
    return write(sidecar, gaps(own, existing, google, with_date))


def same_place(lat: float | None, lon: float | None, other_lat: float | None, other_lon: float | None) -> bool:
    if lat is None or lon is None or other_lat is None or other_lon is None:
        return lat is None and lon is None and other_lat is None and other_lon is None
    return abs(lat - other_lat) < 1e-6 and abs(lon - other_lon) < 1e-6


def changes(own: dict[str, object], existing: dict[str, object], old: GoogleMetadata, new: GoogleMetadata) -> dict[str, str]:
    fields: dict[str, str] = {}
    described = new.description is not None and new.description != old.description and not has_description(own)
    if described and new.description is not None and media.text(media.tag(existing, "Description")) == old.description:
        fields[DESCRIPTION] = new.description
    if new.lat is not None and new.lon is not None and not same_place(new.lat, new.lon, old.lat, old.lon) and not has_gps(own):
        lat, lon, _ = media.location(existing)
        if same_place(lat, lon, old.lat, old.lon):
            fields[LATITUDE], fields[LONGITUDE] = f"{new.lat:.7f}", f"{new.lon:.7f}"
    return fields


def removes(old: GoogleMetadata, new: GoogleMetadata) -> bool:
    lost_place = old.lat is not None and old.lon is not None and (new.lat is None or new.lon is None)
    return lost_place or (old.description is not None and new.description is None)


def applied(own: dict[str, object], existing: dict[str, object], old: GoogleMetadata, new: GoogleMetadata) -> frozenset[str]:
    done: set[str] = set()
    described = new.description is not None and new.description != old.description and not has_description(own)
    if described and media.text(media.tag(existing, "Description")) == new.description:
        done.add(DESCRIPTION)
    if new.lat is not None and new.lon is not None and not same_place(new.lat, new.lon, old.lat, old.lon) and not has_gps(own):
        lat, lon, _ = media.location(existing)
        if same_place(lat, lon, new.lat, new.lon):
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
