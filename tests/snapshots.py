from __future__ import annotations

from collections.abc import Callable
from typing import Any


def snapshot_pending(target: Any) -> Any:
    target.snapshot_pending = True
    return target


def is_snapshot_pending(target: Callable[..., Any] | type) -> bool:
    return bool(getattr(target, "snapshot_pending", False))
