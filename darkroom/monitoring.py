from __future__ import annotations

import json
import re
import sys

URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")
BEARER = re.compile(r"(?i)\b(bearer|x-api-key)[:=\s]+\S+")


def scrub(text: str) -> str:
    return BEARER.sub(r"\1 [removed]", URL_CREDENTIALS.sub(r"\1[removed]@", text))


def log(event: str, **counts: int | str | bool) -> None:
    line = {"event": scrub(event)} | {name: scrub(value) if isinstance(value, str) else value for name, value in counts.items()}
    print(json.dumps(line, sort_keys=True), file=sys.stderr)
