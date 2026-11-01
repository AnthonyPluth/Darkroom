from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from darkroom.config import Settings

CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    body: bytes
    settings: Settings


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)


def json_response(value: object, status: int = 200) -> Response:
    return Response(status, json.dumps(value).encode())


def error_response(message: str, status: int) -> Response:
    return json_response({"error": message}, status)
