from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, managed_metadata, managed_upload
from darkroom.domain.dedupe import same_text
from darkroom.domain.media import CAMERA_RAW_SUFFIXES
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import (DedupeDecision, Export, ManagedAssetOrigin, ManagedAssetVersion, ManagedImportIntent, ManagedImportReceipt,
                                     ManagedSameShotHold, Person, TakeoutEntry)

HOLD: Final = managed_upload.SAME_SHOT_HOLD
TOLERANCE_SECONDS: Final = 1.0
SCHEMA: Final = 1
ALERT_KIND: Final = "same_shot"
VERIFIED_VERSION: Final = "bytes_verified"
OWN_ORIGINS: Final = ("phone", "other")
WAITS_FOR_REVERSAL: Final = "this photo looks like one already in Immich and waits for the owner's decision, so its decision stays as it is"
WAITS_FOR_REASSIGNMENT: Final = "this photo looks like one already in Immich and waits for the owner's decision, so it stays with its person"

Library = Literal["managed", "external", "offline", "trashed"]
Outcome = Literal["clear", "held", "pending", "reversed"]


@dataclass(frozen=True)
class Incoming:
    owner_id: str
    sha1: str
    instant: float
    kind: str
    name: str
    make: str | None
    model: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    edited: bool


@dataclass(frozen=True)
class Candidate:
    asset_id: str
    sha1: str
    kind: str
    library: Library
    instant: float
    camera: bool
    size: bool
    more_pixels: bool


@dataclass(frozen=True)
class Verdict:
    candidates: tuple[Candidate, ...]
    crowded: bool
    edited: bool

    @property
    def held(self) -> bool:
        return self.crowded or bool(self.candidates)


def raw(name: str) -> bool:
    return Path(name).suffix.lower() in CAMERA_RAW_SUFFIXES


def library_of(asset: immich.Asset) -> Library:
    if asset.trashed:
        return "trashed"
    if asset.offline:
        return "offline"
    return "managed" if asset.library_id is None else "external"


def pixels(width: int | None, height: int | None) -> int | None:
    return None if width is None or height is None else width * height


def same_size(incoming: Incoming, shot: immich.Nearby) -> bool:
    if incoming.kind == "VIDEO":
        return incoming.duration_ms is not None and shot.duration_ms is not None and incoming.duration_ms // 1000 == shot.duration_ms // 1000
    return incoming.width is not None and incoming.height is not None and (incoming.width, incoming.height) == (shot.width, shot.height)


def more_pixels(incoming: Incoming, shot: immich.Nearby) -> bool:
    mine, theirs = pixels(incoming.width, incoming.height), pixels(shot.width, shot.height)
    return incoming.kind == "IMAGE" and mine is not None and theirs is not None and mine > theirs


def apart_ms(incoming: Incoming, shot: immich.Nearby) -> int:
    return round(abs(shot.taken.timestamp() - incoming.instant) * 1000)


def match(incoming: Incoming, near: immich.NearShots, takeout_only: Collection[str] = ()) -> Verdict:
    mine = [shot for shot in near.found if shot.asset.owner_id == incoming.owner_id]
    if near.crowded:
        return Verdict((), True, incoming.edited)
    if any(shot.asset.sha1 == incoming.sha1 for shot in mine):
        return Verdict((), False, incoming.edited)
    motion = {shot.asset.live_video_id for shot in mine if shot.asset.live_video_id is not None}
    found = []
    for shot in sorted(mine, key=lambda each: each.asset.id):
        asset = shot.asset
        if (asset.kind != incoming.kind or asset.visibility == "hidden" or asset.id in motion or raw(asset.file_name) != raw(incoming.name)
                or asset.id in takeout_only or apart_ms(incoming, shot) > round(TOLERANCE_SECONDS * 1000)):
            continue
        camera = same_text(incoming.make, shot.make) and same_text(incoming.model, shot.model)
        found.append(Candidate(asset.id, asset.sha1, asset.kind, library_of(asset), shot.taken.timestamp(), camera, same_size(incoming, shot),
                               more_pixels(incoming, shot)))
    return Verdict(tuple(found), False, incoming.edited)


def snapshot(verdict: Verdict) -> str:
    return json.dumps({"schema": SCHEMA, "edited": verdict.edited, "crowded": verdict.crowded,
                       "candidates": [{"asset_id": each.asset_id, "sha1": each.sha1, "kind": each.kind, "library": each.library, "instant": each.instant,
                                       "camera": each.camera, "size": each.size, "more_pixels": each.more_pixels} for each in verdict.candidates]},
                      sort_keys=True)


def takeout_only(opened: Session, person_id: int, owner_id: str, asset_ids: Collection[str]) -> set[str]:
    if not asset_ids:
        return set()
    wanted = ManagedAssetVersion.asset_id.in_(asset_ids)
    mine = (ManagedAssetVersion.person_id == person_id, ManagedAssetVersion.owner_id == owner_id, wanted)
    verified = select(ManagedAssetVersion.id).where(*mine, ManagedAssetVersion.state == VERIFIED_VERSION)
    from_takeout = set(opened.execute(select(ManagedAssetVersion.asset_id).where(ManagedAssetVersion.id.in_(verified), exists().where(
        ManagedAssetOrigin.version_id == ManagedAssetVersion.id, ManagedAssetOrigin.kind == "takeout"))).scalars())
    uploaded = (select(ManagedImportReceipt.asset_id).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
                .where(ManagedImportIntent.person_id == person_id, ManagedImportIntent.owner_id == owner_id, ManagedImportReceipt.asset_id.in_(asset_ids),
                       ManagedImportReceipt.upload_outcome == "created", ManagedImportReceipt.state.in_(managed_upload.VERIFIED)))
    from_receipts = {found for found in opened.execute(uploaded).scalars() if found is not None}
    own = set(opened.execute(select(ManagedAssetVersion.asset_id).where(*mine, exists().where(
        ManagedAssetOrigin.version_id == ManagedAssetVersion.id, ManagedAssetOrigin.kind.in_(OWN_ORIGINS)))).scalars())
    return (from_takeout | from_receipts) - own


def incoming_of(row: ManagedImportReceipt, intent: ManagedImportIntent, entry: TakeoutEntry, name: str, intended: managed_metadata.Intended) -> Incoming | None:
    if intended.taken_utc is None:
        return None
    return Incoming(intent.owner_id, row.source_sha1, intended.taken_utc, intended.kind, name, entry.make, entry.model, entry.width, entry.height,
                    entry.duration_ms, entry.is_edited or entry.edited_of is not None)


def hold_of(opened: Session, receipt_id: int) -> ManagedSameShotHold | None:
    query = select(ManagedSameShotHold).where(ManagedSameShotHold.receipt_id == receipt_id)
    return opened.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()


def make_hold(opened: Session, intent: ManagedImportIntent, receipt_id: int, decision_id: int | None, verdict: Verdict, now: float) -> Outcome:
    opened.commit()
    db.write_lock(opened)
    decision = None if decision_id is None else opened.get(DedupeDecision, decision_id, populate_existing=True)
    if decision is None or decision.reversed_at is not None:
        opened.commit()
        return "reversed"
    row = opened.get(ManagedImportReceipt, receipt_id, populate_existing=True)
    if row is None or row.metadata_hold is not None or row.state != "prepared" or row.send_attempts != 0 or hold_of(opened, receipt_id) is not None:
        found = row is not None and row.metadata_hold == HOLD
        opened.commit()
        return "held" if found else "clear"
    row.metadata_hold, row.updated_at = HOLD, now
    managed_upload.supersede_resolution(opened, receipt_id)
    opened.add(ManagedSameShotHold(person_id=intent.person_id, owner_id=intent.owner_id, receipt_id=receipt_id, found_at=now, candidates=snapshot(verdict)))
    events.record(opened, now, "same_shot_held", f"receipt {receipt_id}", f"{len(verdict.candidates)} candidates{', crowded' if verdict.crowded else ''}")
    opened.commit()
    monitoring.log("photo held as a different-byte same shot", receipt=receipt_id, candidates=len(verdict.candidates), crowded=verdict.crowded)
    return "held"


def gate(opened: Session, reader: immich.ManagedReader, intent: ManagedImportIntent, receipt_id: int, entry: TakeoutEntry, name: str, now: float) -> Outcome:
    row = managed_upload.fresh(opened, receipt_id)
    if row.metadata_hold == HOLD:
        return "held"
    if row.state != "prepared" or row.send_attempts != 0 or row.metadata_hold is not None or hold_of(opened, receipt_id) is not None:
        opened.commit()
        return "clear"
    intended = managed_metadata.intended_of(row.expected_metadata)
    incoming = None if intended is None or intended.hold is not None else incoming_of(row, intent, entry, name, intended)
    if incoming is None:
        return "clear"
    try:
        near = reader.taken_near(incoming.instant, TOLERANCE_SECONDS)
        verdict = match(incoming, near, takeout_only(opened, intent.person_id, intent.owner_id, [shot.asset.id for shot in near.found]))
    except immich.ImmichFailed:
        opened.rollback()
        monitoring.log("same-shot check could not be made", receipt=receipt_id)
        return "pending"
    if not verdict.held:
        opened.commit()
        return "clear"
    return make_hold(opened, intent, receipt_id, entry.decision_id, verdict, now)


def pending_for_receipt_keys(opened: Session, keys: Collection[str]) -> bool:
    if not keys:
        return False
    query = (select(ManagedSameShotHold.id).join(ManagedImportReceipt, ManagedImportReceipt.id == ManagedSameShotHold.receipt_id)
             .where(ManagedImportReceipt.resource_key.in_(keys), ManagedSameShotHold.status == "pending").limit(1))
    return opened.execute(query).first() is not None


def pending_for_decision(opened: Session, decision_id: int) -> bool:
    entries = opened.execute(select(TakeoutEntry.id).where(TakeoutEntry.decision_id == decision_id)).scalars().all()
    return pending_for_receipt_keys(opened, [f"takeout-entry:{entry_id}" for entry_id in entries])


def pending_for_item(opened: Session, item_id: int) -> bool:
    entries = opened.execute(select(TakeoutEntry.id).where(TakeoutEntry.item_id == item_id)).scalars().all()
    decisions = opened.execute(select(DedupeDecision.id).where(DedupeDecision.takeout_item_id == item_id)).scalars().all()
    return pending_for_receipt_keys(opened, [f"takeout-entry:{entry_id}" for entry_id in entries]) or any(pending_for_decision(opened, each) for each in decisions)


def pending_count(opened: Session, export: Export, person: Person) -> int:
    query = (select(ManagedSameShotHold.id).join(ManagedImportReceipt, ManagedImportReceipt.id == ManagedSameShotHold.receipt_id)
             .join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
             .where(ManagedImportIntent.export_id == export.id, ManagedImportIntent.kind == "import", ManagedSameShotHold.person_id == person.id,
                    ManagedSameShotHold.status == "pending"))
    return len(opened.execute(query).all())


def alert_key(export: Export, person: Person) -> str:
    return f"managed:same_shot:{export.id}:{person.slug}"


def alert(opened: Session, settings: config.Settings, export: Export, person: Person, now: float, notifier: alerts.Notifier | None) -> None:
    opened.commit()
    db.write_lock(opened)
    waiting = pending_count(opened, export, person)
    if waiting == 0:
        alerts.close(opened, alert_key(export, person), now)
    else:
        verb = "looks like one already in Immich and waits" if waiting == 1 else "look like ones already in Immich and wait"
        alerts.open_alert(opened, settings, alert_key(export, person), ALERT_KIND, f"export {export.id}",
                          alerts.Wording(alerts.TITLES[ALERT_KIND], f"{alerts.counted(waiting, 'photo')} {verb} for you. Nothing was uploaded or deleted."),
                          None, now, notifier=notifier, about=alerts.person(person.id))
    opened.commit()
