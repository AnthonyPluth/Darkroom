from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darkroom_browser.launch import choose_channel, launch_options

SETTLE_SECONDS = 1.5
COLOURS = {"A": "#FF0000", "B": "#0000FF"}
TITLE = "darkroom-window-"


class Window:
    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = root
        self.orders: queue.Queue[Callable[[Any], None] | None] = queue.Queue()
        self.answers: queue.Queue[BaseException | None] = queue.Queue()
        self.thread = threading.Thread(target=self.serve, name=f"window-{name}", daemon=True)

    def serve(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            profile, downloads = self.root / self.name / "profile", self.root / self.name / "downloads"
            downloads.mkdir(parents=True)
            context = playwright.chromium.launch_persistent_context(**launch_options(profile, downloads, choose_channel("chrome")))
            page = context.pages[0] if context.pages else context.new_page()
            page.set_content(f"<title>{TITLE}{self.name}</title><body style='margin:0;background:{COLOURS[self.name]}'>{self.name}</body>")
            self.answers.put(None)
            while (order := self.orders.get()) is not None:
                try:
                    order(page)
                except BaseException as error:
                    self.answers.put(error)
                else:
                    self.answers.put(None)
            context.close()
        self.answers.put(None)

    def start(self) -> None:
        self.thread.start()
        self.wait()

    def wait(self) -> None:
        error = self.answers.get(timeout=60)
        if error is not None:
            raise error

    def bring_to_front(self) -> None:
        self.orders.put(lambda page: page.bring_to_front())
        self.wait()

    def close(self) -> None:
        self.orders.put(None)
        self.wait()


def top_by_stacking() -> str:
    listed = subprocess.run(["xwininfo", "-root", "-children"], capture_output=True, text=True, check=True).stdout
    names = [line.split(TITLE, 1)[1][:1] for line in listed.splitlines() if TITLE in line]
    return names[0] if names else ""


def top_by_pixels() -> str | None:
    if shutil.which("import") is None:
        return None
    sampled = subprocess.run(["import", "-window", "root", "-crop", "1x1+640+600", "-depth", "8", "txt:-"], capture_output=True, text=True, check=True).stdout.upper()
    return next((name for name, colour in COLOURS.items() if colour.upper() in sampled), "")


def on_top() -> str:
    time.sleep(SETTLE_SECONDS)
    stacked, shown = top_by_stacking(), top_by_pixels()
    return stacked if shown is None or shown == stacked else f"stacking says {stacked}, pixels say {shown}"


def main() -> int:
    seen: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as folder:
        first, second = Window("A", Path(folder)), Window("B", Path(folder))
        first.start()
        second.start()
        seen["after both opened"] = on_top()
        first.bring_to_front()
        seen["after A.bring_to_front"] = on_top()
        second.bring_to_front()
        seen["after B.bring_to_front"] = on_top()
        first.bring_to_front()
        seen["after A.bring_to_front again"] = on_top()
        first.close()
        second.close()
    print(json.dumps({**seen, "checked_by": "stacking and pixels" if shutil.which("import") else "stacking"}))
    expected = {"after both opened": "B", "after A.bring_to_front": "A", "after B.bring_to_front": "B", "after A.bring_to_front again": "A"}
    return 0 if seen == expected else 1


if __name__ == "__main__":
    sys.exit(main())
