from __future__ import annotations

import contextlib
import os
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Final

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from darkroom import config, dates, monitoring
from darkroom.domain import alerts, dedupe, events, exports, managed_switch, media, pixel_folder, takeout_copies
from darkroom.domain.ingest import heartbeat, metadata, pairing, part_pairing, part_reader, place, run, sidecar, unpack
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool
from darkroom.storage import db, paths
from darkroom.storage.models import INGEST_FAILURE_TYPE_LIMIT, DedupeDecision, Export, ExportPart, MediaItem, Person, TakeoutEntry, TakeoutSidecar
from darkroom.storage.paths import Photos

COMMIT_HOOK: Callable[[], None] | None = None

NOT_QUEUED: Final = "not_queued"
QUEUED: Final = "queued"
INGESTING: Final = "ingesting"
INGESTED: Final = "ingested"
REFUSED: Final = "refused"
FAILED_PART: Final = "failed"
LISTED: Final = part_pairing.LISTED
WAITING_SIDECAR: Final = part_pairing.WAITING_SIDECAR
WAITING_EXPORT: Final = part_pairing.WAITING_EXPORT
PLACED: Final = part_pairing.PLACED
REVIEW: Final = part_pairing.REVIEW
SKIPPED: Final = part_pairing.SKIPPED
FAILED: Final = part_pairing.FAILED
RECORDED: Final = "recorded"
HELD: Final = (WAITING_SIDECAR, WAITING_EXPORT)
FINISHED_EXPORTS: Final = frozenset({"imported", "failed", "expired"})
UNNAMED_FAILURE: Final = "Exception"
FINGERPRINT_WORKERS: Final = 12
DECODE_FILE_SECONDS: Final = 120.0
DECODE_BATCH_SECONDS: Final = 1800.0
DECODE_ROUNDS: Final = 3


def fingerprint_workers() -> int:
    return min(FINGERPRINT_WORKERS, os.cpu_count() or 1)


def decode_seconds(files: int) -> float:
    return min(DECODE_BATCH_SECONDS, DECODE_FILE_SECONDS * max(files, 1))


def timed_out() -> media.Decoded:
    error = media.DecodeTimedOut("the file took too long to fingerprint")
    return media.Decoded(error, error)


@dataclass
class PartIngest:
    ingested: bool = False
    refused: bool = False
    placed: int = 0
    review: int = 0
    skipped: int = 0
    held: int = 0
    failed: int = 0
    rescanned: int = 0


@dataclass(frozen=True)
class Target:
    entry: TakeoutEntry
    sidecar_id: int | None


def queue(opened: Session, photos: Photos | None, export: Export, part: ExportPart, now: float) -> None:
    try:
        route = run.admit_import(opened, photos, export)
    except (managed_switch.Refused, managed_switch.Paused):
        return
    if route == run.PER_PART and (not part.ingest_state or part.ingest_state == NOT_QUEUED):
        part.ingest_state, part.ingest_queued_at = QUEUED, now
    export.ingest_route = route


def creation_key(export: Export) -> tuple[float, int]:
    return dates.instant_or_none(export.created_at) or 0.0, export.id


def waits_for_earlier(opened: Session, export: Export) -> bool:
    mine = creation_key(export)
    others = opened.execute(
        select(Export).where(Export.account_id == export.account_id, Export.id != export.id, Export.status.not_in(FINISHED_EXPORTS))
    ).scalars()
    return any(creation_key(other) < mine for other in others)


def queued_parts(opened: Session) -> list[tuple[int, int]]:
    rows = opened.execute(
        select(Export, ExportPart).join(ExportPart, ExportPart.export_id == Export.id)
        .where(ExportPart.ingest_state.in_((QUEUED, INGESTING)), ExportPart.status == "done", Export.status != "imported")
    ).all()
    ordered = sorted(rows, key=lambda row: (row[0].account_id, creation_key(row[0]), row[1].number))
    return [(export.id, part.number) for export, part in ordered]


def part_of(opened: Session, export_id: int, number: int) -> ExportPart | None:
    return opened.execute(select(ExportPart).where(ExportPart.export_id == export_id, ExportPart.number == number)).scalar_one_or_none()


@contextlib.contextmanager
def listed_part(photos: Photos, export: Export, part: ExportPart, person: Person) -> Iterator[tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]]:
    path = unpack.part_path(export, part, photos, person)
    with unpack.open_part(photos, path, part.number) as handle, part_reader.zip_of(handle, path, part.number) as archive:
        yield archive, {entry.name: entry.info for entry in part_reader.entry_listing(archive.infolist())}


@contextlib.contextmanager
def resolution_copy(photos: Photos, export: Export, part: ExportPart, person: Person, entry: TakeoutEntry) -> Iterator[Path]:
    if entry.held_path is not None:
        yield paths.held_copy_at(photos, entry.held_path, unpack.staging_name(export))
        return
    try:
        with listed_part(photos, export, part, person) as (archive, infos):
            info = infos.get(entry.name)
            if info is None:
                raise part_reader.PartRefused("listing_differs", "the part no longer lists the photo it held")
            yield part_reader.stream_one(photos, person, export, archive, entry, info).path
    except part_reader.PartRefused:
        raise unpack.UnpackFailed("the held photo could not be reread from the part") from None
    finally:
        part_reader.sweep_part_scratch(photos, person, export)


def copy_archived_media(opened: Session, photos: Photos, export: Export, person: Person, entry: TakeoutEntry, destination: BinaryIO) -> None:
    part = part_of(opened, export.id, entry.part_number)
    unavailable = "the archived copy could not be read"
    if entry.export_id != export.id or entry.kind != "media" or part is None or part.ingest_state != INGESTED or part.free_state is not None:
        raise OSError(unavailable)
    path = unpack.part_path(export, part, photos, person)
    try:
        with unpack.open_part(photos, path, part.number) as handle, part_reader.zip_of(handle, path, part.number) as archive:
            listed = part_reader.entry_listing(archive.infolist())
            found = next((item for item in listed if item.name == entry.name and item.kind == "media"), None)
            if found is None or (found.bytes, found.crc32) != (entry.bytes, entry.crc32):
                raise OSError(unavailable)
            for chunk in part_reader.entry_chunks(archive, found.info, part.number, [0]):
                destination.write(chunk)
    except (unpack.UnpackFailed, part_reader.PartRefused):
        raise OSError(unavailable) from None


def run_queued(opened: Session, settings: config.Settings, now: float, tools: run.Tools | None = None,
               beat: heartbeat.Heartbeat | None = None) -> list[tuple[int, int]]:
    ran = []
    for export_id, number in queued_parts(opened):
        export, part = opened.get(Export, export_id), part_of(opened, export_id, number)
        if export is None or part is None:
            continue
        if waits_for_earlier(opened, export):
            monitoring.log("part ingest waiting for an earlier export", export=export_id, part=number)
            continue
        try:
            ingest_part(opened, settings, export, part, now, tools, beat)
        except (run.Busy, run.NotReady) as error:
            monitoring.log("part ingest skipped", export=export_id, part=number, reason=type(error).__name__)
            continue
        except Exception as error:
            failed(opened, settings, export_id, number, error, now, tools or run.Tools())
            continue
        ran.append((export_id, number))
    return ran


def failure_code(error: BaseException) -> str:
    if isinstance(error, OSError):
        return "files"
    if isinstance(error, SQLAlchemyError):
        return "database"
    if isinstance(error, (exiftool.Missing, exiftool.Unreadable, exiftool.WriteFailed)):
        return "metadata"
    if isinstance(error, place.PlaceFailed):
        return "placing"
    if isinstance(error, MemoryError):
        return "memory"
    return "unexpected"


def failure_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if name.isascii() and name.isidentifier() and len(name) <= INGEST_FAILURE_TYPE_LIMIT else UNNAMED_FAILURE


def failed(opened: Session, settings: config.Settings, export_id: int, number: int, error: Exception, now: float, tools: run.Tools) -> None:
    opened.rollback()
    export, part = opened.get(Export, export_id), part_of(opened, export_id, number)
    if part is not None:
        part.ingest_state, part.ingest_failure, part.ingest_failure_type = FAILED_PART, failure_code(error), failure_type(error)
    events.record(opened, now, "export_part_ingest_failed", f"export {export_id}", f"part {number}: {failure_type(error)}")
    alerts.open_alert(opened, settings, f"part:failed:{export_id}:{number}", "failure", f"export {export_id}",
                      alerts.failure(f"Ingesting part {number} of export {export_id}"), None, now, notifier=tools.notifier,
                      about=alerts.NOBODY if export is None else alerts.account(export.account_id))
    opened.commit()
    monitoring.log("part ingest failed", export=export_id, part=number, error=type(error).__name__)


def refuse(opened: Session, settings: config.Settings, export: Export, part: ExportPart, refused: part_reader.PartRefused, now: float,
           tools: run.Tools) -> PartIngest:
    opened.rollback()
    part.ingest_state, part.ingest_refusal = REFUSED, refused.reason
    events.record(opened, now, "export_part_refused", f"export {export.id}", f"part {part.number}: {refused.public}")
    alerts.open_alert(opened, settings, f"part:refused:{export.id}:{part.number}", "failure", f"export {export.id}",
                      alerts.failure(f"Reading part {part.number} of export {export.id}"), None, now, notifier=tools.notifier,
                      about=alerts.account(export.account_id))
    opened.commit()
    monitoring.log("part refused", export=export.id, part=part.number, reason=refused.reason)
    return PartIngest(refused=True)


def load_index(opened: Session, export: Export, person: Person) -> dedupe.PersonIndex:
    index = dedupe.PersonIndex.load(opened, export.account_id, person.id)
    own = set(opened.execute(select(MediaItem.id).where(MediaItem.export_id == export.id)).scalars())
    return replace(index, takeout=tuple(shot for shot in index.takeout if shot.id not in own))


class Ingest:
    def __init__(self, opened: Session, settings: config.Settings, photos: Photos, export: Export, part: ExportPart, person: Person, now: float,
                 tools: run.Tools, stack: contextlib.ExitStack, beat: heartbeat.Heartbeat | None = None) -> None:
        self.opened, self.settings, self.photos, self.export, self.part, self.person = opened, settings, photos, export, part, person
        self.now, self.tools, self.stack = now, tools, stack
        self.beat = beat or heartbeat.quiet()
        self.index = load_index(opened, export, person)
        self.dedupe_settings = dedupe.load_settings(opened)
        self.changed: set[int] = set()
        self.scratch: dict[int, Path] = {}
        self.measured: dict[int, MediaItem] = {}
        self.used: dict[int, int | None] = {}
        self.texts: dict[int, str] = {}
        self.slot: tuple[int, contextlib.ExitStack, zipfile.ZipFile, dict[str, zipfile.ZipInfo]] | None = None
        self.managed = place.managed_import(opened, export)
        self.sweep_placements()
        stack.callback(self.sweep_placements)
        stack.callback(self.close_archive)
        self.pool = self.new_pool()
        self.abandoned = 0
        stack.callback(self.close_pool)

    def new_pool(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=fingerprint_workers(), thread_name_prefix=heartbeat.WORKER_PREFIX)

    def close_pool(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)

    def abandon_pool(self, stuck: int) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.abandoned += stuck
        self.pool = self.new_pool()
        monitoring.log("fingerprint workers left behind", export=self.export.id, part=self.part.number, stuck=stuck, total=self.abandoned)

    def commit(self) -> None:
        if COMMIT_HOOK is not None:
            COMMIT_HOOK()
        self.opened.commit()

    def lock(self) -> None:
        db.write_lock(self.opened)
        self.opened.expire_all()

    def media_rows(self) -> list[TakeoutEntry]:
        rows = self.opened.execute(select(TakeoutEntry).where(TakeoutEntry.export_id == self.export.id, TakeoutEntry.kind == "media")).scalars()
        return part_pairing.listed_media(rows)

    def sidecar_rows(self) -> list[TakeoutSidecar]:
        rows = list(self.opened.execute(select(TakeoutSidecar).where(TakeoutSidecar.export_id == self.export.id)).scalars())
        self.texts = {row.entry_id: row.text for row in rows}
        return rows

    def fail(self, entry: TakeoutEntry, stage: str, error: BaseException) -> None:
        entry.outcome, entry.done_at, entry.metadata_pending = FAILED, self.now, False
        run.item_failed(self.opened, self.export, stage, error, self.now)
        self.progress()

    def progress(self) -> None:
        query = select(TakeoutEntry.outcome, func.count()).where(TakeoutEntry.export_id == self.export.id, TakeoutEntry.part_number == self.part.number
                                                                 ).group_by(TakeoutEntry.outcome)
        counted(self.part, Counter({outcome: count for outcome, count in self.opened.execute(query).all()}))
        self.beat.counted(self.part.entries_done or 0, self.part.entries or 0)

    def sweep_placements(self) -> None:
        paths.sweep_placements(self.photos, self.person.slug, unpack.staging_name(self.export))

    def close_archive(self) -> None:
        if self.slot is not None:
            self.slot[1].close()
            self.slot = None

    def archive_of(self, number: int) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
        if self.slot is not None and self.slot[0] == number:
            return self.slot[2], self.slot[3]
        self.close_archive()
        part = part_of(self.opened, self.export.id, number)
        if part is None:
            raise unpack.UnpackFailed("the part is not in the ledger")
        with contextlib.ExitStack() as opening:
            archive, infos = opening.enter_context(listed_part(self.photos, self.export, part, self.person))
            self.slot = number, opening.pop_all(), archive, infos
        return archive, infos

    def release_scratch(self) -> None:
        part_reader.sweep_part_scratch(self.photos, self.person, self.export)
        self.scratch.clear()
        self.measured.clear()
        self.used.clear()

    def in_batches(self, entries: list[TakeoutEntry]) -> Iterator[list[TakeoutEntry]]:
        return part_reader.batches_of(entries, part_reader.BATCH_FILES, part_reader.BATCH_BYTES)

    def in_grouped_batches(self, groups: Sequence[Sequence[TakeoutEntry]]) -> Iterator[list[Sequence[TakeoutEntry]]]:
        return part_reader.grouped_batches_of(groups, part_reader.BATCH_FILES, part_reader.BATCH_BYTES)

    def freed(self, number: int) -> bool:
        part = part_of(self.opened, self.export.id, number)
        return part is not None and part.free_state is not None

    def stored_bytes(self, entry: TakeoutEntry) -> bytes | None:
        text = self.opened.execute(select(TakeoutSidecar.text).where(TakeoutSidecar.entry_id == entry.id)).scalar_one_or_none()
        return None if text is None else text.encode("utf-8")

    def sidecar_bytes(self, entry: TakeoutEntry) -> bytes | None:
        if self.freed(entry.part_number):
            return self.stored_bytes(entry)
        archive, infos = self.archive_of(entry.part_number)
        info = infos.get(entry.name)
        if info is None:
            return None
        try:
            with archive.open(info) as handle:
                return handle.read(sidecar.LIMIT + 1)
        except part_reader.READ_ERRORS:
            return None

    def sidecar_state(self, entry: TakeoutEntry) -> run.SidecarState:
        data = self.sidecar_bytes(entry)
        decoded = None if data is None else sidecar.decode(data)
        if decoded is None or sidecar.parse(decoded.text) is None:
            return "unreadable"
        return "repaired" if decoded.repaired else "read"

    def held_copy(self, entry: TakeoutEntry, held: str) -> Path:
        path = paths.held_copy_at(self.photos, held, unpack.staging_name(self.export))
        if entry.sha1 is None or paths.inbox_sha1(self.photos, path) != entry.sha1:
            raise part_reader.PartRefused("bytes_differ", f"a held copy of part {entry.part_number} doesn't match the part read before")
        return path

    def scratch_of(self, entry: TakeoutEntry) -> Path:
        found = self.scratch.get(entry.id)
        if found is None and entry.held_path is not None:
            found = self.scratch[entry.id] = self.held_copy(entry, entry.held_path)
        if found is None:
            archive, infos = self.archive_of(entry.part_number)
            streamed = part_reader.stream_one(self.photos, self.person, self.export, archive, entry, infos[entry.name])
            if entry.sha1 is not None and streamed.sha1 != entry.sha1:
                raise part_reader.PartRefused("bytes_differ", f"part {entry.part_number}'s files don't match the part read before")
            found = self.scratch[entry.id] = streamed.path
        return found

    def measure(self, targets: Sequence[Target]) -> dict[int, MediaItem]:
        sources: list[tuple[Target, Path]] = []
        for target in targets:
            try:
                sources.append((target, self.scratch_of(target.entry)))
            except Exception as error:
                self.fail(target.entry, "metadata", error)
        found: dict[int, MediaItem] = {}
        with (paths.pinned_inbox_files(self.photos, [source for _, source in sources]) as pinned,
              self.decoding({(pinned[source], Path(target.entry.name)) for target, source in sources if source in pinned}) as decoded):
            tags = self.tools.read(list(pinned.values())) if pinned else {}
            ready = decoded()
            for target, source in sources:
                try:
                    found[target.entry.id] = self.fields_from(target, pinned.get(source), tags, ready)
                except Exception as error:
                    self.fail(target.entry, "metadata", error)
        self.measured.update(found)
        self.used.update({entry_id: target.sidecar_id for target in targets if (entry_id := target.entry.id) in found})
        return found

    @contextlib.contextmanager
    def decoding(self, files: set[tuple[Path, Path]]) -> Iterator[Callable[[], dict[tuple[Path, Path], media.Decoded]]]:
        pending: dict[tuple[Path, Path], Future[media.Decoded]] = {file: self.pool.submit(media.decode, *file) for file in sorted(files)}
        deadline = time.monotonic() + decode_seconds(len(pending))

        def left() -> float:
            return max(0.0, deadline - time.monotonic())

        def ready() -> dict[tuple[Path, Path], media.Decoded]:
            nonlocal pending, deadline
            found: dict[tuple[Path, Path], media.Decoded] = {}
            for round_number in range(DECODE_ROUNDS):
                wait(pending.values(), timeout=left())
                unstarted: list[tuple[Path, Path]] = []
                stuck = 0
                for file, future in pending.items():
                    if future.done() and not future.cancelled():
                        found[file] = future.result()
                    elif future.cancel():
                        unstarted.append(file)
                    else:
                        found[file] = timed_out()
                        stuck += 1
                pending = {}
                if stuck:
                    self.abandon_pool(stuck)
                if not unstarted:
                    break
                if round_number == DECODE_ROUNDS - 1:
                    found |= {file: timed_out() for file in unstarted}
                    break
                pending = {file: self.pool.submit(media.decode, *file) for file in unstarted}
                deadline = time.monotonic() + decode_seconds(len(pending))
            return {file: found[file] for file in sorted(files)}

        try:
            yield ready
        finally:
            for future in pending.values():
                future.cancel()
            _, running = wait(pending.values(), timeout=left())
            if running:
                self.abandon_pool(len(running))

    def fields_from(self, target: Target, opened_path: Path | None, tags: dict[Path, dict[str, object]],
                    decoded: Mapping[tuple[Path, Path], media.Decoded]) -> MediaItem:
        entry = target.entry
        if opened_path is None:
            raise paths.LinkRefused("the scratch file is a link or not a plain file")
        text = self.texts.get(target.sidecar_id) if target.sidecar_id is not None else None
        parsed = None if text is None else sidecar.parse(text)
        item = TakeoutItem(Path(entry.name), entry.sha1 or "", (), None)
        fields = metadata.candidate(item, tags.get(opened_path, {}), parsed, self.person.id, run.UNPLACED, self.now, self.export.id, opened_path,
                                    decoded.get((opened_path, Path(entry.name))))
        if entry.sha1 is not None and fields.sha1 != entry.sha1:
            raise place.PlaceFailed("the file changed since it was read")
        entry.phash, entry.captured_utc, entry.captured_local = fields.phash, fields.captured_utc, fields.captured_local
        entry.width, entry.height, entry.duration_ms, entry.make, entry.model = fields.width, fields.height, fields.duration_ms, fields.make, fields.model
        entry.is_video = Path(entry.name).suffix.lower() in media.VIDEO_SUFFIXES
        entry.is_edited = pairing.original_name(Path(entry.name)) is not None
        return fields

    def batch(self, opened: Session, streamed: Sequence[part_reader.Streamed]) -> None:
        self.process({item.entry_id: item.path for item in streamed})

    def settle_rest(self) -> None:
        self.process({})

    def states(self) -> dict[int, str]:
        parts = self.opened.execute(select(ExportPart).where(ExportPart.export_id == self.export.id)).scalars()
        return {part.number: part.ingest_state for part in parts}

    def process(self, sources: dict[int, Path]) -> None:
        self.scratch, self.measured, self.used = dict(sources), {}, {}
        rows = {row.id: row for row in self.media_rows()}
        sidecars = self.sidecar_rows()
        chosen = part_pairing.sidecar_choices(rows.values(), sidecars)
        self.measure([Target(rows[entry_id], chosen[entry_id].entry_id) for entry_id in sorted(sources)])
        self.commit()
        result = part_pairing.pair_part(self.media_rows(), self.sidecar_rows(), self.part.number, self.index)
        self.progress()
        self.commit()
        states = self.states()
        ready = [entry for entry in result.ready if states.get(entry.part_number) in (INGESTING, INGESTED)]
        for entry in [entry for entry in ready if entry.id in sources]:
            self.decide(entry)
        for chunk in part_reader.batches_of([entry for entry in ready if entry.id not in sources], part_reader.BATCH_FILES, part_reader.BATCH_BYTES):
            self.release_scratch()
            wanted = [Target(entry, entry.sidecar_entry_id) for entry in chunk if self.needs_fields(entry)]
            if wanted:
                self.measure(wanted)
                self.commit()
            for entry in chunk:
                if entry.outcome != FAILED:
                    self.decide(entry)
        self.close_archive()
        for entry in result.metadata_due:
            if states.get(entry.part_number) in (INGESTING, INGESTED):
                self.fill(entry)
        self.tally()
        self.commit()

    def fresh(self, entry: TakeoutEntry) -> bool:
        if entry.id in self.measured and self.used.get(entry.id) == entry.sidecar_entry_id:
            return True
        return entry.id in self.measure([Target(entry, entry.sidecar_entry_id)])

    def needs_fields(self, entry: TakeoutEntry) -> bool:
        row = dedupe.stored_decision(self.opened, self.export.id, entry.name)
        if row is None:
            return not part_pairing.is_tier_1(self.index, part_pairing.candidate_of(entry))
        return row.tier in (4, 5) and not self.settled_elsewhere(row)

    def settled_elsewhere(self, row: DedupeDecision) -> bool:
        return row.tier == 5 and (row.takeout_item_id is not None or takeout_copies.resolved_away(self.opened, row.id))

    def decide(self, entry: TakeoutEntry) -> None:
        self.lock()
        row = dedupe.stored_decision(self.opened, self.export.id, entry.name)
        if row is None:
            if not part_pairing.is_tier_1(self.index, part_pairing.candidate_of(entry)) and not self.fresh(entry):
                self.commit()
                return
            decision = dedupe.decide(self.index, part_pairing.candidate_of(entry), self.dedupe_settings)
            if dedupe.repeated(self.opened, decision, self.export.id):
                self.repeat(entry, decision)
                return
            try:
                row = dedupe.apply(self.opened, decision, self.export, self.now)
            except Exception as error:
                self.opened.rollback()
                self.fail(entry, "dedupe", error)
                self.commit()
                return
            self.commit()
        self.place_decided(entry, row)

    def repeat(self, entry: TakeoutEntry, decision: dedupe.Decision) -> None:
        seen = run.seen_at(self.export, self.now)
        if decision.matched_item_id is not None and seen is not None:
            pixel_folder.seen_again(self.opened, self.export.account_id, decision.matched_item_id, decision.person_id, seen)
        entry.outcome, entry.metadata_pending, entry.done_at = SKIPPED, False, self.now
        self.progress()
        self.commit()

    def light_fields(self, entry: TakeoutEntry, settled: bool = False) -> MediaItem:
        waiting = entry.metadata_pending and not settled
        text = self.texts.get(entry.sidecar_entry_id) if entry.sidecar_entry_id is not None and not waiting else None
        parsed = None if text is None else sidecar.parse(text)
        return MediaItem(sidecar_json=None if parsed is None else parsed.raw, sidecar_from_still=False)

    def inputs(self, entry: TakeoutEntry, row: DedupeDecision) -> tuple[TakeoutItem, MediaItem] | None:
        if row.tier in (4, 5):
            if not self.fresh(entry):
                return None
            fields = self.measured[entry.id]
        else:
            fields = self.light_fields(entry)
        return TakeoutItem(self.scratch.get(entry.id, Path(entry.name)), entry.sha1 or "", (), None), fields

    def place_decided(self, entry: TakeoutEntry, row: DedupeDecision, named: Path | None = None) -> None:
        self.lock()
        if self.settled_elsewhere(row) or (self.managed and row.tier != 4):
            self.finish_entry(entry, row)
            return
        new_row = row.tier == 5 and row.takeout_item_id is None
        given = self.inputs(entry, row)
        if given is None:
            self.commit()
            return
        try:
            with self.copied_ahead(row, given) as ahead:
                if ahead is not None and self.settled_elsewhere(row):
                    self.finish_entry(entry, row)
                    return
                done = place.place(self.opened, self.photos, self.export,
                                   [place.Entry(given[0], given[1], row, Path(entry.name) if named is None else named)],
                                   self.tools.read, self.tools.write, ahead=ahead)
        except Exception as error:
            if isinstance(error, OperationalError) and db.is_busy(error):
                raise
            self.opened.rollback()
            self.fail(entry, "place", error)
            self.commit()
            return
        self.refreshed(done)
        self.mark_changed(row, done, new_row)
        self.finish_entry(entry, row)

    @contextlib.contextmanager
    def copied_ahead(self, row: DedupeDecision, given: tuple[TakeoutItem, MediaItem]) -> Iterator[paths.StagedPlacement | None]:
        if not place.copies_into_google_only(row, self.export):
            yield None
            return
        source, sha1 = given[0].path, given[1].sha1
        self.commit()
        with paths.staged_placement(self.photos, self.person.slug, unpack.staging_name(self.export), place.staged_chunks(self.photos, source),
                                    sha1) as ahead:
            self.lock()
            yield ahead

    def refreshed(self, done: place.PlaceReport) -> None:
        for item_id in done.refreshed:
            events.record(self.opened, self.now, "metadata_refreshed", f"item {item_id}", "description or location changed in Google")

    def mark_changed(self, row: DedupeDecision, done: place.PlaceReport, new_row: bool) -> None:
        owner = run.library_owner(self.opened, row)
        if owner is not None and (done.placed or done.sidecars or done.refreshed or (new_row and row.takeout_item_id is not None)):
            self.changed.add(owner)

    def finish_entry(self, entry: TakeoutEntry, row: DedupeDecision) -> None:
        entry.outcome = PLACED if self.managed and place.copies_into_google_only(row, self.export) else part_pairing.decided_outcome(row.tier, row.takeout_item_id)
        if row.tier == 5:
            entry.item_id = row.takeout_item_id
        entry.decision_id, entry.done_at = row.id, self.now
        self.progress()
        self.commit()

    def fill(self, entry: TakeoutEntry) -> None:
        self.lock()
        row = self.opened.get(DedupeDecision, entry.decision_id) if entry.decision_id is not None else None
        if row is None:
            return
        if self.managed:
            entry.metadata_pending, entry.done_at = False, self.now
            self.commit()
            return
        item = TakeoutItem(Path(entry.name), entry.sha1 or "", (), None)
        try:
            done = place.place(self.opened, self.photos, self.export, [place.Entry(item, self.light_fields(entry, True), row, Path(entry.name))],
                               self.tools.read, self.tools.write)
        except Exception as error:
            self.opened.rollback()
            self.fail(entry, "place", error)
            self.commit()
            return
        self.refreshed(done)
        self.mark_changed(row, done, False)
        entry.metadata_pending, entry.done_at = False, self.now
        self.commit()

    def tally(self) -> None:
        counts: dict[int, Counter[str]] = {}
        query = select(TakeoutEntry.part_number, TakeoutEntry.outcome, func.count()).where(TakeoutEntry.export_id == self.export.id).group_by(
            TakeoutEntry.part_number, TakeoutEntry.outcome)
        for number, outcome, count in self.opened.execute(query).all():
            counts.setdefault(number, Counter())[outcome] += count
        parts = self.opened.execute(select(ExportPart).where(ExportPart.export_id == self.export.id)).scalars()
        for part in parts:
            if part.number == self.part.number or part.ingest_state == INGESTED:
                counted(part, counts.get(part.number, Counter()))

    def changed_since_queued(self) -> set[int]:
        query = select(TakeoutEntry.decision_id).where(TakeoutEntry.export_id == self.export.id, TakeoutEntry.decision_id.is_not(None),
                                                       TakeoutEntry.done_at >= (self.part.ingest_queued_at or 0.0))
        owners = set()
        for decision_id in self.opened.execute(query).scalars():
            row = self.opened.get(DedupeDecision, decision_id)
            owner = None if row is None else run.library_owner(self.opened, row)
            if owner is not None:
                owners.add(owner)
        return owners

    def finish(self, resumed: bool) -> PartIngest:
        self.opened.execute(update(TakeoutEntry).where(
            TakeoutEntry.export_id == self.export.id, TakeoutEntry.part_number == self.part.number, TakeoutEntry.kind != "media",
            TakeoutEntry.outcome == LISTED).values(outcome=RECORDED, done_at=self.now))
        self.tally()
        found = Counter(self.opened.execute(select(TakeoutEntry.outcome).where(TakeoutEntry.export_id == self.export.id,
                                                                               TakeoutEntry.part_number == self.part.number)).scalars())
        if resumed:
            self.changed |= self.changed_since_queued()
        rescanned = run.rescan_people(self.opened, self.settings, sorted(self.changed), self.export, self.now, self.tools)
        self.part.ingest_state, self.part.ingested_at, self.part.ingest_failure, self.part.ingest_failure_type = INGESTED, self.now, None, None
        events.record(self.opened, self.now, "export_part_ingested", f"export {self.export.id}",
                      f"part {self.part.number}: {self.part.entries_done} entries, {self.part.held} held, {self.part.failed_items} failed")
        self.commit()
        monitoring.log("part ingested", export=self.export.id, part=self.part.number, entries=self.part.entries_done, held=self.part.held,
                       failed=self.part.failed_items)
        return PartIngest(ingested=True, placed=found[PLACED], review=found[REVIEW], skipped=found[SKIPPED], held=self.part.held,
                          failed=self.part.failed_items, rescanned=rescanned)


def counted(part: ExportPart, found: Counter[str]) -> None:
    part.entries_done = sum(count for outcome, count in found.items() if outcome != LISTED)
    part.held = sum(found[outcome] for outcome in HELD)
    part.failed_items = found[FAILED]


def ingest_part(opened: Session, settings: config.Settings, export: Export, part: ExportPart, now: float, tools: run.Tools | None = None,
                beat: heartbeat.Heartbeat | None = None) -> PartIngest:
    tools = tools or run.Tools()
    beat = beat or heartbeat.quiet()
    with run.account_lock(settings, export.account_id):
        opened.refresh(export)
        opened.refresh(part)
        if part.ingest_state == INGESTED:
            return PartIngest()
        if part.ingest_state not in (QUEUED, INGESTING) or part.status != "done":
            raise run.NotReady("this part is not queued for ingest")
        person = run.owner_person(opened, export)
        photos = Photos(settings.photos_root)
        try:
            managed_switch.begin(opened, settings, export, person, now)
        except managed_switch.Refused as error:
            for queued in exports.parts_of(opened, export.id):
                if queued.ingest_state in (QUEUED, INGESTING):
                    queued.ingest_state, queued.ingest_refusal = REFUSED, None
            run.refused_import(opened, settings, export, error, now, tools)
            return PartIngest(refused=True)
        run.owner_person(opened, export, claim=True)
        resumed = part.ingest_state == INGESTING
        part.ingest_state, part.ingest_refusal, part.ingest_failure, part.ingest_failure_type = INGESTING, None, None, None
        opened.commit()
        beat.part_began(export.id, part.number, part.entries_done or 0, part.entries or 0, resumed)
        try:
            with contextlib.ExitStack() as stack:
                ingest = Ingest(opened, settings, photos, export, part, person, now, tools, stack, beat)
                try:
                    part_reader.read_part(opened, photos, person, export, part, now, each_batch=ingest.batch, batch_files=part_reader.BATCH_FILES,
                                          batch_bytes=part_reader.BATCH_BYTES, on_step=beat.part_step)
                except part_reader.PartRefused as refused:
                    return refuse(opened, settings, export, part, refused, now, tools)
                beat.part_step("settling")
                try:
                    ingest.settle_rest()
                finally:
                    part_reader.sweep_part_scratch(photos, person, export)
                beat.part_step("finishing")
                return ingest.finish(resumed)
        finally:
            beat.part_ended()
