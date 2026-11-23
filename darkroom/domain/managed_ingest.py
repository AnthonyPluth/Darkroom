from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, exports, immich_reconcile, managed_metadata, managed_upload, media, xmp
from darkroom.domain.managed_upload import Report
from darkroom.providers import immich
from darkroom.storage import db, paths
from darkroom.storage.models import (
    DedupeDecision, Export, ExportPart, GoogleAccount, ManagedImportIntent, ManagedImportReceipt, Person, ReviewItem, TakeoutEntry,
)
from darkroom.storage.paths import Photos

PAUSING: Final = "pausing"
ON: Final = "on"
STOPPED: Final = "stopped"
FINISHED: Final = ("imported", "failed", "expired")
STARTED_PARTS: Final = ("ingesting", "ingested", "failed")
MANIFEST: Final = json.dumps({"schema": 1, "resource": "takeout_entry", "compound": "single"}, sort_keys=True)
KEEP_FOR: Final = 14 * 24 * 3600.0
SAME_SHOT: Final = "same_shot"
SAME_SHOT_SECONDS: Final = 1.0
PAUSED_CLEANUP: Final = "cleanup is paused while this person's Immich-managed imports are on"
GATE_OFF: Final = "cleanup of an Immich-managed import stays off until the owner records the restore rehearsal (#266)"


class Waiting(RuntimeError):
    pass


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    root: TakeoutEntry
    name: str = field(repr=False)
    sidecar: str | None = field(repr=False)
    live: bool
    borrowed: bool


@dataclass(frozen=True)
class RecoveryPoint:
    taken_at: float
    healthy: bool
    assets: frozenset[str] = field(repr=False)


@dataclass(frozen=True)
class Retention:
    rehearsed_at: float | None
    boundary_accepted_at: float | None
    recovery_point: Callable[[ManagedImportIntent], RecoveryPoint | None]
    reader: Callable[[ManagedImportIntent], immich.ManagedReader]


def paused(person: Person | None) -> bool:
    return person is not None and person.managed_imports is not None


def intent_of(opened: Session, export: Export) -> ManagedImportIntent | None:
    return opened.execute(select(ManagedImportIntent).where(ManagedImportIntent.export_id == export.id).order_by(ManagedImportIntent.id).limit(1)).scalar_one_or_none()


def held_back(opened: Session, export: Export) -> bool:
    return intent_of(opened, export) is not None or paused(exports.owner_of(opened, export))


def request(opened: Session, person: Person, on: bool, now: float) -> str | None:
    db.write_lock(opened)
    opened.refresh(person)
    if on and (not immich.is_canonical_id(person.immich_user_id) or not person.immich_api_key_sealed):
        raise Refused("this person has no Immich user and key, so their imports cannot go to Immich")
    started = opened.execute(select(ManagedImportIntent.id).where(ManagedImportIntent.person_id == person.id).limit(1)).first() is not None
    if on:
        person.managed_imports = PAUSING if person.managed_imports in (None, PAUSING) else ON
    else:
        person.managed_imports = STOPPED if started else None
    events.record(opened, now, "managed_imports_set", person.slug, person.managed_imports or "off")
    return person.managed_imports


def started(opened: Session, export: Export) -> bool:
    part = select(ExportPart.id).where(ExportPart.export_id == export.id, ExportPart.ingest_state.in_(STARTED_PARTS)).limit(1)
    decided = select(TakeoutEntry.id).where(TakeoutEntry.export_id == export.id, TakeoutEntry.decision_id.is_not(None)).limit(1)
    return export.stage is not None or opened.execute(part).first() is not None or opened.execute(decided).first() is not None


def legacy_in_flight(opened: Session, person: Person, export: Export) -> bool:
    served = select(GoogleAccount.id).where(GoogleAccount.default_person_id == person.id)
    owned = or_(Export.owner_person_id == person.id, and_(Export.owner_person_id.is_(None), Export.account_id.in_(served)))
    others = opened.execute(select(Export).where(Export.id != export.id, Export.status.not_in(FINISHED), owned)).scalars().all()
    return any(intent_of(opened, other) is None and started(opened, other) for other in others)


def switch(opened: Session, settings: config.Settings, person: Person, export: Export, now: float) -> None:
    if person.managed_imports == ON:
        return
    if person.managed_imports != PAUSING:
        raise Waiting("this person's Immich-managed imports are stopped")
    try:
        with immich_reconcile.person_lock(settings, person):
            db.write_lock(opened)
            opened.refresh(person)
            if person.managed_imports == PAUSING and legacy_in_flight(opened, person, export):
                opened.rollback()
                raise Waiting("waiting for this person's import into the old folders to finish")
            if person.managed_imports == PAUSING:
                person.managed_imports = ON
                events.record(opened, now, "managed_imports_on", person.slug, "the old folders' writers, the Immich reconcile and cleanup are paused")
            opened.commit()
    except immich_reconcile.Busy:
        raise Waiting("waiting for this person's Immich reconcile to finish") from None
    if person.managed_imports != ON:
        raise Waiting("this person's Immich-managed imports are stopped")


def begin(opened: Session, settings: config.Settings, export: Export, person: Person, now: float) -> ManagedImportIntent | None:
    intent = intent_of(opened, export)
    if intent is None and (person.managed_imports is None or started(opened, export)):
        return None
    switch(opened, settings, person, export, now)
    if intent is None:
        if not immich.is_canonical_id(person.immich_user_id):
            raise Waiting("this person has no Immich user")
        db.write_lock(opened)
        intent = ManagedImportIntent(person_id=person.id, export_id=export.id, owner_id=str(person.immich_user_id), resource_manifest=MANIFEST, created_at=now)
        opened.add(intent)
        events.record(opened, now, "managed_import_started", f"export {export.id}", person.slug)
        opened.commit()
        monitoring.log("export imported to Immich-managed storage", export=export.id)
    return intent


def reader_of(settings: config.Settings, person: Person, intent: ManagedImportIntent, client: immich.Client | None) -> immich.ManagedReader:
    try:
        key = immich_reconcile.person_key(settings, person)
    except immich_reconcile.NotReady as error:
        raise immich.ImmichFailed(str(error)) from None
    return immich.ManagedReader(client or immich.Client.at(settings), key, intent.owner_id, access="import")


def resource_key(entry: TakeoutEntry) -> str:
    return f"takeout-entry:{entry.id}"


def wanted(opened: Session, export: Export, root: TakeoutEntry) -> bool:
    row = None if root.decision_id is None else opened.get(DedupeDecision, root.decision_id)
    return (row is not None and row.export_id == export.id and row.account_id == export.account_id and row.reversed_at is None and row.decided_by == "auto"
            and row.tier == 5 and row.takeout_item_id is None and root.outcome != "failed" and root.sha1 is not None)


def receipts_of(opened: Session, intent: ManagedImportIntent) -> dict[str, ManagedImportReceipt]:
    query = select(ManagedImportReceipt).where(ManagedImportReceipt.intent_id == intent.id).order_by(ManagedImportReceipt.id)
    rows = opened.execute(query.execution_options(populate_existing=True)).scalars()
    return {row.resource_key: row for row in rows}


def nameable(name: str) -> bool:
    try:
        immich.upload_filename(Path(name).name)
    except immich.ImmichFailed:
        return False
    return True


def uploaded(opened: Session, export: Export) -> int:
    query = (select(func.count(ManagedImportReceipt.id)).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
             .where(ManagedImportIntent.export_id == export.id, ManagedImportReceipt.state == "committed"))
    return int(opened.execute(query).scalar_one())


class Uploads:
    def __init__(self, opened: Session, photos: Photos, export: Export, intent: ManagedImportIntent, reader: immich.ManagedReader, now: float,
                 pause: Callable[[float], None] | None = None) -> None:
        self.opened, self.photos, self.export, self.intent, self.reader = opened, photos, export, intent, reader
        self.now, self.pause = now, pause or time.sleep
        self.committed = self.held = self.pending = 0

    def due(self, items: Sequence[Item]) -> list[Item]:
        known = receipts_of(self.opened, self.intent)
        found = []
        for item in items:
            if not wanted(self.opened, self.export, item.root):
                continue
            row = known.get(resource_key(item.root))
            if row is not None and row.state == "committed":
                self.committed += 1
            elif item.live or item.root.bytes <= 0 or not nameable(item.name):
                self.held += 1
            else:
                found.append(item)
        self.opened.commit()
        return found

    def receipt(self, item: Item, tags: dict[str, object] | None) -> int | None:
        key = resource_key(item.root)
        found = receipts_of(self.opened, self.intent).get(key)
        if found is not None:
            return found.id
        if tags is None or "Error" in tags or item.root.sha1 is None:
            return None
        kind: managed_metadata.Kind = "VIDEO" if Path(item.name).suffix.lower() in media.VIDEO_SUFFIXES else "IMAGE"
        intended = managed_metadata.intend(tags, xmp.GoogleMetadata.of(item.sidecar), kind, corrects=not item.borrowed)
        db.write_lock(self.opened)
        found = receipts_of(self.opened, self.intent).get(key)
        if found is None:
            found = ManagedImportReceipt(intent_id=self.intent.id, resource_key=key, compound_key=key, source_sha1=item.root.sha1, expected_bytes=item.root.bytes,
                                         expected_metadata=managed_metadata.snapshot(intended), updated_at=self.now)
            self.opened.add(found)
            self.opened.flush()
        receipt_id = found.id
        self.opened.commit()
        return receipt_id

    def same_shot(self, receipt_id: int) -> bool:
        row = managed_upload.fresh(self.opened, receipt_id)
        if row.metadata_hold == SAME_SHOT:
            return True
        intended = managed_metadata.intended_of(row.expected_metadata)
        if row.state != "prepared" or row.send_attempts or intended is None or intended.hold is not None or intended.taken_utc is None:
            return False
        if not any(asset.sha1 != row.source_sha1 for asset in self.reader.taken_near(intended.taken_utc, SAME_SHOT_SECONDS)):
            return False
        managed_upload.move(self.opened, row, ("prepared",), self.now, ManagedImportReceipt.send_attempts == 0, metadata_hold=SAME_SHOT)
        return True

    def send(self, item: Item, source: Path, tags: dict[str, object] | None) -> None:
        receipt_id = self.receipt(item, tags)
        if receipt_id is None:
            self.held += 1
            return
        try:
            if self.same_shot(receipt_id):
                self.held += 1
                return
        except immich.ImmichFailed:
            self.pending += 1
            return
        sha1, size = str(item.root.sha1), item.root.bytes
        with contextlib.ExitStack() as handles:
            def original() -> immich.UploadSource:
                return immich.UploadSource(handles.enter_context(paths.open_inbox_file(self.photos, source)), size, sha1)
            report = managed_metadata.complete_receipt(self.opened, self.reader, managed_metadata.Resource(receipt_id, original, Path(item.name).name),
                                                       self.now, self.pause)
        if report.outcome == "verified":
            report = managed_metadata.commit_compound(self.opened, self.reader, self.intent.id, resource_key(item.root), self.now)
        self.count(receipt_id, report)

    def count(self, receipt_id: int, report: Report) -> None:
        row = managed_upload.fresh(self.opened, receipt_id)
        if row.state == "committed":
            self.committed += 1
        elif report.outcome in ("held", "unusable") or row.metadata_hold is not None:
            self.held += 1
        else:
            self.pending += 1


def alert(opened: Session, settings: config.Settings, export: Export, held: int, pending: int, now: float, notifier: alerts.Notifier | None) -> None:
    subject, about = f"export {export.id}", alerts.account(export.account_id)
    db.write_lock(opened)
    if held:
        alerts.open_alert(opened, settings, f"managed:held:{export.id}", "failure", subject, alerts.Wording(
            "Darkroom needs a look", f"{alerts.counted(held, 'item')} of export {export.id} were not uploaded to Immich and are kept in the inbox. "
            "Open Darkroom for details."), None, now, notifier=notifier, about=about)
    if pending:
        alerts.open_alert(opened, settings, f"managed:pending:{export.id}", "failure", subject, alerts.Wording(
            "Darkroom needs a look", f"{alerts.counted(pending, 'upload')} of export {export.id} to Immich could not be completed yet. "
            "Darkroom keeps the files and tries again."), None, now, notifier=notifier, about=about)
    else:
        alerts.close(opened, f"managed:pending:{export.id}", now)
    opened.commit()


def uncommitted(opened: Session, export: Export, receipts: dict[str, ManagedImportReceipt]) -> bool:
    roots = opened.execute(select(TakeoutEntry).where(TakeoutEntry.export_id == export.id, TakeoutEntry.kind == "media", TakeoutEntry.copy_of_id.is_(None))).scalars()
    if any(row.state != "committed" for row in receipts.values()):
        return True
    return any(wanted(opened, export, root) and resource_key(root) not in receipts for root in roots)


def release_refusal(opened: Session, export: Export, now: float, retention: Retention | None) -> str | None:
    intent = intent_of(opened, export)
    if intent is None:
        return PAUSED_CLEANUP if paused(exports.owner_of(opened, export)) else None
    if retention is None or retention.rehearsed_at is None:
        return GATE_OFF
    if retention.boundary_accepted_at is None:
        return "the owner has not accepted that another Immich client can remove an asset after Darkroom's last read"
    receipts = receipts_of(opened, intent)
    if uncommitted(opened, export, receipts):
        return "an item of this export has no committed Immich receipt"
    point = retention.recovery_point(intent)
    if point is None or not point.healthy or not now - KEEP_FOR <= point.taken_at <= now:
        return "no healthy recovery point within the retention window"
    rows = list(receipts.values())
    if any(row.committed_at is None or row.committed_at > point.taken_at or row.asset_id not in point.assets for row in rows):
        return "the recovery point does not cover every committed asset"
    try:
        reader = retention.reader(intent)
        fresh = [reader.get_asset(str(row.asset_id)) for row in rows]
    except immich.ImmichFailed:
        return "Immich did not confirm every committed asset"
    if any(asset.sha1 != row.source_sha1 for asset, row in zip(fresh, rows, strict=True)):
        return "Immich did not confirm every committed asset"
    return None


def hold_pinned(opened: Session, review: ReviewItem) -> bool:
    decision = opened.get(DedupeDecision, review.decision_id)
    export = None if decision is None else opened.get(Export, decision.export_id)
    return export is not None and held_back(opened, export)
