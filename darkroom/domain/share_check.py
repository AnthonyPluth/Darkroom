from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.storage import db, share, stored_settings
from darkroom.storage.models import Event

FAILED = "share_check_failed"
PASSED = "share_check_passed"
BOUND = "share_bound"
SUBJECT = "share"
SHARE_EVENTS = (FAILED, PASSED, BOUND)

NOT_BOUND = "no share is bound yet"
MISSING = "the sentinel is missing from the share"
CHANGED = "the sentinel on the share is not the one recorded"
UNREADABLE = "the sentinel on the share could not be read"
MOVED = "the share root is not the filesystem it was at startup"
REBOUND = "the share was bound again and its placed copies have not been checked yet"


def sentinel_problem(root: Path, token: str | None) -> str | None:
    if not token:
        return NOT_BOUND
    try:
        found = share.read_sentinel(root)
    except OSError:
        return UNREADABLE
    if found is None:
        return MISSING
    if not hmac.compare_digest(found, share.sentinel_content(token)):
        return CHANGED
    return None


def last_share_event(opened: Session) -> tuple[str, str | None] | None:
    found = opened.execute(
        select(Event.type, Event.detail).where(Event.type.in_(SHARE_EVENTS), Event.subject == SUBJECT).order_by(Event.id.desc()).limit(1)
    ).first()
    return None if found is None else (found[0], found[1])


def report(opened: Session, problem: str | None, now: float) -> None:
    db.write_lock(opened)
    last = last_share_event(opened)
    if problem is not None and last != (FAILED, problem):
        events.record(opened, now, FAILED, SUBJECT, problem)
    elif problem is None and last is not None and last[0] == FAILED:
        events.record(opened, now, PASSED, SUBJECT)
    opened.commit()


@dataclass
class ShareCheck:
    root: Path
    device: int | None
    passed: str | None = None
    rebound: bool = False

    @classmethod
    def at_startup(cls, root: Path) -> ShareCheck:
        return cls(root, share.device_of(root))

    def problem(self, opened: Session, token: str | None = None) -> str | None:
        if self.device is None or share.device_of(self.root) != self.device:
            return MOVED
        return sentinel_problem(self.root, token if token is not None else stored_settings.recorded_share(opened))

    def transport_problem(self, opened: Session) -> str | None:
        token = stored_settings.recorded_share(opened)
        found = self.problem(opened, token)
        if found is None and self.passed is not None and token != self.passed:
            return REBOUND
        return found

    def refusal(self, opened: Session, now: float) -> str | None:
        token = stored_settings.recorded_share(opened)
        found = self.problem(opened, token)
        report(opened, found, now)
        self.rebound = found is None and token != self.passed
        if found is None:
            self.passed = token
        return found
