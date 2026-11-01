from __future__ import annotations

from typing import TypedDict


class Health(TypedDict):
    ok: bool
    version: str


REPLIES: dict[str, type] = {
    "GET /healthz": Health,
}
