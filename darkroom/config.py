from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(part.strip() for part in env(name, default).split(",") if part.strip())


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
    ha_url: str
    ha_token: str
    ha_notify_service: str
    immich_url: str
    immich_admin_key: str
    browser_token: str
    vnc_url: str
    timezone: str


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
        ha_url=env("HA_URL").rstrip("/"),
        ha_token=env("HA_TOKEN"),
        ha_notify_service=env("HA_NOTIFY_SERVICE"),
        immich_url=env("IMMICH_URL").rstrip("/"),
        immich_admin_key=env("IMMICH_ADMIN_KEY"),
        browser_token=env("DARKROOM_BROWSER_TOKEN"),
        vnc_url=env("VNC_URL"),
        timezone=env("TZ", "UTC"),
    )


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
    if 0 < sum(bool(value) for value in (settings.ha_url, settings.ha_token, settings.ha_notify_service)) < 3:
        found.append("HA_URL, HA_TOKEN and HA_NOTIFY_SERVICE go together; set all three or none")
    return found
