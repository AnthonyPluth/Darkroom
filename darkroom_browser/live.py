from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darkroom_browser import selectors
from darkroom_browser.config import env
from darkroom_browser.context import DownloadInterrupted
from darkroom_browser.jobs.download import downloads_folder, share_tree, sweep_downloads
from darkroom_browser.launch import CHROME_CHANNEL, choose_channel, launch_options
from darkroom_browser.stall import POLL_MS, STALL_SECONDS, StallWatch, Verdict, folder_state

DEFAULT_TIMEOUT_MS = 30_000
PROGRESS_SECONDS = 30.0
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

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)

    def content(self) -> str:
        text = self.page.content()
        assert isinstance(text, str)
        return text

    def click(self, selector: str) -> None:
        self.page.click(selector, timeout=DEFAULT_TIMEOUT_MS)

    def type_text(self, selector: str, text: str) -> None:
        if selector not in selectors.TYPEABLE:
            raise NotTypeable("the worker types only into selectors listed in selectors.TYPEABLE, never into a sign-in form")
        self.page.fill(selector, text, timeout=DEFAULT_TIMEOUT_MS)

    def wait_for(self, selector: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.page.wait_for_selector(selector, timeout=timeout_ms)

    def download_to(self, selector: str, folder: Path, progress: Callable[[int], None] | None = None) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        with self.page.expect_download(timeout=DEFAULT_TIMEOUT_MS) as started:
            self.page.click(selector, timeout=DEFAULT_TIMEOUT_MS)
        download = started.value
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


def open_context(profile_dir: Path) -> Live:
    from playwright.sync_api import sync_playwright

    downloads = downloads_folder(profile_dir)
    sweep_downloads(downloads)
    profile_dir.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    channel = choose_channel(env("BROWSER_CHANNEL", CHROME_CHANNEL))
    try:
        context = playwright.chromium.launch_persistent_context(**launch_options(profile_dir, downloads, channel))
    except Exception:
        if channel is None:
            playwright.stop()
            raise
        context = playwright.chromium.launch_persistent_context(**launch_options(profile_dir, downloads, None))
    page = context.pages[0] if context.pages else context.new_page()
    return Live(page, context, playwright, downloads, profile_dir)
