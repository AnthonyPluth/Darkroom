from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from darkroom_browser import monitoring
from darkroom_browser.stall import FolderState


@dataclass(frozen=True)
class Look:
    address: str
    page: str
    primary: bool


@dataclass(frozen=True)
class PartClick:
    selector: str
    href: str
    number: int


class Browser(Protocol):
    def goto(self, url: str) -> None: ...

    def content(self) -> str: ...

    def current_url(self) -> str: ...

    def click(self, selector: str) -> None: ...

    def type_text(self, selector: str, text: str) -> None: ...

    def wait_for(self, selector: str, timeout_ms: int = 30_000) -> None: ...

    def link_targets(self, selector: str) -> list[str]: ...

    def tabs(self) -> list[Look]: ...

    def start_download(self, selector: str, part: PartClick) -> str: ...

    def late_download(self, part: PartClick) -> str | None: ...

    def download_state(self, handle: str) -> FolderState: ...

    def keep_download(self, handle: str, folder: Path) -> Path: ...

    def cancel_download(self, handle: str) -> None: ...

    def drop_strays(self) -> int: ...

    def screenshot(self, path: Path) -> None: ...

    def bring_to_front(self) -> None: ...

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


class Unreachable(Exception):
    pass


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"darkroom answered {status}: {message}")
        self.status = status


class LeaseLost(Refused):
    pass


class Screen:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting: dict[int, int] = {}
        self.arrivals = itertools.count()
        self.turn = 0

    def waits(self, job_id: int) -> None:
        with self.lock:
            if job_id not in self.waiting:
                self.waiting[job_id] = next(self.arrivals)
                self.turn += 1

    def resumed(self, job_id: int) -> None:
        with self.lock:
            if self.waiting.pop(job_id, None) is not None:
                self.turn += 1

    def opened(self) -> None:
        with self.lock:
            self.turn += 1

    def turn_of(self, job_id: int) -> int | None:
        with self.lock:
            if not self.waiting or min(self.waiting, key=self.waiting.__getitem__) != job_id:
                return None
            return self.turn


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
    browser_root: Path | None = None
    screen: Screen = field(default_factory=Screen)
    shown: int | None = None

    def state_root(self) -> Path:
        return self.photos_root / "_browser" if self.browser_root is None else self.browser_root

    def profile_dir(self) -> Path:
        return self.state_root() / self.account / "profile"

    def failure_dir(self) -> Path:
        return self.state_root() / self.account / "failures" / str(self.job_id)

    def failure_folder_name(self) -> str:
        return f"_browser/{self.account}/failures/{self.job_id}"

    def browser(self) -> Browser:
        self.check()
        if self.browser_open is None:
            self.browser_open = self.open_browser(self.profile_dir())
            self.screen.opened()
        return self.browser_open

    def check(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled(f"job {self.job_id} lost its lease")
        self.show_if_first()

    def show_if_first(self) -> None:
        turn = self.screen.turn_of(self.job_id)
        if turn is None or turn == self.shown or self.browser_open is None:
            return
        self.shown = turn
        try:
            self.browser_open.bring_to_front()
        except Exception as error:
            monitoring.log("could not bring the browser window to the front", job=self.job_id, error=type(error).__name__)

    def progress(self, message: str) -> None:
        self.check()
        self.reporter.progress(self.job_id, message)
        self.screen.resumed(self.job_id)

    def wait_for_user(self, reason: str) -> None:
        self.check()
        self.reporter.waiting(self.job_id, reason)
        self.screen.waits(self.job_id)
        self.show_if_first()

    def close_browser(self) -> None:
        self.screen.resumed(self.job_id)
        if self.browser_open is not None:
            opened, self.browser_open = self.browser_open, None
            opened.close()
