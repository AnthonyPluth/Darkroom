from __future__ import annotations

import threading
from pathlib import Path

from darkroom_browser.context import JobContext


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Reporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def progress(self, job_id: int, message: str) -> None:
        self.events.append(("progress", message))

    def waiting(self, job_id: int, reason: str) -> None:
        self.events.append(("waiting", reason))

    def kinds(self, kind: str) -> list[str]:
        return [message for name, message in self.events if name == kind]


class ScriptedBrowser:
    def __init__(self, pages: list[str], after_click: dict[str, list[str]] | None = None) -> None:
        self.pages = list(pages)
        self.reads = 0
        self.visited: list[str] = []
        self.clicks: list[str] = []
        self.waited: list[str] = []
        self.typed: list[str] = []
        self.missing: set[str] = set()
        self.after_click = after_click or {}
        self.closed = False

    def goto(self, url: str) -> None:
        self.visited.append(url)

    def content(self) -> str:
        page = self.pages[min(self.reads, len(self.pages) - 1)]
        self.reads += 1
        return page

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if selector in self.after_click:
            self.pages = list(self.after_click[selector])
            self.reads = 0

    def type_text(self, selector: str, text: str) -> None:
        self.typed.append(selector)

    def wait_for(self, selector: str, timeout_ms: int = 30_000) -> None:
        self.waited.append(selector)
        if selector in self.missing:
            raise TimeoutError(f"no {selector} on this page")

    def download_to(self, selector: str, folder: Path) -> Path:
        raise AssertionError("onboarding jobs download nothing")

    def screenshot(self, path: Path) -> None:
        path.write_bytes(b"\x89PNG")

    def close(self) -> None:
        self.closed = True


def context_for(browser: ScriptedBrowser, clock: Clock | None = None, reporter: Reporter | None = None, wall: float = 1_793_750_400.0, email: str | None = "jo@example.test") -> tuple[JobContext, Clock, Reporter]:
    clock = clock or Clock()
    reporter = reporter or Reporter()
    ctx = JobContext(7, "connect", "1", {} if email is None else {"email": email}, Path("/photos-unused"), reporter, lambda profile: browser, threading.Event(), None, clock.sleep, clock.monotonic, lambda: wall)
    return ctx, clock, reporter

