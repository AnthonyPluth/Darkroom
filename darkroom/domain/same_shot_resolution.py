from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.domain import alerts, backfill, events, hold_resolutions, immich_reconcile, managed_metadata, same_shot
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import (Export, ManagedAssetOrigin, ManagedBackfillItem, ManagedCompoundMember, ManagedImportIntent, ManagedImportReceipt, ManagedPixelSource,
                                     ManagedSameShotHold, Person)

RESOLVED_EVENT: Final = "same_shot_resolved"
ACTIONS: Final = ("upload", "represented", "undo")
NOT_PENDING: Final = "this hold is not waiting for a decision"
NOT_HELD: Final = "this photo is no longer held as a same shot"
SENT: Final = "already sent, so it can't be undone: Darkroom never takes a photo back out of Immich"
NOTHING_TO_UNDO: Final = "this hold has no decision to undo"
NEEDS_ASSET: Final = "an Immich asset is required, one of the hold's candidates"
NO_ASSET: Final = "this action takes no Immich asset"
NOT_A_CANDIDATE: Final = "that asset is not one of this hold's candidates"
CROWDED: Final = "the window held too many assets to list candidates, so only upload can resolve it"
NOT_MANAGED: Final = "that candidate was trashed, offline or in an external library when the hold was made, so only upload can resolve it"
GONE: Final = "that asset is missing, trashed, offline, external or not this person's now, so it can't stand for the photo; upload is still possible"
CHANGED: Final = "that asset holds other bytes than when the hold was made, so it can't stand for the photo; upload is still possible"
DEPENDED: Final = "a Pixel copy or a backfill mapping already depends on this decision, so it can't be undone"
UNAVAILABLE: Final = "Immich could not be asked, so nothing changed; try again"


class Refused(ValueError):
    pass


class Conflict(ValueError):
    pass


class NoSuchHold(LookupError):
    pass


class Unavailable(RuntimeError):
    pass


def source_key(hold: ManagedSameShotHold) -> str:
    return f"{same_shot.DECISION_PREFIX}{hold.id}"


def stored_of(hold: ManagedSameShotHold) -> dict[str, object]:
    try:
        stored = json.loads(hold.candidates)
    except ValueError:
        return {}
    return stored if isinstance(stored, dict) else {}


def candidates_of(hold: ManagedSameShotHold) -> dict[str, dict[str, object]]:
    found = stored_of(hold).get("candidates")
    return {str(each["asset_id"]): each for each in found if isinstance(each, dict) and "asset_id" in each} if isinstance(found, list) else {}


def crowded(hold: ManagedSameShotHold) -> bool:
    return stored_of(hold).get("crowded") is True


def held_of(opened: Session, person_id: int, hold_id: int) -> ManagedSameShotHold:
    found = opened.execute(select(ManagedSameShotHold).where(ManagedSameShotHold.id == hold_id, ManagedSameShotHold.person_id == person_id)
                           .execution_options(populate_existing=True)).scalar_one_or_none()
    if found is None:
        raise NoSuchHold(hold_id)
    return found


def receipt_of(opened: Session, hold: ManagedSameShotHold) -> ManagedImportReceipt:
    return opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.id == hold.receipt_id).execution_options(populate_existing=True)).scalar_one()


def unsent_held(receipt: ManagedImportReceipt) -> bool:
    return receipt.metadata_hold == same_shot.HOLD and hold_resolutions.unsent(receipt)


def listing(opened: Session, person: Person) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    query = select(ManagedSameShotHold).where(ManagedSameShotHold.person_id == person.id, ManagedSameShotHold.status == "pending").order_by(ManagedSameShotHold.id)
    for hold in opened.execute(query).scalars():
        receipt = receipt_of(opened, hold)
        found.append({"id": hold.id, "taken": taken_at(receipt), "edited": stored_of(hold).get("edited") is True, "crowded": crowded(hold),
                      "candidates": [{key: each[key] for key in ("asset_id", "library", "camera", "size", "more_pixels") if key in each}
                                     for each in candidates_of(hold).values()]})
    return found


def taken_at(receipt: ManagedImportReceipt) -> str | None:
    intended = managed_metadata.intended_of(receipt.expected_metadata)
    if intended is None or intended.taken_utc is None:
        return None
    zone = UTC if intended.offset is None else timezone(timedelta(minutes=intended.offset))
    return datetime.fromtimestamp(intended.taken_utc, zone).isoformat(timespec="seconds")


def lines(rows: list[dict[str, object]], immich_url: str) -> list[str]:
    if not rows:
        return ["no same-shot holds are waiting"]
    out = []
    for row in rows:
        flags = [name for name in ("edited", "crowded") if row[name]]
        out.append(f"hold {row['id']}: taken {row['taken'] or 'at an unknown time'}{', ' + ', '.join(flags) if flags else ''}")
        candidates = row["candidates"]
        assert isinstance(candidates, list)
        for each in candidates:
            facts = [f"{label} {'yes' if each[key] else 'no'}" for label, key in (("same camera", "camera"), ("same size", "size"), ("Takeout has more pixels", "more_pixels"))]
            out.append(f"  candidate {each['asset_id']} ({each['library']}; {', '.join(facts)}): {immich_url}/photos/{each['asset_id']}")
    return out


def decided(opened: Session, hold: ManagedSameShotHold, receipt: ManagedImportReceipt, action: str, by: int | None, now: float) -> None:
    suffix = "" if by is None else f", by person {by}"
    events.record(opened, now, RESOLVED_EVENT, f"receipt {receipt.id}", f"hold {hold.id} {action}{suffix}")


def upload(opened: Session, person: Person, hold_id: int, by: int | None, now: float) -> ManagedSameShotHold:
    db.write_lock(opened)
    hold = held_of(opened, person.id, hold_id)
    receipt = receipt_of(opened, hold)
    if hold.status != "pending":
        raise Conflict(NOT_PENDING)
    if not unsent_held(receipt):
        raise Conflict(NOT_HELD)
    hold.status, hold.resolved_at, hold.resolved_by = "upload", now, by
    receipt.metadata_hold, receipt.updated_at = None, now
    decided(opened, hold, receipt, "upload", by, now)
    return hold


def read_back(settings: config.Settings, client: immich.Client | None, person: Person, hold: ManagedSameShotHold, asset_id: str,
              candidate: dict[str, object]) -> tuple[immich.Asset, int]:
    try:
        reader = immich.ManagedReader(client or immich.Client.at(settings), immich_reconcile.person_key(settings, person), hold.owner_id, access="read_or_import")
        reader.check_access()
        found = reader.get_asset(asset_id)
        if found.sha1 != candidate.get("sha1") or found.kind != candidate.get("kind"):
            raise Conflict(CHANGED)
        size = sum(len(chunk) for chunk in reader.original_chunks(asset_id, found.sha1))
    except (immich.NotCustody, immich.NotVisible):
        raise Conflict(GONE) from None
    except immich.ChecksumMismatch:
        raise Conflict(CHANGED) from None
    except (immich.ImmichFailed, immich_reconcile.NotReady) as failure:
        if getattr(failure, "status", None) == 404:
            raise Conflict(GONE) from None
        raise Unavailable(UNAVAILABLE) from None
    return found, size


def represent(opened: Session, settings: config.Settings, client: immich.Client | None, person: Person, hold_id: int, asset_id: object, by: int | None,
              now: float) -> ManagedSameShotHold:
    if not isinstance(asset_id, str) or not asset_id:
        raise Refused(NEEDS_ASSET)
    hold = held_of(opened, person.id, hold_id)
    if hold.status != "pending":
        raise Conflict(NOT_PENDING)
    if crowded(hold):
        raise Conflict(CROWDED)
    candidate = candidates_of(hold).get(asset_id)
    if candidate is None:
        raise Refused(NOT_A_CANDIDATE)
    if candidate.get("library") != "managed":
        raise Conflict(NOT_MANAGED)
    owner = hold.owner_id
    opened.commit()
    found, size = read_back(settings, client, person, hold, asset_id, candidate)
    db.write_lock(opened)
    hold = held_of(opened, person.id, hold_id)
    receipt = receipt_of(opened, hold)
    current = opened.get(Person, person.id, populate_existing=True)
    if hold.status != "pending" or not unsent_held(receipt) or current is None or current.immich_user_id != owner or hold.owner_id != owner:
        opened.rollback()
        raise Conflict(NOT_PENDING if hold.status != "pending" else NOT_HELD)
    snapshot = json.dumps({"origin": "takeout", "same_shot_hold": hold.id, "owner_decision": True}, sort_keys=True)
    try:
        version = backfill.version_of(opened, current, found.id, found.sha1, size, found.kind, found.live_video_id, snapshot, now)
    except immich.NotCustody:
        opened.rollback()
        raise Conflict(CHANGED) from None
    opened.add(ManagedAssetOrigin(version_id=version.id, person_id=version.person_id, owner_id=version.owner_id, asset_id=version.asset_id, sha1=version.sha1,
                                  kind="takeout", source_key=source_key(hold), intent_id=receipt.intent_id, evidence_snapshot=snapshot, recorded_at=now))
    hold.status, hold.resolved_at, hold.resolved_by, hold.asset_id = "represented", now, by, found.id
    opened.flush()
    decided(opened, hold, receipt, "represented", by, now)
    return hold


def depended_on(opened: Session, version_id: int) -> bool:
    pixel = opened.execute(select(ManagedPixelSource.id).where(ManagedPixelSource.version_id == version_id).limit(1)).first()
    mapped = opened.execute(select(ManagedBackfillItem.id).where(ManagedBackfillItem.version_id == version_id).limit(1)).first()
    return pixel is not None or mapped is not None


def compound_unsent(opened: Session, receipt: ManagedImportReceipt) -> bool:
    member = ManagedCompoundMember
    compound_ids = select(member.compound_id).where(member.intent_id == receipt.intent_id, member.resource_key == receipt.resource_key)
    keys = select(member.resource_key).where(member.intent_id == receipt.intent_id, member.compound_id.in_(compound_ids))
    others = opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.intent_id == receipt.intent_id, ManagedImportReceipt.id != receipt.id,
                                                              ManagedImportReceipt.resource_key.in_(keys))
                            .execution_options(populate_existing=True)).scalars()
    return all(hold_resolutions.unsent(each) for each in others)


def undo(opened: Session, person: Person, hold_id: int, by: int | None, now: float) -> ManagedSameShotHold:
    db.write_lock(opened)
    hold = held_of(opened, person.id, hold_id)
    receipt = receipt_of(opened, hold)
    if hold.status == "upload":
        if receipt.metadata_hold is not None or not hold_resolutions.unsent(receipt) or not compound_unsent(opened, receipt):
            raise Conflict(SENT)
        receipt.metadata_hold, receipt.updated_at = same_shot.HOLD, now
    elif hold.status == "represented":
        origin = opened.execute(select(ManagedAssetOrigin).where(ManagedAssetOrigin.source_key == source_key(hold), ManagedAssetOrigin.kind == "takeout",
                                                                 ManagedAssetOrigin.person_id == person.id)).scalar_one_or_none()
        if origin is not None:
            if depended_on(opened, origin.version_id):
                raise Conflict(DEPENDED)
            opened.delete(origin)
    else:
        raise Conflict(NOTHING_TO_UNDO)
    hold.status, hold.resolved_at, hold.resolved_by, hold.asset_id = "pending", None, None, None
    opened.flush()
    decided(opened, hold, receipt, "undo", by, now)
    return hold


def settle_alert(opened: Session, settings: config.Settings, hold: ManagedSameShotHold, now: float, notifier: alerts.Notifier | None = None) -> None:
    receipt = receipt_of(opened, hold)
    intent = opened.get(ManagedImportIntent, receipt.intent_id)
    export = None if intent is None or intent.kind != "import" or intent.export_id is None else opened.get(Export, intent.export_id)
    person = opened.get(Person, hold.person_id)
    if export is not None and person is not None:
        same_shot.alert(opened, settings, export, person, now, notifier)


def act(opened: Session, settings: config.Settings, client: immich.Client | None, person: Person, hold_id: int, action: object, asset_id: object, by: int | None,
        now: float, notifier: alerts.Notifier | None = None) -> ManagedSameShotHold:
    if not isinstance(action, str) or action not in ACTIONS:
        raise Refused(f"the action must be one of {', '.join(ACTIONS)}")
    if action != "represented" and asset_id is not None:
        raise Refused(NO_ASSET)
    held_of(opened, person.id, hold_id)
    if action == "represented":
        hold = represent(opened, settings, client, person, hold_id, asset_id, by, now)
    elif action == "upload":
        hold = upload(opened, person, hold_id, by, now)
    else:
        hold = undo(opened, person, hold_id, by, now)
    opened.commit()
    settle_alert(opened, settings, hold, now, notifier)
    return hold
