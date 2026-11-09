from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

CHROME_CHANNEL = "chrome"
CHROME_BINARY = Path("/opt/google/chrome/chrome")
CHROME_MACHINES = frozenset({"x86_64", "amd64"})
VIEWPORT = {"width": 1280, "height": 900}
IGNORED_DEFAULT_ARGS = ["--enable-automation"]
BLINK_FEATURES_FLAG = "--disable-blink-features"
ARGS = [f"{BLINK_FEATURES_FLAG}=AutomationControlled"]


def choose_channel(requested: str, machine: str | None = None, chrome_installed: bool | None = None) -> str | None:
    wanted = requested.strip().lower()
    if wanted != CHROME_CHANNEL:
        return None
    if (machine if machine is not None else platform.machine()).lower() not in CHROME_MACHINES:
        return None
    installed = CHROME_BINARY.exists() if chrome_installed is None else chrome_installed
    return CHROME_CHANNEL if installed else None


def launch_options(profile_dir: Path, downloads: Path, channel: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": dict(VIEWPORT),
        "accept_downloads": True,
        "chromium_sandbox": True,
        "downloads_path": str(downloads),
        "ignore_default_args": list(IGNORED_DEFAULT_ARGS),
        "args": list(ARGS),
    }
    if channel is not None:
        options["channel"] = channel
    return options
