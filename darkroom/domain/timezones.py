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
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None
    first = local.replace(tzinfo=zone, fold=0).utcoffset()
    second = local.replace(tzinfo=zone, fold=1).utcoffset()
    if first is None or first != second:
        return None
    return int(first.total_seconds() // 60)


def offset_at(lat: float, lon: float, moment_utc: datetime) -> int | None:
    name = finder().timezone_at(lat=lat, lng=lon)
    if name is None:
        return None
    try:
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None
    offset = moment_utc.astimezone(zone).utcoffset()
    return None if offset is None else int(offset.total_seconds() // 60)
