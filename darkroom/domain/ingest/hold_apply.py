from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, backfill, hold_resolutions, holds, managed_ingest, managed_manifest, managed_metadata, managed_upload, media, same_shot, xmp
from darkroom.domain.ingest import run, unpack
from darkroom.providers import exiftool, immich
from darkroom.storage import db, paths
from darkroom.storage.models import DedupeDecision, Export, ManagedHoldResolution, ManagedImportIntent, ManagedImportReceipt, MediaItem, Person, ReviewItem, TakeoutEntry
from darkroom.storage.paths import Photos

SOURCE_ERRORS = (OSError, paths.OutsideRoot, unpack.UnpackFailed, exiftool.Missing, exiftool.Unreadable)


class UnreadableSource(Exception):
    pass


@dataclass(frozen=True)
class Source:
    handle: BinaryIO = field(repr=False)
    filename: str = field(repr=False)
    sidecar: str | None = field(repr=False)
    entry: TakeoutEntry | None = None


ImportSource = Callable[[ExitStack, ManagedImportReceipt, Export, Person], Source]


def kept_source(opened: Session, photos: Photos, stack: ExitStack, receipt: ManagedImportReceipt, export: Export) -> Source:
    review_id = holds.number_after(receipt.resource_key, managed_ingest.KEPT)
    review = None if review_id is None else opened.get(ReviewItem, review_id, populate_existing=True)
    decision = None if review is None else opened.get(DedupeDecision, review.decision_id, populate_existing=True)
    if (review is None or decision is None or decision.export_id != export.id or decision.reversed_at is not None or review.status != "kept_both"
            or review.hold_path is None or review.hold_sha1 != receipt.source_sha1 or review.sidecar_from_still):
        raise UnreadableSource
    handle = stack.enter_context(paths.open_inbox_file(photos, photos.root / review.hold_path))
    return Source(handle, Path(decision.source_path or review.hold_path).name, review.sidecar_json)


def backfill_source(opened: Session, photos: Photos, stack: ExitStack, receipt: ManagedImportReceipt, intent: ManagedImportIntent, person: Person) -> Source:
    item_id = holds.number_after(receipt.resource_key, backfill.KEY)
    item = None if item_id is None else opened.get(MediaItem, item_id, populate_existing=True)
    if (person.managed_backfill != backfill.ON or item is None or item.source != "takeout" or item.person_id != intent.person_id
            or item.export_id != intent.export_id or (item.sha1, item.bytes) != (receipt.source_sha1, receipt.expected_bytes) or item.sidecar_from_still):
        raise UnreadableSource
    if item.export_id is not None:
        export = opened.get(Export, item.export_id, populate_existing=True)
        if export is None or export.status != backfill.IMPORTED:
            raise UnreadableSource
    halves, near = backfill.takeout_holds(opened, person, [item])
    if item.id in halves or item.id in near:
        raise UnreadableSource
    handle = stack.enter_context(paths.open_library_file(photos, person.slug, item.path))
    return Source(handle, Path(item.path).name, item.sidecar_json)


def source_of(opened: Session, photos: Photos, stack: ExitStack, receipt: ManagedImportReceipt, intent: ManagedImportIntent, person: Person,
              import_source: ImportSource) -> Source:
    kind = holds.source_of(receipt.resource_key)
    if kind == "backfill" and intent.kind == backfill.KIND and managed_manifest.resource_of(intent.resource_manifest) == "media_item":
        return backfill_source(opened, photos, stack, receipt, intent, person)
    export = None if intent.export_id is None else opened.get(Export, intent.export_id, populate_existing=True)
    if export is None or intent.kind != "import" or managed_manifest.resource_of(intent.resource_manifest) != "takeout_entry":
        raise UnreadableSource
    if kind == "kept":
        return kept_source(opened, photos, stack, receipt, export)
    if kind == "import":
        return import_source(stack, receipt, export, person)
    raise UnreadableSource


def tags_of(source: Source, receipt: ManagedImportReceipt, read: xmp.Reader) -> dict[str, object]:
    pinned = paths.OPEN_FILES / str(source.handle.fileno())
    if media.sha1_of(pinned) != (receipt.source_sha1, receipt.expected_bytes) or not managed_ingest.nameable(source.filename):
        raise UnreadableSource
    tags = read([pinned]).get(pinned)
    if tags is None or "Error" in tags:
        raise UnreadableSource
    return tags


def send(opened: Session, reader: immich.ManagedReader, source: Source, receipt: ManagedImportReceipt, intent: ManagedImportIntent, now: float) -> bool:
    if source.entry is not None and same_shot.gate(opened, reader, intent, receipt.id, source.entry, source.filename, now) != "clear":
        return False

    def original() -> immich.UploadSource:
        source.handle.seek(0)
        return immich.UploadSource(source.handle, receipt.expected_bytes, receipt.source_sha1)

    report = managed_metadata.complete_receipt(opened, reader, managed_metadata.Resource(receipt.id, original, source.filename), now, time.sleep)
    if report.outcome == "verified":
        report = managed_metadata.commit_compound(opened, reader, intent.id, receipt.compound_key, now)
    if report.outcome == "pending":
        managed_ingest.gave_up(opened, managed_upload.fresh(opened, receipt.id), now)
    return report.outcome == "verified"


def apply_one(opened: Session, settings: config.Settings, photos: Photos, row: ManagedHoldResolution, receipt: ManagedImportReceipt, now: float,
              tools: run.Tools, import_source: ImportSource) -> None:
    intent = opened.get(ManagedImportIntent, receipt.intent_id, populate_existing=True)
    person = None if intent is None else opened.get(Person, intent.person_id, populate_existing=True)
    if intent is None or person is None or person.immich_user_id != intent.owner_id:
        return
    with ExitStack() as stack:
        source = source_of(opened, photos, stack, receipt, intent, person, import_source)
        tags = tags_of(source, receipt, tools.read)
        if not hold_resolutions.apply(opened, row, receipt, tags, xmp.GoogleMetadata.of(source.sidecar), now):
            return
        reader = managed_ingest.reader_of(settings, person, intent, tools.immich)
        if not send(opened, reader, source, receipt, intent, now):
            alert_waiting(opened, settings, row, receipt, now, tools, "pending",
                          "Darkroom could not complete the upload from a hold resolution. It keeps the copy and tries again.")


def alert_waiting(opened: Session, settings: config.Settings, row: ManagedHoldResolution, receipt: ManagedImportReceipt, now: float, tools: run.Tools,
                  reason: str, message: str) -> None:
    if now - row.decided_at < hold_resolutions.DAY_SECONDS:
        return
    db.write_lock(opened)
    current = opened.get(ManagedHoldResolution, row.id, populate_existing=True)
    if current is not None and current.state in hold_resolutions.OPEN:
        intent = opened.get(ManagedImportIntent, receipt.intent_id, populate_existing=True)
        about = alerts.NOBODY if intent is None else alerts.person(intent.person_id)
        managed_ingest.once(opened, settings, f"managed:resolution-{reason}:{row.id}", f"receipt {receipt.id}", message, now, tools.notifier, about)
    opened.commit()


def apply(opened: Session, settings: config.Settings, photos: Photos, now: float, tools: run.Tools, import_source: ImportSource) -> None:
    for resolution_id in hold_resolutions.due(opened):
        found = hold_resolutions.pending(opened, resolution_id)
        if found is None:
            continue
        row, receipt = found
        try:
            apply_one(opened, settings, photos, row, receipt, now, tools, import_source)
        except (UnreadableSource, *SOURCE_ERRORS):
            opened.rollback()
            monitoring.log("photo copy for a hold resolution could not be read", receipt=receipt.id)
            alert_waiting(opened, settings, row, receipt, now, tools, "source",
                          "Darkroom could not read the photo's copy to apply the hold resolution. It keeps the copy and tries again.")
        except (immich.ImmichFailed, run.Busy) as error:
            opened.rollback()
            monitoring.log("hold resolution not sent to Immich", receipt=receipt.id, error=type(error).__name__)
            alert_waiting(opened, settings, row, receipt, now, tools, "pending",
                          "Darkroom could not complete the upload from a hold resolution. It keeps the copy and tries again.")
