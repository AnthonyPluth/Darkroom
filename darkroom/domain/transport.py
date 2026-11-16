from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import dates, monitoring
from darkroom.domain import events
from darkroom.providers import syncthing
from darkroom.storage import db, paths
from darkroom.storage.models import Event, Pixel, PixelTransport
from darkroom.storage.paths import Photos

FOLDER_PREFIX = "darkroom-pixel-"
IGNORED = ("*.xmp",)
SUBJECT = "syncthing"
CORRECTED = "syncthing_corrected"
FOLDER_ADDED = "syncthing_folder_added"
FOLDER_REMOVED = "syncthing_folder_removed"
FOLDER_RELEASED = "syncthing_folder_released"
FAILED = "syncthing_reconcile_failed"
RECOVERED = "syncthing_reconciled"
PAUSED = "syncthing_transport_paused"
OUTCOMES = (FAILED, RECOVERED, PAUSED)

INVALID_DEVICE = "device_id_invalid"
SHARED_DEVICE = "device_id_shared"
PATH_REFUSED = "folder_path_refused"
REMOVED = "folder_removed"
ID_PROBLEMS = (INVALID_DEVICE, SHARED_DEVICE)
HELD = "held"
SHARE = "share_check_failed"
DOWN = "reconcile_failed"

NOT_ASKED_FOR = "a folder Darkroom did not ask for was removed"
NOT_SEND_ONLY = "a folder that was not Send Only was removed"
WRONG_PATH = "a folder whose path was not its Pixel's folder was removed"
DEVICE_NOT_ASKED_FOR = "a device Darkroom did not ask for was removed"
SHARED_WIDER = "the folder was shared with a device other than its Pixel's, or kept versions, and was corrected"
IGNORES_SET = "the folder's ignore patterns were set to keep sidecars off the Pixel"
NOT_APPROVED = "the configuration read back is not the one Darkroom approved"

REFUSED_KEY = "Syncthing refused the API key"
KEPT_NOT_SEND_ONLY = "Syncthing did not keep a folder Send Only"
NOT_KEPT = "Syncthing did not keep the configuration as written"
UNREACHABLE = "Syncthing could not be reached or answered with an error"


def folder_of(pixel_id: int) -> str:
    return f"{FOLDER_PREFIX}{pixel_id}"


def pixel_of(folder: str) -> int | None:
    number = folder.removeprefix(FOLDER_PREFIX)
    if number == folder or not number.isascii() or not number.isdigit() or number.startswith("0"):
        return None
    return int(number)


def device_name(pixel_id: int) -> str:
    return f"darkroom-pixel-{pixel_id}"


def failure_reason(error: syncthing.SyncthingFailed) -> str:
    if isinstance(error, syncthing.KeyRefused):
        return REFUSED_KEY
    if isinstance(error, syncthing.NotSendOnly):
        return KEPT_NOT_SEND_ONLY
    if isinstance(error, syncthing.ReadBackFailed):
        return NOT_KEPT
    return UNREACHABLE


def instant(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dates.instant_or_none(parsed.timestamp()) if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class Wanted:
    pixel_id: int
    device: str | None
    path: str | None = field(repr=False)
    held: bool
    removed: bool
    code: str | None

    @property
    def folder(self) -> str:
        return folder_of(self.pixel_id)

    @property
    def usable(self) -> bool:
        return self.device is not None and self.path is not None and not self.removed


@dataclass(frozen=True)
class Plan:
    wanted: dict[int, Wanted]
    pixels: tuple[int, ...]
    share_problem: str | None

    def devices(self) -> set[str]:
        return {want.device for want in self.wanted.values() if want.device is not None and not want.removed}

    def fingerprint(self) -> tuple[object, ...]:
        return (tuple((want.pixel_id, want.device, want.path, want.held, want.removed) for want in self.wanted.values()), self.pixels, self.share_problem)


@dataclass
class Report:
    connected: bool = False
    last_seen: float | None = None
    completion: float | None = None
    synced: bool = False


@dataclass
class Outcome:
    corrections: list[tuple[str, str, str]] = field(default_factory=list)
    approved: set[int] = field(default_factory=set)
    resumed: set[int] = field(default_factory=set)
    removed: set[int] = field(default_factory=set)
    reports: dict[int, Report] = field(default_factory=dict)
    failure: str | None = None
    paused: bool = False
    own_device: set[int] = field(default_factory=set)

    def corrected(self, kind: str, subject: str, detail: str) -> None:
        self.corrections.append((kind, subject, detail))


class NotRemoved(ValueError):
    pass


NOTHING_TO_RELEASE = "Darkroom has not removed this Pixel's Syncthing folder, so there is nothing to share again"


def removed_before(opened: Session, pixel_id: int) -> bool:
    last = opened.execute(select(Event.type).where(Event.type.in_((FOLDER_REMOVED, FOLDER_RELEASED)), Event.subject == f"pixel {pixel_id}")
                          .order_by(Event.id.desc()).limit(1)).first()
    return last is not None and last[0] == FOLDER_REMOVED


def release_folder(opened: Session, pixel_id: int, now: float) -> None:
    db.write_lock(opened)
    if not removed_before(opened, pixel_id):
        raise NotRemoved(NOTHING_TO_RELEASE)
    events.record(opened, now, FOLDER_RELEASED, f"pixel {pixel_id}")


def plan(opened: Session, photos: Photos, held: Callable[[int], bool], share_problem: str | None) -> Plan:
    rows = opened.execute(select(Pixel.id, Pixel.syncthing_device_id).order_by(Pixel.id)).all()
    devices: dict[int, str | None] = {}
    for pixel_id, raw in rows:
        if raw:
            devices[pixel_id] = syncthing.device_id(raw) if syncthing.is_device_id(raw) else None
    counted = Counter(device for device in devices.values() if device is not None)
    wanted = {}
    for pixel_id, device in devices.items():
        code = INVALID_DEVICE if device is None else SHARED_DEVICE if counted[device] > 1 else None
        try:
            path: str | None = str(photos.pixel_root(pixel_id))
        except paths.OutsideRoot:
            path, code = None, code or PATH_REFUSED
        removed = removed_before(opened, pixel_id)
        wanted[pixel_id] = Wanted(pixel_id, None if code in ID_PROBLEMS else device, path, held(pixel_id), removed,
                                  REMOVED if removed and code not in ID_PROBLEMS else code)
    return Plan(wanted, tuple(pixel_id for pixel_id, _ in rows), share_problem)


def hold_removed(opened: Session, pixel_id: int, now: float) -> None:
    db.write_lock(opened)
    events.record(opened, now, FOLDER_REMOVED, f"pixel {pixel_id}")
    opened.commit()


def paused_folder(client: syncthing.Client, found: syncthing.Folder) -> None:
    if not found.paused:
        client.update_folder(found.id, paused=True)


def correct_folders(opened: Session, client: syncthing.Client, plan: Plan, own: str, outcome: Outcome, now: float) -> set[int]:
    present = set()
    for found in client.folders():
        pixel_id = pixel_of(found.id)
        want = plan.wanted.get(pixel_id) if pixel_id is not None else None
        subject = SUBJECT if want is None else f"pixel {want.pixel_id}"
        if want is None or want.removed:
            client.remove_folder(found.id)
            outcome.corrected(CORRECTED, subject, NOT_ASKED_FOR)
            continue
        if want.path is not None and (found.type != syncthing.SEND_ONLY or found.path != want.path):
            hold_removed(opened, want.pixel_id, now)
            outcome.removed.add(want.pixel_id)
            client.remove_folder(found.id)
            outcome.corrected(CORRECTED, subject, NOT_SEND_ONLY if found.type != syncthing.SEND_ONLY else WRONG_PATH)
            continue
        present.add(want.pixel_id)
        if not want.usable or want.device is None:
            paused_folder(client, found)
            continue
        wider = found.devices - {own} != {want.device} or bool(found.versioning)
        unignored = client.ignores(found.id) != IGNORED
        if wider or unignored:
            paused_folder(client, found)
        if wider:
            client.update_folder(found.id, devices=[want.device], paused=True)
            outcome.corrected(CORRECTED, subject, SHARED_WIDER)
        if unignored:
            client.set_ignores(found.id, IGNORED)
            outcome.corrected(CORRECTED, subject, IGNORES_SET)
    return present


def correct_devices(client: syncthing.Client, plan: Plan, own: str, outcome: Outcome) -> None:
    asked = plan.devices()
    configured = set()
    for found in client.devices():
        if found.device_id == own:
            continue
        if found.device_id not in asked:
            client.remove_device(found.device_id)
            outcome.corrected(CORRECTED, SUBJECT, DEVICE_NOT_ASKED_FOR)
            continue
        configured.add(found.device_id)
    for want in plan.wanted.values():
        if want.usable and want.device is not None and want.device not in configured:
            client.add_device(want.device, device_name(want.pixel_id), paused=True)
            configured.add(want.device)


def add_folders(client: syncthing.Client, plan: Plan, present: set[int], outcome: Outcome) -> None:
    if plan.share_problem is not None:
        return
    for want in plan.wanted.values():
        if want.pixel_id in present or want.pixel_id in outcome.removed or not want.usable or want.held or want.device is None or want.path is None:
            continue
        client.add_folder(want.folder, want.path, [], paused=True)
        client.set_ignores(want.folder, IGNORED)
        client.update_folder(want.folder, devices=[want.device], paused=True)
        outcome.corrected(FOLDER_ADDED, f"pixel {want.pixel_id}", "the Pixel's folder was added, paused, Send Only, with sidecars ignored")


def approve(client: syncthing.Client, plan: Plan, own: str) -> set[int]:
    approved = set()
    for found in client.folders():
        pixel_id = pixel_of(found.id)
        want = plan.wanted.get(pixel_id) if pixel_id is not None else None
        if want is None or want.removed or found.type != syncthing.SEND_ONLY or found.versioning or (want.path is not None and found.path != want.path):
            raise syncthing.ReadBackFailed(NOT_APPROVED)
        if not want.usable or want.device is None:
            if not found.paused:
                raise syncthing.ReadBackFailed(NOT_APPROVED)
            continue
        if found.devices - {own} != {want.device} or client.ignores(found.id) != IGNORED:
            raise syncthing.ReadBackFailed(NOT_APPROVED)
        approved.add(want.pixel_id)
    asked = plan.devices()
    if any(found.device_id != own and found.device_id not in asked for found in client.devices()):
        raise syncthing.ReadBackFailed(NOT_APPROVED)
    return approved


def device_pauses(client: syncthing.Client, own: str | None) -> list[Callable[[], object]]:
    return [partial(client.update_device, device.device_id, paused=True) for device in client.devices() if device.device_id != own and not device.paused]


def folder_pauses(client: syncthing.Client) -> list[Callable[[], object]]:
    return [partial(client.update_folder, found.id, paused=True) for found in client.folders() if not found.paused]


def pause_everything(client: syncthing.Client, own: str) -> None:
    for write in device_pauses(client, own) + folder_pauses(client):
        write()


def attempted(step: Callable[[], object]) -> bool:
    try:
        step()
    except syncthing.SyncthingFailed:
        return False
    return True


def listed(listing: Callable[[], list[Callable[[], object]]]) -> list[Callable[[], object]]:
    try:
        return listing()
    except syncthing.SyncthingFailed:
        return []


def stop(client: syncthing.Client) -> None:
    try:
        own: str | None = client.own_device_id()
    except syncthing.SyncthingFailed:
        own = None
    for listing in (partial(device_pauses, client, own), partial(folder_pauses, client)):
        for write in listed(listing):
            attempted(write)


def resume(client: syncthing.Client, plan: Plan, approved: set[int], outcome: Outcome) -> None:
    states = {device.device_id: device.paused for device in client.devices()}
    folders = {found.id: found.paused for found in client.folders()}
    for want in plan.wanted.values():
        if want.device is None or want.folder not in folders:
            if want.device is not None and states.get(want.device) is False:
                client.update_device(want.device, paused=True)
            continue
        if want.pixel_id not in approved or want.held:
            if states.get(want.device) is False:
                client.update_device(want.device, paused=True)
            if folders[want.folder] is False:
                client.update_folder(want.folder, paused=True)
            continue
        client.update_folder(want.folder, devices=[want.device], paused=False)
        client.update_device(want.device, paused=False)
        outcome.resumed.add(want.pixel_id)
    if client.options().get("localAnnounceEnabled") is not True:
        client.set_options({"localAnnounceEnabled": True})


def read_reports(client: syncthing.Client, plan: Plan, outcome: Outcome) -> None:
    try:
        connections = client.connections()
        stats = client.device_stats()
        for pixel_id in sorted(outcome.resumed):
            want = plan.wanted[pixel_id]
            if want.device is None:
                continue
            completion = client.completion(want.folder, want.device)
            connected = connections.get(want.device, syncthing.Connection(False, "")).connected
            seen = stats.get(want.device)
            outcome.reports[pixel_id] = Report(connected, None if seen is None else instant(seen.last_seen), completion.completion,
                                               connected and completion.completion >= 100.0 and completion.need_items == 0 and completion.need_deletes == 0)
    except syncthing.SyncthingFailed:
        monitoring.log("syncthing's reports could not be read; the transport state stays as it was")


def without_own(plan: Plan, own: str, outcome: Outcome) -> Plan:
    outcome.own_device = {want.pixel_id for want in plan.wanted.values() if want.device == own}
    if not outcome.own_device:
        return plan
    wanted = {pixel_id: replace(want, device=None, code=INVALID_DEVICE) if pixel_id in outcome.own_device else want for pixel_id, want in plan.wanted.items()}
    return Plan(wanted, plan.pixels, plan.share_problem)


def apply(opened: Session, client: syncthing.Client, plan: Plan, now: float) -> Outcome:
    outcome = Outcome()
    try:
        own = client.own_device_id()
        plan = without_own(plan, own, outcome)
        present = correct_folders(opened, client, plan, own, outcome, now)
        correct_devices(client, plan, own, outcome)
        add_folders(client, plan, present, outcome)
        outcome.approved = approve(client, plan, own)
        if plan.share_problem is not None:
            pause_everything(client, own)
            outcome.paused = True
            return outcome
        resume(client, plan, outcome.approved, outcome)
    except syncthing.SyncthingFailed as error:
        outcome.failure, outcome.resumed = failure_reason(error), set()
        stop(client)
        return outcome
    read_reports(client, plan, outcome)
    return outcome


def last_outcome(opened: Session) -> tuple[str, str | None] | None:
    found = opened.execute(select(Event.type, Event.detail).where(Event.type.in_(OUTCOMES), Event.subject == SUBJECT).order_by(Event.id.desc()).limit(1)).first()
    return None if found is None else (found[0], found[1])


def record_outcome(opened: Session, kind: str, detail: str | None, now: float) -> None:
    last = last_outcome(opened)
    if kind == RECOVERED:
        if last is not None and last[0] != RECOVERED:
            events.record(opened, now, RECOVERED, SUBJECT)
    elif last != (kind, detail):
        events.record(opened, now, kind, SUBJECT, detail)


def code_of(want: Wanted | None, plan: Plan, outcome: Outcome) -> str | None:
    if want is None:
        return None
    if outcome.failure is not None:
        return DOWN
    if want.pixel_id in outcome.own_device:
        return INVALID_DEVICE
    if want.code in ID_PROBLEMS:
        return want.code
    if want.pixel_id in outcome.removed:
        return REMOVED
    if want.code is not None:
        return want.code
    if plan.share_problem is not None:
        return SHARE
    return HELD if want.held else None


def record(opened: Session, plan: Plan, outcome: Outcome, now: float) -> None:
    db.write_lock(opened)
    for kind, subject, detail in outcome.corrections:
        events.record(opened, now, kind, subject, detail)
    if outcome.failure is not None:
        record_outcome(opened, FAILED, outcome.failure, now)
    elif outcome.paused:
        record_outcome(opened, PAUSED, plan.share_problem, now)
    else:
        record_outcome(opened, RECOVERED, None, now)
    for pixel in opened.execute(select(Pixel).order_by(Pixel.id)).scalars().all():
        row = opened.get(PixelTransport, pixel.id)
        if row is None:
            row = PixelTransport(pixel_id=pixel.id, updated=now)
            opened.add(row)
        want = plan.wanted.get(pixel.id)
        row.paired = pixel.id in outcome.approved and outcome.failure is None
        row.error, row.updated = code_of(want, plan, outcome), now
        report = outcome.reports.get(pixel.id)
        if report is not None:
            row.last_seen = report.last_seen if report.last_seen is not None else row.last_seen
            row.completion = report.completion
            if report.synced:
                pixel.last_sync = now
    opened.commit()


def reconcile(opened: Session, client: syncthing.Client, photos: Photos, held: Callable[[int], bool], share_problem: str | None, now: float,
              found: Plan | None = None) -> Outcome:
    wanted = found if found is not None else plan(opened, photos, held, share_problem)
    opened.commit()
    outcome = apply(opened, client, wanted, now)
    record(opened, wanted, outcome, now)
    monitoring.log("syncthing reconciled", corrected=len(outcome.corrections), approved=len(outcome.approved), resumed=len(outcome.resumed),
                   paused=outcome.paused, failed=outcome.failure is not None)
    return outcome


def pause(opened: Session, client: syncthing.Client, reason: str, now: float) -> None:
    stop(client)
    db.write_lock(opened)
    record_outcome(opened, PAUSED, reason, now)
    for row in opened.execute(select(PixelTransport)).scalars().all():
        row.paired, row.error, row.updated = False, SHARE, now
    opened.commit()
    monitoring.log("syncthing transport paused: the share check failed")


def observed(client: syncthing.Client) -> tuple[object, ...] | None:
    try:
        return (tuple(sorted(client.folders(), key=lambda found: found.id)), tuple(sorted(client.devices(), key=lambda found: found.device_id)))
    except syncthing.SyncthingFailed:
        return None


@dataclass
class Transport:
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    approved: tuple[object, ...] | None = None
    seen: tuple[object, ...] | None = field(default=None, repr=False)

    def unchanged(self, client: syncthing.Client) -> bool:
        return self.seen is not None and observed(client) == self.seen

    def tick(self, opened: Session, client: syncthing.Client, photos: Photos, held: Callable[[int], bool], share_problem: str | None, now: float,
             force: bool = False) -> Outcome | None:
        with self.lock:
            found = plan(opened, photos, held, share_problem)
            opened.commit()
            if not force and found.fingerprint() == self.approved and self.unchanged(client):
                return None
            outcome = reconcile(opened, client, photos, held, share_problem, now, found)
            self.approved = found.fingerprint() if outcome.failure is None else None
            self.seen = observed(client) if outcome.failure is None else None
            return outcome

    def pause(self, opened: Session, client: syncthing.Client, reason: str, now: float) -> None:
        with self.lock:
            self.approved, self.seen = None, None
            pause(opened, client, reason, now)
