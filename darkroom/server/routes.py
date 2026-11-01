from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from darkroom.server import auth
from darkroom.server.api import status
from darkroom.server.common import Request, Response

AUTH_KINDS = ("session", "device", "worker", "mcp", "public")


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: Callable[[Request], Response]
    auth: str


ROUTES: tuple[Route, ...] = (
    Route("GET", "/healthz", status.healthz, "public"),
    Route("GET", "/auth/login", auth.login, "public"),
    Route("GET", "/auth/callback", auth.callback, "public"),
    Route("POST", "/auth/logout", auth.logout, "session"),
    Route("GET", "/auth/signed-out", auth.signed_out, "public"),
    Route("GET", "/api/me", auth.me, "session"),
)


def find(method: str, path: str) -> Route | None:
    for route in ROUTES:
        if route.method == method and route.path == path:
            return route
    return None
