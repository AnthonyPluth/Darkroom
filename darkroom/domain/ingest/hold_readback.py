from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, exists, update
from sqlalchemy.orm import Session

from darkroom.domain import events, hold_resolutions, managed_metadata, managed_upload
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import ManagedHoldResolution, ManagedImportReceipt


@dataclass(frozen=True)
class Result:
    code: str
    hold: str | None
    facts: str | None = None
    evidence: str | None = None


def unusable(kind: str) -> Result:
    return Result("still_unusable", managed_upload.UNUSABLE, json.dumps({"kind": kind}, sort_keys=True))


def inspect(reader: immich.ManagedReader, receipt: ManagedImportReceipt, intended: managed_metadata.Intended,
            action: str, pause: Callable[[float], None] = time.sleep) -> Result | None:
    asset_id = receipt.asset_id
    if asset_id is None:
        return None
    check = action == "recheck"
    try:
        reader.verify_original(asset_id, receipt.source_sha1, receipt.expected_bytes)
        for attempt in range(managed_metadata.POLLS if check else 1):
            if attempt:
                pause(managed_metadata.POLL_SECONDS)
            asset, seen = reader.extracted(asset_id)
            if asset.sha1 != receipt.source_sha1:
                return unusable("other_bytes")
            if asset.kind != intended.kind or asset.live_video_id is not None:
                return unusable("unavailable")
            if seen is not None and (not check or managed_metadata.problem(intended, asset, seen, None) is None):
                return Result("rechecked" if check else "cleared", None, evidence=managed_metadata.evidence(asset, seen))
    except (immich.NotCustody, immich.NotVisible) as error:
        return unusable(managed_upload.kind_of(error))
    except immich.ChecksumMismatch:
        return unusable("other_bytes")
    hold = receipt.metadata_hold
    if seen is None:
        return Result("still_differs", hold, receipt.hold_facts) if check and hold != managed_upload.UNUSABLE else None
    if hold == managed_upload.UNUSABLE:
        hold = "not_enriched" if receipt.upload_outcome == "duplicate" and intended.sidecar else "not_settled"
    return Result("still_differs", hold, managed_metadata.differences(intended, seen))


def apply(opened: Session, row: ManagedHoldResolution, receipt: ManagedImportReceipt, result: Result, bound: ColumnElement[bool], now: float) -> bool:
    db.write_lock(opened)
    recorded = exists().where(ManagedHoldResolution.id == row.id, ManagedHoldResolution.state == "recorded")
    model = ManagedImportReceipt
    changes: dict[str, object] = {"metadata_hold": result.hold, "hold_facts": result.facts, "updated_at": now}
    if result.evidence is not None:
        changes.update(state="metadata_verified", metadata_evidence=result.evidence, metadata_verified_at=now)
    elif result.code == "still_differs" and receipt.state != "bytes_verified":
        changes.update(state="bytes_verified", metadata_evidence=None, metadata_verified_at=None)
    if changes.get("state") in ("bytes_verified", "metadata_verified") and receipt.bytes_verified_at is None:
        changes.update(verified_sha1=receipt.source_sha1, verified_bytes=receipt.expected_bytes, bytes_verified_at=now)
    query = update(model).where(model.id == receipt.id, model.state == receipt.state, model.updated_at == receipt.updated_at,
                                model.intent_id == receipt.intent_id, model.resource_key == receipt.resource_key, model.compound_key == receipt.compound_key,
                                model.metadata_hold == receipt.metadata_hold, model.hold_facts == receipt.hold_facts,
                                model.asset_id == receipt.asset_id, model.source_sha1 == receipt.source_sha1, model.expected_bytes == receipt.expected_bytes,
                                model.expected_metadata == receipt.expected_metadata, recorded, bound).values(**changes)
    if opened.connection().execute(query).rowcount != 1:
        opened.rollback()
        return False
    row.state = "applied" if result.hold is None else "superseded"
    row.result, row.applied_at = result.code, now
    events.record(opened, now, hold_resolutions.APPLIED_EVENT, f"receipt {receipt.id}", result.code)
    return True
