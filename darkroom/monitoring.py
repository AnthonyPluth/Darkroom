from __future__ import annotations

import json
import re
import sys
import traceback

URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")
URL_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s#\"']*")
BEARER = re.compile(r"(?i)\b(bearer|basic|x-api-key)[:=\s]+\S+")
NAMED_SECRET = re.compile(r"(?i)\b(code|state|nonce|token|id_token|access_token|refresh_token|client_secret|code_verifier|session|cookie|set-cookie|authorization|browser_token|darkroom_browser_token)([:=]\s*)[^\s;&,\"']+")
JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]+\.[\w-]*")


def scrub(text: str) -> str:
    text = URL_CREDENTIALS.sub(r"\1[removed]@", text)
    text = URL_QUERY.sub(r"\1?[removed]", text)
    text = BEARER.sub(r"\1 [removed]", text)
    text = NAMED_SECRET.sub(r"\1\2[removed]", text)
    return JWT.sub("[removed]", text)


def log(event: str, **counts: int | str | bool) -> None:
    line = {"event": scrub(event)} | {name: scrub(value) if isinstance(value, str) else value for name, value in counts.items()}
    print(json.dumps(line, sort_keys=True), file=sys.stderr)


def chain(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in found:
        found.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return found[::-1]


def types_only(error: BaseException) -> list[str]:
    out: list[str] = []
    for link in chain(error):
        if out:
            out.append("\nWhile handling the above, another exception was raised:\n\n")
        if link.__traceback__ is not None:
            out += ["Traceback (most recent call last):\n", *traceback.format_tb(link.__traceback__)]
        out.append(f"{type(link).__module__}.{type(link).__qualname__}: [removed]\n")
    return out


def report(error: BaseException | None = None, *, values: bool = False) -> None:
    error = error or sys.exc_info()[1]
    if error is None:
        return
    text = "".join(traceback.format_exception(error) if values else types_only(error))
    print(scrub(text), file=sys.stderr, end="", flush=True)
