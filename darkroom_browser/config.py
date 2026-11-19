from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from darkroom_browser.context import Stagger

MIN_TOKEN = 32
STAGGER_MINUTES = (1, 120)
STAGGER_PARTS = (2, 4)
DEFAULT_STAGGER_MINUTES = "10"
DEFAULT_STAGGER_PARTS = "2"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(env("TZ", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def read_handed(fd: int) -> str:
    try:
        with os.fdopen(fd, "rb") as pipe:
            return pipe.read().decode("ascii", "replace").strip()
    except OSError:
        return ""


def browser_token() -> str:
    raw = env("DARKROOM_BROWSER_TOKEN_FD")
    return read_handed(int(raw)) if raw.isascii() and raw.isdigit() and int(raw) >= 3 else ""


@dataclass(frozen=True)
class Settings:
    token: str
    photos_root: Path
    display: str
    poll_seconds: float
    worker_name: str
    browser_root: Path | None = None
    stagger: str = "0"
    stagger_minutes: str = DEFAULT_STAGGER_MINUTES
    stagger_parts: str = DEFAULT_STAGGER_PARTS


def whole_in(raw: str, bounds: tuple[int, int]) -> int | None:
    if not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if bounds[0] <= value <= bounds[1] else None


def stagger_of(settings: Settings) -> Stagger | None:
    minutes, parts = whole_in(settings.stagger_minutes, STAGGER_MINUTES), whole_in(settings.stagger_parts, STAGGER_PARTS)
    if settings.stagger != "1" or minutes is None or parts is None:
        return None
    return Stagger(minutes * 60.0, parts)


def load() -> Settings:
    return Settings(
        token=browser_token(),
        photos_root=Path(env("PHOTOS_ROOT", "/photos")),
        display=env("DISPLAY", ":99"),
        poll_seconds=float(env("POLL_SECONDS", "15")),
        worker_name=env("WORKER_NAME") or platform.node() or "browser-worker",
        browser_root=Path(env("BROWSER_ROOT")) if env("BROWSER_ROOT") else None,
        stagger=env("TAKEOUT_STAGGER") or "0",
        stagger_minutes=env("TAKEOUT_STAGGER_MINUTES") or DEFAULT_STAGGER_MINUTES,
        stagger_parts=env("TAKEOUT_STAGGER_MAX_PARTS") or DEFAULT_STAGGER_PARTS,
    )


def token_usable(token: str) -> bool:
    return len(token) >= MIN_TOKEN and token.isascii() and token.isprintable() and " " not in token


def in_the_share(browser_root: Path, photos_root: Path) -> bool:
    root, share = browser_root.resolve(), photos_root.resolve()
    return share.is_relative_to(root) or (root.is_relative_to(share) and root != share / "_browser")


def problems(settings: Settings) -> list[str]:
    found = []
    if not settings.token:
        found.append("no browser token was handed on DARKROOM_BROWSER_TOKEN_FD; the worker starts only under the one container's init")
    elif not token_usable(settings.token):
        found.append(f"the browser token must be at least {MIN_TOKEN} printable ASCII characters without spaces")
    if not settings.photos_root.is_dir():
        found.append("PHOTOS_ROOT is not a directory")
    if settings.browser_root is not None and not settings.browser_root.is_absolute():
        found.append("BROWSER_ROOT must be an absolute path")
    elif settings.browser_root is not None and in_the_share(settings.browser_root, settings.photos_root):
        found.append("BROWSER_ROOT must not be PHOTOS_ROOT, a folder inside it other than its _browser folder, or a folder that holds it")
    if settings.poll_seconds < 1:
        found.append("POLL_SECONDS must be at least 1")
    if settings.stagger not in ("0", "1"):
        found.append("TAKEOUT_STAGGER must be 0 (one Takeout part at a time) or 1 (staggered parts)")
    if whole_in(settings.stagger_minutes, STAGGER_MINUTES) is None:
        found.append(f"TAKEOUT_STAGGER_MINUTES must be a whole number of minutes from {STAGGER_MINUTES[0]} to {STAGGER_MINUTES[1]}")
    if whole_in(settings.stagger_parts, STAGGER_PARTS) is None:
        found.append(f"TAKEOUT_STAGGER_MAX_PARTS must be a whole number from {STAGGER_PARTS[0]} to {STAGGER_PARTS[1]}")
    return found
