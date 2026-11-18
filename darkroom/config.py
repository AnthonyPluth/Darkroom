from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def comma_list(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return comma_list(env(name, default))


def whole_number(raw: str) -> int:
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else 0


def env_flag(name: str) -> bool:
    return env(name) == "1"


handed_tokens: dict[int, str] = {}


def read_handed(fd: int) -> str:
    try:
        with os.fdopen(fd, "rb") as pipe:
            return pipe.read().decode("ascii", "replace").strip()
    except OSError:
        return ""


def handed_on_pipe(raw: str) -> str:
    fd = whole_number(raw)
    if fd >= 3 and fd not in handed_tokens:
        handed_tokens[fd] = read_handed(fd)
    return handed_tokens.get(fd, "")


def browser_token() -> str:
    return handed_on_pipe(env("DARKROOM_BROWSER_TOKEN_FD"))


def viewer_secret() -> str:
    raw = env("DARKROOM_VIEWER_SECRET_FD")
    return handed_on_pipe(raw) if raw else ""


def syncthing_key() -> str:
    raw = env("DARKROOM_SYNCTHING_KEY_FD")
    return handed_on_pipe(raw) if raw else ""


KeyOrigin = Literal["environment", "file", "none"]
VIEWER_FILES = Path("/usr/share/novnc")
VIEWER_SOCKET = Path("/run/darkroom/viewer.sock")
VIEWER_USER = "darkroom"
SYNCTHING_SOCKET = Path("/run/darkroom/syncthing.sock")
WORKER_SOCKET = Path("/run/darkroom/worker-api.sock")
StoredKind = Literal["text", "address", "list", "secret", "sealed"]


@dataclass(frozen=True)
class Storable:
    variable: str
    field: str
    kind: StoredKind = "text"

    @property
    def secret(self) -> bool:
        return self.kind in ("secret", "sealed")


STORABLE = (
    Storable("DARKROOM_PUBLIC_URL", "public_url", "address"),
    Storable("OIDC_ISSUER", "oidc_issuer", "address"),
    Storable("OIDC_CLIENT_ID", "oidc_client_id"),
    Storable("OIDC_CLIENT_SECRET", "oidc_client_secret", "secret"),
    Storable("OIDC_ALLOWED_GROUPS", "oidc_allowed_groups", "list"),
    Storable("OIDC_ALLOWED_EMAILS", "oidc_allowed_emails", "list"),
    Storable("IMMICH_URL", "immich_url", "address"),
    Storable("RESEND_API_KEY", "resend_api_key", "sealed"),
    Storable("ALERT_EMAIL_FROM", "alert_email_from"),
    Storable("ALERT_EMAIL_TO", "alert_email_to"),
)


BUILD_FILE = Path(__file__).resolve().parent / "build-info"
BUILD_FILE_LIMIT = 256
RELEASE_VERSION = re.compile(r"v[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")
SHORT_COMMIT = 7
DEV = "dev"


@dataclass(frozen=True)
class Build:
    version: str = DEV
    commit: str = ""

    @property
    def shown(self) -> str:
        return f"{self.version} ({self.commit})" if self.commit else self.version


def build_of(text: str) -> Build:
    lines = text.splitlines()
    if len(lines) > 2:
        return Build()
    version, commit = ([line.strip() for line in lines] + ["", ""])[:2]
    return Build(version if RELEASE_VERSION.fullmatch(version) else DEV, commit[:SHORT_COMMIT] if COMMIT_HASH.fullmatch(commit) else "")


def read_build(path: Path | None = None) -> Build:
    try:
        with (path or BUILD_FILE).open("rb") as opened:
            raw = opened.read(BUILD_FILE_LIMIT + 1)
        return Build() if len(raw) > BUILD_FILE_LIMIT else build_of(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError):
        return Build()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    photos_root: Path
    data_dir: Path
    public_url: str
    secret_key: str
    database_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_allowed_groups: tuple[str, ...]
    oidc_allowed_emails: tuple[str, ...]
    allow_no_auth: bool
    demo: bool
    immich_url: str
    immich_admin_key: str
    browser_token: str
    timezone: str
    pixel_folder_cap_gb: int = 50
    resend_api_key: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""
    allowed_hosts: tuple[str, ...] = ()
    key_origin: KeyOrigin = "none"
    viewer_secret: str = ""
    one_container: bool = False
    viewer_files: Path = VIEWER_FILES
    from_environment: frozenset[str] = frozenset()
    wizard: bool = False
    blank_in_environment: frozenset[str] = frozenset()
    browser_root: Path | None = None
    syncthing: bool = False
    syncthing_key: str = field(default="", repr=False)
    alerts_off: tuple[str, ...] = ()
    build: Build = Build()


SECRET_KEY_FILE = "secret-key"
DEFAULT_ALLOWED_GROUPS = "parents"


def secret_key_file(data_dir: Path) -> Path:
    return data_dir / SECRET_KEY_FILE


def stored_secret_key(data_dir: Path) -> str:
    try:
        return secret_key_file(data_dir).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def secret_key_of(data_dir: Path) -> tuple[str, KeyOrigin]:
    given = env("DARKROOM_SECRET_KEY")
    if given:
        return given, "environment"
    stored = stored_secret_key(data_dir)
    return stored, "file" if stored else "none"


def load() -> Settings:
    data_dir = Path(env("DARKROOM_DATA_DIR", "/data"))
    secret_key, key_origin = secret_key_of(data_dir)
    return Settings(
        host=env("DARKROOM_HOST", "127.0.0.1"),
        port=int(env("DARKROOM_PORT", "8000")),
        photos_root=Path(env("DARKROOM_PHOTOS_ROOT", "/photos")),
        data_dir=data_dir,
        public_url=env("DARKROOM_PUBLIC_URL").rstrip("/"),
        secret_key=secret_key,
        database_url=env("DATABASE_URL"),
        oidc_issuer=env("OIDC_ISSUER").rstrip("/"),
        oidc_client_id=env("OIDC_CLIENT_ID"),
        oidc_client_secret=env("OIDC_CLIENT_SECRET"),
        oidc_allowed_groups=env_list("OIDC_ALLOWED_GROUPS", DEFAULT_ALLOWED_GROUPS),
        oidc_allowed_emails=env_list("OIDC_ALLOWED_EMAILS"),
        allow_no_auth=env_flag("DARKROOM_ALLOW_NO_AUTH"),
        demo=env_flag("DARKROOM_DEMO"),
        immich_url=env("IMMICH_URL").rstrip("/"),
        immich_admin_key=env("IMMICH_ADMIN_KEY"),
        browser_token=browser_token(),
        timezone=env("TZ", "UTC"),
        pixel_folder_cap_gb=whole_number(env("PIXEL_FOLDER_CAP_GB", "50")),
        resend_api_key=env("RESEND_API_KEY"),
        alert_email_from=env("ALERT_EMAIL_FROM"),
        alert_email_to=env("ALERT_EMAIL_TO"),
        allowed_hosts=env_list("DARKROOM_ALLOWED_HOSTS"),
        key_origin=key_origin,
        viewer_secret=viewer_secret(),
        one_container=env_flag("DARKROOM_ONE_CONTAINER"),
        from_environment=frozenset(entry.variable for entry in STORABLE if env(entry.variable)),
        wizard=env_flag("DARKROOM_WIZARD"),
        blank_in_environment=frozenset(entry.variable for entry in STORABLE if entry.variable in os.environ and not env(entry.variable)),
        browser_root=Path(env("DARKROOM_BROWSER_ROOT")) if env("DARKROOM_BROWSER_ROOT") else None,
        syncthing=env_flag("DARKROOM_SYNCTHING"),
        syncthing_key=syncthing_key(),
        alerts_off=env_list("DARKROOM_ALERTS_OFF"),
        build=read_build(),
    )


def groups_without_a_value(settings: Settings) -> tuple[str, ...]:
    return () if "OIDC_ALLOWED_GROUPS" in settings.blank_in_environment else comma_list(DEFAULT_ALLOWED_GROUPS)


def local_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or "." not in host:
            return True
        return host.endswith((".local", ".lan", ".home.arpa", ".internal", ".ts.net"))
    if address.is_unspecified or address.is_multicast:
        return False
    return address.is_loopback or address.is_link_local or address.is_private or address in ipaddress.ip_network("100.64.0.0/10")


def loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


MIN_BROWSER_TOKEN = 32


def browser_token_usable(token: str) -> bool:
    return len(token) >= MIN_BROWSER_TOKEN and token.isascii() and token.isprintable() and " " not in token


def oidc_configured(settings: Settings) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


NO_AUTH_WITH_DEMO = "DARKROOM_ALLOW_NO_AUTH=1 cannot be combined with DARKROOM_DEMO=1; unset one of them"


def no_auth_misused(settings: Settings) -> str | None:
    if not settings.allow_no_auth:
        return None
    if oidc_configured(settings):
        return "DARKROOM_ALLOW_NO_AUTH=1 cannot be combined with OIDC sign-in; unset one of them"
    if settings.demo:
        return NO_AUTH_WITH_DEMO
    if not local_host(settings.host):
        return f"DARKROOM_ALLOW_NO_AUTH=1 only works when DARKROOM_HOST is a local address, not {settings.host}"
    return None


def no_auth_allowed(settings: Settings) -> bool:
    return settings.allow_no_auth and no_auth_misused(settings) is None


def ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


NOT_IN_A_HOST = frozenset("@/?#\\")
HOST_NAME = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")


def host_name(value: str) -> str:
    value = value.strip()
    if not value or any(character in NOT_IN_A_HOST or character.isspace() for character in value):
        return ""
    if ip_literal(value.strip("[]")):
        return value.strip("[]").lower()
    try:
        name = (urlsplit("//" + value).hostname or "").rstrip(".")
    except ValueError:
        return ""
    return name if ip_literal(name) or HOST_NAME.fullmatch(name) else ""


def public_host(settings: Settings) -> str:
    try:
        return urlsplit(settings.public_url).hostname or ""
    except ValueError:
        return ""


def no_auth_hosts(settings: Settings) -> frozenset[str]:
    named = ("localhost", settings.host, public_host(settings), *settings.allowed_hosts)
    return frozenset(name for name in (host_name(value) for value in named) if name)


def no_auth_host_allowed(settings: Settings, host: str) -> bool:
    name = host_name(host)
    return bool(name) and (ip_literal(name) or name in no_auth_hosts(settings))


def allowed_hosts_problems(settings: Settings) -> list[str]:
    unusable = [value for value in settings.allowed_hosts if not host_name(value)]
    if not unusable:
        return []
    return ["DARKROOM_ALLOWED_HOSTS is a comma-separated list of host names, like tower.local, without a scheme, path or wildcard"]


def demo_misused(settings: Settings) -> str | None:
    if not settings.demo:
        return None
    if oidc_configured(settings):
        return "DARKROOM_DEMO=1 cannot be combined with OIDC sign-in; the demo runs without a provider"
    if settings.allow_no_auth:
        return NO_AUTH_WITH_DEMO
    if not loopback_host(settings.host):
        return f"DARKROOM_DEMO=1 only works when DARKROOM_HOST is a loopback address (127.0.0.1 or ::1), not {settings.host}"
    return None


def demo_allowed(settings: Settings) -> bool:
    return settings.demo and demo_misused(settings) is None


def misused(settings: Settings) -> list[str]:
    return list(dict.fromkeys(line for line in (no_auth_misused(settings), demo_misused(settings)) if line))


def auth_problems(settings: Settings) -> list[str]:
    found = misused(settings)
    if oidc_configured(settings):
        if not settings.public_url.startswith(("http://", "https://")):
            found.append("DARKROOM_PUBLIC_URL must start with http:// or https:// so the sign-in can come back to it")
        elif settings.public_url.startswith("http://") and not local_host(urlsplit(settings.public_url).hostname or ""):
            found.append("DARKROOM_PUBLIC_URL must use https:// for an address reachable beyond the LAN")
        if not settings.oidc_issuer.startswith("https://") and not local_host(urlsplit(settings.oidc_issuer).hostname or ""):
            found.append("OIDC_ISSUER must use https:// unless the provider is on the LAN")
        if not (settings.oidc_allowed_groups or settings.oidc_allowed_emails):
            found.append("set OIDC_ALLOWED_GROUPS or OIDC_ALLOWED_EMAILS so someone can sign in")
    elif not (settings.allow_no_auth or settings.demo):
        found.append("sign-in is not set up: set OIDC_ISSUER and OIDC_CLIENT_ID (or DARKROOM_ALLOW_NO_AUTH=1 on a local address) so someone can use the web UI")
    return found


def browser_root_in_the_share(browser_root: Path, photos_root: Path) -> bool:
    root, share = browser_root.resolve(), photos_root.resolve()
    return share.is_relative_to(root) or (root.is_relative_to(share) and root != share / "_browser")


def browser_root_problems(settings: Settings) -> list[str]:
    if settings.browser_root is None:
        return []
    if not settings.browser_root.is_absolute():
        return [f"DARKROOM_BROWSER_ROOT must be an absolute path: {settings.browser_root}"]
    if browser_root_in_the_share(settings.browser_root, settings.photos_root):
        return ["DARKROOM_BROWSER_ROOT must not be DARKROOM_PHOTOS_ROOT, a folder inside it other than its _browser folder, or a folder that holds it"]
    return []


def problems(settings: Settings) -> list[str]:
    found = []
    if not settings.photos_root.is_dir():
        found.append(f"DARKROOM_PHOTOS_ROOT is not a directory: {settings.photos_root}")
    if not settings.data_dir.is_dir():
        found.append(f"DARKROOM_DATA_DIR is not a directory: {settings.data_dir}")
    found += browser_root_problems(settings)
    if not settings.public_url:
        found.append("DARKROOM_PUBLIC_URL is not set")
    elif port_problem(settings.public_url):
        found.append("DARKROOM_PUBLIC_URL has a port that is not a number from 0 to 65535")
    if bool(settings.oidc_issuer) != bool(settings.oidc_client_id):
        found.append("OIDC_ISSUER and OIDC_CLIENT_ID go together; set both or neither")
    return (found + auth_problems(settings) + pixel_cap_problems(settings) + resend_problems(settings) + allowed_hosts_problems(settings) + syncthing_problems(settings)
            + alerts_off_problems(settings))


SWITCHABLE_ALERTS = ("syncthing_down", "syncthing_settings_failed", "syncthing_version", "share_check_failed", "pixel_held")


def alerts_off_problems(settings: Settings) -> list[str]:
    unknown = [kind for kind in settings.alerts_off if kind not in SWITCHABLE_ALERTS]
    if not unknown:
        return []
    return [f"DARKROOM_ALERTS_OFF names {len(unknown)} unknown alert kind{'' if len(unknown) == 1 else 's'}; it takes only {', '.join(SWITCHABLE_ALERTS)}"]


SYNCTHING_TWO_CONTAINERS = ("DARKROOM_SYNCTHING=1 does nothing outside the one container: only the one container's init starts Darkroom's Syncthing, "
                            "so unset it here (docs/deploy/one-container.md)")


def syncthing_problems(settings: Settings) -> list[str]:
    return [SYNCTHING_TWO_CONTAINERS] if settings.syncthing and not settings.one_container else []


def port_problem(address: str) -> bool:
    try:
        port = urlsplit(address).port
    except ValueError:
        return True
    return port is not None and not 0 <= port <= 65535


def usable_viewer_url(vnc_url: str) -> str | None:
    try:
        parts = urlsplit(vnc_url)
    except ValueError:
        return None
    if port_problem(vnc_url):
        return None
    if parts.scheme in ("http", "https") and parts.netloc and not parts.username and not parts.password:
        return vnc_url
    return None


def relay_viewer_url(settings: Settings) -> str | None:
    if not browser_token_usable(settings.viewer_secret) or usable_viewer_url(settings.public_url) is None:
        return None
    try:
        return relay_address(settings.public_url)
    except ValueError:
        return None


def relay_address(public_url: str) -> str:
    parts = urlsplit(public_url)
    prefix = parts.path.strip("/")
    socket_path = f"{prefix}/viewer/websockify" if prefix else "viewer/websockify"
    host = parts.hostname or ""
    shown = f"[{host}]" if ":" in host else host
    secure = parts.scheme == "https"
    port = parts.port or (443 if secure else 80)
    return (f"{public_url}/viewer/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=3000"
            f"&host={shown}&port={port}&encrypt={int(secure)}&path={socket_path}")


def viewer_link(settings: Settings) -> str | None:
    return relay_viewer_url(settings)


SEALED_PREFIX = "enc:v1:"


ADDRESS = re.compile(r"[^@\s<>,;\"()\[\]\\]+@[^@\s<>,;\"()\[\]\\]+\.[^@\s<>,;\"()\[\]\\]+")


REMOVED_ALERT_VARIABLES = ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN")
IGNORED_VARIABLES_NOTE = "NTFY_URL, NTFY_TOPIC or NTFY_TOKEN is set and ignored: alerts are emailed now, so set RESEND_API_KEY, ALERT_EMAIL_FROM and ALERT_EMAIL_TO (docs/deploy/alerts-email.md) and remove the NTFY_* lines"


def ignored_alert_variables() -> bool:
    return any(env(name) for name in REMOVED_ALERT_VARIABLES)


REMOVED_BROWSER_VARIABLES = ("DARKROOM_BROWSER_TOKEN", "DARKROOM_URL", "VNC_URL", "DARKROOM_VNC_SECRET")


def ignored_browser_variables() -> tuple[str, ...]:
    return tuple(name for name in REMOVED_BROWSER_VARIABLES if env(name))


def ignored_browser_note(names: tuple[str, ...]) -> str:
    return (f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} set and ignored: the separate browser container is gone, and the one container generates "
            "the browser token and serves the remote browser view itself, so remove these lines from .env (docs/deploy/one-container.md)")


def resend_configured(settings: Settings) -> bool:
    return bool(settings.resend_api_key or settings.alert_email_from or settings.alert_email_to)


def resend_problems(settings: Settings) -> list[str]:
    if not resend_configured(settings):
        return []
    if not (settings.resend_api_key and settings.alert_email_from and settings.alert_email_to):
        return ["RESEND_API_KEY, ALERT_EMAIL_FROM and ALERT_EMAIL_TO go together; set all three, or none so alerts stay on the Status page"]
    found = []
    if not settings.resend_api_key.startswith(SEALED_PREFIX):
        found.append("RESEND_API_KEY must be sealed: run `python run.py seal-secret` and paste what it prints")
    for name, value in (("ALERT_EMAIL_FROM", settings.alert_email_from), ("ALERT_EMAIL_TO", settings.alert_email_to)):
        if not ADDRESS.fullmatch(value):
            found.append(f"{name} must be one email address, like alerts@example.com")
    return found


def pixel_cap_problems(settings: Settings) -> list[str]:
    if settings.pixel_folder_cap_gb >= 1:
        return []
    return ["PIXEL_FOLDER_CAP_GB must be a whole number of gigabytes, at least 1 (the default is 50)"]


def refuses_to_start(settings: Settings) -> list[str]:
    return misused(settings) + pixel_cap_problems(settings) + browser_root_problems(settings)
