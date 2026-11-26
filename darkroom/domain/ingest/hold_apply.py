from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import ColumnElement, exists, or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, backfill, events, hold_resolutions, holds, managed_ingest, managed_manifest, managed_metadata, managed_upload, media, same_shot, xmp
from darkroom.domain.ingest import hold_readback, run, unpack
from darkroom.providers import exiftool, immich
from darkroom.storage import db, paths
from darkroom.storage.models import (DedupeDecision, Export, ManagedCompound, ManagedCompoundMember, ManagedHoldResolution, ManagedImportIntent, ManagedImportReceipt,
                                     MediaItem, Person, ReviewItem, TakeoutEntry)
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
    if backfill.takeout_held(opened, person, item):
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


def send(opened: Session, reader: immich.ManagedReader, source: Source, receipt: ManagedImportReceipt, intent: ManagedImportIntent, now: float,
         on_commit: Callable[[], None]) -> bool:
    if source.entry is not None and same_shot.gate(opened, reader, intent, receipt.id, source.entry, source.filename, now) != "clear":
        return False

    def original() -> immich.UploadSource:
        source.handle.seek(0)
        return immich.UploadSource(source.handle, receipt.expected_bytes, receipt.source_sha1)

    report = managed_metadata.complete_receipt(opened, reader, managed_metadata.Resource(receipt.id, original, source.filename), now, time.sleep)
    if report.outcome == "verified":
        report = managed_metadata.commit_compound(opened, reader, intent.id, receipt.compound_key, now, on_commit=on_commit)
    if report.outcome == "pending":
        managed_ingest.gave_up(opened, managed_upload.fresh(opened, receipt.id), now)
    return report.outcome == "verified"


def revisit(opened: Session, receipt: ManagedImportReceipt, intent: ManagedImportIntent, person: Person, hold: str | None, now: float) -> None:
    item_id = holds.number_after(receipt.resource_key, backfill.KEY)
    if intent.kind == backfill.KIND and item_id is not None:
        backfill.revisit_resolution(opened, person, intent, receipt, item_id, hold, now)


def resolved(opened: Session, receipt: ManagedImportReceipt, intent: ManagedImportIntent, person: Person, now: float) -> None:
    revisit(opened, receipt, intent, person, None, now)
    if intent.kind != "import" or intent.export_id is None:
        return
    review_id = holds.number_after(receipt.resource_key, managed_ingest.KEPT)
    if review_id is not None:
        alerts.close(opened, f"managed:kept:{review_id}", now)
    model = ManagedImportReceipt
    resolution = ManagedHoldResolution
    unfinished = exists().where(resolution.receipt_id == model.id, resolution.state == "applied", resolution.result.in_(("cleared", "rechecked")))
    terminal = select(model.id).where(model.intent_id == intent.id, model.state != "committed",
                                      or_(model.metadata_hold.in_(managed_ingest.TERMINAL), unfinished)).limit(1)
    compound = select(ManagedCompound.id).where(ManagedCompound.intent_id == intent.id, ManagedCompound.hold.is_not(None)).limit(1)
    if not opened.execute(terminal).first() and not opened.execute(compound).first():
        alerts.close(opened, f"managed:held:{intent.export_id}", now)


def owner_binding(receipt: ManagedImportReceipt, intent: ManagedImportIntent, person: Person) -> ColumnElement[bool]:
    member = ManagedCompoundMember
    single = ~exists().where(member.intent_id == intent.id, member.resource_key == receipt.resource_key)
    bound = (select(ManagedImportIntent.id).join(Person, Person.id == ManagedImportIntent.person_id)
             .where(ManagedImportIntent.id == intent.id, ManagedImportIntent.person_id == person.id, ManagedImportIntent.owner_id == intent.owner_id,
                    ManagedImportIntent.resource_manifest == intent.resource_manifest, ManagedImportIntent.kind == intent.kind,
                    ManagedImportIntent.export_id == intent.export_id, Person.immich_user_id == intent.owner_id,
                    Person.immich_api_key_sealed == person.immich_api_key_sealed, single))
    if intent.kind != "import":
        bound = bound.where(Person.managed_backfill == backfill.ON)
    return bound.exists()


def post_upload(opened: Session, settings: config.Settings, row: ManagedHoldResolution, receipt: ManagedImportReceipt, intent: ManagedImportIntent,
                person: Person, now: float, tools: run.Tools) -> bool:
    kind = holds.source_of(receipt.resource_key)
    manifest = managed_manifest.resource_of(intent.resource_manifest)
    if ((kind == "backfill" and (intent.kind != backfill.KIND or manifest != "media_item"))
            or (kind in ("import", "kept") and (intent.kind != "import" or manifest != "takeout_entry" or intent.export_id is None)) or kind is None):
        return False
    intended = managed_metadata.intended_of(receipt.expected_metadata)
    if intended is None or intended.role != "single" or receipt.resource_key != receipt.compound_key:
        return False
    bound = owner_binding(receipt, intent, person)
    allowed = opened.scalar(select(bound))
    opened.commit()
    if not allowed:
        return False
    reader = managed_ingest.reader_of(settings, person, intent, tools.immich)
    if row.state == "recorded":
        result = hold_readback.inspect(reader, receipt, intended, row.action, time.sleep)
        if result is None or not hold_readback.apply(opened, row, receipt, result, bound, now):
            return False
        if result.hold is not None:
            revisit(opened, receipt, intent, person, result.hold, now)
            opened.commit()
            return True
        opened.commit()
    report = managed_metadata.commit_compound(opened, reader, intent.id, receipt.compound_key, now,
                                              current=lambda: bool(opened.scalar(select(bound))),
                                              on_commit=lambda: resolved(opened, receipt, intent, person, now))
    if report.outcome == "unusable":
        db.write_lock(opened)
        if opened.scalar(select(bound)):
            current = opened.get(ManagedHoldResolution, row.id, populate_existing=True)
            if current is not None and current.state == "superseded" and current.result in ("cleared", "rechecked"):
                current.result = "still_unusable"
                events.record(opened, now, hold_resolutions.APPLIED_EVENT, f"receipt {receipt.id}", current.result)
            revisit(opened, receipt, intent, person, managed_ingest.UNUSABLE, now)
        opened.commit()
    return report.outcome in ("verified", "unusable")


def apply_one(opened: Session, settings: config.Settings, photos: Photos, row: ManagedHoldResolution, receipt: ManagedImportReceipt, now: float,
              tools: run.Tools, import_source: ImportSource) -> None:
    intent = opened.get(ManagedImportIntent, receipt.intent_id, populate_existing=True)
    person = None if intent is None else opened.get(Person, intent.person_id, populate_existing=True)
    if intent is None or person is None or person.immich_user_id != intent.owner_id:
        return
    if row.action in hold_resolutions.POST_UPLOAD:
        if not post_upload(opened, settings, row, receipt, intent, person, now, tools):
            alert_waiting(opened, settings, row, receipt, now, tools, "pending", "Darkroom could not verify the Immich copy for a hold resolution. It tries again.")
        return
    with ExitStack() as stack:
        source = source_of(opened, photos, stack, receipt, intent, person, import_source)
        tags = tags_of(source, receipt, tools.read)
        if not hold_resolutions.apply(opened, row, receipt, tags, xmp.GoogleMetadata.of(source.sidecar), now):
            return
        reader = managed_ingest.reader_of(settings, person, intent, tools.immich)
        if not send(opened, reader, source, receipt, intent, now, lambda: resolved(opened, receipt, intent, person, now)):
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
            message = ("Darkroom could not verify the Immich copy for a hold resolution. It tries again." if row.action in hold_resolutions.POST_UPLOAD else
                       "Darkroom could not complete the upload from a hold resolution. It keeps the copy and tries again.")
            alert_waiting(opened, settings, row, receipt, now, tools, "pending", message)
