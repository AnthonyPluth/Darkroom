from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from darkroom import monitoring, oidc
from darkroom.config import Settings
from darkroom.domain import alerts, browser_jobs, downloads, events, exports, index
from darkroom.domain.ingest import cleanup, run
from darkroom.storage import db
from darkroom.storage.models import Person
from darkroom.storage.paths import Photos

Job = Callable[[float], None]


@dataclass
class Registered:
    name: str
    run: Job
    interval: float | None
    hour: int | None
    next_due: float
    own_thread: bool = False
    worker: threading.Thread | None = None


class Scheduler:
    def __init__(self, zone: str = "UTC") -> None:
        self.zone = ZoneInfo(zone)
        self.jobs: dict[str, Registered] = {}
        self.stopping = threading.Event()

    def every(self, name: str, interval: float, run: Job, first_after: float = 0.0, now: float = 0.0, own_thread: bool = False) -> None:
        self.jobs[name] = Registered(name, run, interval, None, now + first_after, own_thread)

    def daily_at(self, name: str, hour: int, run: Job, now: float = 0.0) -> None:
        self.jobs[name] = Registered(name, run, None, hour, self.next_daily(hour, now))

    def next_daily(self, hour: int, now: float) -> float:
        local = datetime.fromtimestamp(now, self.zone)
        target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def guarded(self, job: Registered, now: float) -> None:
        try:
            job.run(now)
        except Exception as error:
            monitoring.log("job failed", job=job.name, error=type(error).__name__)

    def run_due(self, now: float) -> list[str]:
        ran = []
        for job in self.jobs.values():
            if now < job.next_due:
                continue
            if job.own_thread:
                if job.worker is not None and job.worker.is_alive():
                    continue
                job.worker = threading.Thread(target=self.guarded, args=(job, now), name=f"job-{job.name}", daemon=True)
                job.worker.start()
            else:
                self.guarded(job, now)
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
INDEX_SECONDS = 3600.0
INDEX_FIRST_AFTER_SECONDS = 120.0
SESSION_RECHECK_SECONDS = 3600.0
BROWSER_LEASE_TICK_SECONDS = 60.0
IMPORT_TICK_SECONDS = 60.0
EXPORT_CHECK_TICK_SECONDS = 600.0
EXPIRY_WATCH_SECONDS = 3600.0
CLEANUP_HOUR = 4


def recheck_sessions(settings: Settings, engine: Engine, at: float) -> oidc.RecheckSummary:
    with db.session(engine) as opened:
        summary = oidc.recheck_due(opened, settings, at)
        if summary.notable():
            events.record(opened, at, "sessions_rechecked", "sessions", summary.detail())
    monitoring.log("sessions rechecked", checked=summary.checked, kept=summary.kept, ended=summary.ended, capped=summary.capped, unreachable=summary.unreachable)
    return summary


def build(settings: Settings, engine: Engine, now: float = 0.0) -> Scheduler:
    scheduler = Scheduler(settings.timezone)

    def tick_alerts(at: float) -> None:
        with db.session(engine) as opened:
            downloads.watch_expiry(opened, settings, at)
            alerts.tick(opened, settings, at)

    def tick_sessions(at: float) -> None:
        recheck_sessions(settings, engine, at)

    def index_originals(at: float) -> None:
        photos = Photos(settings.photos_root)
        with db.session(engine) as opened:
            for person in opened.query(Person).order_by(Person.id):
                index.index_person(opened, photos, person, at)

    scheduler.every("alerts.tick", ALERT_TICK_SECONDS, tick_alerts, now=now)
    scheduler.every("index", INDEX_SECONDS, index_originals, first_after=INDEX_FIRST_AFTER_SECONDS, now=now)
    def expire_browser_leases(at: float) -> None:
        with db.session(engine) as opened:
            expired = browser_jobs.expire_leases(opened, settings, at)
            downloads.jobs_failed(opened, expired, at)
        if expired:
            monitoring.log("browser jobs lost their lease", count=len(expired))

    def check_exports(at: float) -> None:
        try:
            with db.session(engine) as opened:
                queued = exports.check_due(opened, at, scheduler.zone)
        except browser_jobs.Busy:
            monitoring.log("export status checks postponed: the database was busy")
            return
        if queued:
            monitoring.log("export status checks queued", count=queued)

    def watch_expiry(at: float) -> None:
        with db.session(engine) as opened:
            opened_now = downloads.watch_expiry(opened, settings, at)
        if opened_now:
            monitoring.log("export expiry alerts opened", count=opened_now)

    def run_imports(at: float) -> None:
        with db.session(engine) as opened:
            run.run_waiting(opened, settings, at)

    def clean_inbox(at: float) -> None:
        with db.session(engine) as opened:
            report = cleanup.clean(opened, Photos(settings.photos_root), at)
        monitoring.log("inbox cleaned", staging=report.staging, parts=report.parts, holds=report.holds, kept=report.kept, refused=report.refused, failed=report.failed)

    scheduler.every("sessions.recheck", SESSION_RECHECK_SECONDS, tick_sessions, now=now)
    scheduler.every("browser.leases", BROWSER_LEASE_TICK_SECONDS, expire_browser_leases, now=now)
    scheduler.every("imports", IMPORT_TICK_SECONDS, run_imports, now=now, own_thread=True)
    scheduler.every("exports.check", EXPORT_CHECK_TICK_SECONDS, check_exports, now=now)
    scheduler.every("exports.expiry", EXPIRY_WATCH_SECONDS, watch_expiry, now=now)
    scheduler.daily_at("cleanup", CLEANUP_HOUR, clean_inbox, now=now)
    return scheduler
