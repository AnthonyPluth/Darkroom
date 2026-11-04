from __future__ import annotations

import base64
import hashlib
import html
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import Engine

from darkroom.config import Settings, usable_viewer_url
from darkroom.dates import utc_now
from darkroom.storage.paths import Readable

CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"


def viewer_url(vnc_url: str) -> str | None:
    return usable_viewer_url(vnc_url)


def app_csp(vnc_url: str = "") -> str:
    viewer = viewer_url(vnc_url)
    if viewer is None:
        return CSP
    parts = urlsplit(viewer)
    return f"{CSP}; frame-src 'self' {parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class Principal:
    kind: str
    sub: str
    email: str | None = None
    name: str | None = None
    groups: tuple[str, ...] = ()
    renewed: int | None = None
    device_id: int | None = None
    person_id: int | None = None


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
    params: Mapping[str, str] = field(default_factory=dict)
    stream: Readable | None = None


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


def engine_of(request: Request) -> Engine:
    if request.engine is None:
        raise RuntimeError("this route needs the database")
    return request.engine


def now() -> float:
    return utc_now().timestamp()


def path_id(request: Request) -> int | None:
    raw = request.params.get("id", "")
    return int(raw) if raw.isdigit() and len(raw) <= 12 else None


def first_query(request: Request, name: str) -> str | None:
    values = request.query.get(name)
    return values[0] if values else None


def json_response(value: object, status: int = 200, cookies: tuple[str, ...] = ()) -> Response:
    return Response(status, json.dumps(value).encode(), cookies=cookies)


def error_response(message: str, status: int) -> Response:
    return json_response({"error": message}, status)


def redirect(location: str, cookies: tuple[str, ...] = ()) -> Response:
    return Response(303, b"", "text/plain; charset=utf-8", {"Location": location}, cookies)


PAGE_STYLE = """
*{box-sizing:border-box}
:root{color-scheme:dark}
body{margin:0;background:#16140f;color:#f1ece2;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{display:grid;min-height:100vh}
.side{display:flex;flex-direction:column;gap:32px;padding:24px 16px 0}
.brand{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:700}
.dot{width:14px;height:14px;border-radius:50%;background:#e2693f;display:block}
.pitch{display:none}
main{display:flex;align-items:flex-start;justify-content:center;padding:32px 16px}
.card{width:100%;max-width:400px;background:#1f1c17;border:1px solid #3a352c;border-radius:12px;padding:20px;display:flex;flex-direction:column;gap:16px}
h1{margin:0;font-size:24px;line-height:1.2}
p{margin:0}
.muted{color:#b3ab9d}
.alert{background:#3a1a1a;color:#f08a84;border:1px solid #f08a84;border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:4px}
.alert b{display:flex;align-items:center;gap:8px}
.alert i{width:10px;height:10px;background:#f08a84;border-radius:1px;display:block}
.alert span{color:#f1ece2}
a.button{display:flex;align-items:center;justify-content:center;min-height:48px;padding:0 20px;border-radius:8px;border:1px solid #e2693f;background:#e2693f;color:#1a0e08;font-weight:600;text-decoration:none}
a.button:focus-visible{outline:3px solid #f1ece2;outline-offset:2px}
@media (min-width:768px){
.wrap{grid-template-columns:1fr 1fr}
.side{background:#1b1813;padding:64px;justify-content:space-between;gap:0}
.pitch{display:flex;flex-direction:column;gap:16px;max-width:420px}
.pitch strong{font-size:36px;line-height:1.1;letter-spacing:-.01em}
.pitch span{color:#b3ab9d;font-size:16px}
main{align-items:center;padding:48px}
.card{padding:32px;gap:20px}
h1{font-size:28px}
}
"""
PAGE_CSP = f"{CSP}; style-src 'sha256-{base64.b64encode(hashlib.sha256(PAGE_STYLE.encode()).digest()).decode()}'"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Darkroom</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
<div class="side">
<div class="brand"><span class="dot"></span>Darkroom</div>
<div class="pitch"><strong>Photos leave the phone only after Google Photos has them.</strong><span>Darkroom syncs the family's iPhones to the Pixels, imports Google Takeout, and keeps one copy of everything.</span></div>
</div>
<main><div class="card">
<h1>Sign in</h1>
{body}{hint}{link}
</div></main>
</div>
</body>
</html>
"""


def page(status: int, title: str, message: str, link: tuple[str, str] | None = None, cookies: tuple[str, ...] = (), hint: str = "") -> Response:
    button = f'<a class="button" href="{html.escape(link[0], quote=True)}">{html.escape(link[1])}</a>' if link else ""
    if status >= 400:
        body = f'<div role="alert" class="alert"><b><i></i>{html.escape(title)}</b><span>{html.escape(message)}</span></div>'
    else:
        body = f'<p class="muted"><b>{html.escape(title)}.</b> {html.escape(message)}</p>'
    note = f'<p class="muted">{html.escape(hint)}</p>' if hint else ""
    document = PAGE.format(title=html.escape(title), body=body, hint=note, link=button, style=PAGE_STYLE).encode()
    return Response(status, document, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP, "X-Content-Type-Options": "nosniff"}, cookies)
