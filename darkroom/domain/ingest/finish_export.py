from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, dedupe, events, exports
from darkroom.domain.ingest import pairing, part_ingest, part_pairing, run
from darkroom.storage.models import Alert, DedupeDecision, Export, ExportPart, ImportRun, Person, TakeoutEntry, TakeoutSidecar
from darkroom.storage.paths import Photos

COMMIT_HOOK: Callable[[], None] | None = None

ACTIONS: Final[dict[int, dedupe.Action]] = {1: "skip", 2: "skip", 3: "skip", 4: "review", 5: "google_only"}
UNDECIDED: Final = frozenset({part_ingest.WAITING_SIDECAR, part_ingest.WAITING_EXPORT, part_ingest.LISTED})
STILL_OPEN: Final = ("downloading", "downloaded", "importing")
GAVE_UP: Final = ("failed", "expired")


@dataclass(frozen=True)
class Unit:
    item: pairing.TakeoutItem
    root: TakeoutEntry
    sidecar_id: int | None

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
    for item in report.items:
        roots = [row for path in item.copies if (row := by_path.get(path)) is not None and row.copy_of_id is None]
        if not roots:
            continue
        root = min(roots, key=lambda row: (row.decision_id is None, row.id))
        found = None if item.sidecar is None or item.sidecar_from_still else owners.get(item.sidecar)
        (trashed if item.special == pairing.TRASHED else units).append(Unit(item, root, found))
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
        by_id = {unit.root.id: unit for unit in held}
        for chunk in self.in_batches([unit.root for unit in held]):
            self.release_scratch()
            self.measure([part_ingest.Target(entry, by_id[entry.id].sidecar_id) for entry in chunk])
            self.commit()
        self.close_archive()
        self.release_scratch()

    def candidate(self, unit: Unit) -> dedupe.TakeoutCandidate:
        edited_of = None if unit.item.edited_of is None else str(unit.item.edited_of)
        return replace(part_pairing.candidate_of(unit.root), source_path=unit.path, edited_of=edited_of)

    def judge(self, canon: Canon, held: Sequence[Unit]) -> dict[str, dedupe.Decision]:
        live = [unit for unit in canon.units if unit.root.outcome != part_ingest.FAILED or unit.root.decision_id is not None]
        candidates = {unit.path: self.candidate(unit) for unit in live}
        with_peers = replace(self.index, peers=tuple(candidates.values()), decided={})
        originals: dict[str, dedupe.Decision] = {}
        for unit in live:
            if unit.root.is_edited:
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
        judged = self.judge(canon, held)
        for unit in held:
            if unit.path in judged and needs_decision(unit.root):
                self.settle(unit, judged[unit.path])

    def fill_pending(self, canon: Canon) -> None:
        self.sidecar_rows()
        for unit in canon.units:
            root = unit.root
            if not root.metadata_pending:
                continue
            root.sidecar_entry_id = unit.sidecar_id
            if unit.sidecar_id is None:
                root.metadata_pending = False
                self.commit()
            else:
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

    def leave_out(self, canon: Canon) -> None:
        for unit in canon.trashed:
            if unit.root.outcome in UNDECIDED:
                unit.root.outcome, unit.root.done_at = part_ingest.SKIPPED, self.now
        still = [unit.root.id for unit in canon.units if unit.root.outcome in part_ingest.HELD or needs_decision(unit.root)]
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
        self.row.sidecars_unreadable = self.row.sidecars_repaired = 0
        self.tally()
        self.export.stage = self.row.stage = "placed"
        self.commit()


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
    if export.status == "imported":
        raise run.AlreadyImported("this export is already imported")
    with run.account_lock(settings, export.account_id):
        opened.refresh(export)
        if export.status == "imported":
            raise run.AlreadyImported("this export is already imported")
        if export.status == "expired" or not complete(opened, export):
            raise run.NotReady("not every part of this export has been downloaded and ingested")
        return stages(opened, settings, export, now, tools or run.Tools())


def held_count(opened: Session, export: Export) -> int:
    rows = opened.execute(select(TakeoutEntry.id).where(TakeoutEntry.export_id == export.id, TakeoutEntry.kind == "media",
                                                        TakeoutEntry.copy_of_id.is_(None), TakeoutEntry.outcome.in_(part_ingest.HELD))).all()
    return len(rows)


def alert_unfinished(opened: Session, settings: config.Settings, now: float, tools: run.Tools) -> int:
    opened_now = 0
    for export in opened.execute(select(Export).where(Export.status.in_(GAVE_UP)).order_by(Export.id)).scalars().all():
        key = f"export:unfinished:{export.id}"
        if complete(opened, export) or opened.execute(select(Alert.id).where(Alert.key == key).limit(1)).first() is not None:
            continue
        held = held_count(opened, export)
        if held:
            alerts.open_alert(opened, settings, key, "failure", f"export {export.id}", alerts.export_unfinished(str(export.id), held), None, now,
                              notifier=tools.notifier, about=alerts.account(export.account_id))
            opened.commit()
            opened_now += 1
    return opened_now


def ready_exports(opened: Session) -> list[int]:
    statuses = (*STILL_OPEN, "failed")
    found = opened.execute(select(Export).join(ExportPart, ExportPart.export_id == Export.id)
                           .where(Export.status.in_(statuses), ExportPart.ingest_state == part_ingest.INGESTED).distinct().order_by(Export.id)).scalars()
    return [export.id for export in found if ready(opened, export)]


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
    return done


def run_ready(opened: Session, settings: config.Settings, now: float, tools: run.Tools | None = None) -> list[int]:
    return finish_ready(opened, settings, now, tools) if part_ingest.ENABLED else []
