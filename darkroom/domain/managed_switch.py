from __future__ import annotations

from typing import Final, Literal, get_args

from sqlalchemy import update
from sqlalchemy.orm import Session

from darkroom.domain.ingest import run
from darkroom.storage import db
from darkroom.storage.models import Export, Person
from darkroom.storage.paths import Photos

State = Literal["off", "pausing", "on", "stopped"]
Outcome = Literal["run", "wait", "refuse"]
Writer = Literal["legacy", "route", "ingest", "whole_import", "review", "cleanup", "holds", "sweep", "part_free"]
Row = dict[State, Outcome]
STATES: Final = get_args(State)
FINISHED: Final = frozenset({"imported", "failed", "expired"})
WAITS: Final[dict[State, str]] = {
    "pausing": "this person's Immich-managed imports are being switched on, so this waits until they are",
    "on": "this person's imports go to Immich, so this is kept",
    "stopped": "this person's Immich-managed imports are stopped, so this waits until they are on again",
    "off": "",
}
UNKNOWN_STATE: Final = "this person's managed-imports setting is not one Darkroom knows, so this waits"
REFUSED: Final = "this person's imports go to Immich, so nothing is written to their old folders"

ALWAYS: Row = {"off": "run", "pausing": "run", "on": "run", "stopped": "run"}
OFF_ONLY: Row = {"off": "run", "pausing": "refuse", "on": "refuse", "stopped": "refuse"}
KEPT: Row = {"off": "run", "pausing": "wait", "on": "wait", "stopped": "wait"}
FRESH: Row = {"off": "run", "pausing": "wait", "on": "run", "stopped": "wait"}
OLD: Row = {"off": "run", "pausing": "run", "on": "refuse", "stopped": "refuse"}

TABLE: Final[dict[tuple[Writer, run.Way | None], Row]] = {
    ("legacy", None): OFF_ONLY,
    ("route", "fresh"): FRESH,
    ("route", "old"): ALWAYS,
    ("route", "managed"): ALWAYS,
    ("ingest", "fresh"): FRESH,
    ("ingest", "old"): OLD,
    ("ingest", "managed"): ALWAYS,
    ("whole_import", "fresh"): OFF_ONLY,
    ("whole_import", "old"): OLD,
    ("whole_import", "managed"): OFF_ONLY,
    ("review", "fresh"): OLD,
    ("review", "old"): OLD,
    ("review", "managed"): ALWAYS,
    ("cleanup", None): KEPT,
    ("holds", None): KEPT,
    ("sweep", None): ALWAYS,
    ("part_free", None): KEPT,
}
BY_THE_WAY: Final = frozenset(writer for writer, way in TABLE if way is not None)


class Paused(run.NotReady):
    pass


class Refused(RuntimeError):
    pass


def state_of(person: Person) -> State | None:
    found = person.managed_imports
    if found is None:
        return "off"
    return next((state for state in STATES if state == found), None)


def outcome(state: State, writer: Writer, way: run.Way | None, export: Export | None) -> Outcome:
    if writer == "part_free" and state == "pausing" and way == "old" and export is not None and export.status not in FINISHED:
        return "run"
    return TABLE[(writer, way if writer in BY_THE_WAY else None)][state]


def hold(opened: Session, photos: Photos, person: Person, writer: Writer, export: Export | None = None) -> None:
    if writer in BY_THE_WAY and export is None:
        raise ValueError(f"{writer} needs the export it writes for")
    db.write_lock(opened)
    opened.execute(update(Person).where(Person.id == person.id).values(managed_imports=Person.managed_imports))
    opened.expire_all()
    opened.refresh(person)
    state = state_of(person)
    if state is None:
        raise Paused(UNKNOWN_STATE)
    way: run.Way | None = None
    if export is not None and (writer in BY_THE_WAY or writer == "part_free"):
        opened.refresh(export)
        way = run.way(opened, photos, export)
    found = outcome(state, writer, way, export)
    if found == "wait":
        raise Paused(WAITS[state])
    if found == "refuse":
        raise Refused(REFUSED)
