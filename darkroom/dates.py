from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LATEST_INSTANT = datetime(2100, 1, 1, tzinfo=UTC).timestamp()
LATEST_STORED = datetime(9000, 1, 1, tzinfo=UTC).timestamp()


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


def is_instant(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return 0 < value <= LATEST_INSTANT


def plausibly_created(value: object, observed_by: float) -> bool:
    return is_instant(value) and isinstance(value, int | float) and value <= observed_by


def usable_stored(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return 0 < value <= LATEST_STORED


def instant_or_none(value: float | None) -> float | None:
    return float(value) if value is not None and usable_stored(value) else None
