from __future__ import annotations

import contextlib
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePath
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts
from darkroom.storage import db
from darkroom.storage.models import Alert, Export, ExportPart

ITEMS_BETWEEN_LOGS: Final = 250
SECONDS_BETWEEN_LOGS: Final = 300.0
STACK_FRAMES: Final = 15
STALLED_KIND: Final = "part_stalled"
STALLED_PREFIX: Final = "part:stalled:"
INGESTING: Final = "ingesting"
WORKER_PREFIX: Final = "fingerprint"


@dataclass(frozen=True)
class Current:
    export_id: int
    part: int
    count: int


@dataclass(frozen=True)
class Snapshot:
    thread: int | None
    started: float | None
    step: str | None
    current: Current | None


class Heartbeat:
    """What the import tick is doing, for its log lines, the scheduler's "still running" line and the stall watch.

    Only counts, ids, durations and fixed step names are kept or logged, never a name or a path."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.guard = threading.Lock()
        self.thread: int | None = None
        self.started: float | None = None
        self.step_name: str | None = None
        self.current: Current | None = None
        self.part_started = 0.0
        self.logged_count = 0
        self.logged_at = 0.0
        self.ticks = 0

    @contextlib.contextmanager
    def ticking(self, **counts: int) -> Iterator[None]:
        with self.guard:
            self.thread, self.started, self.step_name, self.current = threading.get_ident(), self.clock(), None, None
            self.ticks += 1
        monitoring.log("import tick started", **counts)
        completed = False
        try:
            yield
            completed = True
        finally:
            with self.guard:
                elapsed = 0.0 if self.started is None else self.clock() - self.started
                self.thread, self.started, self.step_name, self.current = None, None, None, None
            monitoring.log("import tick finished", seconds=round(elapsed), completed=completed)

    def step(self, name: str, **counts: int) -> None:
        with self.guard:
            self.step_name = name
            first = self.ticks <= 1
        if first or counts:
            monitoring.log("import tick step", step=name, **counts)

    def part_began(self, export_id: int, number: int, count: int, entries: int, resumed: bool) -> None:
        now = self.clock()
        with self.guard:
            self.current = Current(export_id, number, count)
            self.part_started, self.logged_at, self.logged_count = now, now, count
        monitoring.log("part ingest started", export=export_id, part=number, done=count, entries=entries, resumed=resumed)

    def part_step(self, name: str) -> None:
        with self.guard:
            self.step_name = name
            current = self.current
        if current is not None:
            monitoring.log("part ingest step", export=current.export_id, part=current.part, step=name, done=current.count,
                           seconds=round(self.clock() - self.part_started))

    def counted(self, count: int, entries: int) -> None:
        now = self.clock()
        with self.guard:
            current = self.current
            if current is None:
                return
            self.current = Current(current.export_id, current.part, count)
            due = count - self.logged_count >= ITEMS_BETWEEN_LOGS or (count != self.logged_count and now - self.logged_at >= SECONDS_BETWEEN_LOGS)
            if due:
                self.logged_count, self.logged_at = count, now
        if due:
            monitoring.log("part ingest progress", export=current.export_id, part=current.part, done=count, entries=entries,
                           seconds=round(now - self.part_started))

    def part_ended(self) -> None:
        with self.guard:
            self.current = None
            if self.thread is not None:
                self.step_name = "part ingest"

    def snapshot(self) -> Snapshot:
        with self.guard:
            return Snapshot(self.thread, self.started, self.step_name, self.current)

    def fields(self) -> dict[str, int | str | bool]:
        seen = self.snapshot()
        found: dict[str, int | str | bool] = {"step": seen.step or "none"}
        if seen.current is not None:
            found |= {"export": seen.current.export_id, "part": seen.current.part, "done": seen.current.count}
        return found


def quiet() -> Heartbeat:
    return Heartbeat()


def stack_of(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "gone"
    frames = traceback.extract_stack(frame)[-STACK_FRAMES:]
    return " < ".join(f"{PurePath(each.filename).name}:{each.lineno}:{each.name}" for each in reversed(frames))


def log_stacks(beat: Heartbeat, export_id: int, number: int) -> None:
    """Where the import thread and its fingerprint workers are waiting: module file names, lines and functions only."""
    seen = beat.snapshot()
    if seen.thread is not None:
        monitoring.log("stalled import stack", export=export_id, part=number, thread="import", frames=stack_of(seen.thread))
    for index, thread in enumerate(sorted((each for each in threading.enumerate() if each.name.startswith(WORKER_PREFIX)), key=lambda each: each.name)):
        if thread.ident is not None:
            monitoring.log("stalled import stack", export=export_id, part=number, thread=f"worker {index}", frames=stack_of(thread.ident))


def stall_seconds(settings: config.Settings) -> float:
    return 60.0 * config.import_stall_minutes(settings)


def stalled_key(export_id: int, number: int) -> str:
    return f"{STALLED_PREFIX}{export_id}:{number}"


class StallWatch:
    """Opens one alert for a part whose stored count has not moved for the stall time while the import tick is working on it."""

    def __init__(self) -> None:
        self.seen: dict[tuple[int, int], tuple[int, float]] = {}

    def review(self, opened: Session, settings: config.Settings, beat: Heartbeat, now: float, notifier: alerts.Notifier | None = None) -> list[tuple[int, int]]:
        current = beat.snapshot().current
        watched = None if current is None else (current.export_id, current.part)
        part = None if watched is None else opened.execute(
            select(ExportPart).where(ExportPart.export_id == watched[0], ExportPart.number == watched[1], ExportPart.ingest_state == INGESTING)
        ).scalar_one_or_none()
        if part is None:
            watched = None
        self.seen = {key: value for key, value in self.seen.items() if key == watched}
        stalled: list[tuple[int, int]] = []
        if watched is not None and part is not None:
            count = part.entries_done or 0
            last = self.seen.get(watched)
            if last is None or last[0] != count:
                self.seen[watched] = (count, now)
            elif now - last[1] >= stall_seconds(settings):
                stalled.append(watched)
        wanted = {stalled_key(*key) for key in stalled}
        open_keys = set(opened.execute(select(Alert.key).where(Alert.kind == STALLED_KIND, Alert.closed.is_(None))).scalars())
        if open_keys == wanted:
            return stalled
        db.write_lock(opened)
        for key in sorted(open_keys - wanted):
            alerts.close(opened, key, now)
        for export_id, number in stalled:
            self.open(opened, settings, beat, export_id, number, now, notifier)
        opened.commit()
        return stalled

    def open(self, opened: Session, settings: config.Settings, beat: Heartbeat, export_id: int, number: int, now: float,
             notifier: alerts.Notifier | None) -> None:
        key = stalled_key(export_id, number)
        if opened.execute(select(Alert.id).where(Alert.key == key, Alert.closed.is_(None))).first() is not None:
            return
        export = opened.get(Export, export_id)
        minutes = config.import_stall_minutes(settings)
        alerts.open_alert(opened, settings, key, STALLED_KIND, f"export {export_id}", alerts.part_stalled(number, export_id, minutes), None, now,
                          notifier=notifier, about=alerts.NOBODY if export is None else alerts.account(export.account_id))
        monitoring.log("part ingest stalled", export=export_id, part=number, minutes=minutes, step=beat.snapshot().step or "none")
        log_stacks(beat, export_id, number)
