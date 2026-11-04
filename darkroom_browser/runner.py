from __future__ import annotations

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

OpenBrowser = Callable[[Path], Browser]


class Heartbeat:
    def __init__(self, client: Client, ctx: JobContext, every: float) -> None:
        self.client = client
        self.ctx = ctx
        self.every = every
        self.stopping = threading.Event()
        self.gave_up = False
        self.thread = threading.Thread(target=self.loop, daemon=True, name=f"heartbeat-{ctx.job_id}")

    def start(self) -> None:
        self.thread.start()

    def loop(self) -> None:
        while not self.stopping.wait(self.every):
            try:
                self.client.heartbeat(self.ctx.job_id)
            except LeaseLost:
                monitoring.log("heartbeat refused; the job lost its lease", job=self.ctx.job_id)
                self.ctx.cancelled.set()
                return
            except (Unreachable, Refused) as error:
                monitoring.log("heartbeat did not reach darkroom", error=type(error).__name__)

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
                 sleep: Callable[[float], None] = time.sleep, heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
        self.settings = settings
        self.client = client
        self.open_browser = open_browser
        self.registry = registry if registry is not None else jobs.REGISTRY
        self.sleep = sleep
        self.heartbeat_seconds = heartbeat_seconds
        self.stopping = threading.Event()

    def context_for(self, job: dict[str, Any]) -> JobContext:
        payload = job.get("payload") or {}
        return JobContext(int(job["id"]), str(job["kind"]), str(job["account"]), dict(payload) if isinstance(payload, dict) else {},
                          self.settings.photos_root, self.client, self.open_browser)

    def run_job(self, job: dict[str, Any]) -> str:
        ctx = self.context_for(job)
        monitoring.log("browser job started", job=ctx.job_id, kind=ctx.kind)
        beat = Heartbeat(self.client, ctx, self.heartbeat_seconds)
        beat.start()
        outcome = "failed"
        try:
            work = self.registry.get(ctx.kind)
            if work is None:
                raise jobs.UnknownJob(f"no job called {ctx.kind!r}")
            result = work(ctx)
            ctx.check()
            beat.stop()
            self.client.done(ctx.job_id, result)
            outcome = "done"
        except Cancelled:
            monitoring.log("browser job cancelled", job=ctx.job_id)
            outcome = "cancelled"
        except LeaseLost:
            monitoring.log("browser job lost its lease", job=ctx.job_id)
            outcome = "cancelled"
        except Exception as error:
            self.report_failure(ctx, error)
        finally:
            beat.stop()
            ctx.close_browser()
        monitoring.log("browser job finished", job=ctx.job_id, kind=ctx.kind, outcome=outcome)
        return outcome

    def report_failure(self, ctx: JobContext, error: BaseException) -> None:
        captured = None
        try:
            captured = capture.capture(ctx.browser_open, ctx.failure_dir())
        except Exception as capture_error:
            monitoring.log("failure capture failed", error=type(capture_error).__name__)
        folder = ctx.failure_folder_name() if captured is not None else None
        try:
            self.client.failed(ctx.job_id, type(error).__name__, folder, captured.files() if captured is not None else None)
        except (Unreachable, Refused) as report_error:
            monitoring.log("could not report the failure", error=type(report_error).__name__)

    def tick(self) -> bool:
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
        self.run_job(job)
        return True

    def run_forever(self) -> None:
        monitoring.log("browser worker ready", poll_seconds=self.settings.poll_seconds)
        while not self.stopping.is_set():
            self.tick()

    def stop(self) -> None:
        self.stopping.set()
