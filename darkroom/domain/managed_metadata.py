from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_manifest, managed_upload, media, xmp
from darkroom.domain.ingest import metadata as ingest_metadata
from darkroom.domain.managed_upload import Report
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import ManagedCompound, ManagedCompoundMember, ManagedImportIntent, ManagedImportReceipt

Role = Literal["single", "live_still", "live_video"]
Kind = Literal["IMAGE", "VIDEO"]
Source = Literal["media", "sidecar", "sidecar_offset", "owner"]
Hold = Literal["unknown_offset", "no_instant", "unwritable", "mismatch", "not_enriched", "not_linked"]
SCHEMA = 1
NOT_A_CAPTURE_DATE = "DateCreated"
ROLES = ("single", "live_still", "live_video")
KINDS = ("IMAGE", "VIDEO")
FILE_OR_GOOGLE_SOURCES = (None, "media", "sidecar", "sidecar_offset")
SOURCES = (*FILE_OR_GOOGLE_SOURCES, "owner")
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


@dataclass(frozen=True)
class OwnerValues:
    offset: int | None = None
    wall: datetime | None = None
    instant: float | None = None
    drop_description: bool = False


def agreed_offset(local: str | None, google: xmp.GoogleMetadata, corrects: bool) -> int | None:
    if local is None or google.taken_utc is None or not corrects:
        return None
    try:
        return ingest_metadata.zone_offset(local, datetime.fromtimestamp(google.taken_utc, UTC))
    except (OverflowError, OSError, ValueError):
        return None


def taken_of(own: dict[str, object], fields: dict[str, str], place: tuple[float | None, float | None], google: xmp.GoogleMetadata,
             corrects: bool) -> tuple[float | None, int | None, Source | None, Hold | None]:
    if xmp.CREATED in fields:
        parsed = media.parse_local(fields[xmp.CREATED])
        if parsed is None or parsed[1] is None:
            return None, None, None, "unknown_offset"
        utc = media.utc_or_none(parsed[0], parsed[1])
        return (None, None, None, "no_instant") if utc is None else (utc, parsed[1], "sidecar", None)
    own_lat, own_lon, _ = media.location(own)
    utc, local, offset, source = media.capture(own, own_lat, own_lon)
    if source == "quicktime_utc" and utc is not None:
        offset = media.wall_clock_at(utc, *place)[1]
    if source == "gps_lookup" and utc is not None and media.wall_clock_at(utc, *place)[1] != offset:
        return None, None, None, "unknown_offset"
    if utc is None and (derived := agreed_offset(local, google, corrects)) is not None and local is not None:
        agreed = media.utc_or_none(datetime.fromisoformat(local), derived)
        if agreed is not None and media.wall_clock_at(agreed, *place)[1] == derived:
            return agreed + media.seconds_fraction(own), derived, "sidecar_offset", None
    if utc is None and local is None:
        return None, None, None, "no_instant"
    if utc is None or offset is None:
        return None, None, None, "unknown_offset"
    return utc, offset, "media", None


def owner_time(own: dict[str, object], fields: dict[str, str], google: xmp.GoogleMetadata, place: tuple[float | None, float | None],
               owner: OwnerValues) -> tuple[float | None, int | None, Source | None, Hold | None] | None:
    offset = owner.offset
    if offset is None:
        return None
    instant = owner.instant
    if instant is None and owner.wall is not None:
        instant = media.utc_or_none(owner.wall, offset)
    if instant is None and xmp.CREATED in fields:
        instant = google.taken_utc
    if instant is None:
        own_lat, own_lon, _ = media.location(own)
        utc, local, _, source = media.capture(own, own_lat, own_lon)
        if source == "quicktime_utc":
            instant = utc
        elif local is not None:
            whole = media.utc_or_none(datetime.fromisoformat(local), offset)
            instant = None if whole is None else whole + media.seconds_fraction(own)
    if instant is None or media.wall_clock_at(instant, *place)[1] not in (None, offset):
        return None, None, None, "unknown_offset"
    created = xmp.created_text(instant, None, None, offset)
    if created is None:
        return None, None, None, "unknown_offset"
    fields[xmp.CREATED] = created
    return instant, offset, "owner", None


def intend(tags: dict[str, object], google: xmp.GoogleMetadata, kind: Kind, role: Role = "single", corrects: bool = True,
           owner: OwnerValues = OwnerValues()) -> Intended:
    own = {key: value for key, value in tags.items() if key.rsplit(":", 1)[-1] != NOT_A_CAPTURE_DATE}
    fields = xmp.gaps(own, {}, google, with_date=True, still=kind == "IMAGE", corrects=corrects)
    own_lat, own_lon, _ = media.location(own)
    lat, lon = own_lat, own_lon
    place_from: Source | None = None if own_lat is None else "media"
    if xmp.LATITUDE in fields:
        lat, lon, place_from = float(fields[xmp.LATITUDE]), float(fields[xmp.LONGITUDE]), "sidecar"
    if owner.drop_description:
        fields.pop(xmp.DESCRIPTION, None)
    description = media.text(media.tag(own, *xmp.DESCRIPTION_TAGS))
    description_from: Source | None = None if description is None else "media"
    if xmp.DESCRIPTION in fields:
        description, description_from = fields[xmp.DESCRIPTION], "sidecar"
    taken, offset, taken_from, hold = owner_time(own, fields, google, (lat, lon), owner) or taken_of(own, fields, (lat, lon), google, corrects)
    try:
        xmp.upload_sidecar(fields)
    except ValueError:
        hold = hold or "unwritable"
    return Intended(role, kind, taken, offset, taken_from, lat, lon, place_from, description, description_from, tuple(sorted(fields.items())), hold)


def snapshot(intended: Intended) -> str:
    return json.dumps({"schema": SCHEMA, "role": intended.role, "kind": intended.kind, "taken_utc": intended.taken_utc, "offset": intended.offset,
                       "taken_from": intended.taken_from, "latitude": intended.latitude, "longitude": intended.longitude, "place_from": intended.place_from,
                       "description": intended.description, "description_from": intended.description_from, "sidecar": dict(intended.sidecar),
                       "hold": intended.hold}, sort_keys=True)


def number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def readable(found: dict[str, Any]) -> bool:
    sidecar, offset = found["sidecar"], found["offset"]
    if (found["role"] not in ROLES or found["kind"] not in KINDS or found["hold"] not in (None, *HELD_BEFORE_UPLOAD)
            or found["taken_from"] not in SOURCES or any(found[name] not in FILE_OR_GOOGLE_SOURCES for name in ("place_from", "description_from"))
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


def differences(intended: Intended, seen: immich.Extracted) -> str:
    instant = instant_of(seen.taken)
    shown_offset = None if instant is None else zone_offset(seen.zone, instant)
    differs = [name for name, same in (
        ("capture_time", instant is not None and intended.taken_utc is not None and abs(instant - intended.taken_utc) < SAME_INSTANT_SECONDS),
        ("offset", shown_offset == intended.offset), ("place", same_place(intended, seen)),
        ("description", normalized(seen.description) == normalized(intended.description))) if not same]
    try:
        wanted = taken_text(intended)
    except (OverflowError, OSError, ValueError):
        wanted = None
    return json.dumps({"differs": differs, "immich_taken": None if instant is None else seen.taken, "immich_offset": shown_offset,
                       "intended_taken": wanted, "intended_offset": intended.offset}, sort_keys=True)


def held_facts(tags: dict[str, object], google: xmp.GoogleMetadata, intended: Intended) -> str | None:
    if intended.hold is None:
        return None
    own = {key: value for key, value in tags.items() if key.rsplit(":", 1)[-1] != NOT_A_CAPTURE_DATE}
    own_lat, own_lon, _ = media.location(own)
    utc, local, _, source = media.capture(own, own_lat, own_lon)
    parsed = media.parse_local(dict(intended.sidecar).get(xmp.CREATED))
    wall = local if local is not None else (None if parsed is None else parsed[0].isoformat(timespec="seconds"))
    unwritable = intended.hold == "unwritable"
    return json.dumps({"wall_clock": wall, "file_utc": utc if source == "quicktime_utc" else None, "google_utc": google.taken_utc,
                       "place_known": intended.latitude is not None,
                       "description_length": len(intended.description) if unwritable and intended.description is not None else None,
                       "description_unwritable": unwritable}, sort_keys=True)


def evidence(asset: immich.Asset, seen: immich.Extracted | None) -> str:
    shown: dict[str, object] = {} if seen is None else {"taken": seen.taken, "zone": seen.zone, "latitude": seen.latitude, "longitude": seen.longitude, "description": seen.description}
    return json.dumps(shown | {"kind": asset.kind, "live_video_id": asset.live_video_id}, sort_keys=True)


def verify_metadata(opened: Session, reader: immich.ManagedReader, receipt_id: int, intended: Intended, now: float,
                    pause: Callable[[float], None], video_id: str | None) -> Report:
    found = "not_extracted"
    shown: tuple[immich.Extracted, str | None] | None = None
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
        except (immich.NotCustody, immich.NotVisible) as error:
            return managed_upload.hold_unusable(opened, row, ("bytes_verified",), now, managed_upload.kind_of(error))
        except immich.ImmichFailed:
            return Report("pending", "Immich's extracted metadata could not be read")
        if asset.sha1 != row.source_sha1:
            return managed_upload.hold_unusable(opened, row, ("bytes_verified",), now, "other_bytes", "Immich's asset holds other bytes than the source")
        seen_problem = problem(intended, asset, seen, video_id)
        if seen_problem is None:
            moved = managed_upload.move(opened, row, ("bytes_verified",), now, state="metadata_verified", metadata_evidence=evidence(asset, seen),
                                        metadata_verified_at=now, metadata_hold=None, hold_facts=None)
            return VERIFIED if moved else Report("pending", "the receipt moved")
        found = seen_problem
        shown = None if seen is None else (seen, differences(intended, seen))
    hold = "not_enriched" if found == "mismatch" and row.upload_outcome == "duplicate" and intended.sidecar else found
    facts = shown[1] if shown is not None and hold in ("mismatch", "not_enriched") else None
    if hold != "not_extracted" and (row.metadata_hold != hold or (facts is not None and row.hold_facts != facts)):
        managed_upload.move(opened, row, ("bytes_verified",), now, metadata_hold=hold, **({} if facts is None else {"hold_facts": facts}))
    return Report("pending", REASONS[hold])


def complete_receipt(opened: Session, reader: immich.ManagedReader, resource: Resource, now: float,
                     pause: Callable[[float], None] = time.sleep, live_video_id: str | None = None) -> Report:
    row = managed_upload.fresh(opened, resource.receipt_id)
    intended = intended_of(row.expected_metadata)
    if intended is None:
        return Report("pending", "the receipt's intended metadata cannot be read")
    if intended.hold is not None and intended.hold in HELD_BEFORE_UPLOAD:
        if row.metadata_hold != intended.hold:
            managed_upload.move(opened, row, (row.state,), now, ManagedImportReceipt.expected_metadata == row.expected_metadata,
                                ManagedImportReceipt.metadata_hold == row.metadata_hold, metadata_hold=intended.hold)
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
        uploaded = managed_upload.upload_receipt(opened, reader, row.id, body, now, expected_metadata=row.expected_metadata)
    except (immich.ImmichFailed, OSError):
        return Report("pending", "the upload could not be built from the source")
    if uploaded.outcome != "verified":
        return uploaded
    return verify_metadata(opened, reader, row.id, intended, now, pause, live_video_id)


def members(opened: Session, intent_id: int, compound_key: str) -> list[ManagedImportReceipt]:
    receipt = ManagedImportReceipt
    query = (select(receipt).where(receipt.intent_id == intent_id, receipt.compound_key == compound_key).order_by(receipt.id)
             .execution_options(populate_existing=True))
    rows = list(opened.scalars(query))
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


def role_of(row: ManagedImportReceipt) -> str | None:
    intended = intended_of(row.expected_metadata)
    return None if intended is None else intended.role


def manifest_refusal(opened: Session, intent_id: int, compound_key: str) -> Report | None:
    intent = opened.get(ManagedImportIntent, intent_id, populate_existing=True)
    if intent is None or not managed_manifest.known(intent.resource_manifest):
        return None
    receipt, member = ManagedImportReceipt, ManagedCompoundMember
    rows = list(opened.execute(select(receipt).where(receipt.intent_id == intent_id, receipt.compound_key == compound_key)
                               .execution_options(populate_existing=True)).scalars())
    compound = opened.execute(select(ManagedCompound).where(ManagedCompound.intent_id == intent_id, ManagedCompound.compound_key == compound_key)
                              .execution_options(populate_existing=True)).scalar_one_or_none()
    if compound is None:
        named = opened.execute(select(member.id).where(member.intent_id == intent_id, member.resource_key == compound_key).limit(1)).first()
        if len(rows) == 1 and rows[0].resource_key == compound_key and named is None:
            return None
        return Report("pending", "the item's receipts are not one single resource, as its manifest requires")
    if compound.hold is not None:
        return Report("held", "the Live Photo is held, so nothing of it is committed")
    frozen = [(found.resource_key, found.role, found.source_sha1, found.expected_bytes)
              for found in opened.execute(select(member).where(member.compound_id == compound.id).execution_options(populate_existing=True)).scalars()]
    present = [(row.resource_key, role_of(row), row.source_sha1, row.expected_bytes) for row in rows]
    if Counter(frozen) != Counter(present):
        return Report("pending", "the Live Photo's receipts differ from its frozen members")
    return None


def commit_compound(opened: Session, reader: immich.ManagedReader, intent_id: int, compound_key: str, now: float, *,
                    current: Callable[[], bool] | None = None, on_commit: Callable[[], None] | None = None) -> Report:
    rows = members(opened, intent_id, compound_key)
    if rows and all(row.state == "committed" for row in rows):
        if on_commit is not None:
            db.write_lock(opened)
            if current is None or current():
                on_commit()
            opened.commit()
        return Report("verified", "every resource of the item is committed")
    if not rows or any(row.state not in ("metadata_verified", "committed") for row in rows):
        return Report("pending", "a resource of the item has no verified metadata yet")
    intents = [intended_of(row.expected_metadata) for row in rows]
    roles = sorted(intent.role if intent is not None else "" for intent in intents)
    if "" in roles or (roles != ["live_still", "live_video"] and roles != ["single"] * len(rows)) or len({row.asset_id for row in rows}) != len(rows):
        return Report("pending", "the item's resources are neither one Live Photo nor separate assets")
    assets = []
    for row in rows:
        try:
            assets.append(reader.get_asset(str(row.asset_id)))
        except (immich.NotCustody, immich.NotVisible) as error:
            return managed_upload.hold_unusable(opened, row, ("metadata_verified",), now, managed_upload.kind_of(error),
                                                "an asset of the item is not an available managed original of this person")
        except immich.ImmichFailed:
            return Report("pending", "the item's assets could not be read again")
    for asset, row in zip(assets, rows, strict=True):
        if asset.sha1 != row.source_sha1:
            return managed_upload.hold_unusable(opened, row, ("metadata_verified",), now, "other_bytes", "an asset of the item holds other bytes than its source")
    if not linked([intent for intent in intents if intent is not None], assets):
        return Report("pending", REASONS["not_linked"] if "live_still" in roles else "separate assets of the item are linked")
    opened.commit()
    db.write_lock(opened)
    if current is not None and not current():
        opened.rollback()
        return Report("pending", "the item's owner or source changed while it was checked")
    refusal = manifest_refusal(opened, intent_id, compound_key)
    if refusal is not None:
        opened.rollback()
        return refusal
    receipt = ManagedImportReceipt
    changed = 0
    for row in rows:
        if row.state == "metadata_verified":
            changed += opened.connection().execute(update(receipt).where(receipt.id == row.id, receipt.state == "metadata_verified", receipt.asset_id == row.asset_id,
                                                                        receipt.updated_at == row.updated_at, receipt.metadata_hold.is_(None))
                                                   .values(state="committed", committed_at=now, updated_at=now)).rowcount
    present = set(opened.scalars(select(receipt.id).where(receipt.intent_id == intent_id, receipt.compound_key == compound_key)))
    if changed != sum(row.state == "metadata_verified" for row in rows) or present != {row.id for row in rows}:
        opened.rollback()
        return Report("pending", "the item changed while it was checked")
    opened.connection().execute(update(ManagedCompound).where(ManagedCompound.intent_id == intent_id, ManagedCompound.compound_key == compound_key,
                                                              ManagedCompound.committed_at.is_(None)).values(committed_at=now))
    if on_commit is not None:
        on_commit()
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
    sent = managed_upload.fresh(opened, video.receipt_id)
    try:
        seen = reader.get_asset(str(sent.asset_id))
    except (immich.NotCustody, immich.NotVisible) as error:
        return managed_upload.hold_unusable(opened, sent, ("metadata_verified",), now, managed_upload.kind_of(error),
                                            "the Live Photo's video is no longer an available managed original of this person")
    except immich.ImmichFailed:
        return Report("pending", "the Live Photo's video could not be read again before its still was sent")
    if seen.kind != "VIDEO" or seen.sha1 != sent.source_sha1:
        return managed_upload.hold_unusable(opened, sent, ("metadata_verified",), now, "other_bytes", "the Live Photo's video is no longer the video that was sent")
    second = complete_receipt(opened, reader, still, now, pause, sent.asset_id)
    if second.outcome != "verified":
        return second
    return commit_compound(opened, reader, rows[0].intent_id, rows[0].compound_key, now)
