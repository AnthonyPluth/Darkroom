from __future__ import annotations

import contextlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, dedupe, events, exports, holds, managed_compound, managed_ingest, same_shot
from darkroom.domain.ingest import hold_apply, pairing, part_ingest, part_pairing, run
from darkroom.providers import immich
from darkroom.storage import db, paths
from darkroom.storage.models import Alert, DedupeDecision, Export, ExportPart, ImportRun, ManagedImportReceipt, Person, TakeoutEntry, TakeoutSidecar
from darkroom.storage.paths import Photos

COMMIT_HOOK: Callable[[], None] | None = None

ACTIONS: Final[dict[int, dedupe.Action]] = {1: "skip", 2: "skip", 3: "skip", 4: "review", 5: "google_only"}
UNDECIDED: Final = frozenset({part_ingest.WAITING_SIDECAR, part_ingest.WAITING_EXPORT, part_ingest.LISTED})
STILL_OPEN: Final = ("downloading", "downloaded", "importing")
GAVE_UP: Final = ("failed", "expired")


class UploadsPending(RuntimeError):
    pass


@dataclass(frozen=True)
class Unit:
    item: pairing.TakeoutItem
    root: TakeoutEntry
    sidecar_id: int | None
    sidecar_entry: TakeoutEntry | None = None

    @property
    def path(self) -> str:
        return str(self.item.path)


@dataclass(frozen=True)
class Canon:
    report: pairing.PairingReport
    units: tuple[Unit, ...]
    trashed: tuple[Unit, ...]


def is_listed(name: str) -> bool:
    return not name.endswith("/") and not any(part.startswith(".") for part in name.split("/")[:-1])


def entries_of(opened: Session, export: Export) -> list[TakeoutEntry]:
    rows = opened.execute(select(TakeoutEntry).where(TakeoutEntry.export_id == export.id).order_by(TakeoutEntry.id)).scalars()
    return [row for row in rows if is_listed(row.name)]


def canonical(opened: Session, export: Export) -> Canon:
    rows = entries_of(opened, export)
    texts = {found.entry_id: found.text for found in opened.execute(select(TakeoutSidecar).where(TakeoutSidecar.export_id == export.id)).scalars()}
    folders: dict[Path, list[str]] = {}
    by_path = {Path(row.name): row for row in rows}
    for path in by_path:
        folders.setdefault(path.parent, []).append(path.name)
    digests = {path: row.sha1 for path, row in by_path.items() if row.kind == "media" and row.sha1 is not None}
    read = {path: texts[row.id] for path, row in by_path.items() if row.kind == "sidecar" and row.id in texts}
    report = pairing.pair_names([(folder, sorted(names)) for folder, names in sorted(folders.items())], digests, reading=read.get)
    owners = {path: row.id for path, row in by_path.items() if row.id in texts}
    units: list[Unit] = []
    trashed: list[Unit] = []
    roots = [row for row in rows if row.kind == "media" and row.copy_of_id is None and row.sha1 is not None]
    taken: set[int] = set()
    pending: list[tuple[pairing.TakeoutItem, TakeoutEntry | None]] = []
    for item in report.items:
        own = [row for path in item.copies if (row := by_path.get(path)) is not None and row.copy_of_id is None and row.id not in taken]
        found = min(own, key=lambda row: (row.name != str(item.path), row.decision_id is None, row.id)) if own else None
        if found is not None:
            taken.add(found.id)
        pending.append((item, found))
    for item, found in pending:
        if found is None:
            spare = [row for row in roots if row.id not in taken and row.sha1 == item.sha1]
            found = min(spare, key=lambda row: (row.name != str(item.path), row.decision_id is None, row.id)) if spare else None
            if found is not None:
                taken.add(found.id)
        if found is not None:
            chosen = None if item.sidecar is None or item.sidecar_from_still else owners.get(item.sidecar)
            (trashed if item.special == pairing.TRASHED else units).append(Unit(item, found, chosen, None if item.sidecar is None else by_path.get(item.sidecar)))
    for row in roots:
        if row.id not in taken:
            alone = pairing.TakeoutItem(Path(row.name), row.sha1 or "", (Path(row.name),), None)
            units.append(Unit(alone, row, row.sidecar_entry_id))
    return Canon(report, tuple(sorted(units, key=lambda unit: unit.root.id)), tuple(sorted(trashed, key=lambda unit: unit.root.id)))


def needs_decision(root: TakeoutEntry) -> bool:
    return root.decision_id is None and root.sha1 is not None and root.outcome in UNDECIDED


def decision_from(row: DedupeDecision, fallback: int) -> dedupe.Decision:
    return dedupe.Decision(row.source_path or "", row.account_id, fallback if row.person_id is None else row.person_id, row.tier, ACTIONS[row.tier], row.reason, row.matched_item_id, None,
                           "owner" if row.decided_by == "owner" else "auto")


def same_json(first: str | None, second: str | None) -> bool:
    if first == second:
        return True
    try:
        return first is not None and second is not None and bool(json.loads(first) == json.loads(second))
    except ValueError:
        return False


class Finish(part_ingest.Ingest):
    def __init__(self, opened: Session, settings: config.Settings, photos: Photos, export: Export, part: ExportPart, person: Person, now: float,
                 tools: run.Tools, stack: contextlib.ExitStack, row: ImportRun) -> None:
        super().__init__(opened, settings, photos, export, part, person, now, tools, stack)
        self.row = row
        self.before = {int(each) for each in json.loads(row.changed_people or "[]")}
        stack.callback(self.release_scratch)

    def commit(self) -> None:
        if COMMIT_HOOK is not None:
            COMMIT_HOOK()
        self.row.changed_people = json.dumps(sorted(self.before | self.changed))
        self.opened.commit()

    def text_of(self, entry_id: int | None) -> str | None:
        return None if entry_id is None else self.texts.get(entry_id)

    def remeasure(self, held: Sequence[Unit]) -> None:
        for unit in held:
            unit.root.sidecar_entry_id = unit.sidecar_id
        self.commit()
        self.measure_in_batches(held, restore=False)

    def measure_in_batches(self, units: Sequence[Unit], restore: bool) -> None:
        by_id = {unit.root.id: unit for unit in units}
        kept = {unit.root.id: (unit.root.outcome, unit.root.done_at, unit.root.metadata_pending) for unit in units}
        try:
            for chunk in self.in_batches([unit.root for unit in units]):
                self.release_scratch()
                self.measure([part_ingest.Target(entry, by_id[entry.id].sidecar_id) for entry in chunk])
                if restore:
                    for entry in chunk:
                        entry.outcome, entry.done_at, entry.metadata_pending = kept[entry.id]
                self.commit()
        finally:
            self.close_archive()
            self.release_scratch()

    def refresh_peers(self, canon: Canon, held: Sequence[Unit]) -> None:
        waiting = {unit.root.id for unit in held}
        stale = [unit for unit in canon.units if unit.root.id not in waiting and unit.root.decision_id is not None and unit.sidecar_id is not None
                 and unit.root.sha1 is not None and unit.root.captured_utc is None]
        if stale:
            self.measure_in_batches(stale, restore=True)

    def candidate(self, unit: Unit) -> dedupe.TakeoutCandidate:
        edited_of = None if unit.item.edited_of is None else str(unit.item.edited_of)
        edited = pairing.original_name(unit.item.path) is not None
        return replace(part_pairing.candidate_of(unit.root), source_path=unit.path, is_edited=edited, edited_of=edited_of)

    def judge(self, canon: Canon, held: Sequence[Unit]) -> dict[str, dedupe.Decision]:
        live = [unit for unit in canon.units if unit.root.outcome != part_ingest.FAILED or unit.root.decision_id is not None]
        candidates = {unit.path: self.candidate(unit) for unit in live}
        with_peers = replace(self.index, peers=tuple(candidates.values()), decided={})
        originals: dict[str, dedupe.Decision] = {}
        for unit in live:
            if candidates[unit.path].is_edited:
                continue
            stored = None if unit.root.decision_id is None else self.opened.get(DedupeDecision, unit.root.decision_id)
            if stored is None:
                originals[unit.path] = dedupe.decide(with_peers, candidates[unit.path], self.dedupe_settings)
            elif stored.reversed_at is None:
                originals[unit.path] = decision_from(stored, self.index.default_person_id)
        with_originals = replace(with_peers, decided=originals)
        wanted = {unit.path for unit in held}
        return {path: originals.get(path) or dedupe.decide(with_originals, candidates[path], self.dedupe_settings) for path in candidates if path in wanted}

    def settle(self, unit: Unit, decision: dedupe.Decision) -> None:
        self.lock()
        entry = unit.root
        row = dedupe.stored_decision(self.opened, self.export.id, unit.path)
        if row is None:
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
        self.place_decided(entry, row, Path(unit.path))

    def decide_held(self, canon: Canon) -> None:
        self.sidecar_rows()
        held = [unit for unit in canon.units if needs_decision(unit.root)]
        if not held:
            return
        self.remeasure(held)
        self.refresh_peers(canon, held)
        self.lock()
        judged = self.judge(canon, held)
        by_id = {unit.root.id: unit for unit in held}
        for chunk in self.in_batches([unit.root for unit in held]):
            try:
                for entry in chunk:
                    unit = by_id[entry.id]
                    if unit.path in judged and needs_decision(unit.root):
                        self.settle(unit, judged[unit.path])
            finally:
                self.close_archive()
                self.release_scratch()

    def fill_pending(self, canon: Canon) -> None:
        self.sidecar_rows()
        for unit in canon.units:
            root = unit.root
            if not root.metadata_pending:
                continue
            self.lock()
            root.sidecar_entry_id = unit.sidecar_id
            if unit.sidecar_id is None:
                root.metadata_pending = False
                self.commit()
            else:
                self.opened.flush()
                self.fill(root)

    def canonicalise(self, canon: Canon) -> None:
        self.lock()
        self.sidecar_rows()
        held = {row.source_path: row.id for row in self.opened.execute(select(DedupeDecision).where(DedupeDecision.export_id == self.export.id)).scalars()}
        renamed = differ = 0
        for unit in canon.units:
            root = unit.root
            row = None if root.decision_id is None else self.opened.get(DedupeDecision, root.decision_id)
            if row is not None:
                if row.source_path != unit.path and unit.path not in held:
                    held.pop(row.source_path, None)
                    row.source_path = unit.path
                    held[unit.path] = row.id
                    self.opened.flush()
                renamed += row.source_path != root.name
            if root.sidecar_entry_id != unit.sidecar_id and not same_json(self.text_of(root.sidecar_entry_id), self.text_of(unit.sidecar_id)):
                differ += 1
        self.row.paths_renamed, self.row.sidecars_differ = renamed, differ
        if differ:
            events.record(self.opened, self.now, "export_sidecar_choices_differ", f"export {self.export.id}", f"{differ} items")
        self.export.stage = self.row.stage = "deduped"
        self.commit()

    def sidecar_counts(self, canon: Canon) -> tuple[int, int]:
        paired = sorted((unit.sidecar_entry for unit in canon.units
                         if unit.sidecar_entry is not None and (unit.root.outcome != part_ingest.FAILED or unit.root.decision_id is not None)),
                        key=lambda entry: (entry.part_number, entry.id))
        states: Counter[str] = Counter()
        try:
            for entry in paired:
                states[self.sidecar_state(entry)] += 1
        finally:
            self.close_archive()
        return states["unreadable"], states["repaired"]

    def leave_out(self, canon: Canon) -> None:
        self.lock()
        for unit in canon.trashed:
            if unit.root.outcome in UNDECIDED:
                unit.root.outcome, unit.root.done_at = part_ingest.SKIPPED, self.now
        still = [entry.id for entry in entries_of(self.opened, self.export) if entry.kind == "media" and entry.copy_of_id is None
                 and entry.sha1 is not None and entry.outcome in UNDECIDED]
        if still:
            self.commit()
            raise run.StageFailed(f"{len(still)} held items could not be decided")
        kinds = dict(canon.report.not_imported)
        if canon.trashed:
            kinds[pairing.TRASHED] = len(canon.trashed)
        media = [entry for entry in entries_of(self.opened, self.export) if entry.kind == "media" and entry.copy_of_id is None]
        self.row.failed = sum(1 for entry in media if entry.outcome == part_ingest.FAILED)
        self.row.repeated = sum(1 for unit in canon.units if unit.root.outcome == part_ingest.SKIPPED and unit.root.decision_id is None)
        self.row.not_imported = json.dumps(kinds, sort_keys=True)
        self.row.orphan_sidecars, self.row.unpaired_media = len(canon.report.orphan_sidecars), len(canon.report.unpaired_media)
        self.row.sidecars_unreadable, self.row.sidecars_repaired = self.sidecar_counts(canon)
        self.tally()
        self.export.stage = self.row.stage = "placed"
        self.commit()

    def send_to_immich(self) -> None:
        intent = managed_ingest.intent_of(self.opened, self.export)
        if intent is None:
            return
        self.sidecar_rows()
        due: list[tuple[managed_ingest.Item, ...]] = []
        try:
            reader = managed_ingest.reader_of(self.settings, self.person, intent, self.tools.immich)
            uploads = managed_ingest.Uploads(self.opened, self.photos, self.export, intent, reader, self.now)
            due = uploads.due(upload_items(canonical(self.opened, self.export), self.texts))
            if due:
                reader.check_access()
        except immich.ImmichFailed as error:
            self.opened.rollback()
            managed_ingest.alert(self.opened, self.settings, self.export, 0, max(len(due), 1), self.now, self.tools.notifier)
            raise UploadsPending(str(error)) from None
        by_root = {item.root.id: item for group in due for item in group}
        for chunk in self.in_grouped_batches([[item.root for item in group] for group in due]):
            try:
                self.release_scratch()
                entries = [entry for group in chunk for entry in group]
                sources = {entry.id: self.scratch_of(entry) for entry in entries}
                with paths.pinned_inbox_files(self.photos, list(sources.values())) as pinned:
                    read = self.tools.read(list(pinned.values())) if pinned else {}
                    tags = {entry_id: read.get(pinned[source]) if source in pinned else None for entry_id, source in sources.items()}
                for group in chunk:
                    items = [by_root[entry.id] for entry in group]
                    if len(items) == 2:
                        uploads.send_pair(items[0], items[1], sources, tags)
                    else:
                        uploads.send(items[0], sources[items[0].root.id], tags[items[0].root.id])
            finally:
                self.close_archive()
                self.release_scratch()
        managed_ingest.alert(self.opened, self.settings, self.export, uploads.held, uploads.pending, self.now, self.tools.notifier)
        same_shot.alert(self.opened, self.settings, self.export, self.person, self.now, self.tools.notifier)
        monitoring.log("managed uploads", export=self.export.id, committed=uploads.committed, held=uploads.held, same_shot=uploads.same, pending=uploads.pending)
        if uploads.pending:
            raise UploadsPending(f"{uploads.pending} uploads to Immich are not complete yet")


def live_roots(canon: Canon) -> set[int]:
    stills = {str(item.live_pair_of) for item in canon.report.items if item.live_pair_of is not None}
    return {unit.root.id for unit in canon.units if unit.item.live_pair_of is not None or unit.path in stills}


def upload_items(canon: Canon, texts: Mapping[int, str]) -> list[managed_ingest.Item]:
    live = live_roots(canon)
    grouping = managed_compound.groups(canon)
    pairs: dict[int, tuple[managed_compound.Pair, managed_compound.MemberRole]] = {}
    for each in grouping.pairs:
        pairs[each.still.root.id], pairs[each.video.root.id] = (each, "live_still"), (each, "live_video")
    sidecars = {unit.root.id: unit.sidecar_id for unit in canon.units}
    found = []
    for unit in canon.units:
        pair, role = pairs.get(unit.root.id, (None, None))
        sidecar_id = sidecars[pair.still.root.id] if pair is not None and role == "live_video" and unit.item.sidecar_from_still else unit.sidecar_id
        sidecar = None if sidecar_id is None else texts.get(sidecar_id)
        reason = grouping.held.get(unit.root.id)
        found.append(managed_ingest.Item(unit.root, unit.path, sidecar, unit.root.id in live or reason is not None, unit.item.sidecar_from_still, pair, role,
                                         reason))
    return found


def live_decisions(opened: Session, export: Export) -> set[int]:
    canon = canonical(opened, export)
    live = live_roots(canon) | managed_compound.groups(canon).held.keys()
    return {unit.root.decision_id for unit in canon.units if unit.root.id in live and unit.root.decision_id is not None}


def live_review_items(opened: Session, export: Export, decision_id: int) -> tuple[managed_ingest.Item, managed_ingest.Item]:
    if not complete(opened, export) or run.before(export, "placed"):
        raise managed_ingest.Refused("the Live Photo can be reviewed once all parts of the export have been processed")
    rows = opened.execute(select(TakeoutSidecar.entry_id, TakeoutSidecar.text).where(TakeoutSidecar.export_id == export.id)).all()
    items = upload_items(canonical(opened, export), {entry_id: text for entry_id, text in rows})
    chosen = next((item for item in items if item.root.decision_id == decision_id), None)
    if chosen is None or chosen.pair is None:
        if chosen is not None and chosen.hold == "live_ambiguous":
            raise managed_ingest.Refused("live_ambiguous: the Live Photo's other half could not be identified")
        raise managed_ingest.Refused(managed_ingest.LIVE_PARTIAL)
    roots = {item.root.id: item for item in items}
    return roots[chosen.pair.still.root.id], roots[chosen.pair.video.root.id]


def complete(opened: Session, export: Export) -> bool:
    return exports.all_done(opened, export) and all(part.ingest_state == part_ingest.INGESTED for part in exports.parts_of(opened, export.id))


def retry_asked(opened: Session, export: Export) -> bool:
    last = run.latest_run(opened, export.id)
    return export.status == "failed" and last is not None and last.status == "queued"


def ready(opened: Session, export: Export) -> bool:
    if export.status not in STILL_OPEN and not retry_asked(opened, export):
        return False
    return complete(opened, export) and not part_ingest.waits_for_earlier(opened, export)


def differences_of(row: ImportRun) -> str:
    return f", {row.paths_renamed or 0} paths renamed to their canonical copy, {row.sidecars_differ or 0} sidecar choices differ"


def stages(opened: Session, settings: config.Settings, export: Export, now: float, tools: run.Tools) -> ImportRun:
    photos = Photos(settings.photos_root)
    row = run.current_run(opened, export, now)
    if export.status == "failed":
        export.status = "importing"
    opened.commit()
    export_id, run_id = export.id, row.id
    try:
        part = exports.parts_of(opened, export.id)[-1]
        person = run.owner_person(opened, export, claim=True)
        with contextlib.ExitStack() as stack:
            finishing = Finish(opened, settings, photos, export, part, person, now, tools, stack, row)
            if run.before(export, "deduped"):
                canon = canonical(opened, export)
                finishing.fill_pending(canon)
                finishing.decide_held(canon)
                finishing.canonicalise(canonical(opened, export))
            if run.before(export, "placed"):
                finishing.leave_out(canonical(opened, export))
            if finishing.managed and run.before(export, "counted"):
                finishing.send_to_immich()
            if run.before(export, "counted"):
                run.stage_count(opened, settings, export, row, now, tools)
            if run.before(export, "rescanned"):
                run.stage_rescan(opened, settings, export, row, now, tools)
            if run.before(export, "reported"):
                run.stage_report(opened, settings, export, row, now, tools)
    except run.ReportPending:
        monitoring.log("finish left to resume", export=export_id, stage="reported")
        found = opened.get(ImportRun, run_id)
        assert found is not None
        return found
    except UploadsPending:
        opened.rollback()
        monitoring.log("finish left to resume", export=export_id, stage="uploads")
        found = opened.get(ImportRun, run_id)
        assert found is not None
        return found
    except Exception as error:
        run.failed(opened, settings, export_id, run_id, error, now, tools)
        found = opened.get(ImportRun, run_id)
        assert found is not None
        return found
    export.status = "imported"
    row.status, row.finished = "done", now
    run.mark(opened, export, row, "done")
    events.record(opened, now, "import_done", f"export {export.id}", f"{run.summary_of(export, row)}{run.left_out_of(row)}{differences_of(row)}")
    opened.commit()
    monitoring.log("imported", export=export.id, added=row.added, review=row.review, failed=row.failed)
    return row


def finish(opened: Session, settings: config.Settings, export: Export, now: float, tools: run.Tools | None = None) -> ImportRun:
    from darkroom.domain import managed_switch

    if export.status == "imported":
        raise run.AlreadyImported("this export is already imported")
    with run.account_lock(settings, export.account_id):
        db.write_lock(opened)
        opened.refresh(export)
        if export.status == "imported":
            opened.rollback()
            raise run.AlreadyImported("this export is already imported")
        opened.expire_all()
        if export.status == "expired" or not complete(opened, export):
            opened.rollback()
            raise run.NotReady("not every part of this export has been downloaded and ingested")
        person = exports.owner_of(opened, export)
        if person is not None:
            try:
                managed_switch.check(opened, Photos(settings.photos_root), person, "ingest", export)
            except managed_switch.Refused as error:
                return run.refused_import(opened, settings, export, error, now, tools or run.Tools())
        return stages(opened, settings, export, now, tools or run.Tools())


def held_count(opened: Session, export: Export) -> int:
    rows = opened.execute(select(TakeoutEntry.id).where(TakeoutEntry.export_id == export.id, TakeoutEntry.kind == "media",
                                                        TakeoutEntry.copy_of_id.is_(None), TakeoutEntry.outcome.in_(part_ingest.HELD))).all()
    return len(rows)


def alert_unfinished(opened: Session, settings: config.Settings, now: float, tools: run.Tools) -> int:
    opened_now = 0
    for export_id in opened.execute(select(Export.id).where(Export.status.in_(GAVE_UP)).order_by(Export.id)).scalars().all():
        db.write_lock(opened)
        opened.expire_all()
        export = opened.get(Export, export_id)
        key = f"export:unfinished:{export_id}"
        if export is None or export.status not in GAVE_UP or complete(opened, export):
            opened.rollback()
            continue
        if opened.execute(select(Alert.id).where(Alert.key == key).limit(1)).first() is not None:
            opened.rollback()
            continue
        held = held_count(opened, export)
        if held:
            alerts.open_alert(opened, settings, key, "failure", f"export {export.id}", alerts.export_unfinished(str(export.id), held), None, now,
                              notifier=tools.notifier, about=alerts.account(export.account_id))
            opened_now += 1
        opened.commit()
    return opened_now


def ready_exports(opened: Session) -> list[int]:
    statuses = (*STILL_OPEN, "failed")
    found = opened.execute(select(Export).join(ExportPart, ExportPart.export_id == Export.id)
                           .where(Export.status.in_(statuses), ExportPart.ingest_state == part_ingest.INGESTED).distinct().order_by(Export.id)).scalars()
    return [export.id for export in found if ready(opened, export)]


def source_for_resolution(opened: Session, settings: config.Settings, photos: Photos, stack: contextlib.ExitStack,
                          receipt: ManagedImportReceipt, export: Export, person: Person) -> hold_apply.Source:
    stack.enter_context(run.account_lock(settings, export.account_id))
    opened.refresh(export)
    root_id = holds.number_after(receipt.resource_key, "takeout-entry:")
    canon = canonical(opened, export)
    unit = next((item for item in canon.units if item.root.id == root_id), None)
    if (export.status not in (*STILL_OPEN, "imported") or run.before(export, "placed")
            or unit is None or unit.root.id in live_roots(canon) or unit.item.sidecar_from_still
            or not managed_ingest.wanted(opened, export, unit.root)
            or (unit.root.sha1, unit.root.bytes) != (receipt.source_sha1, receipt.expected_bytes)):
        raise hold_apply.UnreadableSource
    part = part_ingest.part_of(opened, export.id, unit.root.part_number)
    if part is None:
        raise hold_apply.UnreadableSource
    text = opened.scalars(select(TakeoutSidecar.text).where(TakeoutSidecar.export_id == export.id, TakeoutSidecar.entry_id == unit.sidecar_id)).one_or_none()
    source = stack.enter_context(part_ingest.resolution_copy(photos, export, part, person, unit.root))
    return hold_apply.Source(stack.enter_context(paths.open_inbox_file(photos, source)), Path(unit.path).name, text, entry=unit.root)


def finish_ready(opened: Session, settings: config.Settings, now: float, tools: run.Tools | None = None) -> list[int]:
    chosen = tools or run.Tools()
    done = []
    for export_id in ready_exports(opened):
        export = opened.get(Export, export_id)
        if export is None:
            continue
        try:
            finish(opened, settings, export, now, chosen)
        except (run.Busy, run.AlreadyImported, run.NotReady) as error:
            monitoring.log("finish skipped", export=export_id, reason=type(error).__name__)
            continue
        done.append(export_id)
    alert_unfinished(opened, settings, now, chosen)
    photos = Photos(settings.photos_root)
    managed_ingest.send_kept(opened, settings, photos, now, chosen.immich)
    hold_apply.apply(opened, settings, photos, now, chosen,
                     lambda stack, receipt, export, person: source_for_resolution(opened, settings, photos, stack, receipt, export, person))
    return done
