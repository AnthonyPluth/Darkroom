from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from darkroom.domain import media, timezones
from darkroom.domain.ingest.pairing import PairingReport, TakeoutItem
from darkroom.domain.ingest.sidecar import Sidecar
from darkroom.storage.models import MediaItem

ZONE_STEP_MINUTES = 15
WESTMOST_OFFSET_MINUTES = -12 * 60
EASTMOST_OFFSET_MINUTES = 14 * 60
AGREEMENT_SECONDS = 2
CAPTURE_DATE_TAGS = ("DateTimeOriginal", "CreationDate")
SCAN_DATE_TAG = "CreateDate"


@dataclass(frozen=True)
class Counts:
    paired: int
    unpaired: int
    orphans: int
    edited: int
    live_pairs: int
    album_only: int


def summarize(report: PairingReport) -> Counts:
    return Counts(
        paired=sum(1 for i in report.items if i.sidecar is not None), unpaired=len(report.unpaired_media), orphans=len(report.orphan_sidecars),
        edited=sum(1 for i in report.items if i.edited_of is not None), live_pairs=sum(1 for i in report.items if i.live_pair_of is not None),
        album_only=sum(1 for i in report.items if not i.in_year_folder),
    )


def moment_of(sidecar: Sidecar | None) -> datetime | None:
    if sidecar is None or sidecar.photo_taken_utc is None:
        return None
    try:
        return datetime.fromtimestamp(sidecar.photo_taken_utc, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def zone_offset(local: str, moment: datetime) -> int | None:
    wall = datetime.fromisoformat(local)
    apart = (wall - moment.replace(tzinfo=None)).total_seconds()
    minutes = round(apart / 60 / ZONE_STEP_MINUTES) * ZONE_STEP_MINUTES
    if abs(apart - minutes * 60) > AGREEMENT_SECONDS or not WESTMOST_OFFSET_MINUTES <= minutes <= EASTMOST_OFFSET_MINUTES:
        return None
    return minutes


def scan_dates_dropped(item: TakeoutItem, tags: dict[str, object], sidecar: Sidecar | None) -> dict[str, object]:
    if item.path.suffix.lower() in media.VIDEO_SUFFIXES or moment_of(sidecar) is None or media.tag(tags, *CAPTURE_DATE_TAGS) is not None:
        return tags
    return {key: value for key, value in tags.items() if key.rsplit(":", 1)[-1] != SCAN_DATE_TAG}


def candidate(item: TakeoutItem, tags: dict[str, object], sidecar: Sidecar | None, person_id: int, relative: str, now: float, export_id: int | None = None,
              opened: Path | None = None) -> MediaItem:
    dated = scan_dates_dropped(item, tags, sidecar)
    print_ = media.fingerprint(opened or item.path, dated, item.path)
    lat, lon, gps_source = print_.lat, print_.lon, print_.gps_source
    if sidecar is not None and sidecar.lat is not None and sidecar.lon is not None and (gps_source == "none" or not (item.sidecar_from_still or media.same_place(lat, lon, sidecar.lat, sidecar.lon))):
        lat, lon, gps_source = sidecar.lat, sidecar.lon, "google"
    utc, local, offset, time_source = print_.captured_utc, print_.captured_local, print_.offset_minutes, print_.time_source
    if time_source == "quicktime_utc" and utc is not None and offset is None:
        local, offset = media.wall_clock_at(utc, lat, lon)
    has_own_date = utc is not None or (item.sidecar_from_still and local is not None)
    moment = None if has_own_date else moment_of(sidecar)
    if moment is not None and local is not None:
        derived = zone_offset(local, moment)
        exif_utc = None if derived is None else media.utc_or_none(datetime.fromisoformat(local), derived)
        if derived is not None and exif_utc is not None:
            utc, offset, time_source = exif_utc + media.seconds_fraction(dated), derived, "sidecar_offset"
    elif moment is not None and sidecar is not None and sidecar.photo_taken_utc is not None:
        utc, time_source = sidecar.photo_taken_utc, "sidecar"
        offset = timezones.offset_at(lat, lon, moment) if lat is not None and lon is not None else None
        if offset is not None:
            local = (moment + timedelta(minutes=offset)).replace(tzinfo=None).isoformat(timespec="seconds")
    return MediaItem(
        person_id=person_id, source="takeout", path=relative, sha1=print_.sha1, bytes=print_.bytes, phash=print_.phash, dhash=print_.dhash,
        captured_utc=utc, captured_local=local, offset_minutes=offset, time_source=time_source,
        width=print_.width, height=print_.height, duration_ms=print_.duration_ms, make=print_.make, model=print_.model,
        lat=lat, lon=lon, gps_source=gps_source, sidecar_json=sidecar.raw if sidecar else None, sidecar_from_still=item.sidecar_from_still, export_id=export_id, indexed_at=now,
    )
