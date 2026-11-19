from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

from darkroom_browser import monitoring, selectors
from darkroom_browser.config import env
from darkroom_browser.context import DownloadInterrupted, Look, PartClick
from darkroom_browser.jobs.download import downloads_folder, remove_download, share_tree, sweep_downloads
from darkroom_browser.launch import CHROME_CHANNEL, BrowserLaunchFailed, choose_channel, launch_failure_reason, launch_options
from darkroom_browser.pages import takeout
from darkroom_browser.stall import IN_PROGRESS_SUFFIX, FolderState, file_state

DEFAULT_TIMEOUT_MS = 30_000
MAX_OPENERS = 8
FILE_WAIT_MS = 10_000
FILE_POLL_MS = 200


class NotTypeable(ValueError):
    pass


class Live:
    def __init__(self, page: Any, context: Any = None, playwright: Any = None, downloads: Path | None = None, profile: Path | None = None) -> None:
        self.page = page
        self.context = context
        self.playwright = playwright
        self.downloads = downloads
        self.profile = profile
        self.arrived: list[Any] = []
        self.clicked: str | None = None
        self.flights: dict[str, Any] = {}
        self.before_click: set[str] = set()
        self.last_click: PartClick | None = None

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

    def is_from(self, download: Any, part: PartClick) -> bool:
        if self.clicked != part.selector or not self.started_in_own_tab(download):
            return False
        return takeout.download_of_part(getattr(download, "url", None), part.href)

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

    def stems(self) -> set[str]:
        if self.downloads is None:
            return set()
        found = set()
        for each in self.downloads.iterdir():
            if each.is_symlink() or not each.is_file():
                continue
            found.add(each.name.removesuffix(IN_PROGRESS_SUFFIX))
        return found

    def own_file(self, download: Any, before: set[str]) -> str:
        waited = 0
        while True:
            new = self.stems() - before - set(self.flights)
            if len(new) == 1:
                return new.pop()
            if len(new) > 1 or waited >= FILE_WAIT_MS:
                with contextlib.suppress(Exception):
                    download.cancel()
                raise DownloadInterrupted("Darkroom could not tell which file in Chromium's download folder is this part's")
            self.page.wait_for_timeout(FILE_POLL_MS)
            waited += FILE_POLL_MS

    def start_download(self, selector: str, part: PartClick) -> str:
        if self.downloads is None:
            raise DownloadInterrupted("staggered downloads need Chromium's download folder")
        before = self.stems()
        self.clicked, self.before_click, self.last_click = selector, before, part
        with self.page.expect_download(timeout=DEFAULT_TIMEOUT_MS) as started:
            self.page.click(selector, timeout=DEFAULT_TIMEOUT_MS)
        download = started.value
        self.arrived[:] = [each for each in self.arrived if each is not download]
        handle = self.own_file(download, before)
        self.flights[handle] = download
        return handle

    def late_download(self, part: PartClick) -> str | None:
        own = [each for each in self.arrived if self.is_from(each, part)]
        if not own:
            return None
        self.arrived[:] = [each for each in self.arrived if each is not own[0]]
        handle = self.own_file(own[0], self.before_click)
        self.flights[handle] = own[0]
        return handle

    def download_state(self, handle: str) -> FolderState:
        if self.downloads is None:
            return FolderState(0, 0, False)
        return file_state(self.downloads, handle)

    def keep_download(self, handle: str, folder: Path) -> Path:
        download = self.flights.pop(handle)
        folder.mkdir(parents=True, exist_ok=True)
        failure = download.failure()
        if failure is not None:
            self.remove_files(handle)
            raise DownloadInterrupted(str(failure)[:200])
        source = Path(download.path())
        if source.name != handle:
            self.remove_files(handle)
            raise DownloadInterrupted("Chromium saved this part's download under another file")
        target = folder / Path(download.suggested_filename).name
        shutil.move(source, target)
        return target

    def cancel_download(self, handle: str) -> None:
        download = self.flights.pop(handle, None)
        if download is not None:
            with contextlib.suppress(Exception):
                download.cancel()
        self.remove_files(handle)

    def remove_files(self, handle: str) -> None:
        if self.downloads is not None:
            remove_download(self.downloads, handle)

    def drop_strays(self) -> int:
        last = self.last_click
        own = [each for each in self.arrived if last is not None and self.is_from(each, last)]
        strays = [each for each in self.arrived if not any(each is mine for mine in own)]
        self.arrived = own
        for stray in strays:
            with contextlib.suppress(Exception):
                stray.cancel()
        return len(strays)

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
