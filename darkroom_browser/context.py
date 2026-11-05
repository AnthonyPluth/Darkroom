from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class Browser(Protocol):
    def goto(self, url: str) -> None: ...

    def content(self) -> str: ...

    def click(self, selector: str) -> None: ...

    def type_text(self, selector: str, text: str) -> None: ...

    def wait_for(self, selector: str, timeout_ms: int = 30_000) -> None: ...

    def download_to(self, selector: str, folder: Path, progress: Callable[[int], None] | None = None) -> Path: ...

    def screenshot(self, path: Path) -> None: ...

    def close(self) -> None: ...


class Reporter(Protocol):
    def progress(self, job_id: int, message: str) -> None: ...

    def waiting(self, job_id: int, reason: str) -> None: ...

    def download_plan(self, job_id: int) -> dict[str, Any]: ...

    def part(self, job_id: int, number: int, body: dict[str, object]) -> dict[str, Any]: ...


class Cancelled(Exception):
    pass


class DownloadInterrupted(Exception):
    pass


class JobFailure(Exception):
    pass


@dataclass
class JobContext:
    job_id: int
    kind: str
    account: str
    payload: dict[str, Any]
    photos_root: Path
    reporter: Reporter
    open_browser: Callable[[Path], Browser]
    cancelled: threading.Event = field(default_factory=threading.Event)
    browser_open: Browser | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time

    def profile_dir(self) -> Path:
        return self.photos_root / "_browser" / self.account / "profile"

    def failure_dir(self) -> Path:
        return self.photos_root / "_browser" / self.account / "failures" / str(self.job_id)

    def failure_folder_name(self) -> str:
        return f"_browser/{self.account}/failures/{self.job_id}"

    def browser(self) -> Browser:
        self.check()
        if self.browser_open is None:
            self.browser_open = self.open_browser(self.profile_dir())
        return self.browser_open

    def check(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled(f"job {self.job_id} lost its lease")

    def progress(self, message: str) -> None:
        self.check()
        self.reporter.progress(self.job_id, message)

    def wait_for_user(self, reason: str) -> None:
        self.check()
        self.reporter.waiting(self.job_id, reason)

    def close_browser(self) -> None:
        if self.browser_open is not None:
            opened, self.browser_open = self.browser_open, None
            opened.close()
