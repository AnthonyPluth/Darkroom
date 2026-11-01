from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath

from darkroom.server.common import CSP, Response, error_response

ROOT = Path(__file__).resolve().parent.parent / "static" / "app"
INDEX = "index.html"


def page_address(relative: str) -> bool:
    return "." not in PurePosixPath(relative).name


def serve(path: str, root: Path | None = None) -> Response:
    folder = root or ROOT
    relative = path.lstrip("/") or INDEX
    base = folder.resolve()
    candidate = (folder / relative).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        if not page_address(relative):
            return error_response("not found", 404)
        candidate = base / INDEX
        if not candidate.is_file():
            return error_response("not found", 404)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
        content_type += "; charset=utf-8"
    return Response(200, candidate.read_bytes(), content_type, {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"})
