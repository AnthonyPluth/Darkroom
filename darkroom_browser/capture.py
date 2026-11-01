from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from darkroom_browser import monitoring
from darkroom_browser.context import Browser
from darkroom_browser.scrub import scrub_html

SCREENSHOT = "screenshot.png"
PAGE = "page.html"


@dataclass(frozen=True)
class Captured:
    folder: Path
    screenshot: bool
    page: bool


def capture(browser: Browser | None, folder: Path) -> Captured | None:
    if browser is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    screenshot = page = False
    try:
        browser.screenshot(folder / SCREENSHOT)
        screenshot = True
    except Exception as error:
        monitoring.log("screenshot failed", error=type(error).__name__)
    try:
        (folder / PAGE).write_text(scrub_html(browser.content()), encoding="utf-8")
        page = True
    except Exception as error:
        monitoring.log("page snapshot failed", error=type(error).__name__)
    if not (screenshot or page):
        return None
    return Captured(folder, screenshot, page)
