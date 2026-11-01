from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from darkroom import monitoring
from darkroom.config import Settings
from darkroom.domain import alerts
from darkroom.storage import db

Job = Callable[[float], None]


@dataclass
class Registered:
    name: str
    run: Job
    interval: float | None
    hour: int | None
    next_due: float


class Scheduler:
    def __init__(self, zone: str = "UTC") -> None:
        self.zone = ZoneInfo(zone)
        self.jobs: dict[str, Registered] = {}
        self.stopping = threading.Event()

    def every(self, name: str, interval: float, run: Job, first_after: float = 0.0, now: float = 0.0) -> None:
        self.jobs[name] = Registered(name, run, interval, None, now + first_after)

    def daily_at(self, name: str, hour: int, run: Job, now: float = 0.0) -> None:
        self.jobs[name] = Registered(name, run, None, hour, self.next_daily(hour, now))

    def next_daily(self, hour: int, now: float) -> float:
        local = datetime.fromtimestamp(now, self.zone)
        target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def run_due(self, now: float) -> list[str]:
        ran = []
        for job in self.jobs.values():
            if now < job.next_due:
                continue
            try:
                job.run(now)
            except Exception as error:
                monitoring.log("job failed", job=job.name, error=type(error).__name__)
            job.next_due = now + job.interval if job.interval is not None else self.next_daily(job.hour or 0, now)
            ran.append(job.name)
        return ran

    def run_one(self, name: str, now: float) -> None:
        self.jobs[name].run(now)

    def loop(self, clock: Callable[[], float], poll: float = 30.0) -> None:
        while not self.stopping.wait(poll):
            self.run_due(clock())

    def start(self, clock: Callable[[], float], poll: float = 30.0) -> threading.Thread:
        thread = threading.Thread(target=self.loop, args=(clock, poll), daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.stopping.set()


ALERT_TICK_SECONDS = 600.0


def build(settings: Settings, engine: Engine, now: float = 0.0) -> Scheduler:
    scheduler = Scheduler(settings.timezone)

    def tick_alerts(at: float) -> None:
        with db.session(engine) as opened:
            alerts.tick(opened, settings, at)

    scheduler.every("alerts.tick", ALERT_TICK_SECONDS, tick_alerts, now=now)
    return scheduler
