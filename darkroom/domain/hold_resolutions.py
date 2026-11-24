from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Final

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from darkroom.domain import events, holds, managed_metadata, media
from darkroom.storage import db
from darkroom.storage.models import HOLD_RESOLUTION_OFFSET_ACTIONS, ManagedHoldResolution, ManagedImportReceipt

RESOLVED_EVENT: Final = "managed_hold_resolved"
UNDONE_EVENT: Final = "managed_hold_undone"
OPEN: Final = ("recorded", "applied")
EASTMOST: Final = media.EASTMOST_OFFSET_MINUTES
WESTMOST: Final = media.WESTMOST_OFFSET_MINUTES
STEP: Final = 15
EARLIEST: Final = datetime(1900, 1, 1)
DAY_SECONDS: Final = 24 * 3600.0
LOCAL_TIME: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")
NOT_HELD: Final = "this photo is no longer held"
ALREADY: Final = "this hold already has a resolution"
NOTHING_TO_UNDO: Final = "this hold has no resolution to undo"
SENT: Final = "already sent to Immich, so it can't be undone"
NOT_ALLOWED: Final = "this hold can't be resolved that way"
NEEDS_OFFSET: Final = "an offset is required, from -12:00 to +14:00 in 15-minute steps"
BAD_OFFSET: Final = "the offset must be a whole number of minutes from -720 to 840, a multiple of 15"
NO_OFFSET: Final = "this action takes no offset"
NEEDS_TIME: Final = "a capture time is required, as YYYY-MM-DDTHH:MM:SS with its offset"
BAD_TIME: Final = "the capture time must be a date and time as YYYY-MM-DDTHH:MM:SS, from 1900-01-01 to tomorrow, with no offset in the text"
NO_TIME: Final = "this action takes no capture time"
PLACE: Final = "this photo's place was in a different time zone then, so Immich would not show the offset you gave"
UNREADABLE: Final = "the saved metadata of this photo can't be restored, so the resolution can't be undone"


class Refused(ValueError):
    pass


class Conflict(ValueError):
    pass


class NoSuchHold(LookupError):
    pass


def open_row(opened: Session, receipt_id: int) -> ManagedHoldResolution | None:
    return opened.execute(select(ManagedHoldResolution).where(ManagedHoldResolution.receipt_id == receipt_id, ManagedHoldResolution.state.in_(OPEN))).scalar_one_or_none()


def offset_of(value: object) -> int:
    if value is None:
        raise Refused(NEEDS_OFFSET)
    if isinstance(value, bool) or not isinstance(value, int) or not WESTMOST <= value <= EASTMOST or value % STEP != 0:
        raise Refused(BAD_OFFSET)
    return value


def local_of(value: object) -> datetime:
    if value is None:
        raise Refused(NEEDS_TIME)
    found_parts = LOCAL_TIME.fullmatch(value) if isinstance(value, str) else None
    if found_parts is None:
        raise Refused(BAD_TIME)
    try:
        year, month, day, hour, minute, second = (int(part) for part in found_parts.groups())
        found = datetime(year, month, day, hour, minute, second)
    except ValueError as error:
        raise Refused(BAD_TIME) from error
    if found < EARLIEST:
        raise Refused(BAD_TIME)
    return found


def place_offset(receipt: ManagedImportReceipt, instant: float) -> int | None:
    intended = managed_metadata.intended_of(receipt.expected_metadata)
    if intended is None:
        return None
    return media.wall_clock_at(instant, intended.latitude, intended.longitude)[1]


def agrees_with_place(receipt: ManagedImportReceipt, wall: datetime, offset: int) -> bool:
    instant = media.utc_or_none(wall, offset)
    if instant is None:
        return False
    at_place = place_offset(receipt, instant)
    return at_place is None or at_place == offset


def wall_clock_of(receipt: ManagedImportReceipt) -> datetime | None:
    facts = holds.facts_of(receipt.hold_facts)
    raw = None if facts is None else facts.get("wall_clock")
    try:
        return local_of(raw)
    except Refused:
        return None


def values_of(receipt: ManagedImportReceipt, action: str, offset_minutes: object, taken_local: object, now: float) -> tuple[int | None, str | None]:
    if action not in HOLD_RESOLUTION_OFFSET_ACTIONS:
        if offset_minutes is not None:
            raise Refused(NO_OFFSET)
        if taken_local is not None:
            raise Refused(NO_TIME)
        return None, None
    offset = offset_of(offset_minutes)
    if action != "capture_time":
        if taken_local is not None:
            raise Refused(NO_TIME)
        wall = wall_clock_of(receipt)
        if action == "offset" and wall is not None and not agrees_with_place(receipt, wall, offset):
            raise Refused(PLACE)
        return offset, None
    wall = local_of(taken_local)
    instant = media.utc_or_none(wall, offset)
    if instant is None or instant > now + DAY_SECONDS:
        raise Refused(BAD_TIME)
    if not agrees_with_place(receipt, wall, offset):
        raise Refused(PLACE)
    return offset, wall.isoformat(timespec="seconds")


def by_suffix(person_id: int | None) -> str:
    return "" if person_id is None else f", by person {person_id}"


def resolve(opened: Session, receipt_id: int, action: str, offset_minutes: object, taken_local: object, by: int | None, now: float) -> ManagedHoldResolution:
    db.write_lock(opened)
    receipt = opened.get(ManagedImportReceipt, receipt_id)
    if receipt is None or holds.source_of(receipt.resource_key) is None:
        raise NoSuchHold(receipt_id)
    hold = receipt.metadata_hold
    if hold is None or hold not in holds.ACTIONS or receipt.state == "committed":
        raise Conflict(NOT_HELD)
    if action not in holds.allowed(hold, holds.facts_of(receipt.hold_facts)):
        raise Refused(NOT_ALLOWED)
    if open_row(opened, receipt_id) is not None:
        raise Conflict(ALREADY)
    offset, taken = values_of(receipt, action, offset_minutes, taken_local, now)
    row = ManagedHoldResolution(receipt_id=receipt_id, hold=hold, action=action, offset_minutes=offset, taken_local=taken, state="recorded", decided_by_person_id=by, decided_at=now)
    try:
        with opened.begin_nested():
            opened.add(row)
            opened.flush()
    except IntegrityError as error:
        raise Conflict(ALREADY) from error
    events.record(opened, now, RESOLVED_EVENT, f"receipt {receipt_id}", f"{hold} {action}{by_suffix(by)}")
    return row


def restorable(row: ManagedHoldResolution) -> dict[str, object] | None:
    try:
        saved = None if row.previous_metadata is None else json.loads(row.previous_metadata)
    except ValueError:
        return None
    if not isinstance(saved, dict) or not isinstance(saved.get("expected_metadata"), str):
        return None
    hold = saved.get("metadata_hold")
    facts = saved.get("hold_facts")
    if (hold is not None and not isinstance(hold, str)) or (facts is not None and not isinstance(facts, str)):
        return None
    return saved


def unsent(receipt: ManagedImportReceipt) -> bool:
    return receipt.state == "prepared" and receipt.send_attempts == 0 and receipt.send_lease_until is None


def undoable(receipt: ManagedImportReceipt, row: ManagedHoldResolution) -> bool:
    if row.state == "recorded":
        return True
    return row.state == "applied" and unsent(receipt) and restorable(row) is not None


def undo(opened: Session, receipt_id: int, by: int | None, now: float) -> ManagedHoldResolution:
    db.write_lock(opened)
    receipt = opened.get(ManagedImportReceipt, receipt_id)
    if receipt is None or holds.source_of(receipt.resource_key) is None:
        raise NoSuchHold(receipt_id)
    row = open_row(opened, receipt_id)
    if row is None:
        raise Conflict(NOTHING_TO_UNDO)
    if row.state == "applied":
        saved = restorable(row)
        if not unsent(receipt):
            raise Conflict(SENT)
        if saved is None:
            raise Conflict(UNREADABLE)
        changes: dict[str, object] = {"expected_metadata": saved["expected_metadata"], "metadata_hold": saved.get("metadata_hold"), "updated_at": now}
        if "hold_facts" in saved:
            changes["hold_facts"] = saved["hold_facts"]
        result = opened.execute(update(ManagedImportReceipt).where(ManagedImportReceipt.id == receipt_id, ManagedImportReceipt.state == "prepared",
                                                                    ManagedImportReceipt.send_attempts == 0, ManagedImportReceipt.send_lease_until.is_(None))
                                .values(**changes))
        assert isinstance(result, CursorResult)
        restored = result.rowcount
        if restored != 1:
            raise Conflict(SENT)
        opened.expire(receipt)
    row.state, row.undone_at = "undone", now
    opened.flush()
    events.record(opened, now, UNDONE_EVENT, f"receipt {receipt_id}", f"{row.hold} {row.action}{by_suffix(by)}")
    return row


def view(row: ManagedHoldResolution, receipt: ManagedImportReceipt) -> dict[str, object]:
    return {"id": row.id, "receipt_id": row.receipt_id, "hold": row.hold, "action": row.action, "offset_minutes": row.offset_minutes, "taken_local": row.taken_local,
            "state": row.state, "result": row.result, "decided_by_person_id": row.decided_by_person_id, "decided_at": row.decided_at, "applied_at": row.applied_at,
            "undone_at": row.undone_at, "can_undo": undoable(receipt, row)}


def attach(opened: Session, found: list[dict[str, object]]) -> None:
    ids = [int(str(row["id"])) for row in found]
    rows = {row.receipt_id: row for row in opened.execute(select(ManagedHoldResolution).where(ManagedHoldResolution.receipt_id.in_(ids), ManagedHoldResolution.state.in_(OPEN))).scalars()}
    receipts = {receipt.id: receipt for receipt in opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.id.in_(rows))).scalars()}
    for row in found:
        number = int(str(row["id"]))
        row["resolution"] = view(rows[number], receipts[number]) if number in rows else None
