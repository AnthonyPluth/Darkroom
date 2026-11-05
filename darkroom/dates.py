from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def local_today(zone: str) -> str:
    return utc_now().astimezone(ZoneInfo(zone)).date().isoformat()


def one_year_after(moment: float) -> float:
    start = datetime.fromtimestamp(moment, UTC)
    try:
        later = start.replace(year=start.year + 1)
    except ValueError:
        later = start.replace(year=start.year + 1, day=28)
    return later.timestamp()
