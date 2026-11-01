from __future__ import annotations

import json
import urllib.error

from darkroom import monitoring, tls
from darkroom.config import Settings

TIMEOUT = 15.0
SENT = "sent"
DISABLED = "disabled"
FAILED = "failed"


def configured(settings: Settings) -> bool:
    return bool(settings.ha_url and settings.ha_token and settings.ha_notify_service)


def notify(settings: Settings, title: str, message: str, url: str | None = None) -> str:
    if not configured(settings):
        return DISABLED
    payload: dict[str, object] = {"title": title, "message": message}
    if url:
        payload["data"] = {"url": url}
    endpoint = f"{settings.ha_url}/api/services/notify/{settings.ha_notify_service}"
    headers = {"Authorization": f"Bearer {settings.ha_token}", "Content-Type": "application/json"}
    for attempt in (1, 2):
        try:
            with tls.urlopen(endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST", timeout=TIMEOUT, allow_http=True) as response:
                response.read()
            return SENT
        except (urllib.error.URLError, OSError, tls.Refused) as error:
            monitoring.log("home assistant notify failed", attempt=attempt, error=type(error).__name__)
    return FAILED
