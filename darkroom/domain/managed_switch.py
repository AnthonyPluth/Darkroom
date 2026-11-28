from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Final, Literal, get_args

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, managed_ingest, writer_locks
from darkroom.domain.ingest import run
from darkroom.providers import immich
from darkroom.storage import db, paths
from darkroom.storage.models import DuplicateRemoval, Event, Export, ManagedImportIntent, Person
from darkroom.storage.paths import Photos

State = Literal["off", "pausing", "on", "stopped"]
Outcome = Literal["run", "wait", "refuse"]
Writer = Literal["legacy", "route", "ingest", "part_queue", "whole_import", "whole_registration", "review", "review_folder", "cleanup", "holds", "sweep", "part_free"]
Mode = Literal["classic", "paused", "catalogue"]
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
PAUSING_REFUSED: Final = "this person's imports are being switched to Immich, so a new old-folder import cannot start"
RECONCILE_PAUSED: Final = "the Immich reconcile is paused while this person's imports are being switched to Immich, so it only finishes removals it had begun"
NO_USER_WAIT: Final = "this person's imports go to Immich but they have no Immich user saved, so this waits until one is"
NO_USER_ALERT: Final = "Immich-managed imports of {slug} are on but they have no Immich user saved, so their new Takeout export {export} waits. Open Darkroom for details."
STOPPED_ALERT: Final = "Immich-managed imports of {slug} are stopped, so their new Takeout export {export} waits until they are on again. Open Darkroom for details."
NO_KEY: Final = "this person has no Immich user and key saved, so their imports can't go to Immich"
PIXEL_SOURCE_OFF: Final = "Pixel copies from Immich are not turned on (DARKROOM_PIXEL_IMMICH_SOURCE), so this person's new iPhone photos could not reach their Pixels; nothing was changed"
UNKNOWN_REQUEST: Final = "this person's managed-imports setting is not one Darkroom knows, so it is left as it is"
WAITING_REASONS: Final[dict[str, str]] = {
    "reconcile": "waiting for this person's Immich reconcile to finish",
    "legacy": "waiting for this person's legacy import to finish",
    "account": "waiting for an import of a Google account of this person to finish",
    "accounts_changed": "a Google account was linked to this person while the switch was being made; trying again",
    "old_way_export": "waiting for a Takeout export of this person that started the old way to be imported",
    "recovery": "an interrupted removal in this person's folders is being finished",
    "no_key": "this person's Immich user or key is missing; saving it and asking again is needed",
    "pixel_source": "Pixel copies from Immich are not turned on (DARKROOM_PIXEL_IMMICH_SOURCE); turning them on lets the switch finish",
}
PAUSING_ALERT_AFTER: Final = alerts.DAY

ALWAYS: Row = {"off": "run", "pausing": "run", "on": "run", "stopped": "run"}
OFF_ONLY: Row = {"off": "run", "pausing": "refuse", "on": "refuse", "stopped": "refuse"}
KEPT: Row = {"off": "run", "pausing": "wait", "on": "wait", "stopped": "wait"}
FRESH: Row = {"off": "run", "pausing": "wait", "on": "run", "stopped": "wait"}
OLD: Row = {"off": "run", "pausing": "run", "on": "refuse", "stopped": "refuse"}

TABLE: Final[dict[tuple[Writer, run.Way | None], Row]] = {
    ("legacy", None): OFF_ONLY,
    ("route", "fresh"): FRESH,
    ("route", "old"): OLD,
    ("route", "managed"): ALWAYS,
    ("ingest", "fresh"): FRESH,
    ("ingest", "old"): OLD,
    ("ingest", "managed"): ALWAYS,
    ("part_queue", "fresh"): ALWAYS,
    ("part_queue", "old"): OLD,
    ("part_queue", "managed"): ALWAYS,
    ("whole_import", "fresh"): OFF_ONLY,
    ("whole_import", "old"): OLD,
    ("whole_import", "managed"): OFF_ONLY,
    ("whole_registration", None): OFF_ONLY,
    ("review", "fresh"): OLD,
    ("review", "old"): OLD,
    ("review", "managed"): ALWAYS,
    ("review_folder", None): OLD,
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


def claim(opened: Session, person: Person) -> None:
    db.write_lock(opened)
    opened.execute(update(Person).where(Person.id == person.id).values(managed_imports=Person.managed_imports))
    opened.expire_all()
    opened.refresh(person)


def check(opened: Session, photos: Photos | None, person: Person, writer: Writer, export: Export | None = None) -> None:
    if writer in BY_THE_WAY and export is None:
        raise ValueError(f"{writer} needs the export it writes for")
    claim(opened, person)
    check_claimed(opened, photos, person, writer, export)


def check_claimed(opened: Session, photos: Photos | None, person: Person, writer: Writer, export: Export | None) -> None:
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
        raise Refused(PAUSING_REFUSED if state == "pausing" else REFUSED)


def import_route(opened: Session, photos: Photos | None, person: Person, export: Export) -> str:
    claim(opened, person)
    route = run.planned_route(opened, photos, export)
    check_claimed(opened, photos, person, "part_queue" if route == run.PER_PART else "whole_import", export)
    return route


def hold(opened: Session, photos: Photos | None, person: Person, writer: Writer, export: Export | None = None) -> None:
    try:
        check(opened, photos, person, writer, export)
    except (Paused, Refused):
        opened.rollback()
        raise


def reconcile_mode(opened: Session, person: Person) -> Mode:
    claim(opened, person)
    state = state_of(person)
    if state == "off":
        return "classic"
    return "catalogue" if state in ("on", "stopped") else "paused"


def note_reconcile_paused(opened: Session, person: Person, now: float) -> None:
    last = select(Event.time).where(Event.type == "immich_reconcile_paused", Event.subject == person.slug).order_by(Event.id.desc()).limit(1)
    before = opened.execute(last).scalar_one_or_none()
    if before is None or person.managed_imports_since is None or before < person.managed_imports_since:
        events.record(opened, now, "immich_reconcile_paused", person.slug, RECONCILE_PAUSED)


def stopped_alert_key(export: Export) -> str:
    return f"managed:stopped:{export.id}"


def begin(opened: Session, settings: config.Settings, export: Export, person: Person, now: float) -> ManagedImportIntent | None:
    intent = managed_ingest.intent_of(opened, export)
    if intent is not None:
        return intent
    photos = Photos(settings.photos_root)
    opened.commit()
    try:
        check(opened, photos, person, "route", export)
    except Paused:
        opened.rollback()
        if state_of(person) == "stopped":
            alerts.open_alert_committed(opened, settings, stopped_alert_key(export), "managed_stopped", person.slug,
                                        alerts.Wording(alerts.TITLES["managed_stopped"], STOPPED_ALERT.format(slug=person.slug, export=export.id)), None, now,
                                        about=alerts.person(person.id))
        raise
    if state_of(person) != "on" or run.way(opened, photos, export) != "fresh":
        opened.commit()
        return None
    if not immich.is_canonical_id(person.immich_user_id):
        opened.rollback()
        alerts.open_alert_committed(opened, settings, f"managed:no_user:{export.id}", "managed_no_user", person.slug,
                                    alerts.Wording(alerts.TITLES["managed_no_user"], NO_USER_ALERT.format(slug=person.slug, export=export.id)), None, now,
                                    about=alerts.person(person.id))
        raise Paused(NO_USER_WAIT)
    intent = ManagedImportIntent(person_id=person.id, export_id=export.id, owner_id=str(person.immich_user_id), resource_manifest=managed_ingest.MANIFEST, created_at=now)
    opened.add(intent)
    events.record(opened, now, "managed_import_started", f"export {export.id}", person.slug)
    alerts.close(opened, stopped_alert_key(export), now)
    alerts.close(opened, f"managed:no_user:{export.id}", now)
    opened.commit()
    monitoring.log("export imported to Immich-managed storage", export=export.id)
    return intent


@dataclass(frozen=True)
class Waiting:
    reason: str
    exports: tuple[int, ...] = ()

    @property
    def sentence(self) -> str:
        return WAITING_REASONS[self.reason]


def alert_key(person: Person) -> str:
    return f"managed:pausing:{person.id}"


def has_key(person: Person) -> bool:
    return immich.is_canonical_id(person.immich_user_id) and bool(person.immich_api_key_sealed)


def has_import_intent(opened: Session, person: Person) -> bool:
    found = select(ManagedImportIntent.id).where(ManagedImportIntent.person_id == person.id, ManagedImportIntent.kind == "import").limit(1)
    return opened.execute(found).first() is not None


def pausing(opened: Session) -> list[Person]:
    return list(opened.execute(select(Person).where(Person.managed_imports == "pausing").order_by(Person.id)).scalars())


def set_state(opened: Session, person: Person, target: State, now: float, event: str) -> None:
    person.managed_imports = None if target == "off" else target
    person.managed_imports_since = now
    person.managed_switch_waiting = None
    events.record(opened, now, event, person.slug, target)
    if target != "pausing":
        alerts.close(opened, alert_key(person), now)


def request(opened: Session, person: Person, want_on: bool, now: float, settings: config.Settings) -> str | None:
    claim(opened, person)
    state = state_of(person)
    if state is None:
        opened.rollback()
        return UNKNOWN_REQUEST
    target: State
    if want_on:
        if state in ("pausing", "on"):
            opened.rollback()
            return None
        if not has_key(person):
            opened.rollback()
            return NO_KEY
        if not settings.pixel_immich_source:
            opened.rollback()
            return PIXEL_SOURCE_OFF
        target = "on" if state == "stopped" else "pausing"
    else:
        if state in ("off", "stopped"):
            opened.rollback()
            return None
        target = "stopped" if has_import_intent(opened, person) else "off"
    set_state(opened, person, target, now, "managed_imports_set")
    opened.commit()
    return None


def old_way_exports(opened: Session, photos: Photos, person: Person, accounts: set[int], *, given_up: bool = False) -> tuple[int, ...]:
    theirs = or_(Export.owner_person_id == person.id, and_(Export.owner_person_id.is_(None), Export.account_id.in_(accounts)))
    found = select(Export).where(theirs, Export.status.in_(("failed", "expired")) if given_up else Export.status.not_in(FINISHED)).order_by(Export.id)
    return tuple(export.id for export in opened.execute(found).scalars() if run.way(opened, photos, export) == "old")


def interrupted(opened: Session, photos: Photos, person: Person) -> bool:
    pending = select(DuplicateRemoval.id).where(DuplicateRemoval.person_id == person.id, DuplicateRemoval.state == "pending").limit(1)
    if opened.execute(pending).first() is not None:
        return True
    folder = photos.inside(photos.person_part(person.slug), "google-only")
    return any(path.resolve().is_relative_to(folder) for prefix in (paths.DROPPING, paths.PLACING) for found in paths.leftovers(photos, prefix) for path in found)


def flip(opened: Session, settings: config.Settings, person: Person, accounts: set[int], now: float) -> Waiting | None:
    claim(opened, person)
    if state_of(person) != "pausing":
        opened.rollback()
        return None
    waiting: Waiting | None = None
    photos = Photos(settings.photos_root)
    if writer_locks.accounts_of(opened, person) - accounts:
        waiting = Waiting("accounts_changed")
    elif exports := old_way_exports(opened, photos, person, accounts):
        waiting = Waiting("old_way_export", exports)
    elif interrupted(opened, photos, person):
        waiting = Waiting("recovery")
    elif not has_key(person):
        waiting = Waiting("no_key")
    elif not settings.pixel_immich_source:
        waiting = Waiting("pixel_source")
    if waiting is not None:
        opened.rollback()
        return waiting
    set_state(opened, person, "on", now, "managed_imports_on")
    opened.commit()
    return None


def note(opened: Session, settings: config.Settings, person_id: int, waiting: Waiting, now: float) -> None:
    db.write_lock(opened)
    opened.execute(update(Person).where(Person.id == person_id, Person.managed_imports == "pausing").values(managed_switch_waiting=waiting.reason))
    opened.expire_all()
    person = opened.get(Person, person_id)
    if person is None or state_of(person) != "pausing" or person.managed_imports_since is None or now - person.managed_imports_since < PAUSING_ALERT_AFTER:
        opened.commit()
        return
    message = f"Switching {person.slug}'s imports to Immich has been waiting for a day: {waiting.sentence}. Open Darkroom for details."
    alerts.open_alert_committed(opened, settings, alert_key(person), "managed_pausing", person.slug, alerts.Wording(alerts.TITLES["managed_pausing"], message), None, now,
                                about=alerts.person(person.id))


def advance(opened: Session, settings: config.Settings, person: Person, now: float) -> Waiting | None:
    if state_of(person) != "pausing":
        return None
    person_id, accounts = person.id, writer_locks.accounts_of(opened, person)
    opened.commit()
    waiting: Waiting | None
    with contextlib.ExitStack() as stack:
        try:
            writer_locks.hold(stack, opened, settings, [person], accounts)
        except writer_locks.Busy as error:
            waiting = Waiting(error.kind)
        else:
            waiting = flip(opened, settings, person, accounts, now)
    if waiting is not None:
        note(opened, settings, person_id, waiting, now)
    return waiting
