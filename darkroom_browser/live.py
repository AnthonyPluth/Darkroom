from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darkroom_browser import monitoring, selectors
from darkroom_browser.config import env
from darkroom_browser.context import DownloadInterrupted, Look, PartClick
from darkroom_browser.jobs.download import downloads_folder, share_tree, sweep_downloads
from darkroom_browser.launch import CHROME_CHANNEL, BrowserLaunchFailed, choose_channel, launch_failure_reason, launch_options
from darkroom_browser.pages import takeout
from darkroom_browser.stall import POLL_MS, STALL_SECONDS, StallWatch, Verdict, folder_state

DEFAULT_TIMEOUT_MS = 30_000
PROGRESS_SECONDS = 30.0
MAX_OPENERS = 8
CANCELLED = "canceled"


class NotTypeable(ValueError):
    pass


class Live:
    def __init__(self, page: Any, context: Any = None, playwright: Any = None, downloads: Path | None = None, profile: Path | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.page = page
        self.context = context
        self.playwright = playwright
        self.downloads = downloads
        self.profile = profile
        self.clock = clock
        self.arrived: list[Any] = []
        self.clicked: str | None = None

    def watch_downloads(self) -> None:
        if self.context is None:
            return
        self.context.on("page", self.watch_page)
        for page in self.context.pages:
            self.watch_page(page)

    def watch_page(self, page: Any) -> None:
        page.on("download", self.remember)

    def remember(self, download: Any) -> None:
        self.arrived.append(download)

    def open_pages(self) -> list[Any]:
        others = [] if self.context is None else [page for page in self.context.pages if page is not self.page and not page.is_closed()]
        return [self.page, *others]

    def tabs(self) -> list[Look]:
        return [Look(str(page.url), read_page(page), page is self.page) for page in self.open_pages()]

    def download_started(self) -> bool:
        if self.arrived:
            return True
        return self.downloads is not None and folder_state(self.downloads).files > 0

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)

    def content(self) -> str:
        text = self.page.content()
        assert isinstance(text, str)
        return text

    def current_url(self) -> str:
        return str(self.page.url)

    def click(self, selector: str) -> None:
        self.page.click(selector, timeout=DEFAULT_TIMEOUT_MS)

    def type_text(self, selector: str, text: str) -> None:
        if selector not in selectors.TYPEABLE:
            raise NotTypeable("the worker types only into selectors listed in selectors.TYPEABLE, never into a sign-in form")
        self.page.fill(selector, text, timeout=DEFAULT_TIMEOUT_MS)

    def wait_for(self, selector: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.page.wait_for_selector(selector, timeout=timeout_ms)

    def link_targets(self, selector: str) -> list[str]:
        found = self.page.eval_on_selector_all(selector, "links => links.map(link => link.href)")
        return [each if isinstance(each, str) else "" for each in found]

    def download_to(self, selector: str, folder: Path, progress: Callable[[int], None] | None = None) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        self.arrived.clear()
        self.clicked = selector
        with self.page.expect_download(timeout=DEFAULT_TIMEOUT_MS) as started:
            self.page.click(selector, timeout=DEFAULT_TIMEOUT_MS)
        download = started.value
        self.arrived[:] = [each for each in self.arrived if each is not download]
        return self.keep(download, folder, progress)

    def collect_download(self, folder: Path, part: PartClick, progress: Callable[[int], None] | None = None) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        if not self.arrived and self.downloads is not None:
            watch = StallWatch(self.clock())
            while not self.arrived and watch.judge(self.clock(), folder_state(self.downloads)) is Verdict.WAIT:
                self.page.wait_for_timeout(POLL_MS)
        if not self.arrived:
            self.sweep()
            raise DownloadInterrupted("a download appeared in Chromium's folder without Playwright's event")
        arrived, self.arrived = self.arrived, []
        own = [each for each in arrived if self.is_from(each, part)]
        for other in arrived:
            if other is not (own[0] if own else None):
                with contextlib.suppress(Exception):
                    other.cancel()
        if not own:
            self.sweep()
            raise DownloadInterrupted("a download started by itself, but it was not this part's from Takeout")
        return self.keep(own[0], folder, progress)

    def is_from(self, download: Any, part: PartClick) -> bool:
        if self.clicked != part.selector or not self.started_in_own_tab(download):
            return False
        return takeout.download_of_part(getattr(download, "url", None), getattr(download, "suggested_filename", None), part.href, part.number)

    def started_in_own_tab(self, download: Any) -> bool:
        page = getattr(download, "page", None)
        for _ in range(MAX_OPENERS):
            if page is None:
                return False
            if page is self.page:
                return True
            try:
                page = page.opener()
            except Exception:
                return False
        return False

    def sweep(self) -> None:
        if self.downloads is not None:
            sweep_downloads(self.downloads)

    def keep(self, download: Any, folder: Path, progress: Callable[[int], None] | None) -> Path:
        target = folder / Path(download.suggested_filename).name
        self.wait_for_download(download, progress)
        shutil.move(download.path(), target)
        return target

    def wait_for_download(self, download: Any, progress: Callable[[int], None] | None) -> None:
        if self.downloads is None:
            self.raise_if_failed(download)
            return
        started = self.clock()
        watch = StallWatch(started)
        reported = started
        while True:
            now = self.clock()
            state = folder_state(self.downloads)
            verdict = watch.judge(now, state)
            if verdict is not Verdict.WAIT:
                break
            if progress is not None and now - reported >= PROGRESS_SECONDS:
                reported = now
                with contextlib.suppress(Exception):
                    progress(state.size)
            self.page.wait_for_timeout(POLL_MS)
        if verdict is Verdict.STALLED:
            self.cancel_stalled(download)
        else:
            self.raise_if_failed(download)

    def cancel_stalled(self, download: Any) -> None:
        with contextlib.suppress(Exception):
            download.cancel()
        failure = download.failure()
        if failure is None:
            return
        if self.downloads is not None:
            sweep_downloads(self.downloads)
        if str(failure) == CANCELLED:
            raise DownloadInterrupted(f"no data arrived for {int(STALL_SECONDS)} seconds")
        raise DownloadInterrupted(str(failure)[:200])

    def raise_if_failed(self, download: Any) -> None:
        failure = download.failure()
        if failure is not None:
            if self.downloads is not None:
                sweep_downloads(self.downloads)
            raise DownloadInterrupted(str(failure)[:200])

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)

    def close(self) -> None:
        failure: Exception | None = None
        for closable in (self.context, self.playwright):
            if closable is None:
                continue
            try:
                if closable is self.playwright:
                    closable.stop()
                else:
                    closable.close()
            except Exception as error:
                failure = failure or error
        if self.profile is not None:
            with contextlib.suppress(OSError):
                share_tree(self.profile.parent)
        if failure is not None:
            raise failure


def read_page(page: Any) -> str:
    try:
        text = page.content()
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


def launch_context(playwright: Any, profile_dir: Path, downloads: Path, channel: str | None) -> Any:
    channels = (channel, None) if channel is not None else (None,)
    for candidate in channels:
        try:
            return playwright.chromium.launch_persistent_context(**launch_options(profile_dir, downloads, candidate))
        except Exception as error:
            reason = launch_failure_reason(error)
            monitoring.log("chrome_launch_failed" if candidate else "chromium_launch_failed",
                           error=type(error).__name__, channel=candidate or "chromium", reason=reason)
            if candidate is None:
                raise BrowserLaunchFailed(reason) from None
    raise AssertionError("no browser launch attempted")


def open_context(profile_dir: Path, working_root: Path) -> Live:
    from playwright.sync_api import sync_playwright

    downloads = downloads_folder(profile_dir, working_root)
    sweep_downloads(downloads)
    profile_dir.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    try:
        context = launch_context(playwright, profile_dir, downloads, choose_channel(env("BROWSER_CHANNEL", CHROME_CHANNEL)))
    except BaseException:
        playwright.stop()
        raise
    page = context.pages[0] if context.pages else context.new_page()
    opened = Live(page, context, playwright, downloads, profile_dir)
    opened.watch_downloads()
    return opened
