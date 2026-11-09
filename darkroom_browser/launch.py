from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from darkroom_browser.context import JobFailure

CHROME_CHANNEL = "chrome"
CHROME_BINARY = Path("/opt/google/chrome/chrome")
CHROME_MACHINES = frozenset({"x86_64", "amd64"})
VIEWPORT = {"width": 1280, "height": 900}
IGNORED_DEFAULT_ARGS = ["--enable-automation"]
BLINK_FEATURES_FLAG = "--disable-blink-features"
ARGS = [f"{BLINK_FEATURES_FLAG}=AutomationControlled"]

LAUNCH_FAILURES = {
    "sandbox_unavailable": "the browser sandbox could not start; apply deploy/browser/seccomp.json and allow unprivileged user namespaces on the host",
    "profile_in_use": "the browser profile is locked; stop every worker using it before checking for stale Singleton locks",
    "permission_denied": "the browser cannot access its files; check ownership of _browser, _inbox and the browser home for the configured UID and GID",
    "display_unavailable": "the browser cannot reach Xvfb; check DISPLAY and the browser container startup logs",
    "browser_missing": "the browser executable is missing; rebuild or pull the matching browser image",
    "browser_exited": "the browser exited during startup; check the container memory limit, sandbox, display and profile ownership",
}


class BrowserLaunchFailed(JobFailure):
    def __init__(self, reason: str) -> None:
        super().__init__(LAUNCH_FAILURES[reason])
        self.reason = reason


def launch_failure_reason(error: Exception) -> str:
    message = str(error).lower()
    patterns = (
        ("profile_in_use", ("processsingleton", "singletonlock", "profile appears to be in use", "user data directory is already in use")),
        ("sandbox_unavailable", ("no usable sandbox", "failed to move to new namespace", "failed to unshare", "operation not permitted", "chroot:", "running as root without --no-sandbox")),
        ("display_unavailable", ("missing x server", "cannot open display", "unable to open x display", "$display")),
        ("permission_denied", ("permission denied", "access denied", "chrome_crashpad_handler: --database is required")),
        ("browser_missing", ("executable doesn't exist", "executable does not exist", "distribution 'chrome' is not found")),
    )
    return next((reason for reason, matches in patterns if any(match in message for match in matches)), "browser_exited")


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
