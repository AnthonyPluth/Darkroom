from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darkroom_browser import capture, jobs, monitoring
from darkroom_browser.client import Client, LeaseLost, Refused, Unreachable
from darkroom_browser.config import Settings
from darkroom_browser.context import Browser, Cancelled, JobContext, JobFailure, Screen

HEARTBEAT_SECONDS = 30.0
BACKOFF_SECONDS = 60.0
JOIN_SECONDS = 40.0
GIVE_UP_SECONDS = 120.0
ABANDONED_EXIT = 3
SCHEDULE_KINDS = ("schedule", "renew")
MAX_MESSAGE = 500
CUT = "..."
TAKE_SECONDS = 5.0
INTERACTIVE_LANE = "interactive"
IN_USE_MESSAGE = "AccountInUse: the worker already had a job open for this account, so it ran none of this one"

OpenBrowser = Callable[[Path], Browser]


def failure_message(error: BaseException) -> str:
    name = type(error).__name__
    said = f"{name}: {error}" if isinstance(error, JobFailure) else name
    return said if len(said) <= MAX_MESSAGE else said[:MAX_MESSAGE - len(CUT)] + CUT


def unrecorded_message(error: BaseException) -> str:
    return f"ScheduleNotConfirmed: the work finished but Darkroom could not record it ({type(error).__name__}); check Takeout before trying again"


def exit_now() -> None:
    os._exit(ABANDONED_EXIT)


def ignore_ack(sent: float) -> None:
    pass


class Watchdog:
    def __init__(self, job_id: int, limit: float, abandon: Callable[[], None], since: float) -> None:
        self.job_id = job_id
        self.limit = limit
        self.abandon = abandon
        self.last_ok = since
        self.disarmed = threading.Event()
        self.fired = False
        self.thread = threading.Thread(target=self.loop, daemon=True, name=f"watchdog-{job_id}")

    def start(self) -> None:
        self.thread.start()

    def acknowledged(self, sent: float) -> None:
        self.last_ok = max(self.last_ok, sent)

    def loop(self) -> None:
        while not self.disarmed.wait(max(0.0, self.last_ok + self.limit - time.monotonic())):
            if time.monotonic() >= self.last_ok + self.limit:
                self.fired = True
                monitoring.log("darkroom has not acknowledged the job for too long; the worker exits so its browser goes with it", job=self.job_id)
                self.abandon()
                return

    def disarm(self) -> None:
        self.disarmed.set()


class Heartbeat:
    def __init__(self, client: Client, ctx: JobContext, every: float, acknowledged: Callable[[float], None] = ignore_ack) -> None:
        self.client = client
        self.ctx = ctx
        self.every = every
        self.acknowledged = acknowledged
        self.stopping = threading.Event()
        self.gave_up = False
        self.thread = threading.Thread(target=self.loop, daemon=True, name=f"heartbeat-{ctx.job_id}")

    def start(self) -> None:
        self.thread.start()

    def loop(self) -> None:
        while not self.stopping.wait(self.every):
            sent = time.monotonic()
            try:
                self.client.heartbeat(self.ctx.job_id)
            except LeaseLost:
                monitoring.log("heartbeat refused; the job lost its lease", job=self.ctx.job_id)
                self.ctx.cancelled.set()
                return
            except (Unreachable, Refused) as error:
                monitoring.log("heartbeat did not reach darkroom", error=type(error).__name__)
            else:
                self.acknowledged(sent)

    def stop(self) -> None:
        self.stopping.set()
        if self.gave_up:
            return
        if self.thread.is_alive():
            self.thread.join(timeout=JOIN_SECONDS)
        if self.thread.is_alive():
            self.gave_up = True
            monitoring.log("heartbeat still in flight; reporting without waiting for it", job=self.ctx.job_id)


class Runner:
    def __init__(self, settings: Settings, client: Client, open_browser: OpenBrowser, registry: dict[str, jobs.Job] | None = None,
                 sleep: Callable[[float], None] | None = None, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 give_up_seconds: float = GIVE_UP_SECONDS, abandon: Callable[[], None] = exit_now) -> None:
        self.settings = settings
        self.client = client
        self.open_browser = open_browser
        self.registry = registry if registry is not None else jobs.REGISTRY
        self.sleep = sleep if sleep is not None else self.pause
        self.heartbeat_seconds = heartbeat_seconds
        self.give_up_seconds = give_up_seconds
        self.abandon = abandon
        self.stopping = threading.Event()
        self.screen = Screen()
        self.held = threading.Condition()
        self.in_use: set[str] = set()
        self.running: dict[int, JobContext] = {}
        self.threads: list[threading.Thread] = []
        self.failure: Exception | None = None

    def pause(self, seconds: float) -> None:
        self.stopping.wait(seconds)

    def lanes(self) -> list[str | None]:
        if self.settings.workers <= 1:
            return [None]
        return [INTERACTIVE_LANE, *[None] * (self.settings.workers - 1)]

    def context_for(self, job: dict[str, Any]) -> JobContext:
        payload = job.get("payload") or {}
        return JobContext(int(job["id"]), str(job["kind"]), str(job["account"]), dict(payload) if isinstance(payload, dict) else {},
                          self.settings.photos_root, self.client, self.open_browser, browser_root=self.settings.browser_root, screen=self.screen)

    def run_job(self, job: dict[str, Any], claim_sent: float | None = None) -> str:
        ctx = self.context_for(job)
        with self.held:
            self.running[ctx.job_id] = ctx
            if self.stopping.is_set():
                ctx.cancelled.set()
        try:
            return self.run_context(ctx, claim_sent)
        finally:
            with self.held:
                self.running.pop(ctx.job_id, None)

    def run_context(self, ctx: JobContext, claim_sent: float | None) -> str:
        monitoring.log("browser job started", job=ctx.job_id, kind=ctx.kind)
        watchdog = Watchdog(ctx.job_id, self.give_up_seconds, self.abandon, claim_sent if claim_sent is not None else time.monotonic())
        beat = Heartbeat(self.client, ctx, self.heartbeat_seconds, watchdog.acknowledged)
        watchdog.start()
        beat.start()
        outcome = "failed"
        worked = False
        try:
            work = self.registry.get(ctx.kind)
            if work is None:
                raise jobs.UnknownJob(f"no job called {ctx.kind!r}")
            result = work(ctx)
            worked = True
            ctx.check()
            beat.stop()
            if self.closed(ctx, watchdog):
                self.client.done(ctx.job_id, result)
                outcome = "done"
            else:
                outcome = "abandoned"
        except Cancelled:
            monitoring.log("browser job cancelled", job=ctx.job_id)
            outcome = "cancelled"
        except LeaseLost:
            monitoring.log("browser job lost its lease", job=ctx.job_id)
            outcome = "cancelled"
        except Exception as error:
            outcome = self.report_failure(ctx, error, beat, watchdog, worked)
        finally:
            beat.stop()
            if not self.closed(ctx, watchdog):
                outcome = "abandoned"
        monitoring.log("browser job finished", job=ctx.job_id, kind=ctx.kind, outcome=outcome)
        return outcome

    def closed(self, ctx: JobContext, watchdog: Watchdog) -> bool:
        try:
            ctx.close_browser()
        except Exception as error:
            monitoring.log("browser did not close; the worker exits so it goes with it", job=ctx.job_id, error=type(error).__name__)
            self.abandon()
            return False
        if watchdog.fired:
            return False
        watchdog.disarm()
        return True

    def report_failure(self, ctx: JobContext, error: BaseException, beat: Heartbeat, watchdog: Watchdog, worked: bool = False) -> str:
        beat.stop()
        captured = None
        try:
            captured = capture.capture(ctx.browser_open, ctx.failure_dir())
        except Exception as capture_error:
            monitoring.log("failure capture failed", error=type(capture_error).__name__)
        folder = ctx.failure_folder_name() if captured is not None else None
        if not self.closed(ctx, watchdog):
            return "abandoned"
        try:
            self.client.failed(ctx.job_id, unrecorded_message(error) if worked and ctx.kind in SCHEDULE_KINDS else failure_message(error), folder, captured.files() if captured is not None else None)
        except (Unreachable, Refused) as report_error:
            monitoring.log("could not report the failure", error=type(report_error).__name__)
        return "failed"

    def take(self, account: str) -> bool:
        with self.held:
            if not self.held.wait_for(lambda: account not in self.in_use, timeout=TAKE_SECONDS):
                return False
            self.in_use.add(account)
            return True

    def give_back(self, account: str) -> None:
        with self.held:
            self.in_use.discard(account)
            self.held.notify_all()

    def refuse(self, job: dict[str, Any]) -> None:
        monitoring.log("darkroom handed out a job for an account that already has one open here; it is reported failed, not run", job=str(job.get("id")))
        try:
            self.client.failed(int(job["id"]), IN_USE_MESSAGE, None)
        except (Unreachable, Refused) as error:
            monitoring.log("could not report the refused job", error=type(error).__name__)

    def tick(self, lane: str | None = None) -> bool:
        claim_sent = time.monotonic()
        try:
            job = self.client.claim(self.settings.worker_name, lane)
        except Refused as error:
            monitoring.log("claim refused", status=error.status)
            self.sleep(BACKOFF_SECONDS)
            return False
        except Unreachable as error:
            monitoring.log("darkroom unreachable", error=type(error).__name__)
            self.sleep(BACKOFF_SECONDS)
            return False
        if job is None:
            self.sleep(self.settings.poll_seconds)
            return False
        account = str(job.get("account", ""))
        if not self.take(account):
            self.refuse(job)
            self.sleep(self.settings.poll_seconds)
            return False
        try:
            self.run_job(job, claim_sent)
        finally:
            self.give_back(account)
        return True

    def claims(self, lane: str | None) -> None:
        try:
            while not self.stopping.is_set():
                self.tick(lane)
        except Exception as error:
            monitoring.log("a claim loop failed; the worker stops every job and exits", error=type(error).__name__)
            self.failure = self.failure or error
            self.halt()

    def run_forever(self) -> None:
        lanes = self.lanes()
        monitoring.log("browser worker ready", poll_seconds=self.settings.poll_seconds, workers=len(lanes))
        self.threads = [threading.Thread(target=self.claims, args=(lane,), daemon=True, name=f"claims-{number}") for number, lane in enumerate(lanes)]
        for thread in self.threads:
            thread.start()
        for thread in self.threads:
            thread.join()
        if self.failure is not None:
            raise self.failure

    def halt(self) -> None:
        self.stopping.set()
        with self.held:
            for ctx in self.running.values():
                ctx.cancelled.set()

    def stop(self) -> None:
        self.halt()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join()
