from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final, Literal

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, immich_reconcile
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import (
    REHEARSAL_DATABASES,
    Export,
    ImportRun,
    ManagedAssetVersion,
    ManagedImportIntent,
    ManagedImportReceipt,
    Person,
    RecoveryPoint,
    RecoveryPointAsset,
    RetentionAttestation,
)

Kind = Literal["restore_rehearsal", "remote_deletion_boundary"]
REHEARSAL: Final[Kind] = "restore_rehearsal"
BOUNDARY: Final[Kind] = "remote_deletion_boundary"
WITHDRAWABLE: Final[dict[str, Kind]] = {"rehearsal": REHEARSAL, "boundary": BOUNDARY}
EVIDENCE: Final = re.compile(r"https://github\.com/AnthonyPluth/Darkroom/issues/266#issuecomment-[1-9][0-9]{0,19}")
DAY: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
RECORDED: Final = "retention_recorded"
WITHDRAWN: Final = "retention_withdrawn"

BOUNDARY_TEXT: Final = (
    "Darkroom cannot make Immich and Darkroom atomic. Another Immich client can trash or delete a photo after Darkroom's last read of it "
    "and before Darkroom deletes the inbox copy; Darkroom's write lock coordinates only Darkroom's own jobs. Darkroom reads every photo again "
    "under its write lock right before each delete, which narrows that window to the moment between the read and the delete, and a recovery point "
    "covers that moment: a photo removed in it is still in a backup Darkroom has evidence for. Darkroom also cannot check that a backup really "
    "restores; it relies on your restore rehearsal and on the backup job's own report of success."
)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


SCOPES: Final = ("darkroom-db", "darkroom-secret", "immich-db", "immich-media")
MAX_KEPT_DAYS: Final = 3650
TOOL: Final = re.compile(r"[a-z0-9-]{1,32}")
SNAPSHOT: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")
NUMBER: Final = re.compile(r"[1-9][0-9]{0,8}")
DAY_SECONDS: Final = 24 * 3600.0
OPEN_LIMIT: Final = DAY_SECONDS
STALE_AFTER: Final = 3 * DAY_SECONDS
WINDOW: Final = 14 * DAY_SECONDS
FINISHED: Final = "recovery_point_finished"
ABANDONED: Final = "recovery_point_abandoned"
NOT_FINISHED: Final = "not_finished"
REPORTED_FAILED: Final = "reported_failed"
SCOPES_MISSING: Final = "scopes_missing"
FAILED_KEY: Final = "retention:backup_failed"
STALE_KEY: Final = "retention:backup_stale"
FAILED_KIND: Final = "backup_failed"
STALE_KIND: Final = "backup_stale"

REASONS: Final = {
    "evidence": "--evidence must be the link to a comment on issue #266, https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-<number>",
    "rehearsed_on": "--rehearsed-on must be a date written YYYY-MM-DD",
    "rehearsed_in_future": "--rehearsed-on is after today",
    "immich_version": f"--immich-version must be one of the Immich versions Darkroom supports for managed imports: {', '.join(sorted(immich.MANAGED_VERSIONS))}",
    "darkroom_version": "--darkroom-version must be a Darkroom release tag, such as v5.9.0",
    "database": f"--database must be one of: {', '.join(REHEARSAL_DATABASES)}",
    "withdraw_what": "withdraw needs rehearsal or boundary",
    "nothing_active": "nothing is recorded to withdraw",
    "point_unknown": "the recovery point is not a number Darkroom has recorded",
    "point_not_open": "that recovery point is not open: it was already finished or was abandoned, and a finished or abandoned point is never reopened",
    "status": "--status must be succeeded or failed",
    "point_expired": "that recovery point has been open longer than the limit, so it counts as abandoned and cannot be finished",
    "taken_at": "--taken-at must be a UTC time with its offset, such as 2026-11-24T03:15:00Z",
    "taken_before_opened": "--taken-at is before the recovery point was opened",
    "taken_in_future": "--taken-at is after now",
    "kept_for_days": f"--kept-for-days must be a whole number of days from 1 to {MAX_KEPT_DAYS}",
    "scopes": f"--scopes must be a comma-separated list of: {', '.join(SCOPES)}",
    "tool": "--tool must be a label of 1 to 32 lowercase letters, digits and hyphens",
    "snapshot": "--snapshot must be 1 to 128 letters, digits and . _ : - characters, never a path",
    "succeeded_needs": "a succeeded report needs --taken-at, --kept-for-days, --scopes and --tool",
    "manifest_changed": "the recovery point's manifest no longer matches the digest recorded when it was opened",
}


class Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return REASONS[self.reason]


@dataclass(frozen=True)
class Rehearsal:
    evidence_url: str
    rehearsed_on: str
    immich_version: str
    darkroom_version: str
    database_kind: str


@dataclass(frozen=True)
class Attestation:
    id: int
    kind: str
    recorded_at: float
    evidence_url: str
    rehearsed_on: str | None
    immich_version: str | None
    darkroom_version: str | None
    database_kind: str | None
    boundary_text_sha256: str | None


@dataclass(frozen=True)
class Status:
    rehearsal: Attestation | None
    boundary: Attestation | None
    withdrawn: int


def accepts_current_text(found: Attestation) -> bool:
    return found.boundary_text_sha256 == text_sha256(BOUNDARY_TEXT)


def checked_evidence(url: str) -> str:
    if not EVIDENCE.fullmatch(url):
        raise Refused("evidence")
    return url


def checked_rehearsal(evidence_url: str, rehearsed_on: str, immich_version: str, darkroom_version: str, database_kind: str, today: str) -> Rehearsal:
    checked_evidence(evidence_url)
    if not DAY.fullmatch(rehearsed_on):
        raise Refused("rehearsed_on")
    try:
        day = date.fromisoformat(rehearsed_on)
    except ValueError:
        raise Refused("rehearsed_on") from None
    if day > date.fromisoformat(today):
        raise Refused("rehearsed_in_future")
    if immich_version not in immich.MANAGED_VERSIONS:
        raise Refused("immich_version")
    if not config.RELEASE_VERSION.fullmatch(darkroom_version):
        raise Refused("darkroom_version")
    if database_kind not in REHEARSAL_DATABASES:
        raise Refused("database")
    return Rehearsal(evidence_url, rehearsed_on, immich_version, darkroom_version, database_kind)


def active_row(opened: Session, kind: Kind) -> RetentionAttestation | None:
    return opened.execute(select(RetentionAttestation).where(RetentionAttestation.kind == kind, RetentionAttestation.withdrawn_at.is_(None))).scalar_one_or_none()


def withdraw_active(opened: Session, kind: Kind, now: float) -> RetentionAttestation | None:
    found = active_row(opened, kind)
    if found is not None:
        found.withdrawn_at = now
        opened.flush()
        events.record(opened, now, WITHDRAWN, f"attestation {found.id}", kind)
        monitoring.log("retention attestation withdrawn", kind=kind, id=found.id)
    return found


def store(opened: Session, now: float, row: RetentionAttestation) -> Attestation:
    kind: Kind = REHEARSAL if row.kind == REHEARSAL else BOUNDARY
    withdraw_active(opened, kind, now)
    opened.add(row)
    opened.flush()
    events.record(opened, now, RECORDED, f"attestation {row.id}", kind)
    monitoring.log("retention attestation recorded", kind=kind, id=row.id)
    return described(row)


def record_rehearsal(opened: Session, now: float, rehearsal: Rehearsal) -> Attestation:
    db.write_lock(opened)
    return store(opened, now, RetentionAttestation(kind=REHEARSAL, recorded_at=now, evidence_url=rehearsal.evidence_url, rehearsed_on=rehearsal.rehearsed_on,
                                                   immich_version=rehearsal.immich_version, darkroom_version=rehearsal.darkroom_version,
                                                   database_kind=rehearsal.database_kind))


def accept_boundary(opened: Session, now: float, evidence_url: str) -> Attestation:
    db.write_lock(opened)
    return store(opened, now, RetentionAttestation(kind=BOUNDARY, recorded_at=now, evidence_url=checked_evidence(evidence_url),
                                                   boundary_text_sha256=text_sha256(BOUNDARY_TEXT)))


def withdraw(opened: Session, now: float, which: str) -> Attestation:
    kind = WITHDRAWABLE.get(which)
    if kind is None:
        raise Refused("withdraw_what")
    db.write_lock(opened)
    found = withdraw_active(opened, kind, now)
    if found is None:
        raise Refused("nothing_active")
    return described(found)


def described(row: RetentionAttestation) -> Attestation:
    return Attestation(row.id, row.kind, row.recorded_at, row.evidence_url, row.rehearsed_on, row.immich_version, row.darkroom_version, row.database_kind, row.boundary_text_sha256)


def active(opened: Session, kind: Kind) -> Attestation | None:
    found = active_row(opened, kind)
    return None if found is None else described(found)


def status(opened: Session) -> Status:
    withdrawn = opened.execute(select(func.count()).select_from(RetentionAttestation).where(RetentionAttestation.withdrawn_at.is_not(None))).scalar_one()
    return Status(active(opened, REHEARSAL), active(opened, BOUNDARY), withdrawn)


OPENED: Final = "recovery_point_opened"
FAILED: Final = "recovery_point_failed"
UNREACHABLE: Final = "immich_unreachable"
RECEIPT: Final = "receipt"
VERSION: Final = "version"


@dataclass(frozen=True)
class Candidate:
    owner_id: str = field(repr=False)
    asset_id: str = field(repr=False)
    sha1: str = field(repr=False)
    source: str


@dataclass(frozen=True)
class Group:
    person_id: int
    owner_id: str = field(repr=False)
    candidates: tuple[Candidate, ...]
    key: str | None = field(repr=False)


@dataclass(frozen=True)
class Begun:
    point_id: int
    state: str
    listed: int
    confirmed: int
    refused: int


def manifest_sha256(rows: Iterable[tuple[str, str, str]]) -> str:
    return hashlib.sha256("".join(f"{owner} {asset} {sha1}\n" for owner, asset, sha1 in sorted(rows)).encode()).hexdigest()


def committed_by(opened: Session, opened_at: float) -> dict[tuple[int, str], dict[tuple[str, str], Candidate]]:
    found: dict[tuple[int, str], dict[tuple[str, str], Candidate]] = {}
    receipts = select(ManagedImportIntent.person_id, ManagedImportIntent.owner_id, ManagedImportReceipt.asset_id, ManagedImportReceipt.source_sha1).join(
        ManagedImportReceipt, ManagedImportReceipt.intent_id == ManagedImportIntent.id).where(
        ManagedImportReceipt.state == "committed", ManagedImportReceipt.committed_at <= opened_at, ManagedImportReceipt.asset_id.is_not(None)).order_by(
        ManagedImportReceipt.id)
    versions = select(ManagedAssetVersion.person_id, ManagedAssetVersion.owner_id, ManagedAssetVersion.asset_id, ManagedAssetVersion.sha1).where(
        ManagedAssetVersion.state == "bytes_verified", ManagedAssetVersion.bytes_verified_at <= opened_at).order_by(
        ManagedAssetVersion.id)
    for source, query in ((RECEIPT, receipts), (VERSION, versions)):
        for person_id, owner_id, asset_id, sha1 in opened.execute(query):
            if asset_id is None:
                continue
            found.setdefault((person_id, owner_id), {}).setdefault((asset_id, sha1), Candidate(owner_id, asset_id, sha1, source))
    return found


def key_of(settings: config.Settings, person: Person) -> str | None:
    try:
        return immich_reconcile.person_key(settings, person)
    except (immich_reconcile.NotReady, immich.ImmichFailed):
        return None


def groups_at(engine: Engine, settings: config.Settings, opened_at: float) -> list[Group]:
    with db.session(engine) as opened:
        return [Group(person_id, owner_id, tuple(candidates.values()), key_of(settings, opened.get_one(Person, person_id)))
                for (person_id, owner_id), candidates in sorted(committed_by(opened, opened_at).items())]


def confirmed_in(group: Group, client: immich.Client | None, settings: config.Settings) -> tuple[list[Candidate], bool]:
    if group.key is None:
        return [], False
    try:
        reader = immich.ManagedReader(client or immich.Client.at(settings), group.key, group.owner_id, access="read_or_import")
        reads = reader.custody_all(sorted({candidate.asset_id for candidate in group.candidates}))
    except immich.ImmichFailed:
        return [], False
    kept: dict[str, Candidate] = {}
    for candidate in group.candidates:
        found = reads.found.get(candidate.asset_id)
        try:
            same = found is not None and immich.asset_of(found).sha1 == candidate.sha1
        except immich.ImmichFailed:
            same = False
        if same:
            kept.setdefault(candidate.asset_id, candidate)
    return list(kept.values()), reads.complete or bool(reads.found)


def begin(engine: Engine, settings: config.Settings, opened_at: float, client: immich.Client | None = None) -> Begun:
    groups = groups_at(engine, settings, opened_at)
    listed = sum(len({candidate.asset_id for candidate in group.candidates}) for group in groups)
    confirmed: dict[tuple[str, str], Candidate] = {}
    answered = False
    for group in groups:
        kept, complete = confirmed_in(group, client, settings)
        for candidate in kept:
            confirmed.setdefault((candidate.owner_id, candidate.asset_id), candidate)
        answered = answered or complete
    unreachable = bool(groups) and not answered
    manifest = [] if unreachable else list(confirmed.values())
    rows = [(candidate.owner_id, candidate.asset_id, candidate.sha1) for candidate in manifest]
    with db.session(engine, writing=True) as opened:
        point = RecoveryPoint(opened_at=opened_at, state="failed" if unreachable else "open", manifest_count=len(rows), manifest_sha256=manifest_sha256(rows),
                              finished_at=opened_at if unreachable else None, failure=UNREACHABLE if unreachable else None)
        opened.add(point)
        opened.flush()
        opened.add_all(RecoveryPointAsset(recovery_point_id=point.id, owner_id=candidate.owner_id, asset_id=candidate.asset_id, sha1=candidate.sha1, source=candidate.source)
                       for candidate in manifest)
        events.record(opened, opened_at, FAILED if unreachable else OPENED, f"recovery point {point.id}", f"listed {listed}, confirmed {len(rows)}")
        monitoring.log("recovery point " + ("failed" if unreachable else "opened"), id=point.id, listed=listed, confirmed=len(rows))
        return Begun(point.id, point.state, listed, len(rows), listed - len(rows))


@dataclass(frozen=True)
class Report:
    succeeded: bool
    taken_at: float | None
    kept_for_days: int | None
    scopes: tuple[str, ...]
    tool: str | None
    snapshot_ref: str | None = field(repr=False)


@dataclass(frozen=True)
class Finished:
    point_id: int
    state: str
    failure: str | None
    kept_until: float | None


def point_number(text: str) -> int:
    if not NUMBER.fullmatch(text):
        raise Refused("point_unknown")
    return int(text)


def checked_time(text: str) -> float:
    if len(text) > 40:
        raise Refused("taken_at")
    try:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise Refused("taken_at")
        return moment.astimezone(UTC).timestamp()
    except (ValueError, OverflowError):
        raise Refused("taken_at") from None


def checked_scopes(text: str) -> tuple[str, ...]:
    if text == "":
        return ()
    named = text.split(",")
    if any(name not in SCOPES for name in named):
        raise Refused("scopes")
    return tuple(sorted(set(named)))


def checked_days(text: str) -> int:
    if not NUMBER.fullmatch(text) or int(text) > MAX_KEPT_DAYS:
        raise Refused("kept_for_days")
    return int(text)


def checked_report(status: str, taken_at: str | None, kept_for_days: str | None, scopes: str | None, tool: str | None, snapshot_ref: str | None) -> Report:
    if status not in ("succeeded", "failed"):
        raise Refused("status")
    if tool is not None and not TOOL.fullmatch(tool):
        raise Refused("tool")
    if snapshot_ref is not None and not SNAPSHOT.fullmatch(snapshot_ref):
        raise Refused("snapshot")
    taken = None if taken_at is None else checked_time(taken_at)
    days = None if kept_for_days is None else checked_days(kept_for_days)
    named = () if scopes is None else checked_scopes(scopes)
    if status == "succeeded" and (taken is None or days is None or scopes is None or tool is None):
        raise Refused("succeeded_needs")
    return Report(status == "succeeded", taken, days, named, tool, snapshot_ref)


def manifest_intact(opened: Session, point: RecoveryPoint) -> bool:
    rows = [(row.owner_id, row.asset_id, row.sha1) for row in opened.execute(select(RecoveryPointAsset).where(RecoveryPointAsset.recovery_point_id == point.id)).scalars()]
    return len(rows) == point.manifest_count and manifest_sha256(rows) == point.manifest_sha256


def finish(opened: Session, now: float, point_id: int, report: Report) -> Finished:
    db.write_lock(opened)
    point = opened.get(RecoveryPoint, point_id)
    if point is None:
        raise Refused("point_unknown")
    opened.refresh(point)
    if point.state != "open":
        raise Refused("point_not_open")
    if point.opened_at <= now - OPEN_LIMIT:
        raise Refused("point_expired")
    if report.succeeded:
        if report.taken_at is None or report.taken_at < point.opened_at:
            raise Refused("taken_before_opened")
        if report.taken_at > now:
            raise Refused("taken_in_future")
        if not manifest_intact(opened, point):
            raise Refused("manifest_changed")
    complete = report.succeeded and set(report.scopes) == set(SCOPES)
    point.finished_at = now
    point.tool = report.tool
    point.snapshot_ref = report.snapshot_ref
    point.scopes = ",".join(report.scopes) or None
    if complete and report.taken_at is not None and report.kept_for_days is not None:
        point.state = "succeeded"
        point.taken_at = report.taken_at
        point.kept_until = report.taken_at + report.kept_for_days * DAY_SECONDS
    else:
        point.state = "failed"
        point.failure = SCOPES_MISSING if report.succeeded else REPORTED_FAILED
    opened.flush()
    events.record(opened, now, FINISHED, f"recovery point {point.id}", point.state if point.failure is None else f"{point.state}: {point.failure}")
    monitoring.log("recovery point finished", id=point.id, state=point.state, failure=point.failure or "none", manifest=point.manifest_count)
    return Finished(point.id, point.state, point.failure, point.kept_until)


def abandon_stale(opened: Session, now: float) -> int:
    stale = opened.execute(select(RecoveryPoint).where(RecoveryPoint.state == "open", RecoveryPoint.opened_at <= now - OPEN_LIMIT).order_by(RecoveryPoint.id)).scalars().all()
    for point in stale:
        point.state = "abandoned"
        point.failure = NOT_FINISHED
        point.finished_at = now
        events.record(opened, now, ABANDONED, f"recovery point {point.id}", NOT_FINISHED)
        monitoring.log("recovery point abandoned", id=point.id, manifest=point.manifest_count)
    return len(stale)


def latest_finished(opened: Session) -> RecoveryPoint | None:
    return opened.execute(select(RecoveryPoint).where(RecoveryPoint.state != "open").order_by(RecoveryPoint.opened_at.desc(), RecoveryPoint.id.desc()).limit(1)).scalar_one_or_none()


def covering_exists(opened: Session, now: float) -> bool:
    found = opened.execute(select(RecoveryPoint.id).where(
        RecoveryPoint.state == "succeeded", RecoveryPoint.scopes == ",".join(SCOPES), RecoveryPoint.taken_at >= now - WINDOW, RecoveryPoint.kept_until > now).limit(1)).first()
    return found is not None


def waiting_since(opened: Session, now: float) -> float | None:
    done = select(ImportRun.export_id, func.max(ImportRun.finished).label("finished")).where(ImportRun.status == "done").group_by(ImportRun.export_id).subquery()
    managed = select(ManagedImportIntent.export_id).where(ManagedImportIntent.kind == "import")
    oldest = opened.execute(select(func.min(done.c.finished)).join(Export, Export.id == done.c.export_id).where(
        Export.status == "imported", Export.id.in_(managed), done.c.finished <= now - WINDOW)).scalar_one_or_none()
    return None if oldest is None else float(oldest) + WINDOW


FAILED_MESSAGE: Final = ("The last Immich backup Darkroom was told about did not succeed, so Darkroom keeps every Immich import's files in the inbox. "
                         "Check the backup job.")
STALE_MESSAGE: Final = "No recent Immich backup covers the photos Darkroom is keeping, so their inbox files stay."


@dataclass(frozen=True)
class Watched:
    abandoned: int
    backup_failed: bool
    backup_stale: bool


def watch(opened: Session, settings: config.Settings, now: float, notifier: alerts.Notifier | None = None) -> Watched:
    db.write_lock(opened)
    abandoned = abandon_stale(opened, now)
    latest = latest_finished(opened)
    failed = latest is not None and latest.state != "succeeded"
    since = waiting_since(opened, now)
    stale = since is not None and now - since >= STALE_AFTER and not covering_exists(opened, now)
    for key, kind, message, raised in ((FAILED_KEY, FAILED_KIND, FAILED_MESSAGE, failed), (STALE_KEY, STALE_KIND, STALE_MESSAGE, stale)):
        if raised:
            alerts.open_alert(opened, settings, key, kind, "recovery points", alerts.Wording(alerts.TITLES[kind], message), None, now, notifier=notifier, about=alerts.NOBODY)
        else:
            alerts.close(opened, key, now)
    return Watched(abandoned, failed, stale)
