from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(part.strip() for part in env(name, default).split(",") if part.strip())


def whole_seconds(raw: str) -> int:
    return int(raw) if raw.isdigit() and len(raw) <= 9 else 0


def env_flag(name: str) -> bool:
    return env(name) == "1"


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
    oidc_device_groups: tuple[str, ...]
    allow_no_auth: bool
    demo: bool
    immich_url: str
    immich_admin_key: str
    browser_token: str
    vnc_url: str
    timezone: str
    upload_read_timeout: int


def load() -> Settings:
    return Settings(
        host=env("DARKROOM_HOST", "127.0.0.1"),
        port=int(env("DARKROOM_PORT", "8000")),
        photos_root=Path(env("DARKROOM_PHOTOS_ROOT", "/photos")),
        data_dir=Path(env("DARKROOM_DATA_DIR", "/data")),
        public_url=env("DARKROOM_PUBLIC_URL").rstrip("/"),
        secret_key=env("DARKROOM_SECRET_KEY"),
        database_url=env("DATABASE_URL"),
        oidc_issuer=env("OIDC_ISSUER").rstrip("/"),
        oidc_client_id=env("OIDC_CLIENT_ID"),
        oidc_client_secret=env("OIDC_CLIENT_SECRET"),
        oidc_allowed_groups=env_list("OIDC_ALLOWED_GROUPS", "parents"),
        oidc_allowed_emails=env_list("OIDC_ALLOWED_EMAILS"),
        oidc_device_groups=env_list("OIDC_DEVICE_GROUPS", "family,parents"),
        allow_no_auth=env_flag("DARKROOM_ALLOW_NO_AUTH"),
        demo=env_flag("DARKROOM_DEMO"),
        immich_url=env("IMMICH_URL").rstrip("/"),
        immich_admin_key=env("IMMICH_ADMIN_KEY"),
        browser_token=env("DARKROOM_BROWSER_TOKEN"),
        vnc_url=env("VNC_URL"),
        timezone=env("TZ", "UTC"),
        upload_read_timeout=whole_seconds(env("UPLOAD_READ_TIMEOUT", "3600")),
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
    if not settings.secret_key:
        found.append("DARKROOM_SECRET_KEY is not set")
    if not settings.public_url:
        found.append("DARKROOM_PUBLIC_URL is not set")
    if bool(settings.oidc_issuer) != bool(settings.oidc_client_id):
        found.append("OIDC_ISSUER and OIDC_CLIENT_ID go together; set both or neither")
    return found + auth_problems(settings) + browser_token_problems(settings.browser_token) + upload_timeout_problems(settings)


def upload_timeout_problems(settings: Settings) -> list[str]:
    if settings.upload_read_timeout >= 1:
        return []
    return ["UPLOAD_READ_TIMEOUT must be a whole number of seconds, at least 1 (the default is 3600)"]


def refuses_to_start(settings: Settings) -> list[str]:
    return misused(settings) + upload_timeout_problems(settings)
