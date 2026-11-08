from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.dates import utc_now
from darkroom.domain import accounts, alerts, dedupe, events, exports, pixel_folder, takeout_copies, xmp
from darkroom.domain.ingest import metadata, pairing, place, sidecar, unpack
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.providers import exiftool, immich
from darkroom.storage import locks, paths
from darkroom.storage.models import Alert, DedupeDecision, Export, GoogleAccount, ImportRun, MediaItem, Person, Pixel, PixelPerson
from darkroom.storage.paths import Photos

DAY = 24 * 3600.0
UNMATCHED_GRACE = 7 * DAY
STAGES = ("queued", "unpacked", "deduped", "placed", "counted", "rescanned", "reported", "done")
IMPORTABLE = frozenset({"downloaded", "unpacking", "importing", "failed"})
EARLY_STAGES = frozenset({None, "unpack", "queued", "unpacked"})
WAITING = frozenset({"queued", "running"})
UNPLACED = "unplaced"
LOCKS: dict[int, threading.Lock] = {}
LOCKS_GUARD = threading.Lock()


class NotReady(RuntimeError):
    pass


class AlreadyImported(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class StageFailed(RuntimeError):
    pass


class ReportPending(RuntimeError):
    pass


@dataclass(frozen=True)
class Tools:
    read: xmp.Reader = exiftool.read
    write: xmp.Writer = exiftool.write_xmp
    immich: immich.Client | None = None
    notifier: alerts.Notifier | None = None


@dataclass
class Prepared:
    staging: Path
    items: list[TakeoutItem] = field(default_factory=list)
    fields: dict[Path, MediaItem] = field(default_factory=dict)

    def source_of(self, item: TakeoutItem) -> str:
        return str(item.path.relative_to(self.staging))


def stage_index(stage: str | None) -> int:
    return STAGES.index(stage) if stage in STAGES else -1


def before(export: Export, stage: str) -> bool:
    return stage_index(export.stage) < STAGES.index(stage)


def lock_file(settings: config.Settings, account_id: int) -> Path:
    return settings.data_dir / "locks" / f"import-account-{account_id}.lock"


@contextmanager
def account_lock(settings: config.Settings, account_id: int) -> Iterator[None]:
    with LOCKS_GUARD:
        local = LOCKS.setdefault(account_id, threading.Lock())
    if not local.acquire(blocking=False):
        raise Busy("an import is already running for this account")
    try:
        with locks.exclusive(lock_file(settings, account_id)):
            yield
    except locks.Held as error:
        raise Busy("an import is already running for this account") from error
    finally:
        local.release()


def latest_run(opened: Session, export_id: int) -> ImportRun | None:
    return opened.execute(select(ImportRun).where(ImportRun.export_id == export_id).order_by(ImportRun.id.desc()).limit(1)).scalar_one_or_none()


def current_run(opened: Session, export: Export, now: float) -> ImportRun:
    run = latest_run(opened, export.id)
    if run is None or run.status == "done":
        run = ImportRun(export_id=export.id, started=now)
        opened.add(run)
    run.status, run.error, run.finished = "running", None, None
    opened.flush()
    return run


def owner_person(opened: Session, export: Export, claim: bool = False) -> Person:
    person = exports.claim_owner(opened, export) if claim else exports.owner_of(opened, export)
    if person is None:
        raise StageFailed("the export's account has no default person")
    return person


def item_failed(opened: Session, export: Export, stage: str, error: BaseException, now: float) -> None:
    events.record(opened, now, "import_item_failed", f"export {export.id}", f"{stage}: {type(error).__name__}")


def mark(opened: Session, export: Export, run: ImportRun, stage: str) -> None:
    export.stage = run.stage = stage
    opened.commit()


def stage_unpack(opened: Session, photos: Photos, export: Export, run: ImportRun, now: float) -> None:
    person = owner_person(opened, export, claim=True)
    report = unpack.unpack(opened, photos, person, export, now)
    if export.status == "failed":
        raise StageFailed(f"the export could not be unpacked: {'; '.join(report.public) or 'see the events'}")
    export.status = "importing"
    mark(opened, export, run, "unpacked")


def prepare(opened: Session, photos: Photos, export: Export, now: float, tools: Tools, report: bool) -> Prepared:
    person = owner_person(opened, export)
    staging = staging_of(opened, photos, export)
    if staging is None:
        raise StageFailed("the export's staging folder is missing")
    prepared = Prepared(staging, pairing.pair(staging).items)
    tags = tools.read([item.path for item in prepared.items]) if prepared.items else {}
    for item in prepared.items:
        try:
            raw = item.sidecar.read_text(encoding="utf-8") if item.sidecar is not None else None
            parsed = sidecar.parse(raw) if raw else None
            prepared.fields[item.path] = metadata.candidate(item, tags.get(item.path, {}), parsed, person.id, UNPLACED, now, export.id)
        except Exception as error:
            if report:
                item_failed(opened, export, "metadata", error, now)
    return prepared


def stored_decisions(opened: Session, export: Export) -> dict[str, DedupeDecision]:
    rows = opened.execute(select(DedupeDecision).where(DedupeDecision.export_id == export.id)).scalars()
    return {row.source_path: row for row in rows if row.source_path is not None}


def stage_dedupe(opened: Session, export: Export, run: ImportRun, prepared: Prepared, now: float) -> None:
    known = stored_decisions(opened, export)
    candidates = [dedupe.candidate_of(item, prepared.fields[item.path], prepared.staging) for item in prepared.items if item.path in prepared.fields]
    if any(candidate.source_path not in known for candidate in candidates):
        index = dedupe.PersonIndex.load(opened, export.account_id, owner_person(opened, export).id)
        for decision in dedupe.decide_all(index, candidates, dedupe.load_settings(opened)):
            if decision.source_path in known:
                continue
            if dedupe.repeated(opened, decision, export.id):
                if decision.matched_item_id is not None:
                    pixel_folder.seen_again(opened, export.account_id, decision.matched_item_id, decision.person_id,
                                            export.created_at if export.created_at is not None else now)
                    opened.commit()
                continue
            try:
                dedupe.apply(opened, decision, export, now)
                opened.commit()
            except Exception as error:
                opened.rollback()
                item_failed(opened, export, "dedupe", error, now)
                opened.commit()
    mark(opened, export, run, "deduped")


def repeated_items(opened: Session, export: Export, prepared: Prepared, known: dict[str, DedupeDecision]) -> set[str]:
    candidates = [dedupe.candidate_of(item, prepared.fields[item.path], prepared.staging) for item in prepared.items if item.path in prepared.fields]
    if all(candidate.source_path in known for candidate in candidates):
        return set()
    index = dedupe.PersonIndex.load(opened, export.account_id, owner_person(opened, export).id)
    decisions = dedupe.decide_all(index, candidates, dedupe.load_settings(opened))
    return {decision.source_path for decision in decisions if decision.source_path not in known and dedupe.repeated(opened, decision, export.id)}


def library_owner(opened: Session, row: DedupeDecision) -> int | None:
    item_id = row.takeout_item_id if row.tier == 5 else row.matched_item_id
    item = None if item_id is None else opened.get(MediaItem, item_id)
    return None if item is None else item.person_id


def stage_place(opened: Session, photos: Photos, export: Export, run: ImportRun, prepared: Prepared, now: float, tools: Tools) -> None:
    changed: set[int] = set(json.loads(run.changed_people or "[]"))
    failed = repeated = 0
    known = stored_decisions(opened, export)
    repeats = repeated_items(opened, export, prepared, known)
    for item in prepared.items:
        fields = prepared.fields.get(item.path)
        row = known.get(prepared.source_of(item)) if fields is not None else None
        if fields is not None and row is None and prepared.source_of(item) in repeats:
            repeated += 1
            continue
        if fields is None or row is None:
            failed += 1
            continue
        if row.tier == 5 and (row.takeout_item_id is not None or takeout_copies.resolved_away(opened, row.id)):
            continue
        new_row = row.tier == 5 and row.takeout_item_id is None
        try:
            done = place.place(opened, photos, export, [place.Entry(item, fields, row)], tools.read, tools.write)
        except Exception as error:
            opened.rollback()
            failed += 1
            item_failed(opened, export, "place", error, now)
            opened.commit()
            continue
        for item_id in done.refreshed:
            events.record(opened, now, "metadata_refreshed", f"item {item_id}", "description or location changed in Google")
        owner = library_owner(opened, row)
        if owner is not None and (done.placed or done.sidecars or done.refreshed or (new_row and row.takeout_item_id is not None)):
            changed.add(owner)
        run.changed_people = json.dumps(sorted(changed))
        opened.commit()
    run.failed, run.repeated = failed, repeated
    run.changed_people = json.dumps(sorted(changed))
    mark(opened, export, run, "placed")


def tier_counts(opened: Session, export: Export) -> dict[int, int]:
    rows = opened.execute(
        select(DedupeDecision.tier, func.count(DedupeDecision.id))
        .where(DedupeDecision.export_id == export.id, DedupeDecision.decided_by == "auto", DedupeDecision.reversed_at.is_(None))
        .group_by(DedupeDecision.tier)
    ).all()
    return {int(tier): int(count) for tier, count in rows}


def added_items(opened: Session, export: Export) -> list[MediaItem]:
    return list(opened.execute(
        select(MediaItem).join(DedupeDecision, DedupeDecision.takeout_item_id == MediaItem.id)
        .where(DedupeDecision.export_id == export.id, DedupeDecision.tier == 5, DedupeDecision.decided_by == "auto",
               DedupeDecision.reversed_at.is_(None))
    ).scalars().unique())


def pixels_serving(opened: Session, account_id: int, person_id: int) -> list[Pixel]:
    return list(opened.execute(
        select(Pixel).join(PixelPerson, PixelPerson.pixel_id == Pixel.id)
        .where(Pixel.google_account_id == account_id, PixelPerson.person_id == person_id).order_by(Pixel.id)
    ).scalars())


def unmatched_count(opened: Session, person_id: int, cutoff: float) -> int:
    return int(opened.execute(
        select(func.count(MediaItem.id)).where(
            MediaItem.person_id == person_id, MediaItem.source == "phone", MediaItem.indexed_at <= cutoff, MediaItem.confirmed_in_google.is_(False))
    ).scalar_one())


def unmatched_key(account_id: int, person_id: int, pixel_id: int | None) -> str:
    return f"originals_unmatched:{account_id}:{person_id}:{'account' if pixel_id is None else pixel_id}"


def check_unmatched(opened: Session, settings: config.Settings, export: Export, now: float, notifier: alerts.Notifier | None) -> dict[str, int]:
    cutoff = (export.ready_at if export.ready_at is not None else now) - UNMATCHED_GRACE
    found: dict[str, int] = {}
    wanted: set[str] = set()
    for person_id in sorted(accounts.served_people(opened, export.account_id)):
        person = opened.get(Person, person_id)
        if person is None:
            continue
        count = unmatched_count(opened, person_id, cutoff)
        found[person.slug] = count
        pixels: list[Pixel | None] = [*pixels_serving(opened, export.account_id, person_id)] or [None]
        for pixel in pixels:
            key = unmatched_key(export.account_id, person_id, None if pixel is None else pixel.id)
            if count == 0:
                alerts.close(opened, key, now)
                continue
            wanted.add(key)
            wording = alerts.originals_unmatched(person.name, None if pixel is None else pixel.name, count)
            alert = alerts.open_alert_committed(opened, settings, key, "originals_unmatched", person.slug, wording, None, now, notifier=notifier,
                                                 about=alerts.person(person.id))
            alert.message = wording.message
    prefix = f"originals_unmatched:{export.account_id}:"
    for alert in opened.execute(select(Alert).where(Alert.key.startswith(prefix), Alert.closed.is_(None))).scalars():
        if alert.key not in wanted:
            alert.closed = now
    return found


def stage_count(opened: Session, settings: config.Settings, export: Export, run: ImportRun, now: float, tools: Tools) -> None:
    tiers = tier_counts(opened, export)
    added = added_items(opened, export)
    run.added = len(added)
    run.skipped_t1, run.skipped_t2, run.skipped_t3 = tiers.get(1, 0), tiers.get(2, 0), tiers.get(3, 0)
    run.review = tiers.get(4, 0)
    run.missing_gps = sum(1 for item in added if item.lat is None or item.lon is None)
    run.unmatched_by_person = json.dumps(check_unmatched(opened, settings, export, now, tools.notifier), sort_keys=True)
    mark(opened, export, run, "counted")


def library_of(opened: Session, client: immich.Client, person: Person) -> str:
    if person.immich_library_id:
        return person.immich_library_id
    if not person.immich_user_id:
        raise immich.ImmichFailed("the person has no Immich user")
    found = client.find_library(person.immich_user_id)
    if found is None:
        raise immich.ImmichFailed("the person's Immich user owns no library")
    person.immich_library_id = found
    opened.flush()
    return found


def rescan(opened: Session, settings: config.Settings, client: immich.Client, person: Person, export: Export, now: float,
           notifier: alerts.Notifier | None) -> bool:
    try:
        library = library_of(opened, client, person)
        try:
            client.rescan_library(library)
        except immich.ImmichFailed as error:
            if error.status == 404:
                person.immich_library_id = None
            raise
    except immich.ImmichFailed as error:
        events.record(opened, now, "immich_failed", person.slug, str(error)[:200])
        alerts.open_alert(opened, settings, f"immich:failed:{export.id}:{person.id}", "failure", person.slug,
                          alerts.failure(f"The Immich rescan of {person.name}'s library"), None, now, notifier=notifier, about=alerts.person(person.id))
        return False
    events.record(opened, now, "immich_rescanned", person.slug)
    return True


def rescan_people(opened: Session, settings: config.Settings, person_ids: list[int], export: Export, now: float, tools: Tools) -> int:
    if not person_ids:
        return 0
    try:
        client = tools.immich or immich.Client.of(settings)
    except immich.NotConfigured:
        events.record(opened, now, "immich_not_configured", f"export {export.id}", f"{len(person_ids)} libraries not rescanned")
        return 0
    done = 0
    for person_id in person_ids:
        person = opened.get(Person, person_id)
        if person is not None and rescan(opened, settings, client, person, export, now, tools.notifier):
            done += 1
    return done


def stage_rescan(opened: Session, settings: config.Settings, export: Export, run: ImportRun, now: float, tools: Tools) -> None:
    rescan_people(opened, settings, json.loads(run.changed_people or "[]"), export, now, tools)
    mark(opened, export, run, "rescanned")


def summary_of(export: Export, run: ImportRun) -> str:
    skipped = run.skipped_t1 + run.skipped_t2 + run.skipped_t3
    earlier = f", {run.repeated} already imported earlier" if run.repeated else ""
    text = f"Export {export.id}: {run.added} added, {skipped} skipped as duplicates{earlier}, {run.review} in review, {run.missing_gps} missing GPS"
    return f"{text}, {run.failed} could not be imported" if run.failed else text


def stage_report(opened: Session, settings: config.Settings, export: Export, run: ImportRun, now: float, tools: Tools) -> None:
    key = f"export_imported:{export.id}"
    if opened.execute(select(Alert.id).where(Alert.key == key).limit(1)).first() is None:
        alerts.open_alert_committed(opened, settings, key, "export_imported", f"export {export.id}", alerts.export_imported(summary_of(export, run)),
                                    None, now, tools.notifier, about=alerts.account(export.account_id))
        try:
            mark(opened, export, run, "reported")
        except Exception as error:
            opened.rollback()
            raise ReportPending("the summary was sent but its stage was not saved") from error
        return
    mark(opened, export, run, "reported")


def failed(opened: Session, settings: config.Settings, export_id: int, run_id: int, error: Exception, now: float, tools: Tools) -> None:
    opened.rollback()
    export, run = opened.get(Export, export_id), opened.get(ImportRun, run_id)
    public = str(error) if isinstance(error, StageFailed) else f"{type(error).__name__} during the import"
    if export is not None:
        export.status = "failed"
    if run is not None:
        run.status, run.error, run.finished = "failed", public[:500], now
    events.record(opened, now, "import_failed", f"export {export_id}", public[:200])
    alerts.open_alert(opened, settings, f"import:failed:{export_id}:{run_id}", "failure", f"export {export_id}",
                      alerts.failure(f"The import of export {export_id}"), None, now, notifier=tools.notifier,
                      about=alerts.account(export.account_id) if export is not None else alerts.NOBODY)
    opened.commit()
    monitoring.log("import failed", export=export_id, error=type(error).__name__)


def run_stages(opened: Session, settings: config.Settings, export: Export, now: float, tools: Tools) -> ImportRun:
    photos = Photos(settings.photos_root)
    run = current_run(opened, export, now)
    if export.status == "failed":
        export.status = "downloaded" if before(export, "unpacked") else "importing"
    opened.commit()
    export_id, run_id = export.id, run.id
    try:
        if before(export, "unpacked"):
            stage_unpack(opened, photos, export, run, now)
        prepared = None
        if before(export, "deduped"):
            prepared = prepare(opened, photos, export, now, tools, report=True)
            stage_dedupe(opened, export, run, prepared, now)
        if before(export, "placed"):
            prepared = prepared or prepare(opened, photos, export, now, tools, report=False)
            stage_place(opened, photos, export, run, prepared, now, tools)
        if before(export, "counted"):
            stage_count(opened, settings, export, run, now, tools)
        if before(export, "rescanned"):
            stage_rescan(opened, settings, export, run, now, tools)
        if before(export, "reported"):
            stage_report(opened, settings, export, run, now, tools)
    except ReportPending:
        monitoring.log("import left to resume", export=export_id, stage="reported")
        found = opened.get(ImportRun, run_id)
        assert found is not None
        return found
    except Exception as error:
        failed(opened, settings, export_id, run_id, error, now, tools)
        found = opened.get(ImportRun, run_id)
        assert found is not None
        return found
    export.status = "imported"
    run.status, run.finished = "done", now
    mark(opened, export, run, "done")
    events.record(opened, now, "import_done", f"export {export.id}", summary_of(export, run))
    opened.commit()
    monitoring.log("imported", export=export.id, added=run.added, review=run.review, failed=run.failed)
    return run


def import_export(opened: Session, settings: config.Settings, export: Export, now: float, tools: Tools | None = None) -> ImportRun:
    if export.status == "imported":
        raise AlreadyImported("this export is already imported")
    if export.status not in IMPORTABLE:
        raise NotReady("this export has not been downloaded yet")
    with account_lock(settings, export.account_id):
        opened.refresh(export)
        if export.status == "imported":
            raise AlreadyImported("this export is already imported")
        return run_stages(opened, settings, export, now, tools or Tools())


def failed_items_run(opened: Session, export: Export) -> ImportRun | None:
    last = latest_run(opened, export.id)
    return last if last is not None and last.status == "done" and last.failed > 0 else None


def staging_of(opened: Session, photos: Photos, export: Export) -> Path | None:
    owner = None if export.owner_person_id is None else opened.get(Person, export.owner_person_id)
    slugs = [owner.slug] if owner is not None else opened.execute(select(Person.slug).order_by(Person.id)).scalars()
    for slug in slugs:
        try:
            found = photos.staging(slug, unpack.staging_name(export))
        except paths.OutsideRoot:
            continue
        if found.is_dir() and not found.is_symlink():
            return found
    return None


def request_retry(opened: Session, export: Export, photos: Photos | None, now: float) -> Export:
    if failed_items_run(opened, export) is None:
        raise AlreadyImported("this export is already imported")
    if photos is None or staging_of(opened, photos, export) is None:
        raise NotReady("the staging folder of this export is gone, so its failed items cannot be retried")
    export.status, export.stage = "importing", "unpacked"
    opened.add(ImportRun(export_id=export.id, started=now, status="queued", stage="unpacked"))
    opened.flush()
    return export


def request_import(opened: Session, export_id: int, photos: Photos | None = None, now: float | None = None) -> Export:
    export = opened.get(Export, export_id)
    if export is None:
        raise LookupError("no such export")
    if export.status == "imported":
        return request_retry(opened, export, photos, utc_now().timestamp() if now is None else now)
    if export.status not in IMPORTABLE or (export.status == "failed" and export.inbox_dir is None):
        raise NotReady("this export has not been downloaded yet")
    last = latest_run(opened, export.id)
    waiting = last is not None and last.status in WAITING
    if export.stage in EARLY_STAGES and not waiting:
        export.stage = "queued"
        if export.status == "failed":
            export.status = "downloaded"
    elif last is not None and last.status != "running":
        last.status = "queued"
    opened.flush()
    return export


def waiting_exports(opened: Session) -> list[int]:
    queued = select(Export.id).where(Export.stage == "queued", Export.status.in_(IMPORTABLE - {"failed"}))
    resumed = (select(Export.id).join(ImportRun, ImportRun.export_id == Export.id)
               .where(ImportRun.status.in_(WAITING), Export.status.in_(IMPORTABLE)))
    return sorted(set(opened.execute(queued).scalars()) | set(opened.execute(resumed).scalars()))


def run_waiting(opened: Session, settings: config.Settings, now: float, tools: Tools | None = None) -> list[int]:
    ran = []
    for export_id in waiting_exports(opened):
        export = opened.get(Export, export_id)
        if export is None:
            continue
        try:
            import_export(opened, settings, export, now, tools)
        except (Busy, AlreadyImported, NotReady) as error:
            monitoring.log("import skipped", export=export_id, reason=type(error).__name__)
            continue
        ran.append(export_id)
    return ran


def unmatched_by_person(opened: Session) -> dict[str, int]:
    found: dict[str, int] = {}
    for account_id in opened.execute(select(GoogleAccount.id).order_by(GoogleAccount.id)).scalars():
        run = opened.execute(
            select(ImportRun).join(Export, Export.id == ImportRun.export_id)
            .where(Export.account_id == account_id, ImportRun.status == "done", ImportRun.unmatched_by_person.is_not(None))
            .order_by(ImportRun.finished.desc(), ImportRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        counts = json.loads(run.unmatched_by_person) if run is not None and run.unmatched_by_person else {}
        for slug, count in counts.items():
            if isinstance(slug, str) and isinstance(count, int):
                found[slug] = max(found.get(slug, 0), count)
    return found
