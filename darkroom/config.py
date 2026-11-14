from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(part.strip() for part in env(name, default).split(",") if part.strip())


def whole_number(raw: str) -> int:
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else 0


def env_flag(name: str) -> bool:
    return env(name) == "1"


SecretKeySource = Literal["environment", "file", "none"]


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
    vnc_url: str
    timezone: str
    pixel_folder_cap_gb: int = 50
    resend_api_key: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""
    allowed_hosts: tuple[str, ...] = ()
    secret_key_source: SecretKeySource = "none"


SECRET_KEY_FILE = "secret-key"


def secret_key_file(data_dir: Path) -> Path:
    return data_dir / SECRET_KEY_FILE


def stored_secret_key(data_dir: Path) -> str:
    try:
        return secret_key_file(data_dir).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def secret_key_of(data_dir: Path) -> tuple[str, SecretKeySource]:
    given = env("DARKROOM_SECRET_KEY")
    if given:
        return given, "environment"
    stored = stored_secret_key(data_dir)
    return stored, "file" if stored else "none"


def load() -> Settings:
    data_dir = Path(env("DARKROOM_DATA_DIR", "/data"))
    secret_key, secret_key_source = secret_key_of(data_dir)
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
        oidc_allowed_groups=env_list("OIDC_ALLOWED_GROUPS", "parents"),
        oidc_allowed_emails=env_list("OIDC_ALLOWED_EMAILS"),
        allow_no_auth=env_flag("DARKROOM_ALLOW_NO_AUTH"),
        demo=env_flag("DARKROOM_DEMO"),
        immich_url=env("IMMICH_URL").rstrip("/"),
        immich_admin_key=env("IMMICH_ADMIN_KEY"),
        browser_token=env("DARKROOM_BROWSER_TOKEN"),
        vnc_url=env("VNC_URL"),
        timezone=env("TZ", "UTC"),
        pixel_folder_cap_gb=whole_number(env("PIXEL_FOLDER_CAP_GB", "50")),
        resend_api_key=env("RESEND_API_KEY"),
        alert_email_from=env("ALERT_EMAIL_FROM"),
        alert_email_to=env("ALERT_EMAIL_TO"),
        allowed_hosts=env_list("DARKROOM_ALLOWED_HOSTS"),
        secret_key_source=secret_key_source,
    )


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


def browser_token_problems(token: str) -> list[str]:
    if not token:
        return []
    if not browser_token_usable(token):
        return [f"DARKROOM_BROWSER_TOKEN must be at least {MIN_BROWSER_TOKEN} printable ASCII characters without spaces (try `openssl rand -hex 32`)"]
    return []


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


def problems(settings: Settings) -> list[str]:
    found = []
    if not settings.photos_root.is_dir():
        found.append(f"DARKROOM_PHOTOS_ROOT is not a directory: {settings.photos_root}")
    if not settings.data_dir.is_dir():
        found.append(f"DARKROOM_DATA_DIR is not a directory: {settings.data_dir}")
    if not settings.public_url:
        found.append("DARKROOM_PUBLIC_URL is not set")
    if bool(settings.oidc_issuer) != bool(settings.oidc_client_id):
        found.append("OIDC_ISSUER and OIDC_CLIENT_ID go together; set both or neither")
    return found + auth_problems(settings) + browser_token_problems(settings.browser_token) + pixel_cap_problems(settings) + vnc_url_problems(settings) + resend_problems(settings) + allowed_hosts_problems(settings)


def usable_viewer_url(vnc_url: str) -> str | None:
    try:
        parts = urlsplit(vnc_url)
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc and not parts.username and not parts.password:
        return vnc_url
    return None


def vnc_url_problems(settings: Settings) -> list[str]:
    if not settings.vnc_url or usable_viewer_url(settings.vnc_url) is not None:
        return []
    return ["VNC_URL is not usable: it must be an http:// or https:// address without a username or password, so the Remote browser page shows no viewer"]


SEALED_PREFIX = "enc:v1:"


ADDRESS = re.compile(r"[^@\s<>,;\"()\[\]\\]+@[^@\s<>,;\"()\[\]\\]+\.[^@\s<>,;\"()\[\]\\]+")


REMOVED_ALERT_VARIABLES = ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN")
IGNORED_VARIABLES_NOTE = "NTFY_URL, NTFY_TOPIC or NTFY_TOKEN is set and ignored: alerts are emailed now, so set RESEND_API_KEY, ALERT_EMAIL_FROM and ALERT_EMAIL_TO (docs/deploy/alerts-email.md) and remove the NTFY_* lines"


def ignored_alert_variables() -> bool:
    return any(env(name) for name in REMOVED_ALERT_VARIABLES)


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
    return misused(settings) + pixel_cap_problems(settings)
