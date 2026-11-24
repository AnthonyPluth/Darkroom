from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import events
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import REHEARSAL_DATABASES, RetentionAttestation

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


REASONS: Final = {
    "evidence": "--evidence must be the link to a comment on issue #266, https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-<number>",
    "rehearsed_on": "--rehearsed-on must be a date written YYYY-MM-DD",
    "rehearsed_in_future": "--rehearsed-on is after today",
    "immich_version": f"--immich-version must be one of the Immich versions Darkroom supports for managed imports: {', '.join(sorted(immich.MANAGED_VERSIONS))}",
    "darkroom_version": "--darkroom-version must be a Darkroom release tag, such as v5.9.0",
    "database": f"--database must be one of: {', '.join(REHEARSAL_DATABASES)}",
    "withdraw_what": "withdraw needs rehearsal or boundary",
    "nothing_active": "nothing is recorded to withdraw",
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
