from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darkroom_browser import selectors
from darkroom_browser.context import DownloadInterrupted
from darkroom_browser.jobs.download import downloads_folder, sweep_downloads

DEFAULT_TIMEOUT_MS = 30_000
PROGRESS_SECONDS = 30.0
VIEWPORT = {"width": 1280, "height": 900}


class NotTypeable(ValueError):
    pass


class Live:
    def __init__(self, page: Any, context: Any = None, playwright: Any = None, downloads: Path | None = None) -> None:
        self.page = page
        self.context = context
        self.playwright = playwright
        self.downloads = downloads

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
        stop = threading.Event()
        watcher = threading.Thread(target=self.watch_size, args=(stop, progress), daemon=True)
        watcher.start()
        try:
            failure = download.failure()
            if failure is not None:
                raise DownloadInterrupted(str(failure)[:200])
            shutil.move(download.path(), target)
        finally:
            stop.set()
            watcher.join(timeout=5)
        return target

    def watch_size(self, stop: threading.Event, progress: Callable[[int], None] | None) -> None:
        if progress is None or self.downloads is None:
            return
        while not stop.wait(PROGRESS_SECONDS):
            try:
                size = sum(each.stat().st_size for each in self.downloads.iterdir() if each.is_file() and not each.is_symlink())
                progress(size)
            except Exception:
                continue

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
        if failure is not None:
            raise failure


def open_context(profile_dir: Path) -> Live:
    from playwright.sync_api import sync_playwright

    downloads = downloads_folder(profile_dir)
    sweep_downloads(downloads)
    profile_dir.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(user_data_dir=str(profile_dir), headless=False, viewport=VIEWPORT, accept_downloads=True,
                                                            downloads_path=str(downloads))
    page = context.pages[0] if context.pages else context.new_page()
    return Live(page, context, playwright, downloads)
