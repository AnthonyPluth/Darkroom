from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, or_, update
from sqlalchemy.orm import Session

from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import ManagedImportIntent, ManagedImportReceipt

MAX_SENDS = 3
LEASE_SECONDS = 3600.0
VERIFIED = ("bytes_verified", "metadata_verified", "committed")
Outcome = Literal["verified", "unusable", "pending", "held"]
Body = Callable[[], immich.MultipartUpload]


@dataclass(frozen=True)
class Report:
    outcome: Outcome
    reason: str


class Moved(Exception):
    pass


def fresh(opened: Session, receipt_id: int) -> ManagedImportReceipt:
    row = opened.get(ManagedImportReceipt, receipt_id, populate_existing=True)
    if row is None:
        raise LookupError("no such managed import receipt")
    opened.expunge(row)
    opened.commit()
    return row


def move(opened: Session, row: ManagedImportReceipt, before: tuple[str, ...], now: float, *checks: ColumnElement[bool], **values: object) -> bool:
    opened.commit()
    db.write_lock(opened)
    where = [ManagedImportReceipt.id == row.id, ManagedImportReceipt.state.in_(before), *checks]
    if row.asset_id is not None:
        where.append(ManagedImportReceipt.asset_id == row.asset_id)
    changed = opened.connection().execute(update(ManagedImportReceipt).where(*where).values(updated_at=now, **values)).rowcount == 1
    opened.commit()
    return changed


def claim(opened: Session, row: ManagedImportReceipt, now: float) -> int:
    attempt, lease = row.send_attempts + 1, ManagedImportReceipt.send_lease_until
    if not move(opened, row, (row.state,), now, ManagedImportReceipt.send_attempts == row.send_attempts, or_(lease.is_(None), lease <= now),
                state="outcome_unknown", send_attempts=attempt, send_lease_until=now + LEASE_SECONDS):
        raise Moved
    return attempt


def release(opened: Session, row: ManagedImportReceipt, claimed: list[int], now: float) -> None:
    if claimed:
        move(opened, row, ("outcome_unknown",), now, ManagedImportReceipt.send_attempts == claimed[0], send_lease_until=None)


def in_flight(row: ManagedImportReceipt, now: float) -> bool:
    return row.state == "outcome_unknown" and row.send_lease_until is not None and row.send_lease_until > now


def recovered(reader: immich.ManagedReader, row: ManagedImportReceipt) -> str | None:
    match = reader.bulk_check(row.source_sha1)
    if match is None or match.trashed:
        return None
    return match.asset_id if match.asset_id in reader.managed_with_checksum(row.source_sha1) else None


def returned(opened: Session, reader: immich.ManagedReader, row: ManagedImportReceipt, asset_id: str, outcome: str, now: float) -> Report:
    move(opened, row, ("outcome_unknown",), now, state="returned", asset_id=asset_id, upload_outcome=outcome, send_lease_until=None)
    row = fresh(opened, row.id)
    if row.asset_id != asset_id:
        return Report("pending", "the receipt already names another Immich asset")
    return read_back(opened, reader, row, now)


def read_back(opened: Session, reader: immich.ManagedReader, row: ManagedImportReceipt, now: float) -> Report:
    try:
        reader.verify_original(str(row.asset_id), row.source_sha1, row.expected_bytes)
    except (immich.NotCustody, immich.NotVisible):
        return Report("unusable", "Immich's asset is not an available managed original of this person")
    except immich.ImmichFailed:
        return Report("pending", "the original could not be read back and verified")
    move(opened, row, ("returned",), now, state="bytes_verified", verified_sha1=row.source_sha1, verified_bytes=row.expected_bytes, bytes_verified_at=now)
    row = fresh(opened, row.id)
    return Report("verified", "the unedited original matches the source") if row.state in VERIFIED else Report("pending", "the receipt moved")


def send(opened: Session, reader: immich.ManagedReader, row: ManagedImportReceipt, body: Body, now: float) -> Report | None:
    upload = body()
    if (upload.original.sha1, upload.original.size) != (row.source_sha1, row.expected_bytes):
        return Report("pending", "the upload body is not this receipt's source")
    claimed: list[int] = []
    try:
        result = reader.upload(upload, lambda: claimed.append(claim(opened, row, now)))
    except Moved:
        return None
    except immich.UploadUncertain:
        release(opened, row, claimed, now)
        return None
    except immich.ImmichFailed:
        release(opened, row, claimed, now)
        return Report("pending", "the upload was refused or could not be sent")
    return returned(opened, reader, fresh(opened, row.id), result.asset_id, result.status, now)


def upload_receipt(opened: Session, reader: immich.ManagedReader, receipt_id: int, body: Body, now: float) -> Report:
    row = fresh(opened, receipt_id)
    intent = opened.get(ManagedImportIntent, row.intent_id)
    owner = None if intent is None else intent.owner_id
    opened.commit()
    if reader.access != "import" or owner is None or reader.owner_id != owner:
        return Report("pending", "the Immich key is not this import's owner")
    for _ in range(MAX_SENDS):
        if row.state in VERIFIED:
            return Report("verified", "the unedited original matches the source")
        if row.state == "returned":
            return read_back(opened, reader, row, now)
        if in_flight(row, now):
            return Report("pending", "another upload attempt for this receipt is in flight")
        if row.state == "outcome_unknown":
            try:
                found = recovered(reader, row)
            except immich.ImmichFailed:
                return Report("pending", "the uncertain upload could not be checked by checksum")
            if found is not None:
                return returned(opened, reader, row, found, "duplicate", now)
        report = send(opened, reader, row, body, now)
        if report is not None:
            return report
        row = fresh(opened, receipt_id)
    return Report("pending", "the upload's outcome stayed unknown")
