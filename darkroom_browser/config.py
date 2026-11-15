from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

MIN_TOKEN = 32


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def read_handed(fd: int) -> str:
    try:
        with os.fdopen(fd, "rb") as pipe:
            return pipe.read().decode("ascii", "replace").strip()
    except OSError:
        return ""


def browser_token() -> str:
    raw = env("DARKROOM_BROWSER_TOKEN_FD")
    if not raw:
        return env("DARKROOM_BROWSER_TOKEN")
    return read_handed(int(raw)) if raw.isascii() and raw.isdigit() and int(raw) >= 3 else ""


@dataclass(frozen=True)
class Settings:
    darkroom_url: str
    token: str
    photos_root: Path
    display: str
    poll_seconds: float
    worker_name: str
    browser_root: Path | None = None


def load() -> Settings:
    return Settings(
        darkroom_url=env("DARKROOM_URL", "http://darkroom:8000").rstrip("/"),
        token=browser_token(),
        photos_root=Path(env("PHOTOS_ROOT", "/photos")),
        display=env("DISPLAY", ":99"),
        poll_seconds=float(env("POLL_SECONDS", "15")),
        worker_name=env("WORKER_NAME") or platform.node() or "browser-worker",
        browser_root=Path(env("BROWSER_ROOT")) if env("BROWSER_ROOT") else None,
    )


def token_usable(token: str) -> bool:
    return len(token) >= MIN_TOKEN and token.isascii() and token.isprintable() and " " not in token


def in_the_share(browser_root: Path, photos_root: Path) -> bool:
    root, share = browser_root.resolve(), photos_root.resolve()
    return root.is_relative_to(share) and root != share / "_browser"


def problems(settings: Settings) -> list[str]:
    found = []
    if not settings.token:
        found.append("DARKROOM_BROWSER_TOKEN is not set; the worker will not start without the shared token")
    elif not token_usable(settings.token):
        found.append(f"DARKROOM_BROWSER_TOKEN must be at least {MIN_TOKEN} printable ASCII characters without spaces")
    if not settings.darkroom_url.startswith(("http://", "https://")) or len(settings.darkroom_url) <= len("https://"):
        found.append("DARKROOM_URL must be an http:// or https:// address")
    if not settings.photos_root.is_dir():
        found.append("PHOTOS_ROOT is not a directory")
    if settings.browser_root is not None and not settings.browser_root.is_absolute():
        found.append("BROWSER_ROOT must be an absolute path")
    elif settings.browser_root is not None and in_the_share(settings.browser_root, settings.photos_root):
        found.append("BROWSER_ROOT must not be inside PHOTOS_ROOT, except its _browser folder")
    if settings.poll_seconds < 1:
        found.append("POLL_SECONDS must be at least 1")
    return found
