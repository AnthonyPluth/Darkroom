from __future__ import annotations

import html
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import Engine

from darkroom.config import Settings

CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"


@dataclass(frozen=True)
class Principal:
    kind: str
    sub: str
    email: str | None = None
    name: str | None = None
    groups: tuple[str, ...] = ()
    renewed: int | None = None


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    body: bytes
    settings: Settings
    engine: Engine | None = None
    user: Principal | None = None


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: tuple[str, ...] = ()


def header(request: Request, name: str) -> str | None:
    wanted = name.lower()
    for key, value in request.headers.items():
        if key.lower() == wanted:
            return value
    return None


def first_query(request: Request, name: str) -> str | None:
    values = request.query.get(name)
    return values[0] if values else None


def json_response(value: object, status: int = 200, cookies: tuple[str, ...] = ()) -> Response:
    return Response(status, json.dumps(value).encode(), cookies=cookies)


def error_response(message: str, status: int) -> Response:
    return json_response({"error": message}, status)


def redirect(location: str, cookies: tuple[str, ...] = ()) -> Response:
    return Response(303, b"", "text/plain; charset=utf-8", {"Location": location}, cookies)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Darkroom</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 48px 16px; color: #222; background: #fafafa; }}
main {{ max-width: 32rem; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin: 0 0 12px; }}
a.button {{ display: inline-block; margin-top: 16px; padding: 10px 16px; border-radius: 8px; background: #222; color: #fff; text-decoration: none; }}
</style>
</head>
<body><main><h1>{title}</h1><p>{message}</p>{link}</main></body>
</html>
"""


def page(status: int, title: str, message: str, link: tuple[str, str] | None = None, cookies: tuple[str, ...] = ()) -> Response:
    button = f'<a class="button" href="{html.escape(link[0], quote=True)}">{html.escape(link[1])}</a>' if link else ""
    body = PAGE.format(title=html.escape(title), message=html.escape(message), link=button).encode()
    return Response(status, body, "text/html; charset=utf-8", {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"}, cookies)
