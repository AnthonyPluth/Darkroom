from __future__ import annotations

import re
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from darkroom import config, monitoring, tls
from darkroom.providers.push_note import Note, Outcome
from darkroom.storage import secretbox

TIMEOUT = 10.0
MAX_REPLY = 64 * 1024
TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
RETRY_STATUSES = frozenset({408, 425, 429})


class Unusable(Exception):
    pass


@dataclass(frozen=True)
class Ntfy:
    server: str
    topic: str = field(repr=False)
    token: str = field(default="", repr=False)
    timeout: float = TIMEOUT

    def send(self, note: Note) -> Outcome:
        headers = {"Title": note.title, "Tags": note.tags, "Content-Type": "text/plain; charset=utf-8"}
        if note.click:
            headers["Click"] = note.click
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        host = urlsplit(self.server).hostname or ""
        try:
            with tls.urlopen(f"{self.server}/{quote(self.topic, safe='')}", data=note.message.encode(), headers=headers, method="POST",
                             timeout=self.timeout, allow_http=config.local_host(host)) as reply:
                reply.read(MAX_REPLY)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            outcome: Outcome = "retry" if status >= 500 or status in RETRY_STATUSES else "failed"
            monitoring.log("ntfy refused the alert", status=status, retry=outcome == "retry")
            return outcome
        except tls.Refused:
            monitoring.log("ntfy refused the alert", status=0, retry=False)
            return "failed"
        except tls.CONNECTION_PROBLEMS as error:
            monitoring.log("ntfy could not be reached", error=type(error).__name__, retry=True)
            return "retry"
        except ValueError as error:
            monitoring.log("ntfy could not be reached", error=type(error).__name__, retry=False)
            return "failed"
        return "sent"


def unsealed(value: str, name: str, secret: str) -> str:
    try:
        return secretbox.unseal(value, secret)
    except secretbox.Unreadable:
        raise Unusable(f"{name} cannot be read with this DARKROOM_SECRET_KEY; seal it again with `python run.py seal-secret`") from None


def load(settings: config.Settings) -> Ntfy:
    topic = unsealed(settings.ntfy_topic, "NTFY_TOPIC", settings.secret_key)
    if not TOPIC.fullmatch(topic):
        raise Unusable("NTFY_TOPIC must be an ntfy topic name: up to 64 letters, digits, - and _")
    token = unsealed(settings.ntfy_token, "NTFY_TOKEN", settings.secret_key) if settings.ntfy_token else ""
    if token and not (token.isascii() and token.isprintable() and " " not in token):
        raise Unusable("NTFY_TOKEN must be an ntfy access token without spaces")
    return Ntfy(settings.ntfy_url, topic, token)


def problems(settings: config.Settings) -> list[str]:
    if not config.ntfy_configured(settings) or config.ntfy_problems(settings):
        return []
    try:
        load(settings)
    except Unusable as error:
        return [str(error)]
    return []


def of(settings: config.Settings) -> Ntfy | None:
    if not config.ntfy_configured(settings) or config.ntfy_problems(settings):
        return None
    try:
        return load(settings)
    except Unusable:
        return None
