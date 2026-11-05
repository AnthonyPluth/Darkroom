from __future__ import annotations

import math
import string
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe
from darkroom.storage.models import MediaItem

HASH_BITS = 64
HASH_DIGITS = HASH_BITS // 4
HEX = set(string.hexdigits)
PHASH_MAX_DISTANCE = 6
DHASH_MAX_DISTANCE = 8
BURST_PHASH_MAX_DISTANCE = 10
BURST_DHASH_MAX_DISTANCE = 14
BURST_MAX_SECONDS = 2.0


@dataclass(frozen=True)
class Thresholds:
    phash: int = PHASH_MAX_DISTANCE
    dhash: int = DHASH_MAX_DISTANCE
    burst_phash: int = BURST_PHASH_MAX_DISTANCE
    burst_dhash: int = BURST_DHASH_MAX_DISTANCE
    burst_seconds: float = BURST_MAX_SECONDS

    def __post_init__(self) -> None:
        for distance in (self.phash, self.dhash, self.burst_phash, self.burst_dhash):
            if not 0 <= distance <= HASH_BITS:
                raise ValueError(f"a hash distance is between 0 and {HASH_BITS}")
        if self.burst_phash < self.phash or self.burst_dhash < self.dhash:
            raise ValueError("a burst allows at least the distance any two shots do")
        if not math.isfinite(self.burst_seconds) or self.burst_seconds < 0:
            raise ValueError("a burst lasts a finite number of seconds, zero or more")


DEFAULTS = Thresholds()


@dataclass(frozen=True)
class Frame:
    id: int
    phash: int
    dhash: int
    captured_utc: float | None = None
    captured_local: str | None = None


@dataclass(frozen=True)
class Member:
    id: int
    phash_distance: int
    dhash_distance: int


@dataclass(frozen=True)
class Group:
    anchor: int
    members: tuple[Member, ...]

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(member.id for member in self.members)


def hash_of(text: str | None) -> int | None:
    if text is None or len(text) != HASH_DIGITS or not set(text) <= HEX:
        return None
    return int(text, 16)


def frame_of(item_id: int, phash: str | None, dhash: str | None, captured_utc: float | None = None, captured_local: str | None = None) -> Frame | None:
    first, second = hash_of(phash), hash_of(dhash)
    if first is None or second is None:
        return None
    return Frame(item_id, first, second, captured_utc, captured_local)


def distance(mine: int, theirs: int) -> int:
    return (mine ^ theirs).bit_count()


def related(frame: Frame, anchor: Frame, thresholds: Thresholds) -> tuple[int, int] | None:
    phash, dhash = distance(frame.phash, anchor.phash), distance(frame.dhash, anchor.dhash)
    if phash <= thresholds.phash and dhash <= thresholds.dhash:
        return phash, dhash
    if phash <= thresholds.burst_phash and dhash <= thresholds.burst_dhash:
        apart = dedupe.seconds_apart(frame, anchor)
        if apart is not None and apart <= thresholds.burst_seconds:
            return phash, dhash
    return None


class AnchorIndex:
    def __init__(self, radius: int) -> None:
        parts = radius + 1
        self.cuts = [(HASH_BITS * n // parts, HASH_BITS * (n + 1) // parts) for n in range(parts)] if parts <= HASH_BITS else [(0, 0)]
        self.buckets: list[dict[int, list[Frame]]] = [{} for _ in self.cuts]

    def key(self, frame: Frame, part: int) -> int:
        low, high = self.cuts[part]
        return (frame.phash >> low) & ((1 << (high - low)) - 1)

    def add(self, frame: Frame) -> None:
        for part, bucket in enumerate(self.buckets):
            bucket.setdefault(self.key(frame, part), []).append(frame)

    def near(self, frame: Frame) -> list[Frame]:
        found: dict[int, Frame] = {}
        for part, bucket in enumerate(self.buckets):
            found.update((anchor.id, anchor) for anchor in bucket.get(self.key(frame, part), []))
        return list(found.values())


def group(frames: Iterable[Frame], thresholds: Thresholds = DEFAULTS) -> list[Group]:
    anchors = AnchorIndex(max(thresholds.phash, thresholds.burst_phash))
    grouped: dict[int, list[Member]] = {}
    for frame in sorted({frame.id: frame for frame in frames}.values(), key=lambda each: each.id):
        best: tuple[int, int, int, int] | None = None
        for anchor in anchors.near(frame):
            found = related(frame, anchor, thresholds)
            if found is not None and (best is None or (sum(found), anchor.id) < best[:2]):
                best = (sum(found), anchor.id, *found)
        if best is None:
            anchors.add(frame)
            grouped[frame.id] = [Member(frame.id, 0, 0)]
        else:
            grouped[best[1]].append(Member(frame.id, best[2], best[3]))
    return [Group(anchor, tuple(members)) for anchor, members in grouped.items() if len(members) > 1]


def suggest(opened: Session, person_id: int, thresholds: Thresholds = DEFAULTS) -> list[Group]:
    rows = opened.execute(
        select(MediaItem.id, MediaItem.phash, MediaItem.dhash, MediaItem.captured_utc, MediaItem.captured_local)
        .where(MediaItem.person_id == person_id, MediaItem.phash.is_not(None), MediaItem.dhash.is_not(None)).order_by(MediaItem.id)
    ).all()
    frames = (frame_of(*row) for row in rows)
    return group((frame for frame in frames if frame is not None), thresholds)
