from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Outcome = Literal["sent", "retry", "failed"]


@dataclass(frozen=True)
class Note:
    title: str
    message: str
    click: str | None = None
    key: str = ""


class Pusher(Protocol):
    def send(self, note: Note) -> Outcome: ...
