from __future__ import annotations

import contextlib
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, immich_reconcile, managed_compound, managed_manifest, managed_metadata, managed_upload, media, same_shot, xmp
from darkroom.domain.ingest import place, unpack
from darkroom.domain.managed_upload import Report
from darkroom.providers import exiftool, immich
from darkroom.storage import db, paths
from darkroom.storage.models import (Alert, DedupeDecision, Export, ManagedCompound, ManagedCompoundMember, ManagedHoldResolution,
                                     ManagedImportIntent, ManagedImportReceipt, Person, ReviewItem, TakeoutEntry)
from darkroom.storage.paths import Photos

ON: Final = "on"
MANIFEST: Final = managed_manifest.TAKEOUT
SETTLE_SECONDS: Final = 24 * 3600.0
NOT_SETTLED: Final = "not_settled"
UNUSABLE: Final = managed_upload.UNUSABLE
TERMINAL: Final = (*managed_metadata.HELD_BEFORE_UPLOAD, "not_enriched", NOT_SETTLED, same_shot.HOLD, UNUSABLE)
KEPT: Final = "review:"
KEPT_ONLY: Final = "cleanup of an Immich-managed import stays off until the owner records the restore rehearsal (#266)"
LIVE_PARTIAL: Final = "live_partial: the Live Photo's other half is not available to keep"
LIVE_UNREADABLE: Final = "the Live Photo's copies no longer match the export or could not be read"


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    root: TakeoutEntry
    name: str = field(repr=False)
    sidecar: str | None = field(repr=False)
    live: bool
    borrowed: bool
    pair: managed_compound.Pair | None = None
    role: managed_compound.MemberRole | None = None
    hold: managed_compound.Hold | None = None


def intent_of(opened: Session, export: Export) -> ManagedImportIntent | None:
    return opened.execute(select(ManagedImportIntent).where(ManagedImportIntent.export_id == export.id, ManagedImportIntent.kind == "import").order_by(ManagedImportIntent.id).limit(1)).scalar_one_or_none()


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
             .where(ManagedImportIntent.export_id == export.id, ManagedImportIntent.kind == "import", ManagedImportReceipt.state == "committed"))
    return int(opened.execute(query).scalar_one())


class Uploads:
    def __init__(self, opened: Session, photos: Photos, export: Export, intent: ManagedImportIntent, reader: immich.ManagedReader, now: float,
                 pause: Callable[[float], None] | None = None) -> None:
        self.opened, self.photos, self.export, self.intent, self.reader = opened, photos, export, intent, reader
        self.now, self.pause = now, pause or time.sleep
        self.committed = self.held = self.pending = self.same = 0

    def due(self, items: Sequence[Item]) -> list[tuple[Item, ...]]:
        if not managed_manifest.known(self.intent.resource_manifest):
            self.pending += 1
            monitoring.log("managed uploads not started", export=self.export.id, reason=managed_manifest.UNKNOWN)
            return []
        known = receipts_of(self.opened, self.intent)
        roots = {item.root.id: item for item in items}
        found: list[tuple[Item, ...]] = []
        for item in items:
            if not wanted(self.opened, self.export, item.root):
                continue
            if item.pair is not None:
                other = item.pair.video if item.role == "live_still" else item.pair.still
                if not wanted(self.opened, self.export, other.root):
                    try:
                        review_half(self.opened, self.export, other.root)
                    except Refused:
                        self.held += 1
                        monitoring.log("a Live Photo half is held", export=self.export.id, reason="live_partial")
                elif item.role == "live_still":
                    self.pair_due(item, roots[other.root.id], found)
                continue
            row = known.get(resource_key(item.root))
            if row is not None and row.state == "committed":
                self.committed += 1
            elif (row is not None and (row.metadata_hold == UNUSABLE or row.compound_key != row.resource_key)) or item.live or item.root.bytes <= 0 or not nameable(item.name):
                self.held += 1
                if item.hold is not None:
                    monitoring.log("a Live Photo half is held", export=self.export.id, reason=item.hold)
            else:
                found.append((item,))
        self.opened.commit()
        return found

    def pair_due(self, still: Item, video: Item, found: list[tuple[Item, ...]]) -> None:
        compound = None if still.pair is None else managed_compound.settled(self.opened, self.intent.id, still.pair.key)
        if compound is not None and compound.committed_at is not None:
            self.committed += 2
        elif (compound is not None and compound.hold is not None) or any(half.root.bytes <= 0 or not nameable(half.name) for half in (still, video)):
            self.held += 2
        else:
            found.append((still, video))

    def receipt(self, item: Item, tags: dict[str, object] | None) -> int | None:
        key = resource_key(item.root)
        found = receipts_of(self.opened, self.intent).get(key)
        if found is not None:
            return found.id
        if tags is None or "Error" in tags or item.root.sha1 is None:
            return None
        kind: managed_metadata.Kind = "VIDEO" if Path(item.name).suffix.lower() in media.VIDEO_SUFFIXES else "IMAGE"
        google = xmp.GoogleMetadata.of(item.sidecar)
        intended = managed_metadata.intend(tags, google, kind, corrects=not item.borrowed)
        db.write_lock(self.opened)
        found = receipts_of(self.opened, self.intent).get(key)
        if found is None:
            found = ManagedImportReceipt(intent_id=self.intent.id, resource_key=key, compound_key=key, source_sha1=item.root.sha1, expected_bytes=item.root.bytes,
                                         expected_metadata=managed_metadata.snapshot(intended), hold_facts=managed_metadata.held_facts(tags, google, intended),
                                         updated_at=self.now)
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
        outcome = same_shot.gate(self.opened, self.reader, self.intent, receipt_id, item.root, Path(item.name).name, self.now)
        if outcome == "pending":
            self.pending += 1
            return
        if outcome == "held":
            self.same += 1
            return
        if outcome == "reversed":
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

    def send_pair(self, still: Item, video: Item, sources: dict[int, Path], tags: dict[int, dict[str, object] | None]) -> None:
        pair = still.pair
        still_tags, video_tags = tags[still.root.id], tags[video.root.id]
        if pair is None or still_tags is None or video_tags is None or managed_compound.embedded_motion(still_tags):
            self.held += 2
            monitoring.log("a Live Photo is held", export=self.export.id, reason="embedded_motion" if still_tags and managed_compound.embedded_motion(still_tags) else "unreadable")
            return
        planned_still = managed_compound.planned(pair.still, "live_still", still_tags, still.sidecar, False)
        planned_video = managed_compound.planned(pair.video, "live_video", video_tags, video.sidecar, video.borrowed)
        if planned_still is None or planned_video is None:
            self.held += 2
            return
        try:
            frozen = managed_compound.freeze(self.opened, self.intent.id, pair.key, [planned_still, planned_video], self.now,
                                             lambda: self.both_wanted(still, video))
        except managed_compound.Refused:
            self.held += 2
            return
        still_id, video_id = frozen.receipts.get(planned_still.resource_key), frozen.receipts.get(planned_video.resource_key)
        if frozen.hold is not None or still_id is None or video_id is None:
            self.held += 2
            return
        with contextlib.ExitStack() as handles:
            def resource(receipt_id: int, item: Item) -> managed_metadata.Resource:
                def original() -> immich.UploadSource:
                    return immich.UploadSource(handles.enter_context(paths.open_inbox_file(self.photos, sources[item.root.id])), item.root.bytes, str(item.root.sha1))
                return managed_metadata.Resource(receipt_id, original, Path(item.name).name)
            self.send_frozen(pair.key, still.root, resource(still_id, still), resource(video_id, video))

    def send_frozen(self, key: str, still: TakeoutEntry, still_resource: managed_metadata.Resource, video_resource: managed_metadata.Resource) -> str:
        still_id, video_id = still_resource.receipt_id, video_resource.receipt_id
        if not self.cleared(key, still, still_resource.filename, still_id, video_id):
            return "the Live Photo could not pass its upload checks"
        report = managed_metadata.complete_live_photo(self.opened, self.reader, video_resource, still_resource, self.now, self.pause)
        if report.outcome == "unusable":
            managed_compound.set_hold(self.opened, self.intent.id, key, managed_compound.UNUSABLE)
        self.count_pair((still_id, video_id), report)
        return report.reason

    def both_wanted(self, still: Item, video: Item) -> bool:
        self.opened.expire_all()
        return wanted(self.opened, self.export, still.root) and wanted(self.opened, self.export, video.root)

    def cleared(self, key: str, still: TakeoutEntry, name: str, still_id: int, video_id: int) -> bool:
        try:
            rows = [managed_upload.fresh(self.opened, still_id), managed_upload.fresh(self.opened, video_id)]
            if managed_upload.preflight(self.reader, rows[0], rows[1]) == "conflict":
                managed_compound.set_hold(self.opened, self.intent.id, key, managed_compound.CONFLICT)
                self.held += 2
                monitoring.log("a Live Photo conflicts with what Immich holds", export=self.export.id)
                return False
        except immich.ImmichFailed:
            self.opened.rollback()
            self.pending += 2
            return False
        if rows[0].metadata_hold != same_shot.HOLD and (rows[1].state != "prepared" or rows[1].send_attempts != 0):
            return True
        outcome = same_shot.gate(self.opened, self.reader, self.intent, still_id, still, name, self.now)
        if outcome == "clear":
            return True
        if outcome == "pending":
            self.pending += 2
        elif outcome == "held":
            self.same += 1
        else:
            self.held += 2
        return False

    def count_pair(self, receipt_ids: tuple[int, int], report: Report) -> None:
        rows = [managed_upload.fresh(self.opened, receipt_id) for receipt_id in receipt_ids]
        if all(row.state == "committed" for row in rows):
            self.committed += 2
        elif report.outcome in ("held", "unusable") or any(row.metadata_hold in TERMINAL or gave_up(self.opened, row, self.now) for row in rows):
            self.held += 2
        else:
            self.pending += 2

    def count(self, receipt_id: int, report: Report) -> None:
        row = managed_upload.fresh(self.opened, receipt_id)
        if row.state == "committed":
            self.committed += 1
        elif row.metadata_hold == same_shot.HOLD:
            self.same += 1
        elif report.outcome in ("held", "unusable") or row.metadata_hold in TERMINAL or gave_up(self.opened, row, self.now):
            self.held += 1
        else:
            self.pending += 1


def review_half(opened: Session, export: Export, entry: TakeoutEntry) -> ReviewItem | None:
    if wanted(opened, export, entry):
        return None
    decision = None if entry.decision_id is None else opened.get(DedupeDecision, entry.decision_id, populate_existing=True)
    if (decision is None or decision.export_id != export.id or decision.account_id != export.account_id or decision.tier != 4
            or decision.reversed_at is not None or decision.takeout_item_id is not None or entry.outcome != "review"):
        raise Refused(LIVE_PARTIAL)
    query = select(ReviewItem).where(ReviewItem.decision_id == decision.id).execution_options(populate_existing=True)
    found = opened.execute(query).scalar_one_or_none()
    if found is None or found.status not in ("pending", "kept_both") or found.hold_sha1 != entry.sha1:
        raise Refused(LIVE_PARTIAL)
    return found


def review_path(opened: Session, photos: Photos, export: Export, review: ReviewItem) -> Path:
    decision = opened.get(DedupeDecision, review.decision_id)
    person = None if decision is None or decision.person_id is None else opened.get(Person, decision.person_id)
    if person is None or not review.hold_path:
        raise Refused(LIVE_UNREADABLE)
    filename = Path(review.hold_path)
    path = photos.review_hold(person.slug, unpack.staging_name(export)) / filename.name
    if not filename.name or path.relative_to(photos.root) != filename:
        raise Refused(LIVE_UNREADABLE)
    return path


@contextlib.contextmanager
def live_original(opened: Session, photos: Photos, export: Export, person: Person, entry: TakeoutEntry, review: ReviewItem | None) -> Iterator[BinaryIO]:
    with contextlib.ExitStack() as handles:
        if review is not None:
            original = handles.enter_context(paths.open_inbox_file(photos, review_path(opened, photos, export, review)))
        elif entry.held_path is not None:
            path = paths.held_copy_at(photos, entry.held_path, unpack.staging_name(export))
            original = handles.enter_context(paths.open_inbox_file(photos, path))
        else:
            from darkroom.domain.ingest import part_ingest

            original = handles.enter_context(tempfile.TemporaryFile())
            part_ingest.copy_archived_media(opened, photos, export, person, entry, original)
            original.seek(0)
        digest, identity = paths.fingerprint(original)
        if (digest, identity[2]) != (entry.sha1, entry.bytes):
            raise Refused(LIVE_UNREADABLE)
        original.seek(0)
        yield original


LIVE_READ_FAILURES = (OSError, paths.OutsideRoot, paths.NotAHeldCopy, exiftool.Missing, exiftool.Unreadable)


def keep_live(opened: Session, photos: Photos, export: Export, person: Person, current: ReviewItem, items: tuple[Item, Item], now: float, read: xmp.Reader) -> str:
    intent = intent_of(opened, export)
    still, video = items
    pair = still.pair
    if intent is None or intent.person_id != person.id or pair is None or not managed_manifest.known(intent.resource_manifest):
        raise Refused("the Live Photo's import manifest could not be checked")
    choices = [review_half(opened, export, item.root) for item in items]
    if all(choice is None or choice.id != current.id or choice.status != "pending" for choice in choices):
        raise Refused(LIVE_PARTIAL)
    if any(not nameable(item.name) for item in items):
        raise Refused("Immich does not take the name of one of the Live Photo's halves")
    try:
        with contextlib.ExitStack() as handles:
            originals = [handles.enter_context(live_original(opened, photos, export, person, item.root, choice)) for item, choice in zip(items, choices, strict=True)]
            locations = [paths.OPEN_FILES / str(original.fileno()) for original in originals]
            tags = read(locations)
    except LIVE_READ_FAILURES:
        raise Refused(LIVE_UNREADABLE) from None
    still_tags, video_tags = (tags.get(location) for location in locations)
    if still_tags is not None and managed_compound.embedded_motion(still_tags):
        raise Refused("embedded_motion: the Live Photo's still already contains motion video")
    planned_still = managed_compound.planned(pair.still, "live_still", still_tags, still.sidecar, False)
    planned_video = managed_compound.planned(pair.video, "live_video", video_tags, video.sidecar, video.borrowed)
    if planned_still is None or planned_video is None:
        raise Refused(LIVE_UNREADABLE)
    for planned in (planned_still, planned_video):
        if planned.intended.hold is not None:
            raise Refused(f"the Live Photo cannot go to Immich as it is ({managed_metadata.REASONS[planned.intended.hold]})")
    if any(choice is not None and choice.id != current.id and choice.status == "pending" for choice in choices):
        return ", waiting for the Live Photo's other half"
    try:
        frozen = managed_compound.freeze_locked(opened, intent.id, pair.key, [planned_still, planned_video], now, lambda: True)
    except managed_compound.Refused as error:
        raise Refused(str(error)) from None
    if frozen.hold is not None or frozen.receipts.keys() != {planned_still.resource_key, planned_video.resource_key}:
        raise Refused("the Live Photo already has a held or different compound")
    return ", to be uploaded to Immich as a Live Photo"


def keep_for_immich(opened: Session, photos: Photos, export: Export, review: ReviewItem, hold: Path, sha1: str, name: str, live: bool, now: float,
                    read: xmp.Reader) -> None:
    intent = intent_of(opened, export)
    if intent is None:
        raise Refused("only a pair of an Immich-managed import is kept in Immich")
    if f"{KEPT}{review.id}" in receipts_of(opened, intent):
        return
    if live or review.sidecar_from_still:
        raise Refused(LIVE_PARTIAL)
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
    resolution = select(ManagedHoldResolution.id).where(ManagedHoldResolution.receipt_id == receipt_id, ManagedHoldResolution.state == "applied",
                                                        ManagedHoldResolution.result == "cleared")
    if opened.execute(resolution).first() is not None:
        return False
    intent = opened.get(ManagedImportIntent, row.intent_id)
    person = None if intent is None else opened.get(Person, intent.person_id)
    review = opened.get(ReviewItem, int(row.resource_key.removeprefix(KEPT)))
    decision = None if review is None else opened.get(DedupeDecision, review.decision_id)
    if intent is None or person is None or review is None or review.hold_path is None or decision is None:
        return False
    if not managed_manifest.known(intent.resource_manifest):
        overdue(opened, settings, row.id, managed_manifest.UNKNOWN, now)
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


def kept_compounds(opened: Session) -> list[ManagedCompound]:
    member = ManagedCompoundMember
    query = (select(ManagedCompound).join(ManagedImportIntent, ManagedImportIntent.id == ManagedCompound.intent_id)
             .join(member, member.compound_id == ManagedCompound.id)
             .join(TakeoutEntry, member.resource_key == managed_compound.TAKEOUT_ENTRY + cast(TakeoutEntry.id, String))
             .join(ReviewItem, ReviewItem.decision_id == TakeoutEntry.decision_id)
             .where(ManagedImportIntent.kind == "import", ManagedCompound.committed_at.is_(None), ReviewItem.status == "kept_both")
             .distinct().order_by(ManagedCompound.id))
    return list(opened.execute(query).scalars())


def send_kept_compound(opened: Session, settings: config.Settings, photos: Photos, compound: ManagedCompound, now: float, client: immich.Client | None) -> int:
    intent = opened.get(ManagedImportIntent, compound.intent_id)
    export = None if intent is None else opened.get(Export, intent.export_id)
    person = None if intent is None else opened.get(Person, intent.person_id)
    if intent is None or export is None or person is None:
        return 0
    member = ManagedCompoundMember
    query = (select(member, TakeoutEntry, ReviewItem)
             .join(TakeoutEntry, member.resource_key == managed_compound.TAKEOUT_ENTRY + cast(TakeoutEntry.id, String))
             .outerjoin(ReviewItem, ReviewItem.decision_id == TakeoutEntry.decision_id)
             .where(member.compound_id == compound.id).order_by(member.role))
    found = opened.execute(query).all()
    kept = [review for _, _, review in found if review is not None and review.status == "kept_both"]
    frozen = managed_compound.frozen(opened, compound)
    rows = [managed_upload.fresh(opened, receipt_id) for receipt_id in frozen.receipts.values()]
    terminal = frozen.hold or next((row.metadata_hold for row in rows if row.metadata_hold in TERMINAL), None)
    uploads = None
    reason = terminal or "its copies or the person's Immich key could not be used"
    if terminal is None and managed_manifest.known(intent.resource_manifest):
        try:
            if [part.role for part, _, _ in found] != ["live_still", "live_video"]:
                raise Refused(LIVE_PARTIAL)
            with contextlib.ExitStack() as handles:
                resources = []
                for part, entry, _ in found:
                    if entry.export_id != export.id or (entry.sha1, entry.bytes) != (part.source_sha1, part.expected_bytes):
                        raise Refused(LIVE_UNREADABLE)
                    chosen = review_half(opened, export, entry)
                    if chosen is not None and chosen.status != "kept_both":
                        raise Refused(LIVE_PARTIAL)
                    original = handles.enter_context(live_original(opened, photos, export, person, entry, chosen))

                    def source(original: BinaryIO = original, part: ManagedCompoundMember = part) -> immich.UploadSource:
                        original.seek(0)
                        return immich.UploadSource(original, part.expected_bytes, part.source_sha1)

                    resources.append(managed_metadata.Resource(frozen.receipts[part.resource_key], source, Path(entry.name).name))
                reader = reader_of(settings, person, intent, client)
                reader.check_access()
                uploads = Uploads(opened, photos, export, intent, reader, now)
                reason = uploads.send_frozen(compound.compound_key, found[0][1], resources[0], resources[1])
        except (*LIVE_READ_FAILURES, Refused, immich.ImmichFailed, KeyError) as error:
            opened.rollback()
            monitoring.log("kept Live Photo not sent to Immich", compound=compound.id, error=type(error).__name__)
    if uploads is not None and uploads.committed:
        return uploads.committed
    for review in kept:
        if terminal is not None or (uploads is not None and (uploads.held or uploads.same)) or (
                review.resolved_at is not None and now - review.resolved_at >= SETTLE_SECONDS):
            kept_alert(opened, settings, person, review.id, reason, now)
    return 0


def send_kept(opened: Session, settings: config.Settings, photos: Photos, now: float, client: immich.Client | None) -> int:
    query = (select(ManagedImportReceipt.id).where(ManagedImportReceipt.resource_key.startswith(KEPT), ManagedImportReceipt.state != "committed",
                                                     or_(ManagedImportReceipt.metadata_hold.is_(None), ManagedImportReceipt.metadata_hold.not_in(TERMINAL))))
    sent = 0
    for receipt_id in opened.execute(query.order_by(ManagedImportReceipt.id)).scalars().all():
        try:
            sent += send_one(opened, settings, photos, receipt_id, now, client)
        except (immich.ImmichFailed, paths.OutsideRoot, OSError) as error:
            opened.rollback()
            monitoring.log("kept photo not sent to Immich", receipt=receipt_id, error=type(error).__name__)
            overdue(opened, settings, receipt_id, "its copy or the person's Immich key could not be used", now)
    for compound in kept_compounds(opened):
        sent += send_kept_compound(opened, settings, photos, compound, now, client)
    if sent:
        monitoring.log("kept photos sent to Immich", committed=sent)
    return sent


def once(opened: Session, settings: config.Settings, key: str, subject: str, message: str, now: float, notifier: alerts.Notifier | None, about: alerts.About) -> None:
    if opened.execute(select(Alert.id).where(Alert.key == key).limit(1)).first() is None:
        alerts.open_alert(opened, settings, key, "failure", subject, alerts.Wording("Darkroom needs a look", message), None, now, notifier=notifier, about=about)


def gave_up(opened: Session, row: ManagedImportReceipt, now: float) -> bool:
    if row.metadata_hold not in ("mismatch", "not_linked") or row.bytes_verified_at is None or now - row.bytes_verified_at < SETTLE_SECONDS:
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
