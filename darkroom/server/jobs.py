from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom import config, monitoring, oidc
from darkroom.config import Settings
from darkroom.domain import alert_push, alerts, browser_jobs, downloads, events, exports, immich_reconcile, index, pixel_check, pixel_folder, renewal, share_check, syncthing_version, transport, transport_status
from darkroom.domain.ingest import cleanup, finish_export, part_ingest, run
from darkroom.providers import immich, resend, syncthing
from darkroom.providers.push_note import Pusher
from darkroom.storage import db, stored_settings
from darkroom.storage.models import Person
from darkroom.storage.paths import Photos

Job = Callable[[float], None]
SCHEDULER_POLL_SECONDS = 30.0


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
        self.placed_copies = pixel_check.PlacedCopyCheck()
        self.share: share_check.ShareCheck | None = None
        self.syncthing: syncthing_version.Gate | None = None
        self.transport = transport.Transport()

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
        except OperationalError as error:
            if not db.is_busy(error):
                monitoring.log("job failed", job=job.name, error=type(error).__name__)
                return
            monitoring.log("job postponed: the database was busy", job=job.name)
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

    def loop(self, clock: Callable[[], float], poll: float = SCHEDULER_POLL_SECONDS) -> None:
        while not self.stopping.wait(poll):
            self.run_due(clock())

    def start(self, clock: Callable[[], float], poll: float = SCHEDULER_POLL_SECONDS) -> threading.Thread:
        thread = threading.Thread(target=self.loop, args=(clock, poll), daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.stopping.set()


ALERT_TICK_SECONDS = 600.0
PUSH_TICK_SECONDS = 60.0
INDEX_SECONDS = 3600.0
INDEX_FIRST_AFTER_SECONDS = 120.0
SESSION_RECHECK_SECONDS = 3600.0
BROWSER_LEASE_TICK_SECONDS = 60.0
IMPORT_TICK_SECONDS = 60.0
EXPORT_CHECK_TICK_SECONDS = 600.0
EXPIRY_WATCH_SECONDS = 3600.0
CLEANUP_HOUR = 4
PIXEL_TEND_SECONDS = 3600.0
IMMICH_RECONCILE_SECONDS = 900.0
SCHEDULE_WATCH_HOUR = 5
SYNCTHING_RECHECK_SECONDS = 60.0
SYNCTHING_RECONCILE_SECONDS = 60.0


def tend_checks_placed_copies(scheduler: Scheduler) -> bool:
    return "pixels.tend" in scheduler.jobs and isinstance(scheduler.placed_copies, pixel_check.PlacedCopyCheck)


def recheck_sessions(settings: Settings, engine: Engine, at: float) -> oidc.RecheckSummary:
    with db.session(engine) as opened:
        summary = oidc.recheck_due(opened, settings, at)
        if summary.notable():
            events.record(opened, at, "sessions_rechecked", "sessions", summary.detail())
    monitoring.log("sessions rechecked", checked=summary.checked, kept=summary.kept, ended=summary.ended, capped=summary.capped, unreachable=summary.unreachable)
    return summary


def push_alerts(settings: Settings, engine: Engine, pusher: Pusher | None, at: float) -> int:
    with db.session(engine) as opened:
        pushed = alert_push.deliver_due(opened, settings, pusher, at)
    if pushed:
        monitoring.log("alert pushes sent", count=pushed)
    return pushed


def pusher_of(settings: Settings) -> Pusher | None:
    if settings.demo:
        return None
    if config.ignored_alert_variables():
        monitoring.log("NTFY_* is set and ignored; alerts are emailed, so set RESEND_API_KEY, ALERT_EMAIL_FROM and ALERT_EMAIL_TO", level="warning")
    found = resend.of(settings)
    if found is None and config.resend_configured(settings):
        monitoring.log("Resend is set up but unusable, so alerts stay on the Status page; run check-config", problems=len(config.resend_problems(settings) + resend.problems(settings)))
    return found


def syncthing_client(settings: Settings) -> syncthing.Client | None:
    try:
        return syncthing.Client.on_socket(str(config.SYNCTHING_SOCKET), settings.syncthing_key) if settings.syncthing_key else None
    except syncthing.SyncthingFailed:
        return None


def check_syncthing(settings: Settings, engine: Engine, at: float, previous: syncthing_version.Gate | None = None) -> syncthing_version.Gate:
    with db.session(engine, writing=True) as opened:
        return syncthing_version.at_startup(opened, syncthing_client(settings), at, previous)


def transport_client(scheduler: Scheduler, settings: Settings) -> syncthing.Client | None:
    if scheduler.syncthing is None or not scheduler.syncthing.on:
        return None
    return syncthing_client(settings)


def share_problem(scheduler: Scheduler, opened: Session) -> str | None:
    return share_check.MOVED if scheduler.share is None else scheduler.share.transport_problem(opened)


def reconcile_transport(scheduler: Scheduler, settings: Settings, opened: Session, photos: Photos, at: float, force: bool) -> transport.Outcome | None:
    client = transport_client(scheduler, settings)
    if client is None:
        return None
    problem = share_problem(scheduler, opened)
    if problem is not None:
        scheduler.placed_copies = pixel_check.PlacedCopyCheck()
    return scheduler.transport.tick(opened, client, photos, lambda pixel_id: scheduler.placed_copies.held(opened, pixel_id), problem, at, force,
                                    checked=scheduler.placed_copies.ran)


def pause_transport(scheduler: Scheduler, settings: Settings, opened: Session, reason: str, at: float) -> None:
    client = transport_client(scheduler, settings)
    if client is not None:
        scheduler.transport.pause(opened, client, reason, at)


def review_transport(settings: Settings, engine: Engine, at: float) -> None:
    current = stored_settings.effective(settings, engine).settings
    with db.session(engine, writing=True) as opened:
        transport_status.review(opened, current, at)


def gate_syncthing(scheduler: Scheduler, settings: Settings, engine: Engine, now: float) -> Scheduler:
    if settings.demo:
        return scheduler
    scheduler.syncthing = check_syncthing(settings, engine, now)
    photos = Photos(settings.photos_root)

    def reconcile(at: float, force: bool) -> None:
        with db.session(engine) as opened:
            reconcile_transport(scheduler, settings, opened, photos, at, force)

    def recheck_syncthing(at: float) -> None:
        if scheduler.syncthing is not None and not scheduler.syncthing.on:
            scheduler.syncthing = check_syncthing(settings, engine, at, scheduler.syncthing)
            reconcile(at, True)
            review_transport(settings, engine, at)

    scheduler.guarded(Registered("syncthing.reconcile", lambda at: reconcile(at, True), None, None, now), now)
    scheduler.guarded(Registered("syncthing.alerts", lambda at: review_transport(settings, engine, at), None, None, now), now)
    scheduler.every("syncthing.gate", SYNCTHING_RECHECK_SECONDS, recheck_syncthing, first_after=SYNCTHING_RECHECK_SECONDS, now=now)
    scheduler.every("syncthing.reconcile", SYNCTHING_RECONCILE_SECONDS, lambda at: reconcile(at, False), first_after=SYNCTHING_RECONCILE_SECONDS, now=now,
                    own_thread=True)
    return scheduler


def for_serving(settings: Settings, engine: Engine, now: float) -> Scheduler:
    return gate_syncthing(build(settings, engine, now), settings, engine, now)


def pusher_key(settings: Settings) -> tuple[object, ...]:
    return settings.demo, settings.resend_api_key, settings.alert_email_from, settings.alert_email_to, settings.secret_key


def build(settings: Settings, engine: Engine, now: float = 0.0, pusher: Pusher | None = None) -> Scheduler:
    scheduler = Scheduler(settings.timezone)
    share_photos = Photos(settings.photos_root)
    share = scheduler.share = share_check.ShareCheck.at_startup(share_photos.root)
    pushers: dict[tuple[object, ...], Pusher | None] = {}

    def live() -> Settings:
        return stored_settings.effective(settings, engine).settings

    def pusher_for(current: Settings) -> Pusher | None:
        if pusher is not None:
            return pusher
        key = pusher_key(current)
        if key not in pushers:
            pushers.clear()
            pushers[key] = pusher_of(current)
        return pushers[key]

    pusher_for(live())

    def tick_alerts(at: float) -> None:
        current = live()
        with db.session(engine, writing=True) as opened:
            transport_status.review(opened, current, at)
            downloads.watch_expiry(opened, current, at)
            renewal.reword_ended(opened, current, at)
            alerts.tick(opened, current, at)

    def tick_sessions(at: float) -> None:
        recheck_sessions(live(), engine, at)

    def index_originals(at: float) -> None:
        photos = Photos(settings.photos_root)
        with db.session(engine) as opened:
            for person in opened.query(Person).order_by(Person.id).all():
                index.index_person(opened, photos, person, at)
                opened.commit()

    scheduler.every("alerts.tick", ALERT_TICK_SECONDS, tick_alerts, now=now)
    scheduler.every("index", INDEX_SECONDS, index_originals, first_after=INDEX_FIRST_AFTER_SECONDS, now=now)
    def expire_browser_leases(at: float) -> None:
        current = live()
        with db.session(engine, writing=True) as opened:
            expired = browser_jobs.expire_leases(opened, current, at)
            downloads.leases_expired(opened, expired, at)
            renewal.jobs_failed(opened, current, expired, at)
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
        current = live()
        with db.session(engine, writing=True) as opened:
            opened_now = downloads.watch_expiry(opened, current, at)
        if opened_now:
            monitoring.log("export expiry alerts opened", count=opened_now)

    def watch_schedules(at: float) -> None:
        current = live()
        with db.session(engine) as opened:
            queued = renewal.watch_schedules(opened, current, at)
        if queued:
            monitoring.log("schedule renewals queued", count=queued)

    def run_imports(at: float) -> None:
        current = live()
        with db.session(engine) as opened:
            run.run_waiting(opened, current, at)
            part_ingest.run_queued(opened, current, at)
            finish_export.run_ready(opened, current, at)

    def clean_inbox(at: float) -> None:
        with db.session(engine) as opened:
            report = cleanup.clean(opened, Photos(settings.photos_root), at)
        monitoring.log("inbox cleaned", staging=report.staging, parts=report.parts, holds=report.holds, kept=report.kept, refused=report.refused, failed=report.failed)

    def share_stopped(opened: Session, at: float) -> bool:
        refused = share.refusal(opened, at)
        if refused is not None:
            scheduler.placed_copies = pixel_check.PlacedCopyCheck()
            pause_transport(scheduler, settings, opened, refused, at)
            monitoring.log("pixel folders not tended: the share check failed", reason=refused)
            return True
        if share.rebound:
            scheduler.placed_copies = pixel_check.PlacedCopyCheck()
        return False

    def tend_pixels(at: float) -> None:
        tend(at)
        review_transport(settings, engine, at)

    def tend(at: float) -> None:
        photos = share_photos
        with db.session(engine) as opened:
            if share_stopped(opened, at):
                return
            if scheduler.placed_copies.due(opened, photos, at):
                checked = scheduler.placed_copies.run(opened, photos, at)
                monitoring.log("placed copies checked", checked=checked.checked, intact=checked.intact, requeued=checked.requeued, adopted=checked.adopted,
                               unaccounted=sum(checked.unaccounted.values()), held=len(scheduler.placed_copies.held_pixels(opened)))
            if share_stopped(opened, at):
                return
            if share.rebound:
                monitoring.log("pixel folders not tended: the share was bound again during the check; tending at the next cycle")
                return
            reconcile_transport(scheduler, settings, opened, photos, at, force=True)
            tended = pixel_folder.tend(opened, photos, settings.pixel_folder_cap_gb * pixel_folder.GIGABYTE, at)
            held = len(scheduler.placed_copies.held_pixels(opened))
        monitoring.log("pixel folders tended", placed=tended.placed, pruned=tended.pruned, skipped=tended.skipped, deferred=tended.deferred,
                       failed=tended.failed, refused=tended.refused, held=held)

    def reconcile_immich(at: float) -> None:
        current = live()
        try:
            client = immich.Client.at(current)
        except immich.NotConfigured:
            return
        with db.session(engine) as opened:
            done = immich_reconcile.reconcile_all(opened, current, at, client, immich_reconcile.PixelFolderQueue())
        monitoring.log("immich reconcile finished", people=len(done), stored=sum(report.stored for report in done.values()))

    def send_pushes(at: float) -> None:
        current = live()
        push_alerts(current, engine, pusher_for(current), at)

    scheduler.every("alerts.push", PUSH_TICK_SECONDS, send_pushes, now=now, own_thread=True)
    scheduler.every("sessions.recheck", SESSION_RECHECK_SECONDS, tick_sessions, now=now)
    scheduler.every("browser.leases", BROWSER_LEASE_TICK_SECONDS, expire_browser_leases, now=now)
    scheduler.every("imports", IMPORT_TICK_SECONDS, run_imports, now=now, own_thread=True)
    scheduler.every("exports.check", EXPORT_CHECK_TICK_SECONDS, check_exports, now=now)
    scheduler.every("exports.expiry", EXPIRY_WATCH_SECONDS, watch_expiry, now=now)
    scheduler.daily_at("schedules.watch", SCHEDULE_WATCH_HOUR, watch_schedules, now=now)
    scheduler.daily_at("cleanup", CLEANUP_HOUR, clean_inbox, now=now)
    scheduler.every("immich.reconcile", IMMICH_RECONCILE_SECONDS, reconcile_immich, now=now, own_thread=True)
    if not settings.demo:
        scheduler.every("pixels.tend", PIXEL_TEND_SECONDS, tend_pixels, now=now, own_thread=True)
    return scheduler
