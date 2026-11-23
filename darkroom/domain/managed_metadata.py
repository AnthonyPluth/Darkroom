from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_upload, media, xmp
from darkroom.domain.managed_upload import Report
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import ManagedImportReceipt

Role = Literal["single", "live_still", "live_video"]
Kind = Literal["IMAGE", "VIDEO"]
Source = Literal["media", "sidecar"]
Hold = Literal["unknown_offset", "no_instant", "unwritable", "mismatch", "not_enriched", "not_linked"]
SCHEMA = 1
ROLES = ("single", "live_still", "live_video")
KINDS = ("IMAGE", "VIDEO")
SOURCES = (None, "media", "sidecar")
HELD_BEFORE_UPLOAD = ("unknown_offset", "no_instant", "unwritable")
KEYS = {"schema", "role", "kind", "taken_utc", "offset", "taken_from", "latitude", "longitude", "place_from", "description", "description_from", "sidecar", "hold"}
POLLS = 5
POLL_SECONDS = 2.0
SAME_INSTANT_SECONDS = 1.0
SAME_PLACE_DEGREES = 1e-5
ZONE = re.compile(r"(?:UTC)?([+-])(\d{1,2})(?::?(\d{2}))?|UTC")
REASONS = {
    "unknown_offset": "held for review: the capture time's offset is not known, and none is invented",
    "no_instant": "held for review: nothing gives the capture time",
    "unwritable": "held for review: the description cannot be written as XMP",
    "mismatch": "Immich's extracted metadata differs from the intended metadata",
    "not_enriched": "not enriched: the existing Immich asset lacks metadata that only an approved enrichment path may add",
    "not_linked": "the Live Photo's still is not linked to its hidden video",
    "not_extracted": "Immich has not extracted the metadata yet",
}
VERIFIED = Report("verified", "Immich shows the intended metadata")


@dataclass(frozen=True)
class Intended:
    role: Role
    kind: Kind
    taken_utc: float | None
    offset: int | None
    taken_from: Source | None
    latitude: float | None = field(repr=False)
    longitude: float | None = field(repr=False)
    place_from: Source | None
    description: str | None = field(repr=False)
    description_from: Source | None
    sidecar: tuple[tuple[str, str], ...] = field(repr=False)
    hold: Hold | None


@dataclass(frozen=True)
class Resource:
    receipt_id: int
    original: Callable[[], immich.UploadSource] = field(repr=False)
    filename: str = field(repr=False)


def taken_of(own: dict[str, object], fields: dict[str, str], place: tuple[float | None, float | None]) -> tuple[float | None, int | None, Source | None, Hold | None]:
    if xmp.CREATED in fields:
        parsed = media.parse_local(fields[xmp.CREATED])
        if parsed is None or parsed[1] is None:
            return None, None, None, "unknown_offset"
        utc = media.utc_or_none(parsed[0], parsed[1])
        return (None, None, None, "no_instant") if utc is None else (utc, parsed[1], "sidecar", None)
    own_lat, own_lon, _ = media.location(own)
    utc, local, offset, source = media.capture(own, own_lat, own_lon)
    if source == "quicktime_utc" and utc is not None and offset is None:
        offset = media.wall_clock_at(utc, *place)[1]
    if utc is None and local is None:
        return None, None, None, "no_instant"
    if utc is None or offset is None:
        return None, None, None, "unknown_offset"
    return utc, offset, "media", None


def intend(own: dict[str, object], google: xmp.GoogleMetadata, kind: Kind, role: Role = "single", corrects: bool = True) -> Intended:
    fields = xmp.gaps(own, {}, google, with_date=True, still=kind == "IMAGE", corrects=corrects)
    own_lat, own_lon, _ = media.location(own)
    lat, lon = own_lat, own_lon
    place_from: Source | None = None if own_lat is None else "media"
    if xmp.LATITUDE in fields:
        lat, lon, place_from = float(fields[xmp.LATITUDE]), float(fields[xmp.LONGITUDE]), "sidecar"
    description = media.text(media.tag(own, *xmp.DESCRIPTION_TAGS))
    description_from: Source | None = None if description is None else "media"
    if xmp.DESCRIPTION in fields:
        description, description_from = fields[xmp.DESCRIPTION], "sidecar"
    taken, offset, taken_from, hold = taken_of(own, fields, (lat, lon))
    try:
        xmp.upload_sidecar(fields)
    except ValueError:
        hold = hold or "unwritable"
    return Intended(role, kind, taken, offset, taken_from, lat, lon, place_from, description, description_from, tuple(sorted(fields.items())), hold)


def snapshot(intended: Intended) -> str:
    return json.dumps({"schema": SCHEMA, "role": intended.role, "kind": intended.kind, "taken_utc": intended.taken_utc, "offset": intended.offset,
                       "taken_from": intended.taken_from, "latitude": intended.latitude, "longitude": intended.longitude, "place_from": intended.place_from,
                       "description": intended.description, "description_from": intended.description_from, "sidecar": dict(intended.sidecar),
                       "hold": intended.hold}, sort_keys=True, ensure_ascii=False)


def number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def readable(found: dict[str, Any]) -> bool:
    sidecar, offset = found["sidecar"], found["offset"]
    if (found["role"] not in ROLES or found["kind"] not in KINDS or found["hold"] not in (None, *HELD_BEFORE_UPLOAD)
            or any(found[name] not in SOURCES for name in ("taken_from", "place_from", "description_from"))
            or not isinstance(sidecar, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in sidecar.items())
            or not (found["description"] is None or isinstance(found["description"], str))
            or not all(found[name] is None or number(found[name]) for name in ("taken_utc", "latitude", "longitude"))
            or (found["latitude"] is None) != (found["longitude"] is None)
            or not (offset is None or (type(offset) is int and media.WESTMOST_OFFSET_MINUTES <= offset <= media.EASTMOST_OFFSET_MINUTES))):
        return False
    if found["hold"] is not None:
        return True
    try:
        xmp.upload_sidecar(sidecar)
    except ValueError:
        return False
    return found["taken_utc"] is not None and offset is not None


def intended_of(raw: str) -> Intended | None:
    try:
        found = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(found, dict) or set(found) != KEYS or found["schema"] != SCHEMA or not readable(found):
        return None
    return Intended(found["role"], found["kind"], found["taken_utc"], found["offset"], found["taken_from"], found["latitude"], found["longitude"],
                    found["place_from"], found["description"], found["description_from"], tuple(sorted(found["sidecar"].items())), found["hold"])


def taken_text(intended: Intended) -> str:
    if intended.taken_utc is None or intended.offset is None:
        raise ValueError("no intended capture time")
    return datetime.fromtimestamp(intended.taken_utc, timezone(timedelta(minutes=intended.offset))).isoformat()


def sidecar_source(intended: Intended) -> immich.UploadSource | None:
    packet = xmp.upload_sidecar(dict(intended.sidecar))
    if packet is None:
        return None
    return immich.UploadSource(io.BytesIO(packet), len(packet), hashlib.sha1(packet, usedforsecurity=False).hexdigest())  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1


def instant_of(value: str | None) -> float | None:
    try:
        moment = datetime.fromisoformat(value) if value is not None else None
    except ValueError:
        return None
    return None if moment is None or moment.tzinfo is None else moment.timestamp()


def zone_offset(zone: str | None, instant: float) -> int | None:
    if not zone:
        return None
    named = ZONE.fullmatch(zone.strip())
    if named is not None:
        if named.group(1) is None:
            return 0
        hours, minutes = int(named.group(2)), int(named.group(3) or 0)
        total = hours * 60 + minutes
        if minutes >= 60 or total > media.EASTMOST_OFFSET_MINUTES:
            return None
        return total if named.group(1) == "+" else -total
    try:
        offset = datetime.fromtimestamp(instant, UTC).astimezone(ZoneInfo(zone)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError, OSError, OverflowError):
        return None
    return None if offset is None else int(offset.total_seconds() // 60)


def normalized(text: str | None) -> str | None:
    return None if text is None else media.text(text.replace("\r\n", "\n").replace("\r", "\n"))


def same_place(intended: Intended, seen: immich.Extracted) -> bool:
    lat, lon = seen.latitude, seen.longitude
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return intended.latitude is None
    return (intended.latitude is not None and intended.longitude is not None
            and abs(lat - intended.latitude) < SAME_PLACE_DEGREES and abs(lon - intended.longitude) < SAME_PLACE_DEGREES)


def problem(intended: Intended, asset: immich.Asset, seen: immich.Extracted | None, video_id: str | None) -> str | None:
    if asset.kind != intended.kind:
        return "mismatch"
    if intended.role == "live_still" and asset.live_video_id != video_id:
        return "not_linked"
    if seen is None:
        return "not_extracted"
    instant = instant_of(seen.taken)
    if (instant is None or intended.taken_utc is None or abs(instant - intended.taken_utc) >= SAME_INSTANT_SECONDS
            or zone_offset(seen.zone, instant) != intended.offset or not same_place(intended, seen)
            or normalized(seen.description) != normalized(intended.description)):
        return "mismatch"
    return None


def evidence(asset: immich.Asset, seen: immich.Extracted | None) -> str:
    shown: dict[str, object] = {} if seen is None else {"taken": seen.taken, "zone": seen.zone, "latitude": seen.latitude, "longitude": seen.longitude, "description": seen.description}
    return json.dumps(shown | {"kind": asset.kind, "live_video_id": asset.live_video_id}, sort_keys=True, ensure_ascii=False)


def verify_metadata(opened: Session, reader: immich.ManagedReader, receipt_id: int, intended: Intended, now: float,
                    pause: Callable[[float], None], video_id: str | None) -> Report:
    found = "not_extracted"
    for attempt in range(POLLS):
        if attempt:
            pause(POLL_SECONDS)
        row = managed_upload.fresh(opened, receipt_id)
        if row.state in ("metadata_verified", "committed"):
            return VERIFIED
        if row.state != "bytes_verified" or row.asset_id is None:
            return Report("pending", "the original is not verified yet")
        try:
            asset, seen = reader.extracted(row.asset_id)
        except (immich.NotCustody, immich.NotVisible):
            return Report("unusable", "Immich's asset is not an available managed original of this person")
        except immich.ImmichFailed:
            return Report("pending", "Immich's extracted metadata could not be read")
        if asset.sha1 != row.source_sha1:
            return Report("unusable", "Immich's asset holds other bytes than the source")
        seen_problem = problem(intended, asset, seen, video_id)
        if seen_problem is None:
            moved = managed_upload.move(opened, row, ("bytes_verified",), now, state="metadata_verified", metadata_evidence=evidence(asset, seen),
                                        metadata_verified_at=now, metadata_hold=None)
            return VERIFIED if moved else Report("pending", "the receipt moved")
        found = seen_problem
    hold = "not_enriched" if found == "mismatch" and row.upload_outcome == "duplicate" and intended.sidecar else found
    if hold != "not_extracted" and row.metadata_hold != hold:
        managed_upload.move(opened, row, ("bytes_verified",), now, metadata_hold=hold)
    return Report("pending", REASONS[hold])


def complete_receipt(opened: Session, reader: immich.ManagedReader, resource: Resource, now: float,
                     pause: Callable[[float], None] = time.sleep, live_video_id: str | None = None) -> Report:
    row = managed_upload.fresh(opened, resource.receipt_id)
    intended = intended_of(row.expected_metadata)
    if intended is None:
        return Report("pending", "the receipt's intended metadata cannot be read")
    if intended.hold is not None and intended.hold in HELD_BEFORE_UPLOAD:
        if row.metadata_hold != intended.hold:
            managed_upload.move(opened, row, (row.state,), now, metadata_hold=intended.hold)
        return Report("held", REASONS[intended.hold])
    if (intended.role == "live_still") != (live_video_id is not None):
        return Report("pending", "a Live Photo's still is sent only after its video, linked to it")
    try:
        taken = taken_text(intended)
    except (OverflowError, OSError, ValueError):
        return Report("pending", "the intended capture time cannot be sent")

    def body() -> immich.MultipartUpload:
        return immich.MultipartUpload(resource.original(), resource.filename, taken, taken, sidecar_source(intended), live_video_id)
    try:
        uploaded = managed_upload.upload_receipt(opened, reader, row.id, body, now)
    except (immich.ImmichFailed, OSError):
        return Report("pending", "the upload could not be built from the source")
    if uploaded.outcome != "verified":
        return uploaded
    return verify_metadata(opened, reader, row.id, intended, now, pause, live_video_id)


def members(opened: Session, intent_id: int, compound_key: str) -> list[ManagedImportReceipt]:
    receipt = ManagedImportReceipt
    rows = list(opened.scalars(select(receipt).where(receipt.intent_id == intent_id, receipt.compound_key == compound_key).order_by(receipt.id)))
    for row in rows:
        opened.expunge(row)
    opened.commit()
    return rows


def linked(intents: list[Intended], assets: list[immich.Asset]) -> bool:
    by_role = {intent.role: asset for intent, asset in zip(intents, assets, strict=True)}
    if "live_still" in by_role:
        still, video = by_role["live_still"], by_role["live_video"]
        return still.kind == "IMAGE" and video.kind == "VIDEO" and still.live_video_id == video.id and video.visibility == "hidden"
    ids = {asset.id for asset in assets}
    return not any(asset.live_video_id in ids for asset in assets)


def commit_compound(opened: Session, reader: immich.ManagedReader, intent_id: int, compound_key: str, now: float) -> Report:
    rows = members(opened, intent_id, compound_key)
    if rows and all(row.state == "committed" for row in rows):
        return Report("verified", "every resource of the item is committed")
    if not rows or any(row.state not in ("metadata_verified", "committed") for row in rows):
        return Report("pending", "a resource of the item has no verified metadata yet")
    intents = [intended_of(row.expected_metadata) for row in rows]
    roles = sorted(intent.role if intent is not None else "" for intent in intents)
    if "" in roles or (roles != ["live_still", "live_video"] and roles != ["single"] * len(rows)) or len({row.asset_id for row in rows}) != len(rows):
        return Report("pending", "the item's resources are neither one Live Photo nor separate assets")
    try:
        assets = [reader.get_asset(str(row.asset_id)) for row in rows]
    except (immich.NotCustody, immich.NotVisible):
        return Report("unusable", "an asset of the item is not an available managed original of this person")
    except immich.ImmichFailed:
        return Report("pending", "the item's assets could not be read again")
    if any(asset.sha1 != row.source_sha1 for asset, row in zip(assets, rows, strict=True)):
        return Report("unusable", "an asset of the item holds other bytes than its source")
    if not linked([intent for intent in intents if intent is not None], assets):
        return Report("pending", REASONS["not_linked"] if "live_still" in roles else "separate assets of the item are linked")
    opened.commit()
    db.write_lock(opened)
    receipt = ManagedImportReceipt
    changed = 0
    for row in rows:
        if row.state == "metadata_verified":
            changed += opened.connection().execute(update(receipt).where(receipt.id == row.id, receipt.state == "metadata_verified", receipt.asset_id == row.asset_id)
                                                   .values(state="committed", committed_at=now, updated_at=now)).rowcount
    present = set(opened.scalars(select(receipt.id).where(receipt.intent_id == intent_id, receipt.compound_key == compound_key)))
    if changed != sum(row.state == "metadata_verified" for row in rows) or present != {row.id for row in rows}:
        opened.rollback()
        return Report("pending", "the item changed while it was checked")
    opened.commit()
    return Report("verified", "every resource of the item is committed")


def complete_live_photo(opened: Session, reader: immich.ManagedReader, video: Resource, still: Resource, now: float,
                        pause: Callable[[float], None] = time.sleep) -> Report:
    rows = [managed_upload.fresh(opened, part.receipt_id) for part in (video, still)]
    intents = [intended_of(row.expected_metadata) for row in rows]
    if [None if intent is None else intent.role for intent in intents] != ["live_video", "live_still"] or len({(row.intent_id, row.compound_key) for row in rows}) != 1:
        return Report("pending", "the receipts are not one Live Photo's video and still")
    first = complete_receipt(opened, reader, video, now, pause)
    if first.outcome != "verified":
        return first
    second = complete_receipt(opened, reader, still, now, pause, managed_upload.fresh(opened, video.receipt_id).asset_id)
    if second.outcome != "verified":
        return second
    return commit_compound(opened, reader, rows[0].intent_id, rows[0].compound_key, now)
