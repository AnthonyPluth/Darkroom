from __future__ import annotations

import mimetypes
from pathlib import Path

from darkroom.server.common import CSP, Response, error_response

ROOT = Path(__file__).resolve().parent.parent / "static"


def serve(path: str, root: Path = ROOT) -> Response:
    relative = path.lstrip("/") or "index.html"
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        candidate = (root / "index.html").resolve()
        if not candidate.is_file():
            return error_response("not found", 404)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return Response(200, candidate.read_bytes(), content_type, {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"})
