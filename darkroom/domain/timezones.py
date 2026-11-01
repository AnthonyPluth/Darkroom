from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timezonefinder import TimezoneFinder


@lru_cache(maxsize=1)
def finder() -> TimezoneFinder:
    return TimezoneFinder()


def offset_minutes(lat: float, lon: float, local: datetime) -> int | None:
    name = finder().timezone_at(lat=lat, lng=lon)
    if name is None:
        return None
    try:
        offset = local.replace(tzinfo=ZoneInfo(name)).utcoffset()
    except ZoneInfoNotFoundError:
        return None
    return None if offset is None else int(offset.total_seconds() // 60)
