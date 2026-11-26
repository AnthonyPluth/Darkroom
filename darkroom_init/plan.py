from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Secret = Literal["token", "viewer", "syncthing"]
PIPE: dict[Secret, str] = {"token": "{token_fd}", "viewer": "{viewer_fd}", "syncthing": "{syncthing_fd}"}
TOKEN_FD = "DARKROOM_BROWSER_TOKEN_FD"
VIEWER_FD = "DARKROOM_VIEWER_SECRET_FD"
SYNCTHING_KEY_FD = "DARKROOM_SYNCTHING_KEY_FD"
ONE_CONTAINER = "DARKROOM_ONE_CONTAINER"
SYNCTHING_SWITCH = "DARKROOM_SYNCTHING"
SYNCTHING_SWITCH_IGNORED = ("DARKROOM_SYNCTHING is no longer supported and is ignored: Darkroom's Syncthing is always on in the one container, so remove it "
                            "(docs/deploy/one-container.md)")
SYNCTHING_MODULE = "darkroom_init.syncthing"
FD_VARIABLES = frozenset({TOKEN_FD, VIEWER_FD, SYNCTHING_KEY_FD})
VIEWER_PLUGIN = "darkroom_init.viewer_auth.SecretOnPipe"
SOCKETS_MODULE = "darkroom_init.sockets"
INIT_HOME = str(Path(__file__).resolve().parent.parent)
SOCKET_FOLDER = Path("/run/darkroom")
VNC_SOCKET = SOCKET_FOLDER / "vnc.sock"
VIEWER_SOCKET = SOCKET_FOLDER / "viewer.sock"
SYNCTHING_SOCKET = SOCKET_FOLDER / "syncthing.sock"
WORKER_SOCKET = SOCKET_FOLDER / "worker-api.sock"
X11VNC = "/usr/bin/x11vnc"
WEBSOCKIFY = "/usr/bin/websockify"

APP_SETTINGS = (
    "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO", "DARKROOM_ALERTS_OFF", "DARKROOM_ALLOWED_HOSTS", "DARKROOM_ALLOW_NO_AUTH", "DARKROOM_BROWSER_ROOT", "DARKROOM_DATA_DIR", "DARKROOM_DEMO", "DARKROOM_HOST",
    "DARKROOM_IMPORT_STALL_MINUTES", "DARKROOM_MANAGED_RELEASE",
    "DARKROOM_PHOTOS_ROOT", "DARKROOM_PIXEL_IMMICH_SOURCE", "DARKROOM_PORT", "DARKROOM_PUBLIC_URL", "DARKROOM_SECRET_KEY", "DARKROOM_WIZARD", "DATABASE_URL", "IMMICH_ADMIN_KEY", "IMMICH_URL",
    "NTFY_TOKEN", "NTFY_TOPIC", "NTFY_URL", "OIDC_ALLOWED_EMAILS", "OIDC_ALLOWED_GROUPS", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ISSUER", "PIXEL_FOLDER_CAP_GB", "RESEND_API_KEY", "TZ",
)
REMOVED_SETTINGS = ("DARKROOM_BROWSER_TOKEN", "DARKROOM_URL", "DARKROOM_VNC_SECRET", "VNC_URL")
WORKER_SETTINGS = ("BROWSER_CHANNEL", "BROWSER_WORKERS", "POLL_SECONDS", "TZ", "WORKER_NAME", "PLAYWRIGHT_BROWSERS_PATH")
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
    recheck: bool = False


def picked(given: Mapping[str, str], names: tuple[str, ...]) -> dict[str, str]:
    return {name: given[name] for name in names if name in given}


def syncthing_switch_set_off(given: Mapping[str, str]) -> bool:
    value = given.get(SYNCTHING_SWITCH, "").strip()
    return bool(value) and value != "1"


def syncthing_home(given: Mapping[str, str]) -> str:
    return f"{(given.get('DARKROOM_DATA_DIR') or '/data').rstrip('/')}/syncthing"


def removed_and_set(given: Mapping[str, str]) -> dict[str, str]:
    return {name: "set" for name in REMOVED_SETTINGS if given.get(name, "").strip()}


def app_env(given: Mapping[str, str]) -> dict[str, str]:
    handed = {TOKEN_FD: PIPE["token"], VIEWER_FD: PIPE["viewer"], SYNCTHING_KEY_FD: PIPE["syncthing"]}
    fixed = {"PATH": f"/app/.venv/bin:{SYSTEM_PATH}", ONE_CONTAINER: "1"}
    return picked(given, COMMON + APP_SETTINGS) | removed_and_set(given) | PYTHON | fixed | handed


def worker_env(given: Mapping[str, str]) -> dict[str, str]:
    photos = given.get("DARKROOM_PHOTOS_ROOT", "/photos")
    fixed = {
        "PATH": f"/opt/worker/bin:{SYSTEM_PATH}",
        "PHOTOS_ROOT": photos,
        "BROWSER_ROOT": given.get("DARKROOM_BROWSER_ROOT") or f"{photos.rstrip('/')}/_browser",
        "DISPLAY": given.get("DISPLAY", ":99"),
        TOKEN_FD: PIPE["token"],
    }
    return picked(given, COMMON + WORKER_SETTINGS) | PYTHON | fixed


def display_env(given: Mapping[str, str]) -> dict[str, str]:
    return picked(given, COMMON) | {"PATH": SYSTEM_PATH, "DISPLAY": given.get("DISPLAY", ":99")}


def syncthing_env(given: Mapping[str, str]) -> dict[str, str]:
    return picked(given, (*COMMON, "TZ")) | {"PATH": SYSTEM_PATH, "PYTHONPATH": INIT_HOME, "PYTHONDONTWRITEBYTECODE": "1", "STNOUPGRADE": "1", "STNODEFAULTFOLDER": "1"}


def syncthing_spec(given: Mapping[str, str], ready: Callable[[], bool] | None) -> Spec:
    return Spec("syncthing", ("python3", "-P", "-m", SYNCTHING_MODULE, syncthing_home(given)), syncthing_env(given), ready=ready, recheck=True)


def on_private_socket(socket_path: Path, argv: tuple[str, ...]) -> tuple[str, ...]:
    return ("python3", "-P", "-m", SOCKETS_MODULE, str(socket_path), *argv)


def specs(given: Mapping[str, str], app_ready: Callable[[], bool], display_ready: Callable[[], bool], syncthing_ready: Callable[[], bool] | None = None) -> list[Spec]:
    shown = given.get("DISPLAY", ":99")
    wrapped = display_env(given) | {"PYTHONPATH": INIT_HOME, "PYTHONDONTWRITEBYTECODE": "1"}
    vnc = (X11VNC, "-display", shown, "-unixsock", str(VNC_SOCKET), "-rfbport", "0", "-rfbportv6", "0", "-forever", "-shared", "-nopw", "-noxdamage", "-quiet")
    viewer = (WEBSOCKIFY, "--web=/usr/share/novnc", "--web-auth", "--auth-plugin", VIEWER_PLUGIN, "--auth-source", PIPE["viewer"], "--unix-listen", str(VIEWER_SOCKET),
              "--unix-target", str(VNC_SOCKET))
    return [
        syncthing_spec(given, syncthing_ready),
        Spec("app", ("/app/.venv/bin/python", "run.py", "serve"), app_env(given), "/app", ("token", "viewer", "syncthing"), ready=app_ready),
        Spec("display", ("Xvfb", shown, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-nolisten", "local"), display_env(given), quiet=True, ready=display_ready),
        Spec("vnc", on_private_socket(VNC_SOCKET, vnc), wrapped),
        Spec("viewer", on_private_socket(VIEWER_SOCKET, viewer), wrapped, secrets=("viewer",)),
        Spec("worker", ("/opt/worker/bin/python", "-m", "darkroom_browser"), worker_env(given), "/opt/worker/app", ("token",), strays=True),
    ]
