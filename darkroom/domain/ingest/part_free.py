from __future__ import annotations

import hashlib
import json
import os
import time
import zlib
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, managed_switch, retention_gate
from darkroom.domain.ingest import cleanup, pairing, part_ingest, part_reader, run, unpack
from darkroom.storage import db, paths
from darkroom.storage.models import (
    DedupeDecision, Event, Export, ExportPart, ImportRun, ManagedImportIntent, MediaItem, Person, ReviewItem, TakeoutEntry, TakeoutSidecar,
)
from darkroom.storage.paths import Identity, Photos

FREEING: Final = "freeing"
FREED: Final = "freed"
IMPORTED: Final = "imported"
STILL_DOWNLOADING: Final = ("downloading", "downloaded")
SKIPPING_TIERS: Final = (1, 2, 3)
COPY: Final = "copy"
KEPT: Final = "export_part_kept"
HELD_KEPT: Final = "export_held_kept"
HOLD_KEPT: Final = "export_hold_kept"
WORTH_AN_EVENT: Final = frozenset({"unknown_entry", "listing_differs", "unreadable", "placed_differs", "review_differs", "held_differs",
                                   "hash_differs", "changed", "zip_changed"})
HASH_DIFFERS: Final = "hash_differs"
KEY_VERSION: Final = 1
REPROVE_AFTER: Final = 6 * 3600.0
SWITCH_KEPT: Final = "managed_switch"
RELEASE_WAITING: Final = "managed_release_immich_waiting"
RELEASE_RESUMED: Final = "managed_release_immich_resumed"


class Refused(Exception):
    def __init__(self, reason: str, settled: bool = False, *, stop: bool = False, slow_reads: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.settled = settled
        self.stop = stop
        self.slow_reads = slow_reads
        self.key: str | None = None


@dataclass(frozen=True)
class Checked:
    path: Path
    sha1: str
    identity: Identity
    in_the_inbox: bool


@dataclass
class Proof:
    zip: Identity | None = None
    files: dict[Path, Checked] = field(default_factory=dict)


@dataclass
class FreeReport:
    parts: int = 0
    held: int = 0
    holds: int = 0
    kept: int = 0
    failed: int = 0
    bytes: int = 0


def export_name(export: Export) -> str:
    return unpack.staging_name(export)


def zip_path(photos: Photos, person: Person, export: Export, part: ExportPart) -> Path:
    try:
        return paths.part_zip_path(photos, person.slug, export_name(export), part.filename or "")
    except (paths.OutsideRoot, paths.NotAPartZip) as error:
        raise Refused("not_export_folder") from error


def in_export_folder(photos: Photos, person: Person, export: Export, part: ExportPart) -> bool:
    try:
        return str(unpack.part_folder(export, photos, person)) == str(zip_path(photos, person, export, part).parent)
    except Refused:
        return False


def managed(opened: Session, export: Export) -> bool:
    return opened.execute(select(ManagedImportIntent.id).where(ManagedImportIntent.export_id == export.id, ManagedImportIntent.kind == "import").limit(1)).first() is not None


def owner_action(photos: Photos) -> bool:
    return any(found for prefix in (paths.DROPPING, paths.PLACING) for found in paths.leftovers(photos, prefix))


def export_settled(opened: Session, export: Export) -> None:
    runs = list(opened.execute(select(ImportRun).where(ImportRun.export_id == export.id).order_by(ImportRun.id.desc())).scalars())
    if export.status == IMPORTED:
        if not runs or runs[0].status != "done":
            raise Refused("export_busy")
    elif export.status not in STILL_DOWNLOADING or runs or export.stage is not None:
        raise Refused("export_busy")


def shared_checks(opened: Session, export: Export, releasing_managed: bool = False) -> None:
    if not releasing_managed and managed(opened, export):
        raise Refused("managed")
    export_settled(opened, export)
    if part_ingest.waits_for_earlier(opened, export):
        raise Refused("earlier_export")


def part_checks(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart, now: float, releasing_managed: bool) -> None:
    if part.ingest_state != part_ingest.INGESTED or part.ingested_at is None or part.ingested_at >= now or part.status != "done":
        raise Refused("not_settled")
    if not in_export_folder(photos, person, export, part):
        raise Refused("not_export_folder")
    shared_checks(opened, export, releasing_managed)


def listing(photos: Photos, person: Person, export: Export, part: ExportPart) -> tuple[Identity, list[tuple[str, int, int]]]:
    path = zip_path(photos, person, export, part)
    try:
        with paths.open_inbox_file(photos, path) as handle:
            identity = paths.file_identity(os.fstat(handle.fileno()))
            with part_reader.zip_of(handle, path, part.number) as archive:
                listed = part_reader.entry_listing(archive.infolist())
    except FileNotFoundError as error:
        raise Refused("zip_missing") from error
    except (part_reader.PartRefused, unpack.UnpackFailed, zipfile.BadZipFile, paths.OutsideRoot, ValueError) as error:
        raise Refused("listing_differs") from error
    except OSError as error:
        raise Refused("unreadable") from error
    if part.bytes is not None and identity[2] != part.bytes:
        raise Refused("listing_differs")
    return identity, sorted(entry.in_the_zip() for entry in listed)


def ledger_matches(rows: list[TakeoutEntry], listed: list[tuple[str, int, int]]) -> None:
    recorded = sorted((row.name, row.bytes, row.crc32) for row in rows)
    if set(listed) - set(recorded):
        raise Refused("unknown_entry")
    if recorded != listed:
        raise Refused("listing_differs")


def stored_text(opened: Session, entry_id: int | None) -> str | None:
    if entry_id is None:
        return None
    return opened.execute(select(TakeoutSidecar.text).where(TakeoutSidecar.entry_id == entry_id)).scalar_one_or_none()


def sidecar_recorded(opened: Session, row: TakeoutEntry) -> bool:
    text = stored_text(opened, row.id)
    if text is None:
        return False
    data = text.encode("utf-8")
    return len(data) == row.bytes and zlib.crc32(data) == row.crc32


def never_read(row: TakeoutEntry) -> bool:
    if row.name.endswith("/"):
        return row.bytes == 0
    return PurePosixPath(row.name).name in pairing.KNOWN_NON_MEDIA


class Prover:
    def __init__(self, opened: Session, photos: Photos, export: Export, on_disk: bool, require_held: bool = False, *, releasing_managed: bool = False) -> None:
        self.opened, self.photos, self.export, self.on_disk, self.require_held = opened, photos, export, on_disk, require_held
        self.proof = Proof()
        self.roots: dict[int, None] = {}
        self.waiting: list[TakeoutEntry] = []
        self.releasing_managed = releasing_managed

    def check(self, path: Path, sha1: str, in_the_inbox: bool) -> None:
        if not self.on_disk:
            return
        try:
            identity = paths.inbox_identity(self.photos, path) if in_the_inbox else paths.library_identity(path)
        except FileNotFoundError:
            identity = None
        except paths.OutsideRoot as error:
            raise Refused("changed") from error
        if identity is None:
            raise Refused("file_missing")
        self.proof.files[path] = Checked(path, sha1, identity, in_the_inbox)

    def placed(self, root: TakeoutEntry) -> None:
        decision = None if root.decision_id is None else self.opened.get(DedupeDecision, root.decision_id)
        item = None if root.item_id is None else self.opened.get(MediaItem, root.item_id)
        if decision is None or decision.reversed_at is not None or decision.decided_by != "auto" or decision.tier != 5 or decision.takeout_item_id != root.item_id:
            raise Refused("decision_unsafe")
        if item is None or item.source != "takeout" or root.sha1 is None or item.sha1 != root.sha1:
            raise Refused("placed_differs")
        person = self.opened.get(Person, item.person_id)
        try:
            path = self.photos.google_only_file(item.path) if self.on_disk else self.photos.root / item.path
        except (paths.NotALibraryCopy, paths.OutsideRoot) as error:
            raise Refused("file_missing") from error
        if person is None or item.path.split("/")[0] != person.slug:
            raise Refused("placed_differs")
        self.check(path, root.sha1, in_the_inbox=False)

    def review(self, root: TakeoutEntry) -> None:
        review = None if root.decision_id is None else self.opened.execute(
            select(ReviewItem).where(ReviewItem.decision_id == root.decision_id)).scalar_one_or_none()
        if review is None or review.hold_sha1 is None or review.hold_sha1 != root.sha1:
            raise Refused("review_differs")
        hold = cleanup.hold_file(self.photos, self.export, review) if self.on_disk else self.photos.root / (review.hold_path or "")
        if hold is None:
            raise Refused("file_missing")
        self.check(hold, review.hold_sha1, in_the_inbox=True)

    def skipped(self, root: TakeoutEntry) -> None:
        decision = None if root.decision_id is None else self.opened.get(DedupeDecision, root.decision_id)
        if decision is None or decision.decided_by != "auto" or decision.reversed_at is not None or decision.tier not in SKIPPING_TIERS:
            raise Refused("decision_unsafe")
        if decision.matched_item_id is None or self.opened.get(MediaItem, decision.matched_item_id) is None:
            raise Refused("decision_unsafe")
        if root.metadata_pending and stored_text(self.opened, root.sidecar_entry_id) is None:
            raise Refused("metadata_unrecorded")

    def held(self, root: TakeoutEntry) -> None:
        if root.held_path is None or root.sha1 is None:
            if self.require_held:
                raise Refused("held_missing")
            self.waiting.append(root)
            return
        try:
            path = paths.held_copy_at(self.photos, root.held_path, export_name(self.export))
        except (paths.NotAHeldCopy, paths.OutsideRoot) as error:
            raise Refused("held_differs") from error
        self.check(path, root.sha1, in_the_inbox=True)

    def root(self, root: TakeoutEntry, allow_held: bool) -> None:
        if root.id in self.roots:
            return
        if root.copy_of_id is not None or root.kind != "media":
            raise Refused("undecided_entry")
        if root.outcome == part_ingest.PLACED:
            if not self.releasing_managed:
                self.placed(root)
        elif root.outcome == part_ingest.REVIEW:
            self.review(root)
        elif root.outcome == part_ingest.SKIPPED:
            if not self.releasing_managed:
                self.skipped(root)
        elif root.outcome in part_ingest.HELD and allow_held:
            self.held(root)
        elif root.outcome == part_ingest.FAILED:
            raise Refused("failed_entry")
        else:
            raise Refused("undecided_entry")
        self.roots[root.id] = None

    def media(self, row: TakeoutEntry, allow_held: bool) -> TakeoutEntry:
        if row.outcome == part_ingest.FAILED:
            raise Refused("failed_entry")
        if row.outcome == COPY:
            found = None if row.copy_of_id is None else self.opened.get(TakeoutEntry, row.copy_of_id)
            if found is None or found.export_id != self.export.id or found.sha1 is None or found.sha1 != row.sha1:
                raise Refused("undecided_entry")
            self.root(found, allow_held)
            return found
        self.root(row, allow_held)
        return row

    def part(self, rows: list[TakeoutEntry]) -> None:
        for row in rows:
            if row.kind == "media":
                root = self.media(row, allow_held=True)
                if root is row and self.export.status != IMPORTED and row.decision_id is not None and row.captured_utc is None:
                    raise Refused("measure_pending")
            elif row.outcome != part_ingest.RECORDED:
                raise Refused("undecided_entry")
            elif row.kind == "sidecar":
                if not sidecar_recorded(self.opened, row):
                    raise Refused("sidecar_unrecorded")
            elif not never_read(row):
                raise Refused("other_kept")

    def hash_all(self) -> None:
        for found in self.proof.files.values():
            hashed(self.photos, found)


def hashed(photos: Photos, found: Checked) -> None:
    try:
        digest, identity = paths.inbox_fingerprint(photos, found.path) if found.in_the_inbox else paths.plain_fingerprint(found.path)
    except FileNotFoundError as error:
        raise Refused("file_missing") from error
    except paths.OutsideRoot as error:
        raise Refused("changed") from error
    if digest != found.sha1:
        raise Refused(HASH_DIFFERS, settled=identity == found.identity)
    if identity != found.identity:
        raise Refused("changed")


def confirm(photos: Photos, first: Proof, second: Proof) -> Identity:
    if first.zip is None or first.zip != second.zip:
        raise Refused("changed")
    for path, found in second.files.items():
        earlier = first.files.get(path)
        if earlier is None:
            hashed(photos, found)
        elif earlier.identity != found.identity or earlier.sha1 != found.sha1:
            raise Refused("changed")
    if set(first.files) - set(second.files):
        raise Refused("changed")
    return first.zip


def part_rows(opened: Session, export: Export, part: ExportPart) -> list[TakeoutEntry]:
    return part_reader.recorded(opened, export, part)


def proof_key(part: ExportPart, proof: Proof) -> str:
    files = sorted([str(found.path), found.sha1, list(found.identity), found.in_the_inbox] for found in proof.files.values())
    said = json.dumps([KEY_VERSION, part.id, part.number, part.ingested_at, part.bytes, None if proof.zip is None else list(proof.zip), files],
                      separators=(",", ":"))
    return hashlib.sha256(said.encode("utf-8")).hexdigest()


def hash_first(photos: Photos, part: ExportPart, proof: Proof) -> None:
    try:
        for found in proof.files.values():
            hashed(photos, found)
    except Refused as refused:
        if refused.reason == HASH_DIFFERS and refused.settled and proof.zip is not None:
            refused.key = proof_key(part, proof)
        raise


def prove_part(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart, now: float, hashing: bool,
               require_held: bool, *, releasing_managed: bool = False) -> Proof:
    part_checks(opened, photos, person, export, part, now, releasing_managed)
    rows = part_rows(opened, export, part)
    Prover(opened, photos, export, on_disk=False, require_held=require_held, releasing_managed=releasing_managed).part(rows)
    identity, listed = listing(photos, person, export, part)
    ledger_matches(rows, listed)
    prover = Prover(opened, photos, export, on_disk=True, require_held=require_held, releasing_managed=releasing_managed)
    prover.part(rows)
    if owner_action(photos) or (releasing_managed and not no_claims_left(photos, person, export, part)):
        raise Refused("owner_action")
    if hashing:
        prover.hash_all()
    prover.proof.zip = identity
    return prover.proof


def entry_chunks(archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo], entry: TakeoutEntry, number: int) -> Iterator[bytes]:
    info = infos.get(entry.name)
    if info is None:
        raise Refused("listing_differs")
    yield from part_reader.entry_chunks(archive, info, number, [0])


def source_of(root: TakeoutEntry, rows: list[TakeoutEntry], number: int) -> TakeoutEntry:
    if root.part_number == number:
        return root
    found = next((row for row in rows if row.kind == "media" and row.copy_of_id == root.id and row.sha1 == root.sha1), None)
    if found is None:
        raise Refused("held_missing")
    return found


def hold_waiting(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart) -> int:
    rows = part_rows(opened, export, part)
    prover = Prover(opened, photos, export, on_disk=False)
    prover.part(rows)
    if not prover.waiting:
        return 0
    path = zip_path(photos, person, export, part)
    copied = 0
    try:
        with paths.open_inbox_file(photos, path) as handle, part_reader.zip_of(handle, path, part.number) as archive:
            infos = {listed.name: listed.info for listed in part_reader.entry_listing(archive.infolist())}
            for root in prover.waiting:
                if root.sha1 is None:
                    raise Refused("held_missing")
                written = paths.write_held(photos, person.slug, export_name(export), part_reader.scratch_name(root),
                                           entry_chunks(archive, infos, source_of(root, rows, part.number), part.number), root.sha1)
                db.write_lock(opened)
                opened.refresh(root)
                if root.held_path is None:
                    root.held_path = written.relative_to(photos.root).as_posix()
                    copied += 1
                opened.commit()
    except FileNotFoundError as error:
        raise Refused("zip_missing") from error
    except (paths.ChangedWhileCopied, paths.AlreadyExists, part_reader.PartRefused, unpack.UnpackFailed, paths.OutsideRoot) as error:
        raise Refused("held_differs") from error
    return copied


def note_once(opened: Session, now: float, kind: str, subject: str, detail: str) -> bool:
    seen = opened.execute(select(Event.id).where(Event.type == kind, Event.subject == subject, Event.detail == detail).limit(1)).first()
    if seen is None:
        events.record(opened, now, kind, subject, detail)
    return seen is None


def kept(opened: Session, export: Export, subject: str, kind: str, reason: str, now: float) -> None:
    if note_once(opened, now, kind, f"export {export.id}", f"{subject}: {reason}") or reason in WORTH_AN_EVENT:
        monitoring.log("zip kept", export=export.id, what=subject, reason=reason)


def zip_gone(photos: Photos, person: Person, export: Export, part: ExportPart) -> bool:
    return paths.inbox_entry_absent(photos, zip_path(photos, person, export, part))


def zip_present(photos: Photos, person: Person, export: Export, part: ExportPart) -> bool:
    try:
        return not zip_gone(photos, person, export, part)
    except (FileNotFoundError, NotADirectoryError, paths.OutsideRoot, Refused):
        return False


def record_freed(opened: Session, export: Export, part: ExportPart, removed: int, now: float, report: FreeReport) -> None:
    part.free_state, part.freed_at, part.bytes_freed = FREED, now, removed
    part.free_kept_at, part.free_kept_key = None, None
    events.record(opened, now, "export_part_freed", f"export {export.id}", f"part {part.number}: {removed} bytes")
    report.parts += 1
    report.bytes += removed
    opened.commit()
    monitoring.log("zip freed", export=export.id, part=part.number, bytes=removed)


def lock(opened: Session, export: Export, part: ExportPart) -> None:
    db.write_lock(opened)
    opened.expire_all()
    opened.refresh(export)
    opened.refresh(part)


def switch_guard(opened: Session, photos: Photos, person: Person, export: Export) -> None:
    try:
        managed_switch.hold(opened, photos, person, "part_free", export)
    except managed_switch.Paused as paused:
        raise Refused(SWITCH_KEPT) from paused


def lock_for_removal(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart) -> None:
    switch_guard(opened, photos, person, export)
    opened.refresh(part)


def kept_by_switch(opened: Session, photos: Photos, person: Person, export: Export, subject: str, kind: str, now: float, report: FreeReport) -> bool:
    opened.commit()
    try:
        switch_guard(opened, photos, person, export)
    except Refused as refused:
        kept(opened, export, subject, kind, refused.reason, now)
        report.kept += 1
        opened.commit()
        return True
    opened.rollback()
    return False


def remembered(part: ExportPart, now: float) -> bool:
    return part.free_state is None and part.free_kept_at is not None and 0.0 <= now - part.free_kept_at < REPROVE_AFTER


def no_claims_left(photos: Photos, person: Person, export: Export, part: ExportPart) -> bool:
    try:
        return not paths.claims_left_in(photos, zip_path(photos, person, export, part).parent)
    except (OSError, paths.OutsideRoot, paths.LinkRefused, Refused):
        return False


def kept_unchanged(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart, now: float, report: FreeReport) -> bool:
    if not remembered(part, now):
        return False
    opened.commit()
    opened.expire_all()
    if not remembered(part, now) or not no_claims_left(photos, person, export, part):
        return False
    try:
        proof = prove_part(opened, photos, person, export, part, now, hashing=False, require_held=False)
    except Refused as refused:
        reason = refused.reason
    else:
        if part.free_kept_key is None or part.free_kept_key != proof_key(part, proof):
            return False
        reason = HASH_DIFFERS
    kept(opened, export, f"part {part.number}", KEPT, reason, now)
    report.kept += 1
    opened.commit()
    return True


def put_back_part(photos: Photos, person: Person, export: Export, part: ExportPart) -> None:
    try:
        recover_claim(photos, zip_path(photos, person, export, part), export)
    except Refused:
        return


def lock_release(opened: Session, person: Person, export: Export, unit: ExportPart | TakeoutEntry | ReviewItem) -> None:
    managed_switch.claim(opened, person)
    opened.refresh(export)
    opened.refresh(unit)


def release_for(opened: Session, settings: config.Settings, person: Person, unit: ExportPart | TakeoutEntry | ReviewItem, now: float) -> retention_gate.Release:
    checked = retention_gate.release_check(opened, unit, now, settings.managed_release, confirm_metadata_live=True)
    if isinstance(checked, str):
        raise Refused(checked)
    if checked.person_id != person.id:
        raise Refused("changed")
    return checked


def recheck_release(opened: Session, settings: config.Settings, person: Person, unit: ExportPart | TakeoutEntry | ReviewItem, now: float,
                    first: retention_gate.Release) -> None:
    if release_for(opened, settings, person, unit, now) != first:
        raise Refused("changed")


def confirm_release(settings: config.Settings, person: Person, release: retention_gate.Release, photos: Photos, proof: Proof, now: float) -> None:
    started = time.monotonic()
    result = retention_gate.confirm_release(settings, person, release, now)
    if result.reason is not None:
        raise Refused(result.reason, stop=result.stop, slow_reads=result.slow_reads)
    for found in proof.files.values():
        try:
            identity = paths.inbox_identity(photos, found.path)
        except (OSError, paths.OutsideRoot) as error:
            raise Refused("changed") from error
        if identity != found.identity:
            raise Refused("changed")
    if now + (time.monotonic() - started) >= release.valid_until:
        raise Refused(retention_gate.NO_RECOVERY_POINT)


def release_availability(opened: Session, settings: config.Settings, person: Person, now: float, waiting: bool) -> None:
    subject = f"person {person.id}"
    last = opened.scalars(select(Event).where(Event.subject == subject, Event.type.in_((RELEASE_WAITING, RELEASE_RESUMED))).order_by(Event.id.desc()).limit(1)).first()
    key = f"managed:release:{person.id}"
    if waiting:
        if last is None or last.type != RELEASE_WAITING:
            events.record(opened, now, RELEASE_WAITING, subject, retention_gate.IMMICH_UNCONFIRMED)
        elif now >= last.time + alerts.DAY:
            alerts.open_alert(opened, settings, key, RELEASE_WAITING, subject,
                              alerts.Wording(alerts.TITLES[RELEASE_WAITING], "Darkroom cannot verify the photos in Immich for release. It keeps the inbox copies."),
                              None, now, about=alerts.person(person.id))
    elif last is not None and last.type == RELEASE_WAITING:
        events.record(opened, now, RELEASE_RESUMED, subject, "Immich answered")
        alerts.close(opened, key, now)


def note_release_refusal(opened: Session, settings: config.Settings, person: Person, export: Export, refused: Refused, now: float) -> None:
    subject = f"export {export.id}"
    if refused.reason == retention_gate.IMMICH_UNCONFIRMED:
        cleanup.note_kept(opened, now, subject, refused.reason)
    if refused.slow_reads is not None:
        events.record(opened, now, "managed_release_slow", subject, str(refused.slow_reads))
    if refused.stop and refused.slow_reads is None:
        release_availability(opened, settings, person, now, waiting=True)


def free_part(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart, now: float, report: FreeReport,
              settings: config.Settings | None = None) -> bool:
    started = time.monotonic()
    if settings is None and kept_unchanged(opened, photos, person, export, part, now, report):
        return False
    if settings is None and kept_by_switch(opened, photos, person, export, f"part {part.number}", KEPT, now, report):
        put_back_part(photos, person, export, part)
        return False
    if settings is None:
        lock(opened, export, part)
    else:
        lock_release(opened, person, export, part)
    try:
        recover_claim(photos, zip_path(photos, person, export, part), export)
        journalled = part.free_state == FREEING and (settings is None or part.release_point_id is not None)
        gone = journalled and zip_gone(photos, person, export, part)
    except Refused as refused:
        opened.rollback()
        report.failed += 1
        monitoring.log("zip freeing failed", export=export.id, part=part.number, reason=refused.reason)
        return False
    if gone:
        record_freed(opened, export, part, part.bytes or 0, now, report)
        return False
    opened.commit()
    try:
        release = None if settings is None else release_for(opened, settings, person, part, now)
        if release is None:
            first = prove_part(opened, photos, person, export, part, now, hashing=False, require_held=False)
        else:
            first = prove_part(opened, photos, person, export, part, now, hashing=False, require_held=True, releasing_managed=True)
        hash_first(photos, part, first)
        if release is None:
            hold_waiting(opened, photos, person, export, part)
        opened.commit()
        if release is not None:
            lock_release(opened, person, export, part)
            if part.free_state not in (None, FREEING):
                raise Refused("changed")
            part.release_point_id = release.point_id
        if part.free_state is None:
            part.free_state, part.free_started_at = FREEING, now
        opened.commit()
        if release is None or settings is None:
            lock_for_removal(opened, photos, person, export, part)
            second = prove_part(opened, photos, person, export, part, now, hashing=False, require_held=True)
        else:
            lock_release(opened, person, export, part)
            if part.free_state != FREEING or part.release_point_id != release.point_id:
                raise Refused("changed")
            recheck_release(opened, settings, person, part, now + (time.monotonic() - started), release)
            second = prove_part(opened, photos, person, export, part, now, hashing=False, require_held=True, releasing_managed=True)
        checked = confirm(photos, first, second)
        if release is not None and settings is not None:
            confirm_release(settings, person, release, photos, second, now + (time.monotonic() - started))
        try:
            removed = paths.remove_part_zip(photos, person.slug, export_name(export), part.filename or "", checked)
        except paths.NotAPartZip as error:
            raise Refused("zip_changed") from error
        if not zip_gone(photos, person, export, part):
            raise Refused("zip_changed")
    except Refused as refused:
        opened.rollback()
        if settings is None:
            lock(opened, export, part)
        else:
            lock_release(opened, person, export, part)
        if part.free_state == FREEING:
            part.free_state, part.free_started_at = None, None
            part.release_point_id = None
        part.free_kept_at, part.free_kept_key = now, refused.key
        if refused.reason != retention_gate.RETENTION_CLOCK:
            kept(opened, export, f"part {part.number}", KEPT, refused.reason, now)
            report.kept += 1
        if settings is not None:
            note_release_refusal(opened, settings, person, export, refused, now)
        opened.commit()
        return refused.stop
    if settings is not None:
        release_availability(opened, settings, person, now, waiting=False)
    record_freed(opened, export, part, (part.bytes or 0) if removed is None else removed, now, report)
    return False


def held_rows(opened: Session, export: Export) -> list[TakeoutEntry]:
    return list(opened.execute(select(TakeoutEntry).where(TakeoutEntry.export_id == export.id, TakeoutEntry.held_path.is_not(None),
                                                          TakeoutEntry.held_state.is_(None) | (TakeoutEntry.held_state == FREEING))
                               .order_by(TakeoutEntry.id)).scalars())


def any_failed(opened: Session, export: Export) -> bool:
    return opened.execute(select(TakeoutEntry.id).where(TakeoutEntry.export_id == export.id, TakeoutEntry.outcome == part_ingest.FAILED)
                          .limit(1)).first() is not None


@dataclass
class HeldProof:
    proof: Proof
    ready: list[TakeoutEntry]
    kept: dict[str, int]


def prove_held(opened: Session, photos: Photos, export: Export, rows: list[TakeoutEntry], hashing: bool, *, releasing_managed: bool = False) -> HeldProof:
    if export.status != IMPORTED:
        raise Refused("export_busy")
    shared_checks(opened, export, releasing_managed)
    if any_failed(opened, export):
        raise Refused("failed_entry")
    if owner_action(photos):
        raise Refused("owner_action")
    found = HeldProof(Proof(), [], {})
    for row in rows:
        prover = Prover(opened, photos, export, on_disk=True, releasing_managed=releasing_managed)
        try:
            if row.metadata_pending:
                raise Refused("metadata_unrecorded")
            prover.media(row, allow_held=False)
            if hashing:
                prover.hash_all()
        except Refused as refused:
            found.kept[refused.reason] = found.kept.get(refused.reason, 0) + 1
            continue
        found.proof.files.update(prover.proof.files)
        found.ready.append(row)
    for row in found.ready:
        try:
            path = paths.held_copy_at(photos, row.held_path or "", export_name(export))
            if paths.inbox_entry_absent(photos, path):
                continue
            identity = paths.inbox_identity(photos, path)
        except (paths.NotAHeldCopy, paths.OutsideRoot) as error:
            raise Refused("held_differs") from error
        if identity is None:
            raise Refused("held_differs")
        checked = Checked(path, row.sha1 or "", identity, in_the_inbox=True)
        if releasing_managed and hashing:
            hashed(photos, checked)
        found.proof.files[path] = checked
    return found


def same_files(first: Proof, second: Proof) -> bool:
    return first.files.keys() == second.files.keys() and all(first.files[path].identity == found.identity for path, found in second.files.items())


def remove_held(photos: Photos, export: Export, row: TakeoutEntry, proof: Proof) -> int | None:
    path = paths.held_copy_at(photos, row.held_path or "", export_name(export))
    found = proof.files.get(path)
    if found is None:
        return None
    person = (row.held_path or "").split("/")[1]
    try:
        return paths.remove_held_copy(photos, person, export_name(export), path.name, found.identity)
    except paths.NotAHeldCopy as error:
        raise Refused("changed") from error


def keep_held(opened: Session, export: Export, kept_rows: dict[str, int], now: float) -> None:
    for reason, count in sorted(kept_rows.items()):
        if note_once(opened, now, HELD_KEPT, f"export {export.id}", f"{count} held copies: {reason}"):
            monitoring.log("held copies kept", export=export.id, reason=reason, count=count)


def recover_claim(photos: Photos, path: Path, export: Export) -> None:
    if paths.put_back_claimed(photos, path):
        monitoring.log("a file claimed before was put back", export=export.id)


def recover_held_claims(photos: Photos, export: Export, rows: list[TakeoutEntry]) -> None:
    for row in rows:
        try:
            path = paths.held_copy_at(photos, row.held_path or "", export_name(export))
        except (paths.NotAHeldCopy, paths.OutsideRoot):
            continue
        recover_claim(photos, path, export)


def ledger_kept(opened: Session, photos: Photos, export: Export, rows: list[TakeoutEntry]) -> dict[str, int] | None:
    if export.status != IMPORTED:
        raise Refused("export_busy")
    shared_checks(opened, export)
    if any_failed(opened, export):
        raise Refused("failed_entry")
    kept_rows: dict[str, int] = {}
    for row in rows:
        try:
            if row.metadata_pending:
                raise Refused("metadata_unrecorded")
            Prover(opened, photos, export, on_disk=False).media(row, allow_held=False)
        except Refused as refused:
            kept_rows[refused.reason] = kept_rows.get(refused.reason, 0) + 1
            continue
        return None
    return kept_rows


def claims_may_be_left(photos: Photos, export: Export, rows: list[TakeoutEntry]) -> bool:
    folders = set()
    for row in rows:
        try:
            folders.add(paths.held_copy_at(photos, row.held_path or "", export_name(export)).parent)
        except (paths.NotAHeldCopy, paths.OutsideRoot):
            return True
    for folder in folders:
        try:
            if paths.claims_left_in(photos, folder):
                return True
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, paths.OutsideRoot, paths.LinkRefused):
            return True
    return False


def all_kept_on_the_ledger(opened: Session, photos: Photos, export: Export, rows: list[TakeoutEntry], now: float, report: FreeReport) -> bool:
    if any(row.held_state == FREEING for row in rows) or claims_may_be_left(photos, export, rows):
        return False
    try:
        kept_rows = ledger_kept(opened, photos, export, rows)
    except Refused as refused:
        kept(opened, export, "held copies", HELD_KEPT, refused.reason, now)
        report.kept += 1
        opened.commit()
        return True
    if kept_rows is None:
        return False
    keep_held(opened, export, kept_rows, now)
    report.kept += sum(kept_rows.values())
    opened.commit()
    return True


def free_held(opened: Session, photos: Photos, person: Person, export: Export, now: float, report: FreeReport) -> None:
    opened.commit()
    opened.expire_all()
    rows = held_rows(opened, export)
    if not rows or all_kept_on_the_ledger(opened, photos, export, rows, now, report):
        return
    if kept_by_switch(opened, photos, person, export, "held copies", HELD_KEPT, now, report):
        recover_held_claims(photos, export, held_rows(opened, export))
        return
    opened.expire_all()
    rows = held_rows(opened, export)
    recover_held_claims(photos, export, rows)
    try:
        first = prove_held(opened, photos, export, rows, hashing=True)
        chosen = {row.id for row in first.ready}
        for row in rows:
            if row.id in chosen:
                row.held_state = FREEING
            elif row.held_state == FREEING:
                row.held_state, row.held_freed_at = (FREED, now) if held_gone(photos, export, row) else (None, None)
        keep_held(opened, export, first.kept, now)
        opened.commit()
        switch_guard(opened, photos, person, export)
        freeing = [row for row in held_rows(opened, export) if row.held_state == FREEING]
        second = prove_held(opened, photos, export, freeing, hashing=False)
        if {row.id for row in second.ready} != {row.id for row in freeing} or not same_files(first.proof, second.proof):
            raise Refused("changed")
        for row in freeing:
            remove_held(photos, export, row, first.proof)
            if not held_gone(photos, export, row):
                raise Refused("changed")
            row.held_state, row.held_freed_at = FREED, now
            opened.flush()
    except Refused as refused:
        opened.rollback()
        db.write_lock(opened)
        opened.expire_all()
        for row in held_rows(opened, export):
            if row.held_state == FREEING and not held_gone(photos, export, row):
                row.held_state = None
            elif row.held_state == FREEING:
                row.held_state, row.held_freed_at = FREED, now
        kept(opened, export, "held copies", HELD_KEPT, refused.reason, now)
        report.kept += 1
        opened.commit()
        return
    if freeing:
        events.record(opened, now, "export_held_freed", f"export {export.id}", f"{len(freeing)} held copies")
    report.held += len(freeing)
    report.kept += sum(first.kept.values())
    opened.commit()
    monitoring.log("held copies freed", export=export.id, count=len(freeing))


def held_gone(photos: Photos, export: Export, row: TakeoutEntry) -> bool:
    try:
        return paths.inbox_entry_absent(photos, paths.held_copy_at(photos, row.held_path or "", export_name(export)))
    except (paths.NotAHeldCopy, paths.OutsideRoot):
        return False


def proved_held(opened: Session, photos: Photos, export: Export, row: TakeoutEntry, path: Path, *, hashing: bool) -> Proof:
    if claims_may_be_left(photos, export, [row]):
        raise Refused("owner_action")
    held = prove_held(opened, photos, export, [row], hashing=hashing, releasing_managed=True)
    if held.kept:
        raise Refused(next(iter(held.kept)))
    if path not in held.proof.files:
        raise Refused("held_missing")
    return held.proof


def release_held(opened: Session, photos: Photos, person: Person, export: Export, row: TakeoutEntry, settings: config.Settings,
                 now: float, report: FreeReport) -> bool:
    started = time.monotonic()
    lock_release(opened, person, export, row)
    try:
        try:
            path = paths.held_copy_path(photos, person.slug, export_name(export), part_reader.scratch_name(row))
        except (paths.NotAHeldCopy, paths.OutsideRoot) as error:
            raise Refused("held_differs") from error
        if row.held_path != path.relative_to(photos.root).as_posix():
            raise Refused("held_differs")
        recover_claim(photos, path, export)
        recovering = row.held_state == FREEING and row.held_release_point_id is not None and held_gone(photos, export, row)
        if not recovering:
            opened.commit()
            release = release_for(opened, settings, person, row, now)
            first = proved_held(opened, photos, export, row, path, hashing=True)
            opened.commit()
            lock_release(opened, person, export, row)
            if row.held_state not in (None, FREEING):
                raise Refused("changed")
            row.held_state, row.held_release_point_id = FREEING, release.point_id
            opened.commit()
            lock_release(opened, person, export, row)
            if row.held_state != FREEING or row.held_release_point_id != release.point_id:
                raise Refused("changed")
            recheck_release(opened, settings, person, row, now + (time.monotonic() - started), release)
            second = proved_held(opened, photos, export, row, path, hashing=False)
            if not same_files(first, second):
                raise Refused("changed")
            confirm_release(settings, person, release, photos, second, now + (time.monotonic() - started))
            remove_held(photos, export, row, first)
            if not held_gone(photos, export, row):
                raise Refused("changed")
            release_availability(opened, settings, person, now, waiting=False)
    except Refused as refused:
        opened.rollback()
        lock_release(opened, person, export, row)
        if row.held_state == FREEING:
            row.held_state, row.held_release_point_id = None, None
        if refused.reason != retention_gate.RETENTION_CLOCK:
            kept(opened, export, "held copy", HELD_KEPT, refused.reason, now)
            report.kept += 1
        note_release_refusal(opened, settings, person, export, refused, now)
        opened.commit()
        return refused.stop
    row.held_state, row.held_freed_at = FREED, now
    events.record(opened, now, "export_held_freed", f"export {export.id}", "1 held copy")
    report.held += 1
    opened.commit()
    return False


def hold_location(photos: Photos, person: Person, export: Export, review: ReviewItem) -> Path:
    try:
        path = paths.review_hold_at(photos, review.hold_path or "", export_name(export))
    except (paths.NotAReviewHold, paths.OutsideRoot) as error:
        raise Refused("hold_differs") from error
    if (review.hold_path or "").split("/")[1] != person.slug:
        raise Refused("hold_differs")
    return path


def hold_shared(opened: Session, review: ReviewItem) -> bool:
    return opened.execute(select(ReviewItem.id).where(ReviewItem.hold_path == review.hold_path, ReviewItem.id != review.id).limit(1)).first() is not None


def zips_need_hold(opened: Session, export: Export, review: ReviewItem) -> bool:
    roots = select(TakeoutEntry.id).where(TakeoutEntry.export_id == export.id, TakeoutEntry.kind == "media", TakeoutEntry.decision_id == review.decision_id)
    numbers = select(TakeoutEntry.part_number).where(TakeoutEntry.export_id == export.id, TakeoutEntry.id.in_(roots) | TakeoutEntry.copy_of_id.in_(roots))
    zip_left = opened.execute(select(ExportPart.id).where(
        ExportPart.export_id == export.id, ExportPart.number.in_(numbers), ExportPart.filename.is_not(None) | (ExportPart.free_state == FREEING),
        ExportPart.free_state.is_distinct_from(FREED)).limit(1)).first() is not None
    held_left = opened.execute(select(TakeoutEntry.id).where(
        TakeoutEntry.export_id == export.id, TakeoutEntry.held_path.is_not(None), TakeoutEntry.held_state.is_distinct_from(FREED),
        TakeoutEntry.id.in_(roots) | TakeoutEntry.copy_of_id.in_(roots)).limit(1)).first() is not None
    return zip_left or held_left


def recorded_hold(opened: Session, export: Export, review: ReviewItem) -> bool:
    return review.hold_sha1 is not None and opened.execute(select(TakeoutEntry.id).where(
        TakeoutEntry.export_id == export.id, TakeoutEntry.kind == "media", TakeoutEntry.copy_of_id.is_(None), TakeoutEntry.decision_id == review.decision_id,
        TakeoutEntry.sha1 == review.hold_sha1).limit(1)).first() is not None


def proved_hold(opened: Session, photos: Photos, export: Export, review: ReviewItem, path: Path, *, hashing: bool) -> Proof:
    if export.status != IMPORTED:
        raise Refused("export_busy")
    shared_checks(opened, export, True)
    try:
        claimed = paths.claims_left_in(photos, path.parent)
    except (OSError, paths.OutsideRoot):
        claimed = True
    if owner_action(photos) or claimed:
        raise Refused("owner_action")
    if not recorded_hold(opened, export, review):
        raise Refused("review_differs")
    if hold_shared(opened, review):
        raise Refused("hold_shared")
    if zips_need_hold(opened, export, review):
        raise Refused("zip_needs_hold")
    try:
        identity = paths.inbox_identity(photos, path)
    except (OSError, paths.OutsideRoot) as error:
        raise Refused("hold_differs") from error
    if identity is None:
        raise Refused("file_missing")
    found = Checked(path, review.hold_sha1 or "", identity, in_the_inbox=True)
    if hashing:
        hashed(photos, found)
    return Proof(files={path: found})


def remove_hold(photos: Photos, person: Person, export: Export, path: Path, proof: Proof) -> None:
    found = proof.files.get(path)
    if found is None:
        raise Refused("hold_differs")
    try:
        paths.remove_review_hold(photos, person.slug, export_name(export), path.name, found.identity)
    except paths.NotAReviewHold as error:
        raise Refused("changed") from error


def release_review_hold(opened: Session, photos: Photos, person: Person, export: Export, review: ReviewItem, settings: config.Settings,
                        now: float, report: FreeReport) -> bool:
    started = time.monotonic()
    lock_release(opened, person, export, review)
    try:
        path = hold_location(photos, person, export, review)
        recover_claim(photos, path, export)
        recovering = review.hold_free_state == FREEING and review.hold_release_point_id is not None and paths.inbox_entry_absent(photos, path)
        if not recovering:
            opened.commit()
            release = release_for(opened, settings, person, review, now)
            first = proved_hold(opened, photos, export, review, path, hashing=True)
            opened.commit()
            lock_release(opened, person, export, review)
            if review.hold_free_state not in (None, FREEING):
                raise Refused("changed")
            review.hold_free_state, review.hold_release_point_id = FREEING, release.point_id
            opened.commit()
            lock_release(opened, person, export, review)
            if review.hold_free_state != FREEING or review.hold_release_point_id != release.point_id:
                raise Refused("changed")
            recheck_release(opened, settings, person, review, now + (time.monotonic() - started), release)
            second = proved_hold(opened, photos, export, review, path, hashing=False)
            if not same_files(first, second):
                raise Refused("changed")
            confirm_release(settings, person, release, photos, second, now + (time.monotonic() - started))
            remove_hold(photos, person, export, path, first)
            if not paths.inbox_entry_absent(photos, path):
                raise Refused("changed")
            release_availability(opened, settings, person, now, waiting=False)
    except (OSError, paths.OutsideRoot) as error:
        opened.rollback()
        report.failed += 1
        monitoring.log("review hold freeing failed", export=export.id, review=review.id, error=type(error).__name__)
        note_once(opened, now, "export_hold_free_failed", f"export {export.id}", f"review {review.id}: {type(error).__name__}")
        opened.commit()
        return False
    except Refused as refused:
        opened.rollback()
        lock_release(opened, person, export, review)
        if review.hold_free_state == FREEING:
            review.hold_free_state, review.hold_release_point_id = None, None
        if refused.reason != retention_gate.RETENTION_CLOCK:
            kept(opened, export, f"review {review.id}", HOLD_KEPT, refused.reason, now)
            report.kept += 1
        note_release_refusal(opened, settings, person, export, refused, now)
        opened.commit()
        return refused.stop
    review.hold_free_state, review.hold_path = FREED, None
    events.record(opened, now, "export_hold_freed", f"export {export.id}", f"review {review.id}")
    report.holds += 1
    opened.commit()
    monitoring.log("review hold freed", export=export.id, review=review.id)
    return False


def release_holds(opened: Session, export: Export) -> list[ReviewItem]:
    return list(opened.execute(select(ReviewItem).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id).where(
        DedupeDecision.export_id == export.id, ReviewItem.hold_path.is_not(None), ReviewItem.status != "pending",
        ReviewItem.hold_free_state.is_(None) | (ReviewItem.hold_free_state == FREEING)).order_by(ReviewItem.id)).scalars())


def recover_hold_claims(photos: Photos, person: Person, export: Export, reviews: list[ReviewItem]) -> None:
    for review in reviews:
        try:
            recover_claim(photos, hold_location(photos, person, export, review), export)
        except (Refused, OSError, paths.OutsideRoot):
            continue


def candidates(opened: Session) -> list[int]:
    parts = select(ExportPart.export_id).where(ExportPart.ingest_state == part_ingest.INGESTED,
                                               ExportPart.free_state.is_(None) | (ExportPart.free_state == FREEING))
    held = select(TakeoutEntry.export_id).where(TakeoutEntry.held_path.is_not(None), TakeoutEntry.held_state.is_(None) | (TakeoutEntry.held_state == FREEING))
    holds = select(DedupeDecision.export_id).join(ReviewItem, ReviewItem.decision_id == DedupeDecision.id).join(
        ManagedImportIntent, (ManagedImportIntent.export_id == DedupeDecision.export_id) & (ManagedImportIntent.kind == "import")).where(
        ReviewItem.hold_path.is_not(None), ReviewItem.status != "pending", ReviewItem.hold_free_state.is_(None) | (ReviewItem.hold_free_state == FREEING))
    found = set(opened.execute(parts).scalars()) | set(opened.execute(held).scalars()) | set(opened.execute(holds).scalars())
    return sorted(found)


def free_export(opened: Session, photos: Photos, person: Person, export: Export, now: float, report: FreeReport,
                settings: config.Settings, started: float) -> bool:
    managed_settings = settings if managed(opened, export) else None
    for part in list(opened.execute(select(ExportPart).where(ExportPart.export_id == export.id).order_by(ExportPart.number)).scalars()):
        if part.ingest_state != part_ingest.INGESTED or part.free_state == FREED:
            continue
        if part.free_state is None and (not in_export_folder(photos, person, export, part) or not zip_present(photos, person, export, part)):
            continue
        part_at = now if managed_settings is None else now + (time.monotonic() - started)
        if free_part(opened, photos, person, export, part, part_at, report, managed_settings):
            return True
    if export.status == IMPORTED:
        if managed_settings is None:
            free_held(opened, photos, person, export, now, report)
        else:
            for row in held_rows(opened, export):
                if release_held(opened, photos, person, export, row, managed_settings, now + (time.monotonic() - started), report):
                    return True
            reviews = release_holds(opened, export)
            recover_hold_claims(photos, person, export, reviews)
            for review in reviews:
                if release_review_hold(opened, photos, person, export, review, managed_settings, now + (time.monotonic() - started), report):
                    return True
    return False


def free_ready(opened: Session, settings: config.Settings, now: float) -> FreeReport:
    report = FreeReport()
    photos = Photos(settings.photos_root)
    stopped_people: set[int] = set()
    started = time.monotonic()
    for export_id in candidates(opened):
        export = opened.get(Export, export_id)
        if export is None:
            continue
        try:
            with run.account_lock(settings, export.account_id):
                person = run.owner_person(opened, export)
                if person.id in stopped_people and managed(opened, export):
                    continue
                if free_export(opened, photos, person, export, now, report, settings, started):
                    stopped_people.add(person.id)
        except run.Busy:
            continue
        except Exception as error:
            opened.rollback()
            report.failed += 1
            monitoring.log("zip freeing failed", export=export_id, error=type(error).__name__)
            note_once(opened, now, "export_free_failed", f"export {export_id}", type(error).__name__)
            opened.commit()
    return report
