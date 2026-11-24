from __future__ import annotations

import json
from typing import Final

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from darkroom.domain import managed_ingest, review
from darkroom.storage.models import DedupeDecision, ManagedImportIntent, ManagedImportReceipt, MediaItem, ReviewItem, TakeoutEntry

SOURCES: Final = ("import", "kept", "backfill")
LIST_LIMIT: Final = 100
FACT_KEYS: Final = ("wall_clock", "file_utc", "google_utc", "place_known", "description_length", "description_unwritable", "kind", "differs",
                    "immich_taken", "immich_offset", "intended_taken", "intended_offset")
CAUSES: Final = {
    "unknown_offset": "The photo says what time it was on the clock, but not which time zone, and Google's time and the place don't settle it.",
    "no_instant": "Nothing in the photo or from Google says when it was taken.",
    "unwritable": "Google's description has characters that can't go into the file Immich reads, so nothing was sent.",
    "not_enriched": "Immich already had this photo, so it kept its own copy, which lacks something Google knows (a time, an offset, a place or a description). "
                    "Darkroom can't add it to Immich's copy.",
    "not_settled": "Immich read the photo, but for a day it showed different metadata than Darkroom sent.",
    "unusable": "Immich's copy is in the trash, offline, in an external library or someone else's, so Darkroom can't count it.",
}
ACTIONS: Final = {
    "unknown_offset": ("offset", "skip"),
    "no_instant": ("capture_time", "skip"),
    "unwritable": ("drop_description", "skip"),
    "not_enriched": ("accept", "record_offset", "recheck"),
    "not_settled": ("recheck", "accept"),
    "unusable": ("recheck", "skip"),
}


def source_of(resource_key: str) -> str | None:
    if resource_key.startswith("takeout-entry:"):
        return "import"
    if resource_key.startswith(managed_ingest.KEPT):
        return "kept"
    return "backfill" if resource_key.startswith("backfill:media-item:") else None


def facts_of(raw: str | None) -> dict[str, object] | None:
    try:
        found = None if raw is None else json.loads(raw)
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    return {key: found[key] for key in FACT_KEYS if key in found}


def number_after(resource_key: str, prefix: str) -> int | None:
    tail = resource_key.removeprefix(prefix)
    return int(tail) if tail.isdigit() else None


def name_of(opened: Session, source: str, resource_key: str) -> str | None:
    if source == "import":
        entry_id = number_after(resource_key, "takeout-entry:")
        entry = None if entry_id is None else opened.get(TakeoutEntry, entry_id)
        return None if entry is None else review.name_of(entry.name)
    if source == "kept":
        review_id = number_after(resource_key, managed_ingest.KEPT)
        item = None if review_id is None else opened.get(ReviewItem, review_id)
        decision = None if item is None else opened.get(DedupeDecision, item.decision_id)
        return None if decision is None else review.name_of(decision.source_path)
    item_id = number_after(resource_key, "backfill:media-item:")
    media_item = None if item_id is None else opened.get(MediaItem, item_id)
    return None if media_item is None else review.name_of(media_item.path)


def allowed(hold: str, facts: dict[str, object] | None) -> list[str]:
    differs = None if facts is None else facts.get("differs")
    return [action for action in ACTIONS[hold] if action != "record_offset" or (isinstance(differs, list) and "offset" in differs)]


def held(person_id: int | None, source: str | None) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = [ManagedImportReceipt.metadata_hold.in_(managed_ingest.TERMINAL), ManagedImportReceipt.state != "committed"]
    if person_id is not None:
        where.append(ManagedImportIntent.person_id == person_id)
    if source is not None:
        where.append(ManagedImportReceipt.resource_key.startswith({"import": "takeout-entry:", "kept": managed_ingest.KEPT, "backfill": "backfill:media-item:"}[source]))
    return where


def listing(opened: Session, person_id: int | None, source: str | None, after: int | None) -> tuple[list[dict[str, object]], int]:
    base = select(ManagedImportReceipt, ManagedImportIntent).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
    where = held(person_id, source)
    total = int(opened.execute(select(func.count(ManagedImportReceipt.id)).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
                               .where(*where)).scalar_one())
    if after is not None:
        where.append(ManagedImportReceipt.id < after)
    found = []
    for receipt, intent in opened.execute(base.where(*where).order_by(ManagedImportReceipt.id.desc()).limit(LIST_LIMIT)).all():
        kind, hold = source_of(receipt.resource_key), str(receipt.metadata_hold)
        if kind is None or hold not in ACTIONS:
            continue
        facts = facts_of(receipt.hold_facts)
        found.append({"id": receipt.id, "source": kind, "person_id": intent.person_id, "export_id": intent.export_id,
                      "name": name_of(opened, kind, receipt.resource_key), "hold": hold, "reason": CAUSES[hold], "facts": facts,
                      "actions": allowed(hold, facts), "bytes_verified_at": receipt.bytes_verified_at, "updated_at": receipt.updated_at})
    return found, total
