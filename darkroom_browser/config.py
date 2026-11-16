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
    return read_handed(int(raw)) if raw.isascii() and raw.isdigit() and int(raw) >= 3 else ""


def app_port() -> int:
    raw = env("DARKROOM_PORT", "8000")
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 5 else 0


@dataclass(frozen=True)
class Settings:
    app_port: int
    token: str
    photos_root: Path
    display: str
    poll_seconds: float
    worker_name: str
    browser_root: Path | None = None


def load() -> Settings:
    return Settings(
        app_port=app_port(),
        token=browser_token(),
        photos_root=Path(env("PHOTOS_ROOT", "/photos")),
        display=env("DISPLAY", ":99"),
        poll_seconds=float(env("POLL_SECONDS", "15")),
        worker_name=env("WORKER_NAME") or platform.node() or "browser-worker",
        browser_root=Path(env("BROWSER_ROOT")) if env("BROWSER_ROOT") else None,
    )


def darkroom_url(settings: Settings) -> str:
    return f"http://127.0.0.1:{settings.app_port}"


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
    if not 1 <= settings.app_port <= 65535:
        found.append("DARKROOM_PORT must be the app's port, a number from 1 to 65535")
    if not settings.photos_root.is_dir():
        found.append("PHOTOS_ROOT is not a directory")
    if settings.browser_root is not None and not settings.browser_root.is_absolute():
        found.append("BROWSER_ROOT must be an absolute path")
    elif settings.browser_root is not None and in_the_share(settings.browser_root, settings.photos_root):
        found.append("BROWSER_ROOT must not be PHOTOS_ROOT, a folder inside it other than its _browser folder, or a folder that holds it")
    if settings.poll_seconds < 1:
        found.append("POLL_SECONDS must be at least 1")
    return found
