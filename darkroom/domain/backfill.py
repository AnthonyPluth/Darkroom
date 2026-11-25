from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import (alerts, backfill_live, dedupe, events, immich_reconcile, managed_compound, managed_ingest, managed_manifest, managed_metadata,
                             managed_upload, media, pixel_folder, pixel_source, xmp)
from darkroom.domain.ingest import pairing
from darkroom.domain.managed_upload import Report
from darkroom.providers import exiftool, immich
from darkroom.storage import db, paths
from darkroom.storage.models import (DedupeDecision, Export, ImmichAsset, ManagedAssetOrigin, ManagedAssetVersion, ManagedBackfillItem, ManagedImportIntent,
                                     ManagedImportReceipt, ManagedPixelSource, MediaItem, Person, PixelCopy, TakeoutEntry, known_contents)
from darkroom.storage.paths import Photos

ON: Final = "on"
KIND: Final = "backfill"
MANIFEST: Final = managed_manifest.MEDIA_ITEM
KEY: Final = "backfill:media-item:"
SOURCE: Final = "media-item:"
IMMICH_FOLDER: Final = "immich"
NEAR_TIERS: Final = (2, 3, 4)
RETRIED: Final = ("pending", "mismatch", "export_unfinished")
REVISIT: Final = (*RETRIED, "live_photo")
CHUNK: Final = 1024 * 1024
LIVE_SECONDS: Final = 1.0
MOVED: Final = "moved"
IMPORTED: Final = "imported"
ENDED: Final = ("failed", "expired")
STUCK_SECONDS: Final = 24 * 3600.0
REASONS: Final[dict[str, str]] = {
    "missing": "the file is not a plain file in the person's folder any more",
    "changed": "the file's bytes are not the ones Darkroom recorded",
    "unreadable": "the file's metadata could not be read",
    "name_refused": "Immich does not take a file with this name",
    "live_photo": "a Live Photo whose companion cannot be established from its recorded Takeout paths",
    "live_partial": "one half of the Live Photo is missing or is already represented by another photo",
    "live_ambiguous": "the Live Photo's Takeout paths do not identify just one still and one video",
    "embedded_motion": "the still already contains motion video",
    "compound_conflict": "Immich already holds conflicting parts of the Live Photo",
    "same_shot": "the same shot as a phone original with other bytes, or an edited copy, left for the same-shot step (#554)",
    "export_unfinished": ("its Takeout export failed or expired before it was imported, so its companions may be unknown; retry a failed export's import "
                          "from the Exports page and it is backfilled once that import finishes; an expired export cannot finish, so its photos stay listed"),
    "unusable": "Immich's asset is trashed, offline, in an external library or another person's",
    "pending": "Immich could not be reached or has not finished yet; Darkroom tries again",
    "not_in_immich": "the phone original is not recorded as stored in Immich",
    "no_source_account": "uploaded, but its Google account is not known, so it never goes to a Pixel",
    "pixel_refused": "mapped, but its Pixel copies could not be moved to the Immich original",
    **{hold: text for hold, text in managed_metadata.REASONS.items() if hold != "not_extracted"},
    managed_ingest.NOT_SETTLED: "Immich's metadata did not match for a day",
}


@dataclass
class Summary:
    mapped: int = 0
    queued: int = 0
    exceptions: Counter[str] = field(default_factory=Counter)

    def add(self, other: Summary) -> None:
        self.mapped, self.queued = self.mapped + other.mapped, self.queued + other.queued
        self.exceptions.update(other.exceptions)


@dataclass(frozen=True)
class Tools:
    read: xmp.Reader = exiftool.read
    pause: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Verified:
    asset: immich.Asset
    snapshot: str
    receipt: ManagedImportReceipt | None = None


def due(opened: Session) -> list[Person]:
    people = opened.execute(select(Person).where(Person.managed_backfill == ON).order_by(Person.id)).scalars().all()
    return [person for person in people if immich.is_canonical_id(person.immich_user_id)]


def copied(opened: Session, item_id: int) -> bool:
    mapped = select(ManagedBackfillItem.id).where(ManagedBackfillItem.item_id == item_id, ManagedBackfillItem.outcome == "mapped").limit(1)
    may_be_sent = or_(ManagedImportReceipt.state != "prepared", ManagedImportReceipt.metadata_hold.is_(None),
                      ManagedImportReceipt.metadata_hold.not_in(managed_metadata.HELD_BEFORE_UPLOAD))
    receipt = (select(ManagedImportReceipt.id).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
               .where(ManagedImportIntent.kind == KIND, ManagedImportReceipt.resource_key.in_((f"{KEY}{item_id}", f"{SOURCE}{item_id}")), may_be_sent).limit(1))
    return opened.execute(mapped).first() is not None or opened.execute(receipt).first() is not None


def exceptions(opened: Session, person_id: int) -> dict[str, list[int]]:
    found: dict[str, list[int]] = defaultdict(list)
    rows = opened.execute(select(ManagedBackfillItem.reason, ManagedBackfillItem.item_id).where(ManagedBackfillItem.person_id == person_id,
                                                                                               ManagedBackfillItem.reason.is_not(None))
                          .order_by(ManagedBackfillItem.item_id)).all()
    for reason, item_id in rows:
        found[str(reason)].append(item_id)
    return dict(found)


def note(opened: Session, person: Person, item_id: int, reason: str | None, now: float, version_id: int | None = None, receipt_id: int | None = None) -> bool:
    opened.commit()
    db.write_lock(opened)
    item = opened.get(MediaItem, item_id, populate_existing=True)
    if item is None or item.person_id != person.id:
        opened.commit()
        return False
    record(opened, person, item_id, reason, now, version_id, receipt_id)
    opened.commit()
    return True


def record(opened: Session, person: Person, item_id: int, reason: str | None, now: float, version_id: int | None, receipt_id: int | None) -> None:
    row = opened.execute(select(ManagedBackfillItem).where(ManagedBackfillItem.item_id == item_id)).scalar_one_or_none()
    outcome = "mapped" if version_id is not None else "exception"
    if row is None:
        row = ManagedBackfillItem(person_id=person.id, item_id=item_id, outcome=outcome, checked_at=now, since=now)
        opened.add(row)
    elif (row.outcome, row.reason) != (outcome, reason):
        row.since = now
    row.person_id, row.outcome, row.reason, row.version_id, row.receipt_id, row.checked_at = person.id, outcome, reason, version_id, receipt_id, now


def version_of(opened: Session, person: Person, asset_id: str, sha1: str, size: int, kind: str, video_id: str | None, snapshot: str, now: float) -> ManagedAssetVersion:
    owner = str(person.immich_user_id)
    db.write_lock(opened)
    version = opened.execute(select(ManagedAssetVersion).where(ManagedAssetVersion.person_id == person.id, ManagedAssetVersion.owner_id == owner,
                                                               ManagedAssetVersion.asset_id == asset_id, ManagedAssetVersion.sha1 == sha1)
                             .execution_options(populate_existing=True)).scalar_one_or_none()
    if version is None:
        version = ManagedAssetVersion(person_id=person.id, owner_id=owner, asset_id=asset_id, sha1=sha1, bytes=size, kind=kind, state=pixel_source.VERIFIED,
                                      linked_video_asset_id=video_id, metadata_snapshot=snapshot, seen_at=now, bytes_verified_at=now)
        opened.add(version)
    elif version.state != pixel_source.VERIFIED or version.library_id is not None or version.bytes != size:
        if version.bytes is not None and version.bytes != size:
            raise immich.NotCustody("the recorded Immich version holds another size")
        version.bytes, version.state, version.library_id, version.seen_at, version.bytes_verified_at = size, pixel_source.VERIFIED, None, now, now
    if video_id is not None and version.linked_video_asset_id != video_id:
        if version.linked_video_asset_id is not None:
            raise immich.NotCustody("the recorded Immich version is linked to another video")
        version.linked_video_asset_id, version.metadata_snapshot = video_id, snapshot
    opened.flush()
    return version


def origin(opened: Session, version: ManagedAssetVersion, kind: str, item: MediaItem, receipt: ManagedImportReceipt | None, now: float) -> None:
    key = f"{SOURCE}{item.id}"
    found = opened.execute(select(ManagedAssetOrigin.id).where(ManagedAssetOrigin.version_id == version.id, ManagedAssetOrigin.kind == kind,
                                                               ManagedAssetOrigin.source_key == key)).first()
    if found is not None:
        return
    linked = {} if receipt is None else {"intent_id": receipt.intent_id, "receipt_id": receipt.id, "receipt_state": receipt.state}
    opened.add(ManagedAssetOrigin(version_id=version.id, person_id=version.person_id, owner_id=version.owner_id, asset_id=version.asset_id, sha1=version.sha1,
                                  kind=kind, source_key=key, evidence_snapshot=json.dumps({"item": item.id, "backfill": True}, sort_keys=True),
                                  recorded_at=now, **linked))
    opened.flush()


def pixels(opened: Session, settings: config.Settings, item: MediaItem, version_id: int, now: float, report: Summary) -> str | None:
    if not settings.pixel_immich_source or opened.execute(select(PixelCopy.id).where(PixelCopy.item_id == item.id).limit(1)).first() is None:
        return None
    try:
        report.queued += pixel_folder.queue_managed(opened, item, version_id, now, existing_only=True)
    except pixel_source.NotManaged:
        opened.rollback()
        return "pixel_refused"
    opened.commit()
    return None


class Moved(RuntimeError):
    pass


def contents(item: MediaItem) -> tuple[str, str, int, str, int | None]:
    return item.path, item.sha1, item.bytes, item.source, item.export_id


class Backfill:
    def __init__(self, opened: Session, settings: config.Settings, photos: Photos, person: Person, reader: immich.ManagedReader, now: float, tools: Tools) -> None:
        self.opened, self.settings, self.photos, self.person, self.reader, self.now, self.tools = opened, settings, photos, person, reader, now, tools
        self.report = Summary()
        self.handled: set[int] = set()

    def mapped(self, item: MediaItem, version: ManagedAssetVersion, reason: str | None, receipt_id: int | None, queue: bool) -> None:
        version_id = version.id
        self.opened.commit()
        refused = pixels(self.opened, self.settings, item, version_id, self.now, self.report) if queue else None
        if not note(self.opened, self.person, item.id, reason or refused, self.now, version_id, receipt_id):
            return
        self.report.mapped += 1
        if reason or refused:
            self.report.exceptions[str(reason or refused)] += 1

    def exception(self, item: MediaItem, reason: str) -> None:
        if note(self.opened, self.person, item.id, reason, self.now):
            self.report.exceptions[reason] += 1

    def current(self, ids: tuple[int, int], saved: list[tuple[str, str, int, str, int | None]]) -> bool:
        person = self.opened.get(Person, self.person.id, populate_existing=True)
        if person is None or person.managed_backfill != ON or person.immich_user_id != self.reader.owner_id:
            return False
        for item_id, before in zip(ids, saved, strict=True):
            current = self.opened.get(MediaItem, item_id, populate_existing=True)
            if current is None or current.person_id != person.id or contents(current) != before:
                return False
            export = None if current.export_id is None else self.opened.get(Export, current.export_id, populate_existing=True)
            if export is not None and export.status != IMPORTED:
                return False
        return True

    def verify_pair(self, items: tuple[MediaItem, MediaItem], ids: tuple[str, str],
                    receipts: tuple[ManagedImportReceipt | None, ManagedImportReceipt | None]) -> tuple[Verified, Verified]:
        still, video = (self.reader.get_asset(asset_id) for asset_id in ids)
        if still.kind != "IMAGE" or video.kind != "VIDEO" or still.id == video.id or still.live_video_id != video.id or video.visibility != "hidden":
            raise immich.NotCustody("Immich's assets are not one linked Live Photo")
        verified = []
        for item, asset, receipt in zip(items, (still, video), receipts, strict=True):
            if asset.sha1 != item.sha1 or (receipt is not None and (receipt.verified_sha1, receipt.verified_bytes) != (item.sha1, item.bytes)):
                raise immich.ChecksumMismatch("the Live Photo's verified original differs from the file Darkroom recorded")
            self.reader.verify_original(asset.id, item.sha1, item.bytes)
            snapshot = str(receipt.metadata_evidence) if receipt is not None else json.dumps({"origin": "phone", "live_video_id": asset.live_video_id},
                                                                                            sort_keys=True)
            verified.append(Verified(asset, snapshot, receipt))
        return verified[0], verified[1]

    def mapped_pair(self, items: tuple[MediaItem, MediaItem], found: tuple[Verified, Verified], kind: str, wanted: Callable[[], bool]) -> None:
        db.write_lock(self.opened)
        if not wanted():
            self.opened.rollback()
            raise Moved("the Live Photo changed while the backfill ran")
        mapped = []
        for item, member in zip(items, found, strict=True):
            asset, receipt = member.asset, member.receipt
            version = version_of(self.opened, self.person, asset.id, item.sha1, item.bytes, asset.kind, asset.live_video_id, member.snapshot, self.now)
            origin(self.opened, version, kind, item, receipt, self.now)
            receipt_id = None if receipt is None else receipt.id
            record(self.opened, self.person, item.id, None, self.now, version.id, receipt_id)
            mapped.append((version.id, receipt_id))
        self.opened.commit()
        self.report.mapped += 2
        for item, (version_id, receipt_id) in zip(items, mapped, strict=True):
            refused = pixels(self.opened, self.settings, item, version_id, self.now, self.report)
            if refused and note(self.opened, self.person, item.id, refused, self.now, version_id, receipt_id):
                self.report.exceptions[refused] += 1

    def pair_exception(self, items: tuple[MediaItem, MediaItem], reason: str) -> None:
        self.opened.commit()
        db.write_lock(self.opened)
        counted = 0
        for item_id in [item.id for item in items]:
            current = self.opened.get(MediaItem, item_id, populate_existing=True)
            mapped = self.opened.scalar(select(ManagedBackfillItem.id).where(ManagedBackfillItem.item_id == item_id,
                                                                             ManagedBackfillItem.outcome == "mapped"))
            if current is not None and current.person_id == self.person.id and mapped is None:
                record(self.opened, self.person, item_id, reason, self.now, None, None)
                counted += 1
        self.opened.commit()
        if counted:
            self.report.exceptions[reason] += counted

    def stored_phone(self, item_id: int) -> list[ImmichAsset]:
        return list(self.opened.scalars(select(ImmichAsset).where(ImmichAsset.person_id == self.person.id,
                                                                ImmichAsset.original_item_id == item_id)
                                        .order_by(ImmichAsset.id).execution_options(populate_existing=True)))

    def phone_pair(self, item: MediaItem, first: ImmichAsset) -> None:
        query = select(ImmichAsset).where(ImmichAsset.person_id == self.person.id, ImmichAsset.asset_id == first.linked_asset_id)
        linked = self.opened.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
        if linked is None or linked.original_item_id is None or linked.original_item_id == item.id:
            self.exception(item, "live_partial")
            return
        other = self.opened.get(MediaItem, linked.original_item_id, populate_existing=True)
        if other is None or other.person_id != self.person.id or other.source != "phone" or not other.path.startswith(f"{self.person.slug}/originals/{IMMICH_FOLDER}/"):
            self.exception(item, "live_partial")
            return
        items, records = ((other, item), (linked, first)) if first.kind == "live_video" else ((item, other), (first, linked))
        still, video = records
        ids = items[0].id, items[1].id
        self.handled.update(ids)
        asset_ids = still.asset_id, video.asset_id
        saved = [contents(half) for half in items]

        def wanted() -> bool:
            if not self.current(ids, saved):
                return False
            live = [self.stored_phone(half) for half in ids]
            if any(len(rows) != 1 for rows in live):
                return False
            still_row, video_row = live[0][0], live[1][0]
            return (still_row.kind == "photo" and video_row.kind == "live_video" and still_row.status == video_row.status == "stored"
                    and (still_row.asset_id, video_row.asset_id) == asset_ids and (video_row.linked_asset_id, still_row.linked_asset_id) == asset_ids
                    and all(expected[2] > 0 and expected[1] == row.sha1 for expected, row in zip(saved, (still_row, video_row), strict=True)))

        if not wanted():
            self.pair_exception(items, "live_partial")
            return
        self.finish_pair(items, asset_ids, (None, None), "phone", wanted)

    def finish_pair(self, items: tuple[MediaItem, MediaItem], ids: tuple[str, str],
                    receipts: tuple[ManagedImportReceipt | None, ManagedImportReceipt | None], kind: str, wanted: Callable[[], bool]) -> None:
        try:
            found = self.verify_pair(items, ids, receipts)
            self.mapped_pair(items, found, kind, wanted)
        except Moved:
            return
        except (immich.NotCustody, immich.NotVisible):
            self.opened.rollback()
            self.pair_exception(items, "unusable")
        except immich.ChecksumMismatch:
            self.pair_exception(items, "changed")
        except immich.ImmichFailed:
            self.pair_exception(items, "pending")

    def phone(self, item: MediaItem) -> None:
        assets = self.stored_phone(item.id)
        if len(assets) == 1 and assets[0].linked_asset_id is not None:
            self.phone_pair(item, assets[0])
            return
        reason = self.phone_problem(item, assets)
        if reason is not None:
            self.exception(item, reason)

    def phone_problem(self, item: MediaItem, assets: Sequence[ImmichAsset]) -> str | None:
        stored = [asset for asset in assets if asset.status == "stored"]
        if any(asset.kind == "live_video" or asset.linked_asset_id is not None for asset in assets):
            return "live_photo"
        if len(stored) != 1 or item.bytes <= 0:
            return "not_in_immich"
        try:
            asset = self.reader.get_asset(stored[0].asset_id)
            if asset.sha1 != item.sha1:
                return "changed"
            self.reader.verify_original(asset.id, item.sha1, item.bytes)
            version = version_of(self.opened, self.person, asset.id, item.sha1, item.bytes, asset.kind, None, json.dumps({"origin": "phone"}), self.now)
        except (immich.NotCustody, immich.NotVisible):
            self.opened.rollback()
            return "unusable"
        except immich.ChecksumMismatch:
            return "changed"
        except immich.ImmichFailed:
            return "pending"
        origin(self.opened, version, "phone", item, None, self.now)
        self.mapped(item, version, None, None, True)
        return None

    def intent(self, export: Export | None) -> ManagedImportIntent:
        intent = ManagedImportIntent
        query = select(intent).where(intent.person_id == self.person.id, intent.owner_id == str(self.person.immich_user_id), intent.kind == KIND,
                                     intent.export_id.is_(None) if export is None else intent.export_id == export.id).order_by(intent.id).limit(1)
        found = self.opened.execute(query).scalar_one_or_none()
        if found is None:
            db.write_lock(self.opened)
            found = self.opened.execute(query).scalar_one_or_none()
            if found is None:
                found = intent(person_id=self.person.id, export_id=None if export is None else export.id, owner_id=str(self.person.immich_user_id),
                               kind=KIND, resource_manifest=MANIFEST, created_at=self.now)
                self.opened.add(found)
                self.opened.flush()
            self.opened.commit()
        return found

    def receipt(self, intent: ManagedImportIntent, item: MediaItem, handle: BinaryIO) -> int | None:
        query = receipt_query(intent, item)
        found = self.opened.execute(query).scalar_one_or_none()
        if found is not None:
            return found
        pinned = paths.OPEN_FILES / str(handle.fileno())
        tags = self.tools.read([pinned]).get(pinned)
        if tags is None or "Error" in tags:
            return None
        kind: managed_metadata.Kind = "VIDEO" if PurePosixPath(item.path).suffix.lower() in media.VIDEO_SUFFIXES else "IMAGE"
        google = xmp.GoogleMetadata.of(item.sidecar_json)
        intended = managed_metadata.intend(tags, google, kind, corrects=not item.sidecar_from_still)
        db.write_lock(self.opened)
        found = self.opened.execute(query).scalar_one_or_none()
        current = self.opened.get(MediaItem, item.id, populate_existing=True)
        export = None if current is None or current.export_id is None else self.opened.get(Export, current.export_id, populate_existing=True)
        if found is None and (current is None or (current.person_id, current.path, current.sha1, current.bytes) != (self.person.id, item.path, item.sha1, item.bytes)
                              or (export is not None and export.status != IMPORTED)):
            self.opened.commit()
            raise Moved("the photo changed while the backfill ran")
        if found is None:
            key = f"{KEY}{item.id}"
            row = ManagedImportReceipt(intent_id=intent.id, resource_key=key, compound_key=key, source_sha1=item.sha1, expected_bytes=item.bytes,
                                       expected_metadata=managed_metadata.snapshot(intended),
                                       hold_facts=managed_metadata.held_facts(tags, google, intended), updated_at=self.now)
            self.opened.add(row)
            self.opened.flush()
            found = row.id
        self.opened.commit()
        return found

    def takeout(self, item: MediaItem) -> None:
        reason = self.takeout_problem(item)
        if reason is not None and reason != MOVED:
            self.exception(item, reason)

    def pair_input(self, item: MediaItem, role: managed_compound.MemberRole, handle: BinaryIO,
                   still_sidecar: str | None = None) -> managed_compound.Planned | str:
        pinned = paths.OPEN_FILES / str(handle.fileno())
        tags = self.tools.read([pinned]).get(pinned)
        if tags is None or "Error" in tags:
            return "unreadable"
        if role == "live_still" and managed_compound.embedded_motion(tags):
            return "embedded_motion"
        kind: managed_metadata.Kind = "IMAGE" if role == "live_still" else "VIDEO"
        borrowed = item.sidecar_from_still or (item.sidecar_json is None and still_sidecar is not None)
        sidecar = item.sidecar_json if item.sidecar_json is not None else still_sidecar
        intended = managed_metadata.intend(tags, xmp.GoogleMetadata.of(sidecar), kind, role, corrects=not borrowed)
        return managed_compound.Planned(f"{SOURCE}{item.id}", role, item.sha1, item.bytes, intended)

    def drive_pair(self, intent: ManagedImportIntent, pair: backfill_live.Pair, items: tuple[MediaItem, MediaItem],
                   frozen: managed_compound.Frozen, handles: list[BinaryIO], names: list[str]) -> str | None:
        if frozen.hold is not None:
            return frozen.hold
        still_id, video_id = (frozen.receipts.get(f"{SOURCE}{item.id}") for item in items)
        if still_id is None or video_id is None:
            return "live_partial"
        still, video = (managed_upload.fresh(self.opened, receipt_id) for receipt_id in (still_id, video_id))
        for item, receipt in zip(items, (still, video), strict=True):
            if (receipt.source_sha1, receipt.expected_bytes) != (item.sha1, item.bytes):
                return "changed"
        if still.state == video.state == "committed":
            return None
        try:
            decision, linked = managed_upload.preflight_matches(self.reader, still, video)
            if decision == "conflict":
                managed_compound.set_hold(self.opened, intent.id, pair.key, managed_compound.CONFLICT)
                return managed_compound.CONFLICT
            if linked is not None:
                for row, asset_id in zip((still, video), linked, strict=True):
                    managed_upload.move(self.opened, row, ("prepared",), self.now, state="returned", asset_id=asset_id, upload_outcome="duplicate")
        except immich.ImmichFailed:
            self.opened.rollback()
            return "pending"

        def resource(row: ManagedImportReceipt, handle: BinaryIO, name: str) -> managed_metadata.Resource:
            def original() -> immich.UploadSource:
                handle.seek(0)
                return immich.UploadSource(handle, row.expected_bytes, row.source_sha1)
            return managed_metadata.Resource(row.id, original, name)

        report = managed_metadata.complete_live_photo(self.opened, self.reader, resource(video, handles[1], names[1]),
                                                      resource(still, handles[0], names[0]), self.now, self.tools.pause)
        if report.outcome == "unusable":
            managed_compound.set_hold(self.opened, intent.id, pair.key, managed_compound.UNUSABLE)
            return "unusable"
        for receipt_id in (still_id, video_id):
            row = managed_upload.fresh(self.opened, receipt_id)
            if row.metadata_hold in managed_ingest.TERMINAL:
                return row.metadata_hold
            if managed_ingest.gave_up(self.opened, row, self.now):
                return managed_ingest.NOT_SETTLED
        return None if report.outcome == "verified" else "pending"

    def takeout_pair_problem(self, intent: ManagedImportIntent, pair: backfill_live.Pair, items: tuple[MediaItem, MediaItem],
                             wanted: Callable[[], bool]) -> str | None:
        names = [PurePosixPath(item.path).name for item in items]
        if not all(managed_ingest.nameable(name) for name in names):
            return "name_refused"
        if managed_manifest.resource_of(intent.resource_manifest) != "media_item":
            return "pending"
        previous = managed_compound.settled(self.opened, intent.id, pair.key)
        if previous is not None and previous.hold is not None:
            return previous.hold
        if previous is not None and previous.committed_at is not None:
            return None
        with contextlib.ExitStack() as stack:
            try:
                handles = [stack.enter_context(paths.open_library_file(self.photos, self.person.slug, item.path)) for item in items]
                if any(item.bytes <= 0 or os.fstat(handle.fileno()).st_size != item.bytes or sha1_of(handle) != item.sha1
                       for item, handle in zip(items, handles, strict=True)):
                    return "changed"
                members = [] if previous is not None else [self.pair_input(items[0], "live_still", handles[0]),
                                                          self.pair_input(items[1], "live_video", handles[1], items[0].sidecar_json)]
            except (ValueError, FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return "missing"
            except (exiftool.Missing, exiftool.Unreadable, OSError):
                return "pending"
            plans = [member for member in members if not isinstance(member, str)]
            if len(plans) != len(members):
                return next(member for member in members if isinstance(member, str))
            try:
                if previous is None:
                    frozen = managed_compound.freeze(self.opened, intent.id, pair.key, plans, self.now, wanted)
                else:
                    db.write_lock(self.opened)
                    if not wanted():
                        self.opened.rollback()
                        return MOVED
                    frozen = managed_compound.frozen(self.opened, previous)
                    self.opened.commit()
            except managed_compound.Refused:
                return MOVED
            return self.drive_pair(intent, pair, items, frozen, handles, names)

    def takeout_pair(self, pair: backfill_live.Pair, items: tuple[MediaItem, MediaItem], frozen: bool) -> None:
        self.handled.update(pair.items)
        saved = [contents(item) for item in items]
        export = self.opened.get(Export, pair.export, populate_existing=True)
        if export is None or export.status != IMPORTED:
            self.pair_exception(items, "export_unfinished")
            return
        if not frozen and any(copied(self.opened, item.id) for item in items):
            self.pair_exception(items, "compound_conflict")
            return
        intent = self.intent(export)

        def current() -> bool:
            found = self.opened.get(Export, pair.export, populate_existing=True)
            return found is not None and found.status == IMPORTED and self.current(pair.items, saved)

        def wanted() -> bool:
            if not current():
                return False
            if frozen:
                return True
            if any(copied(self.opened, item.id) for item in items):
                return False
            check = backfill_live.recorded(self.opened, self.person.id, pair.items, pair.items, pair.export)
            return check.pairs.get(pair.still) == pair

        reason = self.takeout_pair_problem(intent, pair, items, wanted)
        if reason is not None:
            if reason != MOVED:
                self.pair_exception(items, reason)
            return
        rows = {row.resource_key: row for row in managed_metadata.members(self.opened, intent.id, pair.key)}
        still, video = (rows.get(f"{SOURCE}{half}") for half in pair.items)
        if still is None or video is None or still.state != "committed" or video.state != "committed" or still.asset_id is None or video.asset_id is None:
            self.pair_exception(items, "pending")
            return
        self.finish_pair(items, (still.asset_id, video.asset_id), (still, video), "takeout", current)

    def sent(self, item: MediaItem, intent: ManagedImportIntent, name: str) -> tuple[str | int, Report | None]:
        known = self.opened.execute(receipt_query(intent, item)).scalar_one_or_none()
        if known is not None and managed_upload.fresh(self.opened, known).state == "committed":
            self.opened.commit()
            return known, Report("verified", "the receipt is committed")
        with contextlib.ExitStack() as handles:
            try:
                handle = handles.enter_context(paths.open_library_file(self.photos, self.person.slug, item.path))
                same = item.bytes > 0 and os.fstat(handle.fileno()).st_size == item.bytes and sha1_of(handle) == item.sha1
            except (ValueError, FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return "missing", None
            except OSError:
                return "pending", None
            if not same:
                return "changed", None
            try:
                receipt_id = self.receipt(intent, item, handle)
            except Moved:
                return MOVED, None
            except (exiftool.Missing, exiftool.Unreadable, OSError):
                return "pending", None
            if receipt_id is None:
                return "unreadable", None

            def original() -> immich.UploadSource:
                handle.seek(0)
                return immich.UploadSource(handle, item.bytes, item.sha1)
            return receipt_id, managed_metadata.complete_receipt(self.opened, self.reader, managed_metadata.Resource(receipt_id, original, name), self.now,
                                                                 self.tools.pause)

    def takeout_problem(self, item: MediaItem) -> str | None:
        name = PurePosixPath(item.path).name
        if not managed_ingest.nameable(name):
            return "name_refused"
        export = None if item.export_id is None else self.opened.get(Export, item.export_id)
        intent = self.intent(export)
        receipt_id, outcome = self.sent(item, intent, name)
        if isinstance(receipt_id, str) or outcome is None:
            return str(receipt_id)
        if outcome.outcome == "verified":
            outcome = managed_metadata.commit_compound(self.opened, self.reader, intent.id, f"{KEY}{item.id}", self.now)
        row = managed_upload.fresh(self.opened, receipt_id)
        if row.state != "committed":
            managed_ingest.gave_up(self.opened, row, self.now)
            self.opened.commit()
            row = managed_upload.fresh(self.opened, receipt_id)
            if row.metadata_hold is not None:
                return row.metadata_hold
            return "unusable" if outcome.outcome == "unusable" else "pending"
        intended = managed_metadata.intended_of(row.expected_metadata)
        try:
            version = version_of(self.opened, self.person, str(row.asset_id), row.source_sha1, row.expected_bytes,
                                 "IMAGE" if intended is None else intended.kind, None, str(row.metadata_evidence), self.now)
        except immich.NotCustody:
            self.opened.rollback()
            return "unusable"
        origin(self.opened, version, "takeout" if export is not None else "other", item, row, self.now)
        self.mapped(item, version, None if export is not None else "no_source_account", row.id, export is not None)
        return None

    def convert_deferred(self) -> None:
        if not self.settings.pixel_immich_source:
            return
        unconverted = exists(select(PixelCopy.id).where(PixelCopy.item_id == ManagedBackfillItem.item_id,
                                                        ~exists(select(ManagedPixelSource.id).where(ManagedPixelSource.copy_id == PixelCopy.id))))
        rows = self.opened.execute(select(ManagedBackfillItem).where(ManagedBackfillItem.person_id == self.person.id, ManagedBackfillItem.outcome == "mapped",
                                                                    ManagedBackfillItem.version_id.is_not(None), unconverted,
                                                                    or_(ManagedBackfillItem.reason.is_(None), ManagedBackfillItem.reason == "pixel_refused"))
                                   .order_by(ManagedBackfillItem.item_id)).scalars().all()
        for row in rows:
            item_id, version_id, receipt_id, before = row.item_id, row.version_id, row.receipt_id, row.reason
            item = self.opened.get(MediaItem, item_id)
            if item is None or version_id is None:
                continue
            refused = pixels(self.opened, self.settings, item, version_id, self.now, self.report)
            written = refused == before or note(self.opened, self.person, item_id, refused, self.now, version_id, receipt_id)
            if refused and written:
                self.report.exceptions[refused] += 1

    def run(self, limit: int) -> Summary:
        self.convert_deferred()
        row = ManagedBackfillItem
        mine = and_(row.item_id == MediaItem.id, row.person_id == self.person.id)
        unsettled = ~exists(select(row.id).where(mine, or_(row.reason.is_(None), row.reason.not_in(REVISIT))))
        retried = exists(select(row.id).where(mine, row.reason.in_(REVISIT)))
        checked = func.coalesce(select(row.checked_at).where(mine).scalar_subquery(), 0.0)
        finished = ~exists(select(Export.id).where(Export.id == MediaItem.export_id, Export.status.not_in((IMPORTED, *ENDED))))
        google_only = and_(MediaItem.source == "takeout", MediaItem.path.startswith(f"{self.person.slug}/google-only/"))
        phones = and_(MediaItem.source == "phone", MediaItem.path.startswith(f"{self.person.slug}/originals/{IMMICH_FOLDER}/"))
        waiting = self.opened.execute(select(MediaItem).where(MediaItem.person_id == self.person.id, known_contents(), or_(phones, google_only), unsettled,
                                                              finished).order_by(retried, checked, MediaItem.id).limit(limit)).scalars().all()
        pending = [item for item in waiting if item.source == "takeout"]
        exports = {item.export_id for item in pending if item.export_id is not None}
        status = dict(self.opened.execute(select(Export.id, Export.status).where(Export.id.in_(exports))).tuples().all()) if exports else {}
        takeouts = self.opened.execute(select(MediaItem).where(MediaItem.person_id == self.person.id, known_contents(), google_only)
                                       .order_by(MediaItem.id)).scalars().all() if pending else []
        halves = live_halves(self.opened, self.person, takeouts, pending) if pending else set()
        sources = {item.id: item for item in takeouts}
        pending_ids = {item.id for item in pending}
        frozen = backfill_live.frozen(self.opened, self.person.id, self.reader.owner_id, pending_ids) if pending else backfill_live.Groups({}, {})
        recorded = backfill_live.recorded(self.opened, self.person.id, halves - frozen.holds.keys(), sources.keys()) if halves else backfill_live.Groups({}, {})
        pairs = recorded.pairs | frozen.pairs
        paired_ids = {other for pair in pairs.values() for other in pair.items}
        near = same_shots(self.opened, self.person, [item for item in takeouts if item.id in paired_ids or item.id in pending_ids])
        for item in waiting:
            if item.id in self.handled:
                continue
            seen = None if item.export_id is None else status.get(item.export_id)
            if item.source == "phone":
                self.phone(item)
            elif seen in ENDED:
                self.exception(item, "export_unfinished")
            elif item.export_id is not None and seen != IMPORTED:
                continue
            elif (pair := pairs.get(item.id)) is not None:
                still, video = (sources.get(half) for half in pair.items)
                if still is None or video is None:
                    self.exception(item, "live_partial")
                elif item.id not in frozen.pairs and near.intersection(pair.items):
                    self.handled.update(pair.items)
                    self.pair_exception((still, video), "same_shot")
                else:
                    self.takeout_pair(pair, (still, video), item.id in frozen.pairs)
            elif item.id in halves or item.id in frozen.holds:
                self.exception(item, recorded.holds.get(item.id, "live_photo"))
            elif item.id in near:
                self.exception(item, "same_shot")
            else:
                self.takeout(item)
        return self.report


def receipt_query(intent: ManagedImportIntent, item: MediaItem) -> Select[int]:
    return select(ManagedImportReceipt.id).where(ManagedImportReceipt.intent_id == intent.id, ManagedImportReceipt.resource_key == f"{KEY}{item.id}",
                                                 ManagedImportReceipt.source_sha1 == item.sha1)


def sha1_of(handle: BinaryIO) -> str:
    digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    while chunk := handle.read(CHUNK):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def is_video(path: PurePosixPath) -> bool:
    return path.suffix.lower() in media.VIDEO_SUFFIXES


def is_still(path: PurePosixPath) -> bool:
    return path.suffix.lower() in pairing.STILL_SUFFIXES


def keys(path: PurePosixPath) -> tuple[str, set[str]] | None:
    if is_still(path):
        return "still", {path.stem.lower()}
    if is_video(path):
        return "video", {path.stem.lower()} | ({path.name.lower()} if path.suffix.lower() == pairing.MOTION_CLIP_SUFFIX else set())
    return None


OTHER: Final = {"still": "video", "video": "still"}


class Companions:
    def __init__(self) -> None:
        self.named: set[tuple[str, object, str, str]] = set()
        self.timed: dict[tuple[str, object, int], list[float]] = defaultdict(list)
        self.lookups = 0

    def add(self, scope: object, path: PurePosixPath, captured: float | None) -> None:
        found = keys(path)
        if found is None:
            return
        kind, names = found
        self.named.update((kind, scope, str(path.parent), name) for name in names)
        if captured is not None and math.isfinite(captured):
            self.timed[(kind, scope, math.floor(captured))].append(captured)

    def named_companion(self, scope: object, path: PurePosixPath) -> bool:
        found = keys(path)
        if found is None:
            return False
        kind, names = found
        self.lookups += len(names)
        return any((OTHER[kind], scope, str(path.parent), name) in self.named for name in names)

    def timed_companion(self, scope: object, path: PurePosixPath, captured: float | None) -> bool:
        found = keys(path)
        if found is None or captured is None or not math.isfinite(captured):
            return False
        self.lookups += 3
        second = math.floor(captured)
        return any(abs(other - captured) <= LIVE_SECONDS for nearby in (second - 1, second, second + 1)
                   for other in self.timed.get((OTHER[found[0]], scope, nearby), ()))


def live_halves(opened: Session, person: Person, items: Sequence[MediaItem], candidates: Sequence[MediaItem], index: Companions | None = None) -> set[int]:
    wanted = {item.id for item in candidates}
    found = index or Companions()
    for item in items:
        path = PurePosixPath(item.path)
        found.add(("library", str(path.parent)), path, item.captured_utc)
    own = opened.execute(select(DedupeDecision.takeout_item_id, DedupeDecision.export_id, DedupeDecision.source_path)
                         .join(MediaItem, MediaItem.id == DedupeDecision.takeout_item_id)
                         .where(MediaItem.person_id == person.id, MediaItem.source == "takeout", DedupeDecision.source_path.is_not(None))).all()
    sources: dict[int, list[tuple[int, PurePosixPath]]] = defaultdict(list)
    for item_id, export_id, source in own:
        if item_id in wanted:
            sources[item_id].append((export_id, PurePosixPath(str(source))))
    exports = {export_id for listed in sources.values() for export_id, _ in listed} | {item.export_id for item in candidates if item.export_id is not None}
    if exports:
        for export_id, source in opened.execute(select(DedupeDecision.export_id, DedupeDecision.source_path)
                                                .where(DedupeDecision.export_id.in_(exports), DedupeDecision.source_path.is_not(None))).all():
            found.add(export_id, PurePosixPath(str(source)), None)
        ledger = select(TakeoutEntry.export_id, TakeoutEntry.name, TakeoutEntry.captured_utc, TakeoutEntry.item_id)
        for export_id, name, captured, item_id in opened.execute(ledger.where(TakeoutEntry.export_id.in_(exports), TakeoutEntry.kind == "media")):
            path = PurePosixPath(name)
            found.add(export_id, path, None)
            found.add(("ledger", export_id), path, captured)
            if item_id is not None and item_id in wanted:
                sources[item_id].append((export_id, path))
    halves: set[int] = set()
    for item in candidates:
        path = PurePosixPath(item.path)
        if keys(path) is None:
            continue
        scopes = {export_id for export_id, _ in sources[item.id]} | ({item.export_id} if item.export_id is not None else set())
        library = ("library", str(path.parent))
        if (found.named_companion(library, path) or found.timed_companion(library, path, item.captured_utc)
                or any(found.named_companion(export_id, source) for export_id, source in sources[item.id])
                or any(found.timed_companion(("ledger", export_id), path, item.captured_utc) for export_id in scopes)
                or (is_video(path) and item.sidecar_from_still)):
            halves.add(item.id)
    return halves


def candidate(opened: Session, item: MediaItem) -> dedupe.TakeoutCandidate:
    shot = dedupe.shot_of(item)
    sources = opened.execute(select(DedupeDecision.source_path).where(DedupeDecision.takeout_item_id == item.id)).scalars().all()
    edited = any(source is None or pairing.original_name(Path(source)) is not None for source in sources)
    return dedupe.TakeoutCandidate(source_path=item.path, sha1=item.sha1, phash=item.phash, captured_utc=item.captured_utc, captured_local=item.captured_local,
                                   width=item.width, height=item.height, duration_ms=item.duration_ms, make=item.make, model=item.model,
                                   is_video=shot.is_video, is_raw=shot.is_raw, is_edited=edited)


def edited_copies(opened: Session, items: list[MediaItem], candidates: list[dedupe.TakeoutCandidate]) -> set[int]:
    ids = {item.id for item in items}
    by_ledger: set[int] = {item_id for item_id in opened.execute(select(TakeoutEntry.item_id).where(TakeoutEntry.item_id.in_(ids), or_(TakeoutEntry.is_edited.is_(True),
                                                                                                       TakeoutEntry.edited_of.is_not(None)))).scalars() if item_id is not None}
    return {item.id for item, found in zip(items, candidates, strict=True) if found.is_edited or pairing.original_name(Path(item.path)) is not None} | by_ledger


def same_shots(opened: Session, person: Person, items: list[MediaItem]) -> set[int]:
    if not items:
        return set()
    candidates = [candidate(opened, item) for item in items]
    edited = edited_copies(opened, items, candidates)
    phone = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone", known_contents())
                           .order_by(MediaItem.id)).scalars().all()
    if not phone:
        return edited
    index = dedupe.PersonIndex(0, person.id, frozenset({person.id}), tuple(dedupe.shot_of(found) for found in phone))
    by_path = {item.path: item.id for item in items}
    decided = dedupe.decide_all(index, candidates, dedupe.DedupeSettings())
    return edited | {by_path[found.source_path] for found in decided
                     if found.tier in NEAR_TIERS and found.matched_source != "takeout" and found.source_path in by_path}


def alert_stuck(opened: Session, settings: config.Settings, person: Person, now: float, notifier: alerts.Notifier | None) -> None:
    stuck = opened.execute(select(ManagedBackfillItem.since).where(ManagedBackfillItem.person_id == person.id, ManagedBackfillItem.reason.in_(RETRIED),
                                                                   ManagedBackfillItem.since <= now - STUCK_SECONDS)).scalars().all()
    if not stuck:
        return
    db.write_lock(opened)
    managed_ingest.once(opened, settings, f"managed:backfill:{person.id}:stuck:{int(min(stuck))}", person.slug,
                        f"{alerts.counted(len(stuck), 'photo')} of {person.slug} could not be backfilled to Immich for more than a day. "
                        "Darkroom keeps every file as it is and tries again. Open Darkroom for details.", now, notifier, alerts.person(person.id))
    opened.commit()


def alert(opened: Session, settings: config.Settings, person: Person, report: Summary, now: float, notifier: alerts.Notifier | None) -> None:
    alert_stuck(opened, settings, person, now, notifier)
    lasting = sorted(reason for reason in report.exceptions if reason not in RETRIED)
    if not lasting:
        return
    total = sum(count for reason, count in report.exceptions.items() if reason not in RETRIED)
    listed = "; ".join(f"{report.exceptions[reason]}: {REASONS.get(reason, reason)}" for reason in lasting)
    db.write_lock(opened)
    managed_ingest.once(opened, settings, f"managed:backfill:{person.id}:{'+'.join(lasting)}", person.slug,
                        f"{alerts.counted(total, 'photo')} of {person.slug} need a look before Immich holds them ({listed}). "
                        "Darkroom keeps every file as it is. Open Darkroom for details.", now, notifier, alerts.person(person.id))
    opened.commit()


def run_person(opened: Session, settings: config.Settings, photos: Photos, person: Person, now: float, client: immich.Client | None = None,
               tools: Tools | None = None, limit: int = 500, notifier: alerts.Notifier | None = None) -> Summary:
    if person.managed_backfill != ON or not immich.is_canonical_id(person.immich_user_id):
        return Summary()
    key = immich_reconcile.person_key(settings, person)
    reader = immich.ManagedReader(client or immich.Client.at(settings), key, str(person.immich_user_id), access="import")
    report = Backfill(opened, settings, photos, person, reader, now, tools or Tools()).run(limit)
    events.record(opened, now, "managed_backfill_run", person.slug, f"mapped {report.mapped}, exceptions {sum(report.exceptions.values())}")
    opened.commit()
    alert(opened, settings, person, report, now, notifier)
    monitoring.log("Immich backfill ran", person=person.id, mapped=report.mapped, queued=report.queued, exceptions=sum(report.exceptions.values()))
    return report


def run_all(opened: Session, settings: config.Settings, photos: Photos, now: float, client: immich.Client | None = None, tools: Tools | None = None) -> Summary:
    total = Summary()
    for person in due(opened):
        try:
            total.add(run_person(opened, settings, photos, person, now, client, tools))
        except (immich.ImmichFailed, immich_reconcile.NotReady, OSError) as error:
            opened.rollback()
            monitoring.log("Immich backfill postponed", person=person.id, error=type(error).__name__)
    return total
