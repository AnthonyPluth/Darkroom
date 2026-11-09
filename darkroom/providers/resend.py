from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from darkroom import config, monitoring, tls
from darkroom.providers.push_note import Note, Outcome
from darkroom.storage import secretbox

ENDPOINT = "https://api.resend.com/emails"
TIMEOUT = 10.0
MAX_REPLY = 64 * 1024
RETRY_STATUSES = frozenset({408, 409, 425, 429})


class Unusable(Exception):
    pass


@dataclass(frozen=True)
class Resend:
    api_key: str = field(repr=False)
    sender: str = field(repr=False)
    recipient: str = field(repr=False)
    endpoint: str = ENDPOINT
    timeout: float = TIMEOUT

    def send(self, note: Note) -> Outcome:
        text = f"{note.message}\n\n{note.click}" if note.click else note.message
        body = json.dumps({"from": self.sender, "to": [self.recipient], "subject": note.title, "text": text}).encode()
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "Darkroom"}
        if note.key:
            headers["Idempotency-Key"] = note.key
        host = urlsplit(self.endpoint).hostname or ""
        try:
            with tls.urlopen(self.endpoint, data=body, headers=headers, method="POST", timeout=self.timeout, allow_http=config.local_host(host)) as reply:
                reply.read(MAX_REPLY)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            outcome: Outcome = "retry" if status >= 500 or status in RETRY_STATUSES else "failed"
            monitoring.log("resend refused the alert", status=status, retry=outcome == "retry")
            return outcome
        except tls.Refused:
            monitoring.log("resend refused the alert", status=0, retry=False)
            return "failed"
        except tls.CONNECTION_PROBLEMS as error:
            monitoring.log("resend could not be reached", error=type(error).__name__, retry=True)
            return "retry"
        except ValueError as error:
            monitoring.log("resend could not be reached", error=type(error).__name__, retry=False)
            return "failed"
        return "sent"


def load(settings: config.Settings) -> Resend:
    if not settings.secret_key:
        raise Unusable("RESEND_API_KEY is set but DARKROOM_SECRET_KEY is not, so the key cannot be unsealed")
    try:
        key = secretbox.unseal(settings.resend_api_key, settings.secret_key)
    except secretbox.Unreadable:
        raise Unusable("RESEND_API_KEY cannot be read with this DARKROOM_SECRET_KEY; seal it again with `python run.py seal-secret`") from None
    if not (key.isascii() and key.isprintable() and " " not in key):
        raise Unusable("RESEND_API_KEY must be a Resend API key without spaces")
    return Resend(key, settings.alert_email_from, settings.alert_email_to)


def problems(settings: config.Settings) -> list[str]:
    if not config.resend_configured(settings) or config.resend_problems(settings):
        return []
    try:
        load(settings)
    except Unusable as error:
        return [str(error)]
    return []


def of(settings: config.Settings) -> Resend | None:
    if not config.resend_configured(settings) or config.resend_problems(settings):
        return None
    try:
        return load(settings)
    except Unusable:
        return None
