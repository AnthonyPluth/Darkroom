from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property
from typing import Final, Literal

from sqlalchemy import Engine, Select, func, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, exports, immich_reconcile, managed_ingest, managed_metadata, media
from darkroom.domain.ingest import sidecar
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import (
    REHEARSAL_DATABASES,
    DedupeDecision,
    Export,
    ExportPart,
    ImportRun,
    ManagedAssetVersion,
    ManagedBackfillItem,
    ManagedCompound,
    ManagedCompoundMember,
    ManagedImportIntent,
    ManagedImportReceipt,
    MediaItem,
    Person,
    RecoveryPoint,
    RecoveryPointAsset,
    RetentionAttestation,
    ReviewItem,
    TakeoutEntry,
    TakeoutSidecar,
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


def owner_recorded(opened: Session) -> bool:
    boundary = active(opened, BOUNDARY)
    return active(opened, REHEARSAL) is not None and boundary is not None and accepts_current_text(boundary)


ReleaseUnit = ExportPart | TakeoutEntry | ReviewItem
RETENTION_CLOCK: Final = "retention_clock"
RELEASE_OFF: Final = "release_off"
NO_REHEARSAL: Final = "no_rehearsal"
BOUNDARY_NOT_ACCEPTED: Final = "boundary_not_accepted"
RECEIPT_NOT_COMMITTED: Final = "receipt_not_committed"
KEEPER_NOT_MANAGED: Final = "keeper_not_managed"
METADATA_NOT_IN_IMMICH: Final = "metadata_not_in_immich"
NO_RECOVERY_POINT: Final = "no_recovery_point"
NOT_COVERED: Final = "not_covered"
IMMICH_UNCONFIRMED: Final = "immich_unconfirmed"
RELEASE_PENDING: Final = "release_pending"
READ_DEADLINE: Final = 30.0
REFUSAL_ORDER: Final = (RETENTION_CLOCK, NO_REHEARSAL, BOUNDARY_NOT_ACCEPTED, RECEIPT_NOT_COMMITTED, KEEPER_NOT_MANAGED, METADATA_NOT_IN_IMMICH,
                        FAILED_KIND, NO_RECOVERY_POINT, NOT_COVERED, RELEASE_OFF)


@dataclass(frozen=True)
class Need:
    owner_id: str = field(repr=False)
    asset_id: str = field(repr=False)
    sha1: str = field(repr=False)
    verified_at: float
    kind: str | None = None
    live_video_id: str | None = field(default=None, repr=False)
    google_sidecar: str | None = field(default=None, repr=False)
    offset_minutes: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Release:
    point_id: int
    valid_until: float
    person_id: int
    owner_id: str = field(repr=False)
    needs: frozenset[Need]


@dataclass(frozen=True)
class Confirmation:
    reason: str | None = None
    stop: bool = False
    slow_reads: int | None = None


@dataclass(frozen=True)
class Dependency:
    needs: tuple[Need, ...] = ()
    reason: str | None = None
    metadata_recorded: bool = True


def enclosing_export(opened: Session, unit: ReleaseUnit) -> Export | None:
    if isinstance(unit, ReviewItem):
        decision = opened.get(DedupeDecision, unit.decision_id)
        return None if decision is None else opened.get(Export, decision.export_id)
    return opened.get(Export, unit.export_id)


def clock_allows(opened: Session, export: Export, unit: ReleaseUnit, now: float) -> bool:
    if export.status != "imported":
        return False
    cutoff = now - WINDOW
    if isinstance(unit, ReviewItem) and (unit.status == "pending" or unit.resolved_at is None or unit.resolved_at > cutoff):
        return False
    run = opened.execute(select(ImportRun).where(ImportRun.export_id == export.id).order_by(ImportRun.id.desc()).limit(1)).scalar_one_or_none()
    return run is not None and run.status == "done" and run.finished is not None and run.finished <= cutoff


def receipt_need(opened: Session, intent: ManagedImportIntent, key: str, sha1: str | None, size: int | None = None) -> Dependency:
    receipt = opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.intent_id == intent.id, ManagedImportReceipt.resource_key == key,
                                                               ManagedImportReceipt.source_sha1 == sha1)).scalar_one_or_none()
    if (receipt is None or receipt.state != "committed" or receipt.metadata_hold is not None or receipt.asset_id is None or receipt.committed_at is None
            or (size is not None and receipt.expected_bytes != size)):
        return Dependency(reason=RECEIPT_NOT_COMMITTED)
    if receipt.compound_key == key:
        return Dependency((Need(intent.owner_id, receipt.asset_id, receipt.source_sha1, receipt.committed_at),))
    members = opened.execute(select(ManagedCompoundMember.role, ManagedImportReceipt.asset_id, ManagedImportReceipt.source_sha1,
                                    ManagedImportReceipt.committed_at).join_from(ManagedImportReceipt,
        ManagedCompoundMember, (ManagedCompoundMember.intent_id == intent.id) & (ManagedCompoundMember.resource_key == ManagedImportReceipt.resource_key)
        & (ManagedCompoundMember.source_sha1 == ManagedImportReceipt.source_sha1) & (ManagedCompoundMember.expected_bytes == ManagedImportReceipt.expected_bytes))
        .join(ManagedCompound, ManagedCompound.id == ManagedCompoundMember.compound_id).where(
            ManagedCompound.intent_id == intent.id, ManagedCompound.compound_key == receipt.compound_key, ManagedCompound.committed_at.is_not(None),
            ManagedCompound.hold.is_(None), ManagedImportReceipt.intent_id == intent.id, ManagedImportReceipt.compound_key == receipt.compound_key,
            ManagedImportReceipt.state == "committed", ManagedImportReceipt.metadata_hold.is_(None))).all()
    roles = {role: (asset, digest, at) for role, asset, digest, at in members if asset is not None and at is not None}
    if len(members) != 2 or roles.keys() != {"live_still", "live_video"}:
        return Dependency(reason=RECEIPT_NOT_COMMITTED)
    still, video = roles["live_still"], roles["live_video"]
    if still[0] == video[0] or not any(asset == receipt.asset_id and digest == receipt.source_sha1 for asset, digest, _ in (still, video)):
        return Dependency(reason=RECEIPT_NOT_COMMITTED)
    return Dependency((Need(intent.owner_id, *still, kind="IMAGE", live_video_id=video[0]), Need(intent.owner_id, *video, kind="VIDEO")))


def metadata_shown(raw: str | None, offset_minutes: int | None, seen: object) -> bool:
    if raw is None:
        return True
    google = sidecar.parse(raw)
    if google is None:
        return False
    if not isinstance(seen, dict):
        return False
    if google.lat is not None and google.lon is not None:
        lat, lon = seen.get("latitude"), seen.get("longitude")
        if (not isinstance(lat, int | float) or not isinstance(lon, int | float) or not managed_metadata.number(lat) or not managed_metadata.number(lon)
                or abs(lat - google.lat) >= managed_metadata.SAME_PLACE_DEGREES or abs(lon - google.lon) >= managed_metadata.SAME_PLACE_DEGREES):
            return False
    if google.description is not None:
        described = seen.get("description")
        if not isinstance(described, str) or managed_metadata.normalized(described) != managed_metadata.normalized(google.description):
            return False
    if google.photo_taken_utc is None:
        return True
    taken, zone = seen.get("taken"), seen.get("zone")
    instant = managed_metadata.instant_of(taken if isinstance(taken, str) else None)
    if instant is None or abs(instant - google.photo_taken_utc) >= managed_metadata.SAME_INSTANT_SECONDS:
        return False
    offset = offset_minutes if google.lat is None else media.wall_clock_at(google.photo_taken_utc, google.lat, google.lon)[1]
    return offset is not None and managed_metadata.zone_offset(zone if isinstance(zone, str) else None, instant) == offset


def keeper_need(opened: Session, intent: ManagedImportIntent, decision: DedupeDecision, raw: str | None, metadata_pending: bool = False) -> Dependency:
    item = None if decision.matched_item_id is None else opened.get(MediaItem, decision.matched_item_id)
    if item is None or item.person_id != intent.person_id:
        return Dependency(reason=KEEPER_NOT_MANAGED)
    version = opened.execute(select(ManagedAssetVersion).join(ManagedBackfillItem, ManagedBackfillItem.version_id == ManagedAssetVersion.id).where(
        ManagedBackfillItem.item_id == item.id, ManagedBackfillItem.person_id == intent.person_id, ManagedBackfillItem.outcome == "mapped",
        ManagedAssetVersion.person_id == intent.person_id, ManagedAssetVersion.owner_id == intent.owner_id, ManagedAssetVersion.state == "bytes_verified",
        ManagedAssetVersion.library_id.is_(None), ManagedAssetVersion.sha1 == item.sha1, ManagedAssetVersion.bytes == item.bytes)).scalar_one_or_none()
    if version is None or version.bytes_verified_at is None:
        return Dependency(reason=KEEPER_NOT_MANAGED)
    try:
        seen = json.loads(version.metadata_snapshot)
    except (ValueError, RecursionError):
        seen = None
    need = Need(version.owner_id, version.asset_id, version.sha1, version.bytes_verified_at, version.kind,
                version.linked_video_asset_id, raw, item.offset_minutes)
    return Dependency((need,), METADATA_NOT_IN_IMMICH if metadata_pending else None, metadata_shown(raw, item.offset_minutes, seen))


def review_dependency(opened: Session, intent: ManagedImportIntent, decision: DedupeDecision, review: ReviewItem, sha1: str | None) -> Dependency:
    if review.hold_path is None or sha1 is None or review.hold_sha1 != sha1:
        return Dependency(reason=KEEPER_NOT_MANAGED)
    if review.status == "kept_both":
        return receipt_need(opened, intent, f"{managed_ingest.KEPT}{review.id}", sha1)
    if review.status == "skipped":
        return keeper_need(opened, intent, decision, review.sidecar_json)
    return Dependency(reason=None if review.status == "pending" else KEEPER_NOT_MANAGED)


def entry_dependency(opened: Session, intent: ManagedImportIntent, export: Export, entry: TakeoutEntry) -> Dependency:
    root = entry
    if entry.outcome == "copy":
        copied = None if entry.copy_of_id is None else opened.get(TakeoutEntry, entry.copy_of_id)
        if copied is None or copied.export_id != export.id or copied.sha1 != entry.sha1:
            return Dependency(reason=KEEPER_NOT_MANAGED)
        root = copied
    if (root.kind != "media" or root.outcome not in ("placed", "skipped", "review") or root.copy_of_id is not None or root.sha1 is None
            or root.decision_id is None):
        return Dependency(reason=KEEPER_NOT_MANAGED)
    decision = opened.get(DedupeDecision, root.decision_id)
    if (decision is None or decision.export_id != export.id or decision.account_id != export.account_id or decision.person_id != intent.person_id
            or decision.reversed_at is not None):
        return Dependency(reason=KEEPER_NOT_MANAGED)
    if root.outcome == "review":
        review = opened.execute(select(ReviewItem).where(ReviewItem.decision_id == decision.id)).scalar_one_or_none()
        return Dependency(reason=KEEPER_NOT_MANAGED) if review is None else review_dependency(opened, intent, decision, review, root.sha1)
    if root.outcome == "skipped" and decision.decided_by == "owner" and decision.matched_item_id is None:
        return Dependency()
    if root.outcome == "placed" and managed_ingest.wanted(opened, export, root):
        return receipt_need(opened, intent, managed_ingest.resource_key(root), root.sha1, root.bytes)
    if root.outcome == "skipped" and decision.decided_by == "auto" and decision.tier in (1, 2, 3):
        raw = opened.execute(select(TakeoutSidecar.text).where(TakeoutSidecar.export_id == export.id,
                                                              TakeoutSidecar.entry_id == root.sidecar_entry_id)).scalar_one_or_none()
        return keeper_need(opened, intent, decision, raw, root.metadata_pending or (root.sidecar_entry_id is not None and raw is None))
    return Dependency(reason=KEEPER_NOT_MANAGED)


def dependencies(opened: Session, export: Export, intent: ManagedImportIntent, unit: ReleaseUnit) -> list[Dependency]:
    if isinstance(unit, ReviewItem):
        decision = opened.get(DedupeDecision, unit.decision_id)
        if (decision is None or decision.account_id != export.account_id or decision.person_id != intent.person_id or decision.reversed_at is not None):
            return [Dependency(reason=KEEPER_NOT_MANAGED)]
        return [review_dependency(opened, intent, decision, unit, unit.hold_sha1)]
    if isinstance(unit, TakeoutEntry):
        return [entry_dependency(opened, intent, export, unit)]
    rows = opened.execute(select(TakeoutEntry).where(TakeoutEntry.export_id == export.id, TakeoutEntry.part_number == unit.number)).scalars().all()
    if unit.ingest_state != "ingested" or unit.entries is None or len(rows) != unit.entries:
        return [Dependency(reason=KEEPER_NOT_MANAGED)]
    return [entry_dependency(opened, intent, export, row) for row in rows if row.kind == "media"]


def backup_health_failed(opened: Session, now: float) -> bool:
    latest = latest_finished(opened)
    return (latest is not None and latest.state != "succeeded") or opened.execute(select(RecoveryPoint.id).where(
        RecoveryPoint.state == "open", RecoveryPoint.opened_at <= now - OPEN_LIMIT).limit(1)).first() is not None


def eligible_points(opened: Session, now: float) -> list[RecoveryPoint]:
    return list(opened.execute(select(RecoveryPoint).where(
        RecoveryPoint.state == "succeeded", RecoveryPoint.scopes == ",".join(SCOPES), RecoveryPoint.taken_at.between(now - WINDOW, now),
        RecoveryPoint.kept_until > now).order_by(RecoveryPoint.opened_at.desc(), RecoveryPoint.id.desc())).scalars())


def manifest_rows(opened: Session, point_id: int) -> set[tuple[str, str, str]]:
    rows = opened.execute(select(RecoveryPointAsset.owner_id, RecoveryPointAsset.asset_id, RecoveryPointAsset.sha1)
                          .where(RecoveryPointAsset.recovery_point_id == point_id))
    return {(owner, asset, digest) for owner, asset, digest in rows}


class Coverage:
    def __init__(self, opened: Session, now: float) -> None:
        self.opened, self.now = opened, now
        self.manifests: dict[int, set[tuple[str, str, str]] | None] = {}

    @cached_property
    def failed(self) -> bool:
        return backup_health_failed(self.opened, self.now)

    @cached_property
    def points(self) -> list[RecoveryPoint]:
        return eligible_points(self.opened, self.now)

    def covers(self, point: RecoveryPoint, needs: frozenset[Need]) -> bool:
        if any(need.verified_at > point.opened_at for need in needs):
            return False
        if point.id not in self.manifests:
            found = manifest_rows(self.opened, point.id)
            self.manifests[point.id] = found if len(found) == point.manifest_count and manifest_sha256(found) == point.manifest_sha256 else None
        listed = self.manifests[point.id]
        return listed is not None and all((need.owner_id, need.asset_id, need.sha1) in listed for need in needs)


def release_check(opened: Session, unit: ReleaseUnit, now: float, release_enabled: bool, coverage: Coverage | None = None, *,
                  confirm_metadata_live: bool = False) -> Release | str:
    export = enclosing_export(opened, unit)
    if export is None or not clock_allows(opened, export, unit, now):
        return RETENTION_CLOCK
    if not release_enabled:
        return RELEASE_OFF
    if active(opened, REHEARSAL) is None:
        return NO_REHEARSAL
    boundary = active(opened, BOUNDARY)
    if boundary is None or not accepts_current_text(boundary):
        return BOUNDARY_NOT_ACCEPTED
    uncommitted = select(ManagedImportReceipt.id).join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id).where(
        ManagedImportIntent.export_id == export.id, ManagedImportIntent.kind == "import",
        (ManagedImportReceipt.state != "committed") | ManagedImportReceipt.metadata_hold.is_not(None)).limit(1)
    if opened.execute(uncommitted).first() is not None:
        return RECEIPT_NOT_COMMITTED
    intent = managed_ingest.intent_of(opened, export)
    person = exports.owner_of(opened, export)
    if person is None or intent is None or person.id != intent.person_id or person.immich_user_id != intent.owner_id:
        return KEEPER_NOT_MANAGED
    required = dependencies(opened, export, intent, unit)
    reasons = {found.reason for found in required}
    if not confirm_metadata_live and any(not found.metadata_recorded for found in required):
        reasons.add(METADATA_NOT_IN_IMMICH)
    for reason in (RECEIPT_NOT_COMMITTED, KEEPER_NOT_MANAGED, METADATA_NOT_IN_IMMICH):
        if reason in reasons:
            return reason
    coverage = Coverage(opened, now) if coverage is None else coverage
    if coverage.failed:
        return FAILED_KIND
    if not coverage.points:
        return NO_RECOVERY_POINT
    needs = frozenset(need for found in required for need in found.needs)
    for point in coverage.points:
        if point.kept_until is not None and point.taken_at is not None and coverage.covers(point, needs):
            return Release(point.id, min(point.kept_until, point.taken_at + WINDOW), person.id, intent.owner_id, needs)
    return NOT_COVERED


def unit_refusal(opened: Session, unit: ReleaseUnit, now: float, release_enabled: bool, coverage: Coverage, *, confirm_metadata_live: bool = False) -> str | None:
    checked = release_check(opened, unit, now, release_enabled, coverage, confirm_metadata_live=confirm_metadata_live)
    return checked if isinstance(checked, str) else None


def confirm_release(settings: config.Settings, person: Person, release: Release, now: float) -> Confirmation:
    key = key_of(settings, person)
    if key is None:
        return Confirmation(IMMICH_UNCONFIRMED, stop=True)
    if now >= release.valid_until:
        return Confirmation(NO_RECOVERY_POINT)
    deadline = time.monotonic() + min(READ_DEADLINE, release.valid_until - now)
    try:
        reader = immich.ManagedReader(immich.Client.at(settings), key, release.owner_id, access="read_or_import")
        reads = reader.custody_all(sorted({need.asset_id for need in release.needs}), deadline)
    except immich.ImmichFailed:
        return Confirmation(IMMICH_UNCONFIRMED, stop=True, slow_reads=0 if time.monotonic() >= deadline else None)
    if time.monotonic() >= deadline:
        return Confirmation(IMMICH_UNCONFIRMED, stop=not reads.found, slow_reads=len(reads.found))
    if not reads.complete:
        return Confirmation(IMMICH_UNCONFIRMED, stop=True)
    if reads.refused:
        return Confirmation(IMMICH_UNCONFIRMED)
    for need in release.needs:
        found = reads.found.get(need.asset_id)
        if found is None:
            return Confirmation(IMMICH_UNCONFIRMED)
        try:
            asset = immich.asset_of(found)
        except immich.ImmichFailed:
            return Confirmation(IMMICH_UNCONFIRMED)
        if (asset.sha1 != need.sha1 or (need.kind is not None and asset.kind != need.kind)
                or (need.live_video_id is not None and asset.live_video_id != need.live_video_id)):
            return Confirmation(IMMICH_UNCONFIRMED)
        actual = immich.extracted_of(found.get("exifInfo")) if need.google_sidecar is not None else None
        if not metadata_shown(need.google_sidecar, need.offset_minutes, {} if actual is None else asdict(actual)):
            return Confirmation(METADATA_NOT_IN_IMMICH)
    if time.monotonic() >= deadline:
        return Confirmation(IMMICH_UNCONFIRMED, slow_reads=len(reads.found))
    return Confirmation()


def refusal(opened: Session, unit: ReleaseUnit, now: float, *, release_enabled: bool = False) -> str | None:
    return unit_refusal(opened, unit, now, release_enabled, Coverage(opened, now))


def remaining_parts() -> Select[ExportPart]:
    return select(ExportPart).where(ExportPart.filename.is_not(None) | (ExportPart.free_state == "freeing"),
                                    ExportPart.free_state.is_distinct_from("freed") | ExportPart.freed_at.is_(None))


def remaining_held_copies() -> Select[TakeoutEntry]:
    return select(TakeoutEntry).where(TakeoutEntry.kind == "media",
                                     TakeoutEntry.held_path.is_not(None) | (TakeoutEntry.held_state == "freeing"),
                                     TakeoutEntry.held_state.is_distinct_from("freed") | TakeoutEntry.held_freed_at.is_(None))


def review_holds() -> Select[ReviewItem]:
    return select(ReviewItem).join(DedupeDecision, DedupeDecision.id == ReviewItem.decision_id).where(ReviewItem.hold_path.is_not(None))


def units(opened: Session, export_id: int) -> Iterable[ReleaseUnit]:
    yield from opened.scalars(remaining_parts().where(ExportPart.export_id == export_id))
    yield from opened.scalars(remaining_held_copies().where(TakeoutEntry.export_id == export_id))
    yield from opened.scalars(review_holds().where(DedupeDecision.export_id == export_id, ReviewItem.status != "pending"))


def export_refusals(opened: Session, export_id: int, now: float, coverage: Coverage | None = None, *, release_enabled: bool = False) -> Counter[str]:
    checked = Coverage(opened, now) if coverage is None else coverage
    reasons: Counter[str] = Counter()
    for unit in units(opened, export_id):
        can_release = release_enabled and not isinstance(unit, ReviewItem)
        reason = unit_refusal(opened, unit, now, True, checked, confirm_metadata_live=can_release)
        reasons[reason or (RELEASE_PENDING if can_release else RELEASE_OFF)] += 1
    return reasons


def cleanup_detail(opened: Session, export_id: int, now: float, *, release_enabled: bool = False) -> str | None:
    reasons = export_refusals(opened, export_id, now, release_enabled=release_enabled)
    reason = next((reason for reason in REFUSAL_ORDER[1:] if reasons[reason]), None)
    return managed_ingest.KEPT_ONLY if reason == NO_REHEARSAL else reason


def recovery_status(opened: Session, now: float, *, release_enabled: bool = False) -> list[str]:
    latest_open = opened.execute(select(RecoveryPoint).where(RecoveryPoint.state == "open")
                                 .order_by(RecoveryPoint.opened_at.desc(), RecoveryPoint.id.desc()).limit(1)).scalar_one_or_none()
    points = [f"{point.id} ({point.state})" for point in (latest_open, latest_finished(opened)) if point is not None]
    counts: Counter[str] = Counter()
    coverage = Coverage(opened, now)
    for export_id in waiting_exports(opened, now):
        counts.update(export_refusals(opened, export_id, now, coverage, release_enabled=release_enabled))
    waiting = [f"  {reason}: {counts[reason]}" for reason in (*REFUSAL_ORDER[1:], RELEASE_PENDING) if counts[reason]]
    return [f"Latest recovery points: {', '.join(points) or 'none'}", "Managed files waiting:", *(waiting or ["  none"])]


def waiting_exports(opened: Session, now: float) -> list[int]:
    done = select(ImportRun.export_id, func.max(ImportRun.finished).label("finished")).where(ImportRun.status == "done").group_by(ImportRun.export_id).subquery()
    managed = select(ManagedImportIntent.export_id).where(ManagedImportIntent.kind == "import")
    has_files = (remaining_parts().where(ExportPart.export_id == Export.id).exists()
                 | remaining_held_copies().where(TakeoutEntry.export_id == Export.id).exists()
                 | review_holds().where(DedupeDecision.export_id == Export.id).exists())
    return list(opened.execute(select(Export.id).join(done, Export.id == done.c.export_id).where(
        Export.status == "imported", Export.id.in_(managed), done.c.finished <= now - WINDOW, has_files).order_by(Export.id)).scalars())


def waiting_needs(opened: Session, exports: list[int]) -> set[tuple[str, str, str]]:
    rows = opened.execute(select(ManagedImportIntent.owner_id, ManagedImportReceipt.asset_id, ManagedImportReceipt.source_sha1).join(
        ManagedImportReceipt, ManagedImportReceipt.intent_id == ManagedImportIntent.id).where(
        ManagedImportIntent.export_id.in_(exports), ManagedImportIntent.kind == "import", ManagedImportReceipt.state == "committed",
        ManagedImportReceipt.asset_id.is_not(None)))
    return {(owner_id, asset_id, sha1) for owner_id, asset_id, sha1 in rows if asset_id is not None}


def covering_exists(opened: Session, now: float, needs: set[tuple[str, str, str]]) -> bool:
    return any(needs <= manifest_rows(opened, point.id) for point in eligible_points(opened, now))


def waiting_since(opened: Session, exports: list[int]) -> float | None:
    found = opened.execute(select(func.max(ImportRun.finished)).where(ImportRun.status == "done", ImportRun.export_id.in_(exports)).group_by(
        ImportRun.export_id)).scalars().all()
    finished = [moment for moment in found if moment is not None]
    return min(finished) + WINDOW if finished else None


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
    waiting = waiting_exports(opened, now)
    since = waiting_since(opened, waiting)
    stale = since is not None and now - since >= STALE_AFTER and owner_recorded(opened) and not covering_exists(opened, now, waiting_needs(opened, waiting))
    for key, kind, message, raised in ((FAILED_KEY, FAILED_KIND, FAILED_MESSAGE, failed), (STALE_KEY, STALE_KIND, STALE_MESSAGE, stale)):
        if raised:
            alerts.open_alert(opened, settings, key, kind, "recovery points", alerts.Wording(alerts.TITLES[kind], message), None, now, notifier=notifier, about=alerts.NOBODY)
        else:
            alerts.close(opened, key, now)
    return Watched(abandoned, failed, stale)
