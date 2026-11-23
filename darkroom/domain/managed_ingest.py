from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, immich_reconcile, managed_metadata, managed_upload, media, xmp
from darkroom.domain.ingest import place
from darkroom.domain.managed_upload import Report
from darkroom.providers import immich
from darkroom.storage import db, paths
from darkroom.storage.models import Alert, DedupeDecision, Export, ExportPart, ManagedImportIntent, ManagedImportReceipt, Person, ReviewItem, TakeoutEntry
from darkroom.storage.paths import Photos

ON: Final = "on"
STARTED_PARTS: Final = ("ingesting", "ingested", "failed")
MANIFEST: Final = json.dumps({"schema": 1, "resource": "takeout_entry", "compound": "single"}, sort_keys=True)
SETTLE_SECONDS: Final = 24 * 3600.0
NOT_SETTLED: Final = "not_settled"
TERMINAL: Final = (*managed_metadata.HELD_BEFORE_UPLOAD, "not_enriched", NOT_SETTLED)
KEPT: Final = "review:"
KEPT_ONLY: Final = "cleanup of an Immich-managed import stays off until the owner records the restore rehearsal (#266)"


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    root: TakeoutEntry
    name: str = field(repr=False)
    sidecar: str | None = field(repr=False)
    live: bool
    borrowed: bool


def intent_of(opened: Session, export: Export) -> ManagedImportIntent | None:
    return opened.execute(select(ManagedImportIntent).where(ManagedImportIntent.export_id == export.id).order_by(ManagedImportIntent.id).limit(1)).scalar_one_or_none()


def started(opened: Session, export: Export) -> bool:
    part = select(ExportPart.id).where(ExportPart.export_id == export.id, ExportPart.ingest_state.in_(STARTED_PARTS)).limit(1)
    decided = select(TakeoutEntry.id).where(TakeoutEntry.export_id == export.id, TakeoutEntry.decision_id.is_not(None)).limit(1)
    return export.stage is not None or opened.execute(part).first() is not None or opened.execute(decided).first() is not None


def begin(opened: Session, export: Export, person: Person, now: float) -> ManagedImportIntent | None:
    intent = intent_of(opened, export)
    if intent is not None or person.managed_imports != ON or not immich.is_canonical_id(person.immich_user_id) or started(opened, export):
        return intent
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

    def send(self, item: Item, source: Path, tags: dict[str, object] | None) -> None:
        receipt_id = self.receipt(item, tags)
        if receipt_id is None:
            self.held += 1
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
        elif report.outcome in ("held", "unusable") or row.metadata_hold in TERMINAL or gave_up(self.opened, row, self.now):
            self.held += 1
        else:
            self.pending += 1


def keep_for_immich(opened: Session, photos: Photos, export: Export, review: ReviewItem, hold: Path, sha1: str, name: str, live: bool, now: float,
                    read: xmp.Reader) -> None:
    intent = intent_of(opened, export)
    if intent is None:
        raise Refused("only a pair of an Immich-managed import is kept in Immich")
    if f"{KEPT}{review.id}" in receipts_of(opened, intent):
        return
    if live or Path(review.hold_path or "").suffix.lower() in media.VIDEO_SUFFIXES or review.sidecar_from_still:
        raise Refused("a Live Photo's halves are not uploaded to Immich yet, so the pair stays in review and its copy is kept")
    if not nameable(name):
        raise Refused("Immich does not take a file with this name, so the pair stays in review and its copy is kept")
    with paths.pinned_inbox_files(photos, [hold]) as pinned:
        tags = read([pinned[hold]]).get(pinned[hold]) if hold in pinned else None
        size = pinned[hold].stat().st_size if hold in pinned else 0
    if tags is None or "Error" in tags or size <= 0:
        raise Refused("the held copy could not be read, so it was not kept")
    kind: managed_metadata.Kind = "VIDEO" if hold.suffix.lower() in media.VIDEO_SUFFIXES else "IMAGE"
    intended = managed_metadata.intend(tags, xmp.GoogleMetadata.of(review.sidecar_json), kind, corrects=not review.sidecar_from_still)
    if intended.hold is not None:
        raise Refused(f"the photo cannot go to Immich as it is ({managed_metadata.REASONS[intended.hold]}), so the pair stays in review and its copy is kept")
    opened.add(ManagedImportReceipt(intent_id=intent.id, resource_key=f"{KEPT}{review.id}", compound_key=f"{KEPT}{review.id}", source_sha1=sha1,
                                    expected_bytes=size, expected_metadata=managed_metadata.snapshot(intended), updated_at=now))
    opened.flush()


def send_one(opened: Session, settings: config.Settings, photos: Photos, receipt_id: int, now: float, client: immich.Client | None) -> bool:
    row = managed_upload.fresh(opened, receipt_id)
    intent = opened.get(ManagedImportIntent, row.intent_id)
    person = None if intent is None else opened.get(Person, intent.person_id)
    review = opened.get(ReviewItem, int(row.resource_key.removeprefix(KEPT)))
    decision = None if review is None else opened.get(DedupeDecision, review.decision_id)
    if intent is None or person is None or review is None or review.hold_path is None or decision is None:
        return False
    hold, name = photos.root / review.hold_path, Path(decision.source_path or review.hold_path).name
    reader = reader_of(settings, person, intent, client)
    with contextlib.ExitStack() as handles:
        def original() -> immich.UploadSource:
            return immich.UploadSource(handles.enter_context(paths.open_inbox_file(photos, hold)), row.expected_bytes, row.source_sha1)
        report = managed_metadata.complete_receipt(opened, reader, managed_metadata.Resource(row.id, original, name), now, time.sleep)
    if report.outcome == "verified":
        report = managed_metadata.commit_compound(opened, reader, intent.id, row.compound_key, now)
    row = managed_upload.fresh(opened, row.id)
    if report.outcome in ("held", "unusable") or row.metadata_hold in TERMINAL or gave_up(opened, row, now):
        kept_alert(opened, settings, person, review.id, report.reason, now)
    elif row.state != "committed":
        overdue(opened, settings, row.id, report.reason, now)
    return row.state == "committed"


def overdue(opened: Session, settings: config.Settings, receipt_id: int, reason: str, now: float) -> None:
    row = managed_upload.fresh(opened, receipt_id)
    intent = opened.get(ManagedImportIntent, row.intent_id)
    person = None if intent is None else opened.get(Person, intent.person_id)
    review = opened.get(ReviewItem, int(row.resource_key.removeprefix(KEPT)))
    if person is not None and review is not None and review.resolved_at is not None and now - review.resolved_at >= SETTLE_SECONDS:
        kept_alert(opened, settings, person, review.id, reason, now)


def kept_alert(opened: Session, settings: config.Settings, person: Person, review_id: int, reason: str, now: float) -> None:
    db.write_lock(opened)
    once(opened, settings, f"managed:kept:{review_id}", person.slug, f"A photo kept from review {review_id} is not committed to Immich ({reason}); "
         "its copy is kept in the inbox.", now, None, alerts.person(person.id))
    opened.commit()


def send_kept(opened: Session, settings: config.Settings, photos: Photos, now: float, client: immich.Client | None) -> int:
    query = (select(ManagedImportReceipt.id).where(ManagedImportReceipt.resource_key.startswith(KEPT), ManagedImportReceipt.state != "committed",
                                                     or_(ManagedImportReceipt.metadata_hold.is_(None), ManagedImportReceipt.metadata_hold.not_in(TERMINAL))))
    sent = 0
    for receipt_id in opened.execute(query.order_by(ManagedImportReceipt.id)).scalars().all():
        try:
            sent += send_one(opened, settings, photos, receipt_id, now, client)
        except (immich.ImmichFailed, paths.OutsideRoot, OSError):
            opened.rollback()
            overdue(opened, settings, receipt_id, "its copy or the person's Immich key could not be used", now)
    if sent:
        monitoring.log("kept photos sent to Immich", committed=sent)
    return sent


def once(opened: Session, settings: config.Settings, key: str, subject: str, message: str, now: float, notifier: alerts.Notifier | None, about: alerts.About) -> None:
    if opened.execute(select(Alert.id).where(Alert.key == key).limit(1)).first() is None:
        alerts.open_alert(opened, settings, key, "failure", subject, alerts.Wording("Darkroom needs a look", message), None, now, notifier=notifier, about=about)


def gave_up(opened: Session, row: ManagedImportReceipt, now: float) -> bool:
    if row.metadata_hold != "mismatch" or row.bytes_verified_at is None or now - row.bytes_verified_at < SETTLE_SECONDS:
        return False
    managed_upload.move(opened, row, ("bytes_verified",), now, metadata_hold=NOT_SETTLED)
    return True


def alert(opened: Session, settings: config.Settings, export: Export, held: int, pending: int, now: float, notifier: alerts.Notifier | None) -> None:
    subject, about = f"export {export.id}", alerts.account(export.account_id)
    db.write_lock(opened)
    if held:
        once(opened, settings, f"managed:held:{export.id}", subject, f"{alerts.counted(held, 'item')} of export {export.id} were not uploaded to Immich "
             "or not committed there, and are kept in the inbox. Open Darkroom for details.", now, notifier, about)
    if pending:
        once(opened, settings, f"managed:pending:{export.id}", subject, f"{alerts.counted(pending, 'upload')} of export {export.id} to Immich could not be "
             "completed yet. Darkroom keeps the files and tries again.", now, notifier, about)
    opened.commit()


def hold_pinned(opened: Session, review: ReviewItem) -> bool:
    decision = opened.get(DedupeDecision, review.decision_id)
    export = None if decision is None else opened.get(Export, decision.export_id)
    return export is not None and place.managed_import(opened, export)
