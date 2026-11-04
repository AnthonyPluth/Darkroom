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
from darkroom_browser.context import Browser, Cancelled, JobContext

HEARTBEAT_SECONDS = 30.0
BACKOFF_SECONDS = 60.0
JOIN_SECONDS = 40.0
GIVE_UP_SECONDS = 120.0
ABANDONED_EXIT = 3

OpenBrowser = Callable[[Path], Browser]


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
                 sleep: Callable[[float], None] = time.sleep, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 give_up_seconds: float = GIVE_UP_SECONDS, abandon: Callable[[], None] = exit_now) -> None:
        self.settings = settings
        self.client = client
        self.open_browser = open_browser
        self.registry = registry if registry is not None else jobs.REGISTRY
        self.sleep = sleep
        self.heartbeat_seconds = heartbeat_seconds
        self.give_up_seconds = give_up_seconds
        self.abandon = abandon
        self.stopping = threading.Event()

    def context_for(self, job: dict[str, Any]) -> JobContext:
        payload = job.get("payload") or {}
        return JobContext(int(job["id"]), str(job["kind"]), str(job["account"]), dict(payload) if isinstance(payload, dict) else {},
                          self.settings.photos_root, self.client, self.open_browser)

    def run_job(self, job: dict[str, Any], claim_sent: float | None = None) -> str:
        ctx = self.context_for(job)
        monitoring.log("browser job started", job=ctx.job_id, kind=ctx.kind)
        watchdog = Watchdog(ctx.job_id, self.give_up_seconds, self.abandon, claim_sent if claim_sent is not None else time.monotonic())
        beat = Heartbeat(self.client, ctx, self.heartbeat_seconds, watchdog.acknowledged)
        watchdog.start()
        beat.start()
        outcome = "failed"
        try:
            work = self.registry.get(ctx.kind)
            if work is None:
                raise jobs.UnknownJob(f"no job called {ctx.kind!r}")
            result = work(ctx)
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
            outcome = self.report_failure(ctx, error, beat, watchdog)
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

    def report_failure(self, ctx: JobContext, error: BaseException, beat: Heartbeat, watchdog: Watchdog) -> str:
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
            self.client.failed(ctx.job_id, type(error).__name__, folder, captured.files() if captured is not None else None)
        except (Unreachable, Refused) as report_error:
            monitoring.log("could not report the failure", error=type(report_error).__name__)
        return "failed"

    def tick(self) -> bool:
        claim_sent = time.monotonic()
        try:
            job = self.client.claim(self.settings.worker_name)
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
        self.run_job(job, claim_sent)
        return True

    def run_forever(self) -> None:
        monitoring.log("browser worker ready", poll_seconds=self.settings.poll_seconds)
        while not self.stopping.is_set():
            self.tick()

    def stop(self) -> None:
        self.stopping.set()
