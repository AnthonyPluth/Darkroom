from __future__ import annotations

from typing import TypedDict


class Health(TypedDict):
    ok: bool
    version: str


class Me(TypedDict):
    sub: str
    email: str
    name: str
    groups: list[str]


REPLIES: dict[str, type] = {
    "GET /healthz": Health,
    "GET /api/me": Me,
}
