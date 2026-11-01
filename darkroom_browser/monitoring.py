from __future__ import annotations

import json
import re
import sys

URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")
URL_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s#\"']*")
BEARER = re.compile(r"(?i)\b(bearer|basic|x-api-key)[:=\s]+\S+")
NAMED_SECRET = re.compile(r"(?i)\b(token|browser_token|darkroom_browser_token|cookie|set-cookie|authorization|session)([:=]\s*)[^\s;&,\"']+")
JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]+\.[\w-]*")
SECRET_NAME = re.compile(r"(?i)token|secret|cookie|authorization|session|password")


def scrub(text: str) -> str:
    text = URL_CREDENTIALS.sub(r"\1[removed]@", text)
    text = URL_QUERY.sub(r"\1?[removed]", text)
    text = BEARER.sub(r"\1 [removed]", text)
    text = NAMED_SECRET.sub(r"\1\2[removed]", text)
    return JWT.sub("[removed]", text)


def log(event: str, **counts: int | str | bool | float) -> None:
    line = {"event": scrub(event)} | {name: "[removed]" if SECRET_NAME.search(name) else scrub(value) if isinstance(value, str) else value for name, value in counts.items()}
    sys.stderr.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stderr.flush()
