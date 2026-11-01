from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.storage.models import Event


@dataclass(frozen=True)
class Entry:
    id: int
    time: float
    type: str
    subject: str
    detail: str | None


def record(opened: Session, now: float, type: str, subject: str, detail: str | None = None) -> None:
    opened.add(Event(time=now, type=type, subject=subject, detail=detail))
    opened.flush()


def recent(opened: Session, limit: int = 50, type: str | None = None) -> list[Entry]:
    query = select(Event).order_by(Event.time.desc(), Event.id.desc()).limit(limit)
    if type is not None:
        query = query.where(Event.type == type)
    return [Entry(e.id, e.time, e.type, e.subject, e.detail) for e in opened.execute(query).scalars()]
