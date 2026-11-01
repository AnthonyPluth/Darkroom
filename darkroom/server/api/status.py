from __future__ import annotations

from darkroom import VERSION
from darkroom.server.common import Request, Response, json_response


def healthz(request: Request) -> Response:
    return json_response({"ok": True, "version": VERSION})
