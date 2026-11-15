from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from darkroom import monitoring
from darkroom.domain import events
from darkroom.providers import syncthing

TESTED_FROM = (2, 1, 7)
TESTED_UP_TO = (2, 1, 7)
RELEASE = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
EVENT = "syncthing_transport_off"
SUBJECT = "syncthing"
NO_KEY = "Syncthing's API key was not handed over by the init"
KEY_REFUSED = "Syncthing refused the API key the init handed over"
NOT_ANSWERING = "Syncthing did not answer on its socket"


@dataclass(frozen=True)
class Gate:
    on: bool
    reason: str = ""


def release(found: syncthing.Version) -> tuple[int, int, int] | None:
    matched = RELEASE.fullmatch(found.version)
    if matched is None or not found.is_release:
        return None
    return int(matched[1]), int(matched[2]), int(matched[3])


def tested(found: syncthing.Version) -> bool:
    number = release(found)
    return number is not None and TESTED_FROM <= number <= TESTED_UP_TO


def named(number: tuple[int, int, int]) -> str:
    return "v" + ".".join(str(part) for part in number)


def tested_range() -> str:
    return named(TESTED_FROM) if TESTED_FROM == TESTED_UP_TO else f"{named(TESTED_FROM)} to {named(TESTED_UP_TO)}"


def untested(found: syncthing.Version) -> str:
    shown = found.version if RELEASE.fullmatch(found.version) else "an unrecognised version"
    kind = "" if found.is_release else ", not a release build"
    return f"Syncthing reported {shown}{kind}, outside the tested range {tested_range()}"


def reason_off(client: syncthing.Client | None) -> str | None:
    if client is None:
        return NO_KEY
    try:
        found = client.version()
    except syncthing.KeyRefused:
        return KEY_REFUSED
    except syncthing.SyncthingFailed:
        return NOT_ANSWERING
    return None if tested(found) else untested(found)


def at_startup(opened: Session, client: syncthing.Client | None, now: float) -> Gate:
    reason = reason_off(client)
    if reason is None:
        monitoring.log("syncthing is a tested release; the transport may start", tested=tested_range())
        return Gate(True)
    events.record(opened, now, EVENT, SUBJECT, reason)
    monitoring.log("syncthing transport kept off", reason=reason)
    return Gate(False, reason)
