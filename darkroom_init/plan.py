from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Secret = Literal["token", "viewer"]
PIPE: dict[Secret, str] = {"token": "{token_fd}", "viewer": "{viewer_fd}"}
TOKEN_FD = "DARKROOM_BROWSER_TOKEN_FD"
VIEWER_FD = "DARKROOM_VIEWER_SECRET_FD"
ONE_CONTAINER = "DARKROOM_ONE_CONTAINER"
VIEWER_PLUGIN = "darkroom_init.viewer_auth.SecretOnPipe"
INIT_HOME = str(Path(__file__).resolve().parent.parent)

APP_SETTINGS = (
    "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO", "DARKROOM_ALLOWED_HOSTS", "DARKROOM_ALLOW_NO_AUTH", "DARKROOM_DATA_DIR", "DARKROOM_DEMO", "DARKROOM_HOST",
    "DARKROOM_PHOTOS_ROOT", "DARKROOM_PORT", "DARKROOM_PUBLIC_URL", "DARKROOM_SECRET_KEY", "DARKROOM_WIZARD", "DATABASE_URL", "IMMICH_ADMIN_KEY", "IMMICH_URL",
    "OIDC_ALLOWED_EMAILS", "OIDC_ALLOWED_GROUPS", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER", "PIXEL_FOLDER_CAP_GB", "RESEND_API_KEY", "TZ", "VNC_URL",
)
WORKER_SETTINGS = ("BROWSER_CHANNEL", "POLL_SECONDS", "TZ", "WORKER_NAME", "PLAYWRIGHT_BROWSERS_PATH")
COMMON = ("HOME", "LANG", "LC_ALL")
PYTHON = {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class Spec:
    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str = "/"
    secrets: tuple[Secret, ...] = ()
    quiet: bool = False
    strays: bool = False
    ready: Callable[[], bool] | None = field(default=None, compare=False)


def picked(given: Mapping[str, str], names: tuple[str, ...]) -> dict[str, str]:
    return {name: given[name] for name in names if name in given}


def app_env(given: Mapping[str, str]) -> dict[str, str]:
    return picked(given, COMMON + APP_SETTINGS) | PYTHON | {"PATH": f"/app/.venv/bin:{SYSTEM_PATH}", TOKEN_FD: PIPE["token"], VIEWER_FD: PIPE["viewer"], ONE_CONTAINER: "1"}


def worker_env(given: Mapping[str, str]) -> dict[str, str]:
    fixed = {
        "PATH": f"/opt/worker/bin:{SYSTEM_PATH}",
        "DARKROOM_URL": f"http://127.0.0.1:{given.get('DARKROOM_PORT', '8000')}",
        "PHOTOS_ROOT": given.get("DARKROOM_PHOTOS_ROOT", "/photos"),
        "DISPLAY": given.get("DISPLAY", ":99"),
        TOKEN_FD: PIPE["token"],
    }
    return picked(given, COMMON + WORKER_SETTINGS) | PYTHON | fixed


def display_env(given: Mapping[str, str]) -> dict[str, str]:
    return picked(given, COMMON) | {"PATH": SYSTEM_PATH, "DISPLAY": given.get("DISPLAY", ":99")}


def specs(given: Mapping[str, str], app_ready: Callable[[], bool], display_ready: Callable[[], bool]) -> list[Spec]:
    shown = given.get("DISPLAY", ":99")
    viewer = display_env(given) | {"PYTHONPATH": INIT_HOME, "PYTHONDONTWRITEBYTECODE": "1"}
    return [
        Spec("app", ("/app/.venv/bin/python", "run.py", "serve"), app_env(given), "/app", ("token", "viewer"), ready=app_ready),
        Spec("display", ("Xvfb", shown, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-nolisten", "unix"), display_env(given), quiet=True, ready=display_ready),
        Spec("vnc", ("x11vnc", "-display", shown, "-localhost", "-rfbport", "5900", "-forever", "-shared", "-nopw", "-noxdamage", "-quiet"), display_env(given), quiet=True),
        Spec("viewer", ("websockify", "--web=/usr/share/novnc", "--web-auth", "--auth-plugin", VIEWER_PLUGIN, "--auth-source", PIPE["viewer"], "127.0.0.1:6080", "127.0.0.1:5900"),
             viewer, secrets=("viewer",), quiet=True),
        Spec("worker", ("/opt/worker/bin/python", "-m", "darkroom_browser"), worker_env(given), "/opt/worker/app", ("token",), strays=True),
    ]
