from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy.orm import Session

from darkroom import config
from darkroom.domain import immich_reconcile, managed_switch, writer_locks
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import Person
from darkroom.storage.paths import Photos

LIVE_REFUSED: Final = "Immich did not accept this person's user and key: {reason}; nothing was changed"
CHANGED: Final = "this person's Immich user or key changed while it was being checked; nothing was changed, so ask again"
SIDE_EFFECT: Final = "New iPhone photos are not copied to originals/immich or queued for the Pixels until the switch is on."
STOPPED_NOTE: Final = ("Stopped, not off: something already went to Immich, so new Takeout exports wait, old-folder writers stay refused and nothing is taken out of Immich. "
                       "Ask for on to resume.")
GIVEN_UP: Final = "Failed or expired exports started the old way will not be retried after the switch: {ids}"
PAUSED: Final[dict[managed_switch.State, str]] = {
    "off": "No old-folder writer is paused.",
    "pausing": ("Paused: new Immich reconcile runs (only removals already begun finish), new legacy imports, new Takeout exports, cleanup and freeing for this person's old-way exports. "
                "Old-way exports already started finish."),
    "on": "Paused: Immich reconcile, legacy imports, old-folder writes of imports and reviews, cleanup and freeing for this person's old-way exports. New Takeout exports go to Immich.",
    "stopped": ("Paused: as for on, and new Takeout exports wait until the imports are on again. Exports that already have an import intent finish."),
}
CHECK_TRIES: Final = 3
UNKNOWN_WAITING: Final = "waiting for a reason this version of Darkroom does not know"


@dataclass(frozen=True)
class Result:
    ok: bool
    lines: tuple[str, ...]


def live_check(settings: config.Settings, person: Person, client: immich.Client | None = None) -> str | None:
    try:
        key = immich_reconcile.person_key(settings, person)
        immich.ManagedReader(client or immich.Client.at(settings), key, person.immich_user_id or "", access="import").check_access()
    except (immich_reconcile.NotReady, immich.ImmichFailed) as error:
        return LIVE_REFUSED.format(reason=error)
    return None


def duration(seconds: float) -> str:
    minutes = max(int(seconds // 60), 0)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def ids_of(found: tuple[int, ...]) -> str:
    return ", ".join(str(export_id) for export_id in found)


def status_lines(opened: Session, settings: config.Settings, person: Person, now: float) -> list[str]:
    state = managed_switch.state_of(person)
    if state is None:
        return [f"{person.slug}: {managed_switch.UNKNOWN_REQUEST}"]
    head = f"{person.slug}: Immich-managed imports {state}"
    if person.managed_imports_since is not None:
        head += f", for {duration(now - person.managed_imports_since)}"
    lines = [head, f"Immich user and key: {'saved' if managed_switch.has_key(person) else 'missing'}", PAUSED[state]]
    photos, accounts = Photos(settings.photos_root), writer_locks.accounts_of(opened, person)
    if state == "pausing":
        if person.managed_switch_waiting is None:
            lines.append("Waiting: nothing recorded yet; the next import tick tries the switch.")
        else:
            lines.append(f"Waiting: {managed_switch.WAITING_REASONS.get(person.managed_switch_waiting, UNKNOWN_WAITING)}")
            if person.managed_switch_waiting == "old_way_export":
                lines.append(f"Exports waited for: {ids_of(managed_switch.old_way_exports(opened, photos, person, accounts))}")
    if state in ("pausing", "on"):
        lines.append(SIDE_EFFECT)
    if state in ("off", "pausing"):
        given_up = managed_switch.old_way_exports(opened, photos, person, accounts, given_up=True)
        if given_up:
            lines.append(GIVEN_UP.format(ids=ids_of(given_up)))
    return lines


def locked_credentials(opened: Session, person: Person) -> tuple[managed_switch.State | None, tuple[str | None, str | None]]:
    db.write_lock(opened)
    opened.refresh(person)
    return managed_switch.state_of(person), (person.immich_user_id, person.immich_api_key_sealed)


def turn_on(opened: Session, settings: config.Settings, person: Person, now: float, client: immich.Client | None = None) -> Result:
    validated: tuple[str | None, str | None] | None = None
    for _ in range(CHECK_TRIES):
        state, credentials = locked_credentials(opened, person)
        if state not in ("off", "stopped") or validated == credentials:
            break
        opened.rollback()
        probe = Person(slug=person.slug, immich_user_id=credentials[0], immich_api_key_sealed=credentials[1])
        refused = live_check(settings, probe, client)
        if refused is not None:
            return Result(False, (f"{person.slug}: {refused}",))
        validated = credentials
    else:
        opened.rollback()
        return Result(False, (f"{person.slug}: {CHANGED}",))
    refused = managed_switch.request(opened, person, True, now)
    if refused is not None:
        return Result(False, (f"{person.slug}: {refused}",))
    opened.refresh(person)
    if managed_switch.state_of(person) == "pausing":
        managed_switch.advance(opened, settings, person, now)
        opened.refresh(person)
    return Result(True, tuple(status_lines(opened, settings, person, now)))


def turn_off(opened: Session, settings: config.Settings, person: Person, now: float) -> Result:
    refused = managed_switch.request(opened, person, False, now)
    if refused is not None:
        return Result(False, (f"{person.slug}: {refused}",))
    opened.refresh(person)
    lines = status_lines(opened, settings, person, now)
    return Result(True, tuple([*lines, STOPPED_NOTE] if managed_switch.state_of(person) == "stopped" else lines))
