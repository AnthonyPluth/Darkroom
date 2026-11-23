from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, dedupe, events, immich_reconcile, managed_ingest, managed_metadata, managed_upload, media, pixel_folder, pixel_source, xmp
from darkroom.domain.ingest import pairing
from darkroom.domain.managed_upload import Report
from darkroom.providers import exiftool, immich
from darkroom.storage import db, paths
from darkroom.storage.models import (DedupeDecision, Export, ImmichAsset, ManagedAssetOrigin, ManagedAssetVersion, ManagedBackfillItem, ManagedImportIntent,
                                     ManagedImportReceipt, MediaItem, Person, PixelCopy, known_contents)
from darkroom.storage.paths import Photos

ON: Final = "on"
KIND: Final = "backfill"
MANIFEST: Final = json.dumps({"schema": 1, "resource": "media_item", "compound": "single"}, sort_keys=True)
KEY: Final = "backfill:media-item:"
SOURCE: Final = "media-item:"
IMMICH_FOLDER: Final = "immich"
NEAR_TIERS: Final = (2, 3, 4)
RETRIED: Final = ("pending", "mismatch")
CHUNK: Final = 1024 * 1024
LIVE_SECONDS: Final = 1.0
STUCK_SECONDS: Final = 24 * 3600.0
REASONS: Final[dict[str, str]] = {
    "missing": "the file is not a plain file in the person's folder any more",
    "changed": "the file's bytes are not the ones Darkroom recorded",
    "unreadable": "the file's metadata could not be read",
    "name_refused": "Immich does not take a file with this name",
    "live_photo": "a Live Photo half, left for the Live Photo step (#553)",
    "same_shot": "the same shot as a phone original with other bytes, left for the same-shot step (#554)",
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


def due(opened: Session) -> list[Person]:
    people = opened.execute(select(Person).where(Person.managed_backfill == ON).order_by(Person.id)).scalars().all()
    return [person for person in people if immich.is_canonical_id(person.immich_user_id)]


def copied(opened: Session, item_id: int) -> bool:
    mapped = select(ManagedBackfillItem.id).where(ManagedBackfillItem.item_id == item_id, ManagedBackfillItem.outcome == "mapped").limit(1)
    may_be_sent = or_(ManagedImportReceipt.state != "prepared", ManagedImportReceipt.metadata_hold.is_(None),
                      ManagedImportReceipt.metadata_hold.not_in(managed_metadata.HELD_BEFORE_UPLOAD))
    receipt = (select(ManagedImportReceipt.id).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id)
               .where(ManagedImportIntent.kind == KIND, ManagedImportReceipt.resource_key == f"{KEY}{item_id}", may_be_sent).limit(1))
    return opened.execute(mapped).first() is not None or opened.execute(receipt).first() is not None


def exceptions(opened: Session, person_id: int) -> dict[str, list[int]]:
    found: dict[str, list[int]] = defaultdict(list)
    rows = opened.execute(select(ManagedBackfillItem.reason, ManagedBackfillItem.item_id).where(ManagedBackfillItem.person_id == person_id,
                                                                                               ManagedBackfillItem.reason.is_not(None))
                          .order_by(ManagedBackfillItem.item_id)).all()
    for reason, item_id in rows:
        found[str(reason)].append(item_id)
    return dict(found)


def settled(opened: Session, person: Person) -> tuple[set[int], set[int]]:
    rows = opened.execute(select(ManagedBackfillItem.item_id, ManagedBackfillItem.reason).where(ManagedBackfillItem.person_id == person.id)).all()
    return {item_id for item_id, reason in rows if reason not in RETRIED}, {item_id for item_id, reason in rows if reason in RETRIED}


def note(opened: Session, person: Person, item_id: int, reason: str | None, now: float, version_id: int | None = None, receipt_id: int | None = None) -> None:
    opened.commit()
    db.write_lock(opened)
    row = opened.execute(select(ManagedBackfillItem).where(ManagedBackfillItem.item_id == item_id)).scalar_one_or_none()
    outcome = "mapped" if version_id is not None else "exception"
    if row is None:
        row = ManagedBackfillItem(person_id=person.id, item_id=item_id, outcome=outcome, checked_at=now, since=now)
        opened.add(row)
    elif (row.outcome, row.reason) != (outcome, reason):
        row.since = now
    row.person_id, row.outcome, row.reason, row.version_id, row.receipt_id, row.checked_at = person.id, outcome, reason, version_id, receipt_id, now
    opened.commit()


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


class Backfill:
    def __init__(self, opened: Session, settings: config.Settings, photos: Photos, person: Person, reader: immich.ManagedReader, now: float, tools: Tools) -> None:
        self.opened, self.settings, self.photos, self.person, self.reader, self.now, self.tools = opened, settings, photos, person, reader, now, tools
        self.report = Summary()

    def mapped(self, item: MediaItem, version: ManagedAssetVersion, reason: str | None, receipt_id: int | None, queue: bool) -> None:
        version_id = version.id
        self.opened.commit()
        refused = pixels(self.opened, self.settings, item, version_id, self.now, self.report) if queue else None
        note(self.opened, self.person, item.id, reason or refused, self.now, version_id, receipt_id)
        self.report.mapped += 1
        if reason or refused:
            self.report.exceptions[str(reason or refused)] += 1

    def exception(self, item: MediaItem, reason: str) -> None:
        note(self.opened, self.person, item.id, reason, self.now)
        self.report.exceptions[reason] += 1

    def phone(self, item: MediaItem) -> None:
        reason = self.phone_problem(item)
        if reason is not None:
            self.exception(item, reason)

    def phone_problem(self, item: MediaItem) -> str | None:
        assets = self.opened.execute(select(ImmichAsset).where(ImmichAsset.person_id == self.person.id, ImmichAsset.original_item_id == item.id)
                                     .order_by(ImmichAsset.id)).scalars().all()
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
        intended = managed_metadata.intend(tags, xmp.GoogleMetadata.of(item.sidecar_json), kind, corrects=not item.sidecar_from_still)
        db.write_lock(self.opened)
        found = self.opened.execute(query).scalar_one_or_none()
        current = self.opened.get(MediaItem, item.id, populate_existing=True)
        if found is None and (current is None or (current.person_id, current.path, current.sha1, current.bytes) != (self.person.id, item.path, item.sha1, item.bytes)):
            self.opened.commit()
            return None
        if found is None:
            key = f"{KEY}{item.id}"
            row = ManagedImportReceipt(intent_id=intent.id, resource_key=key, compound_key=key, source_sha1=item.sha1, expected_bytes=item.bytes,
                                       expected_metadata=managed_metadata.snapshot(intended), updated_at=self.now)
            self.opened.add(row)
            self.opened.flush()
            found = row.id
        self.opened.commit()
        return found

    def takeout(self, item: MediaItem) -> None:
        reason = self.takeout_problem(item)
        if reason is not None:
            self.exception(item, reason)

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
        rows = self.opened.execute(select(ManagedBackfillItem).where(ManagedBackfillItem.person_id == self.person.id, ManagedBackfillItem.outcome == "mapped",
                                                                    ManagedBackfillItem.version_id.is_not(None),
                                                                    or_(ManagedBackfillItem.reason.is_(None), ManagedBackfillItem.reason == "pixel_refused"))
                                   .order_by(ManagedBackfillItem.item_id)).scalars().all()
        for row in rows:
            item_id, version_id, receipt_id, before = row.item_id, row.version_id, row.receipt_id, row.reason
            copies = self.opened.execute(select(PixelCopy.id).where(PixelCopy.item_id == item_id)).scalars().all()
            item = self.opened.get(MediaItem, item_id)
            if item is None or version_id is None or all(pixel_source.converted(self.opened, copy_id) for copy_id in copies):
                continue
            refused = pixels(self.opened, self.settings, item, version_id, self.now, self.report)
            if refused != before:
                note(self.opened, self.person, item_id, refused, self.now, version_id, receipt_id)
            if refused:
                self.report.exceptions[refused] += 1

    def run(self, limit: int) -> Summary:
        self.convert_deferred()
        done, retried = settled(self.opened, self.person)
        phones = self.opened.execute(select(MediaItem).where(MediaItem.person_id == self.person.id, MediaItem.source == "phone", known_contents(),
                                                             MediaItem.path.startswith(f"{self.person.slug}/originals/{IMMICH_FOLDER}/"))
                                     .order_by(MediaItem.id)).scalars().all()
        takeouts = self.opened.execute(select(MediaItem).where(MediaItem.person_id == self.person.id, MediaItem.source == "takeout", known_contents(),
                                                               MediaItem.path.startswith(f"{self.person.slug}/google-only/"))
                                       .order_by(MediaItem.id)).scalars().all()
        waiting = sorted((item for item in [*phones, *takeouts] if item.id not in done), key=lambda item: (item.id in retried, item.id))[:limit]
        halves, near = live_halves(self.opened, self.person, takeouts), same_shots(self.opened, self.person, [item for item in waiting if item.source == "takeout"])
        for item in waiting:
            if item.source == "phone":
                self.phone(item)
            elif item.id in halves:
                self.exception(item, "live_photo")
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


def live_halves(opened: Session, person: Person, items: Sequence[MediaItem]) -> set[int]:
    by_id = {item.id: item for item in items}
    named: dict[tuple[object, PurePosixPath], list[tuple[MediaItem, PurePosixPath]]] = defaultdict(list)
    for item in items:
        path = PurePosixPath(item.path)
        named[("library", path.parent)].append((item, path))
    recorded = opened.execute(select(DedupeDecision.takeout_item_id, DedupeDecision.export_id, DedupeDecision.source_path)
                              .join(MediaItem, MediaItem.id == DedupeDecision.takeout_item_id)
                              .where(MediaItem.person_id == person.id, MediaItem.source == "takeout", DedupeDecision.source_path.is_not(None))).all()
    for item_id, export_id, source in recorded:
        if item_id in by_id:
            path = PurePosixPath(str(source))
            named[(export_id, path.parent)].append((by_id[item_id], path))
    halves: set[int] = set()
    for siblings in named.values():
        for video, path in siblings:
            if path.suffix.lower() not in media.VIDEO_SUFFIXES:
                continue
            stems = {path.stem.lower()} | ({path.name.lower()} if path.suffix.lower() == pairing.MOTION_CLIP_SUFFIX else set())
            stills = {item.id for item, name in siblings if name.suffix.lower() in pairing.STILL_SUFFIXES and name.stem.lower() in stems}
            if stills:
                halves |= {video.id, *stills}
    for video in items:
        if PurePosixPath(video.path).suffix.lower() in media.VIDEO_SUFFIXES and (video.sidecar_from_still or video.id in halves):
            halves |= {video.id, *same_instant(video, items)}
    return halves


def same_instant(video: MediaItem, items: Sequence[MediaItem]) -> set[int]:
    if video.captured_utc is None:
        return set()
    folder = PurePosixPath(video.path).parent
    return {item.id for item in items if PurePosixPath(item.path).parent == folder and PurePosixPath(item.path).suffix.lower() in pairing.STILL_SUFFIXES
            and item.captured_utc is not None and abs(item.captured_utc - video.captured_utc) <= LIVE_SECONDS}


def candidate(opened: Session, item: MediaItem) -> dedupe.TakeoutCandidate:
    shot = dedupe.shot_of(item)
    sources = opened.execute(select(DedupeDecision.source_path).where(DedupeDecision.takeout_item_id == item.id)).scalars().all()
    edited = any(source is None or pairing.original_name(Path(source)) is not None for source in sources)
    return dedupe.TakeoutCandidate(source_path=item.path, sha1=item.sha1, phash=item.phash, captured_utc=item.captured_utc, captured_local=item.captured_local,
                                   width=item.width, height=item.height, duration_ms=item.duration_ms, make=item.make, model=item.model,
                                   is_video=shot.is_video, is_raw=shot.is_raw, is_edited=edited)


def same_shots(opened: Session, person: Person, items: list[MediaItem]) -> set[int]:
    phone = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone", known_contents())
                           .order_by(MediaItem.id)).scalars().all()
    if not phone or not items:
        return set()
    index = dedupe.PersonIndex(0, person.id, frozenset({person.id}), tuple(dedupe.shot_of(found) for found in phone))
    by_path = {item.path: item.id for item in items}
    decided = dedupe.decide_all(index, [candidate(opened, item) for item in items], dedupe.DedupeSettings())
    return {by_path[found.source_path] for found in decided
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
