from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from darkroom.domain import media, timezones
from darkroom.domain.ingest.pairing import PairingReport, TakeoutItem
from darkroom.domain.ingest.sidecar import Sidecar
from darkroom.storage.models import MediaItem


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


def candidate(item: TakeoutItem, tags: dict[str, object], sidecar: Sidecar | None, person_id: int, relative: str, now: float, export_id: int | None = None) -> MediaItem:
    print_ = media.fingerprint(item.path, tags)
    lat, lon, gps_source = print_.lat, print_.lon, print_.gps_source
    if gps_source == "none" and sidecar is not None and sidecar.lat is not None and sidecar.lon is not None:
        lat, lon, gps_source = sidecar.lat, sidecar.lon, "google"
    utc, local, offset, time_source = print_.captured_utc, print_.captured_local, print_.offset_minutes, print_.time_source
    moment = moment_of(sidecar) if utc is None else None
    if moment is not None and sidecar is not None and sidecar.photo_taken_utc is not None:
        utc, time_source = sidecar.photo_taken_utc, "sidecar"
        offset = timezones.offset_at(lat, lon, moment) if lat is not None and lon is not None else None
        if local is None and offset is not None:
            local = (moment + timedelta(minutes=offset)).replace(tzinfo=None).isoformat(timespec="seconds")
    return MediaItem(
        person_id=person_id, source="takeout", path=relative, sha1=print_.sha1, bytes=print_.bytes, phash=print_.phash, dhash=print_.dhash,
        captured_utc=utc, captured_local=local, offset_minutes=offset, time_source=time_source,
        width=print_.width, height=print_.height, duration_ms=print_.duration_ms, make=print_.make, model=print_.model,
        lat=lat, lon=lon, gps_source=gps_source, sidecar_json=sidecar.raw if sidecar else None, export_id=export_id, indexed_at=now,
    )
