from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import accounts, events, media
from darkroom.domain.ingest import pairing
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.storage.models import DedupeDecision, Export, GoogleAccount, MediaItem, ReviewItem, Setting
from darkroom.storage.settings_keys import KEEP_EDITED_COPIES

PHASH_MAX_DISTANCE = 8
REVIEW_MAX_SECONDS = 2.0
CONFIRMING_TIERS = (1, 2)
TRUE_VALUES = {"1", "true", "yes", "on"}

Action = Literal["skip", "review", "google_only"]
DecidedBy = Literal["auto", "owner"]


@dataclass(frozen=True)
class DedupeSettings:
    keep_edited_copies: bool = False


@dataclass(frozen=True)
class Shot:
    id: int
    person_id: int
    source: str
    sha1: str
    phash: str | None
    captured_utc: float | None
    captured_local: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    make: str | None
    model: str | None
    is_video: bool


@dataclass(frozen=True)
class TakeoutCandidate:
    source_path: str
    sha1: str
    phash: str | None
    captured_utc: float | None
    captured_local: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    make: str | None
    model: str | None
    is_video: bool
    is_edited: bool = False
    edited_of: str | None = None


@dataclass(frozen=True)
class Decision:
    source_path: str
    account_id: int
    person_id: int
    tier: int
    action: Action
    reason: str
    matched_item_id: int | None = None
    matched_source: str | None = None
    decided_by: DecidedBy = "auto"

    @property
    def confirms(self) -> bool:
        return self.decided_by == "auto" and self.tier in CONFIRMING_TIERS and self.matched_item_id is not None and self.matched_source == "phone"


@dataclass(frozen=True)
class PersonIndex:
    account_id: int
    default_person_id: int
    person_ids: frozenset[int]
    phone: tuple[Shot, ...]
    takeout: tuple[Shot, ...] = ()
    peers: tuple[TakeoutCandidate, ...] = ()
    decided: Mapping[str, Decision] = field(default_factory=dict)

    @classmethod
    def load(cls, opened: Session, account_id: int) -> PersonIndex:
        account = opened.get(GoogleAccount, account_id)
        if account is None:
            raise LookupError("no such Google account")
        people = accounts.served_people(opened, account_id)
        phone = opened.execute(
            select(MediaItem).where(MediaItem.source == "phone", MediaItem.person_id.in_(people)).order_by(MediaItem.id)
        ).scalars()
        takeout = opened.execute(
            select(MediaItem).join(Export, Export.id == MediaItem.export_id)
            .where(MediaItem.source == "takeout", MediaItem.person_id.in_(people), Export.account_id == account_id).order_by(MediaItem.id)
        ).scalars()
        return cls(account_id, account.default_person_id, frozenset(people), tuple(shot_of(i) for i in phone), tuple(shot_of(i) for i in takeout))


def shot_of(item: MediaItem) -> Shot:
    return Shot(
        id=item.id, person_id=item.person_id, source=item.source, sha1=item.sha1, phash=item.phash,
        captured_utc=item.captured_utc, captured_local=item.captured_local, width=item.width, height=item.height,
        duration_ms=item.duration_ms, make=item.make, model=item.model, is_video=Path(item.path).suffix.lower() in media.VIDEO_SUFFIXES,
    )


def candidate_of(item: TakeoutItem, fields: MediaItem, root: Path) -> TakeoutCandidate:
    return TakeoutCandidate(
        source_path=str(item.path.relative_to(root)), sha1=fields.sha1, phash=fields.phash,
        captured_utc=fields.captured_utc, captured_local=fields.captured_local, width=fields.width, height=fields.height,
        duration_ms=fields.duration_ms, make=fields.make, model=fields.model, is_video=item.path.suffix.lower() in media.VIDEO_SUFFIXES,
        is_edited=pairing.original_name(item.path) is not None,
        edited_of=None if item.edited_of is None else str(item.edited_of.relative_to(root)),
    )


def load_settings(opened: Session) -> DedupeSettings:
    stored = opened.get(Setting, KEEP_EDITED_COPIES)
    value = None if stored is None else stored.value
    return DedupeSettings(keep_edited_copies=value is not None and value.strip().lower() in TRUE_VALUES)


def subsecond_ms(utc: float) -> int | None:
    millis = round((utc - math.floor(utc)) * 1000)
    return None if millis in (0, 1000) else millis


def wall_clock(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def same_second(item: TakeoutCandidate, shot: Shot) -> bool:
    if item.captured_utc is not None and shot.captured_utc is not None:
        if math.floor(item.captured_utc) != math.floor(shot.captured_utc):
            return False
        mine, theirs = subsecond_ms(item.captured_utc), subsecond_ms(shot.captured_utc)
        return mine is None or theirs is None or mine == theirs
    if item.captured_local is None or shot.captured_local is None:
        return False
    mine_local, theirs_local = wall_clock(item.captured_local), wall_clock(shot.captured_local)
    return mine_local is not None and mine_local == theirs_local


def seconds_apart(item: TakeoutCandidate, shot: Shot) -> float | None:
    if item.captured_utc is not None and shot.captured_utc is not None:
        return abs(item.captured_utc - shot.captured_utc)
    if item.captured_local is None or shot.captured_local is None:
        return None
    mine, theirs = wall_clock(item.captured_local), wall_clock(shot.captured_local)
    if mine is None or theirs is None:
        return None
    return abs((mine - theirs).total_seconds())


def same_text(mine: str | None, theirs: str | None) -> bool:
    if mine is None or theirs is None or not mine.strip() or not theirs.strip():
        return False
    return mine.strip().casefold() == theirs.strip().casefold()


def same_size(item: TakeoutCandidate, shot: Shot) -> bool:
    if item.is_video != shot.is_video:
        return False
    if item.is_video:
        return item.duration_ms is not None and shot.duration_ms is not None and item.duration_ms // 1000 == shot.duration_ms // 1000
    return item.width is not None and item.height is not None and (item.width, item.height) == (shot.width, shot.height)


def same_shot(item: TakeoutCandidate, shot: Shot) -> bool:
    return same_second(item, shot) and same_text(item.make, shot.make) and same_text(item.model, shot.model) and same_size(item, shot)


def phash_distance(mine: str | None, theirs: str | None) -> int | None:
    if mine is None or theirs is None or len(mine) != len(theirs):
        return None
    try:
        return (int(mine, 16) ^ int(theirs, 16)).bit_count()
    except ValueError:
        return None


def qualifying_peers(index: PersonIndex, item: TakeoutCandidate) -> list[TakeoutCandidate]:
    return [peer for peer in index.peers if peer.source_path != item.source_path and peer.sha1 != item.sha1 and not peer.is_edited]


def owner_of(index: PersonIndex, shots: Iterable[Shot]) -> int:
    owners = {shot.person_id for shot in shots}
    return owners.pop() if len(owners) == 1 else index.default_person_id


def served(index: PersonIndex, shots: Iterable[Shot]) -> list[Shot]:
    return [shot for shot in shots if shot.person_id in index.person_ids]


def by_sha1(index: PersonIndex, item: TakeoutCandidate) -> Shot | None:
    return next((shot for shot in served(index, (*index.phone, *index.takeout)) if shot.sha1 == item.sha1), None)


def nearest(index: PersonIndex, item: TakeoutCandidate) -> tuple[Shot, int, float] | None:
    found: list[tuple[int, float, int, Shot]] = []
    for shot in served(index, index.phone):
        distance, apart = phash_distance(item.phash, shot.phash), seconds_apart(item, shot)
        if distance is not None and apart is not None and distance <= PHASH_MAX_DISTANCE and apart <= REVIEW_MAX_SECONDS:
            found.append((distance, apart, shot.id, shot))
    if not found:
        return None
    distance, apart, _, shot = min(found, key=lambda entry: entry[:3])
    return shot, distance, apart


def decide(index: PersonIndex, item: TakeoutCandidate, settings: DedupeSettings) -> Decision:
    def made(tier: int, action: Action, reason: str, person_id: int, matched: Shot | None = None) -> Decision:
        return Decision(item.source_path, index.account_id, person_id, tier, action, reason, None if matched is None else matched.id, None if matched is None else matched.source)

    exact = by_sha1(index, item)
    if exact is not None:
        return made(1, "skip", f"same SHA-1 as a {exact.source} item", exact.person_id, exact)
    if not item.is_edited:
        shots = [shot for shot in served(index, index.phone) if same_shot(item, shot)]
        rivals = sum(1 for peer in qualifying_peers(index, item) if any(same_shot(peer, shot) for shot in shots))
        if len(shots) == 1 and rivals == 0:
            return made(2, "skip", "same capture second, camera and size", shots[0].person_id, shots[0])
        if shots:
            reason = f"tier 2 is ambiguous: {len(shots)} phone items and {rivals} other Takeout items share the capture second, camera and size"
            return made(4, "review", reason, owner_of(index, shots), shots[0])
    original = None if item.edited_of is None else index.decided.get(item.edited_of)
    if original is not None and original.decided_by == "auto" and original.tier in CONFIRMING_TIERS and not settings.keep_edited_copies:
        matched = next((shot for shot in (*index.phone, *index.takeout) if shot.id == original.matched_item_id), None)
        return made(3, "skip", f"edited copy of an item matched at tier {original.tier}", original.person_id, matched)
    close = nearest(index, item)
    if close is not None:
        shot, distance, apart = close
        return made(4, "review", f"perceptual distance {distance}, {apart:g} s apart", shot.person_id, shot)
    return made(5, "google_only", "no match", index.default_person_id)


def decide_all(index: PersonIndex, items: Iterable[TakeoutCandidate], settings: DedupeSettings) -> list[Decision]:
    every = tuple(items)
    with_peers = replace(index, peers=every, decided={})
    originals = {item.source_path: decide(with_peers, item, settings) for item in every if not item.is_edited}
    with_originals = replace(with_peers, decided=originals)
    return [originals[item.source_path] if not item.is_edited else decide(with_originals, item, settings) for item in every]


def stored_decision(opened: Session, export_id: int, source_path: str) -> DedupeDecision | None:
    return opened.execute(
        select(DedupeDecision).where(DedupeDecision.export_id == export_id, DedupeDecision.source_path == source_path)
    ).scalar_one_or_none()


def checked_match(opened: Session, decision: Decision, export: Export, people: set[int]) -> MediaItem | None:
    if decision.matched_item_id is None:
        return None
    matched = opened.get(MediaItem, decision.matched_item_id)
    if matched is None:
        raise ValueError("the matched item does not exist")
    if matched.person_id not in people:
        raise ValueError("the matched item belongs to someone this account does not serve")
    if matched.source == "takeout":
        origin = None if matched.export_id is None else opened.get(Export, matched.export_id)
        if origin is None or origin.account_id != export.account_id:
            raise ValueError("the matched Takeout item came from another account")
    if decision.tier == 2 and matched.source != "phone":
        raise ValueError("tier 2 matches only phone items")
    return matched


def apply(opened: Session, decision: Decision, export: Export, now: float) -> DedupeDecision:
    if decision.account_id != export.account_id:
        raise ValueError("the decision is for another account")
    existing = stored_decision(opened, export.id, decision.source_path)
    if existing is not None:
        return existing
    people = accounts.served_people(opened, export.account_id)
    if decision.person_id not in people:
        raise ValueError("the decision assigns the item to someone this account does not serve")
    matched = checked_match(opened, decision, export, people)
    row = DedupeDecision(
        export_id=export.id, account_id=export.account_id, source_path=decision.source_path, person_id=decision.person_id,
        matched_item_id=decision.matched_item_id, tier=decision.tier, reason=decision.reason, decided_by=decision.decided_by, decided_at=now,
    )
    opened.add(row)
    opened.flush()
    if decision.tier == 4:
        opened.add(ReviewItem(decision_id=row.id, status="pending"))
    if decision.confirms and matched is not None and matched.source == "phone":
        matched.confirmed_in_google = True
    opened.flush()
    return row


def confirming(opened: Session, item_id: int) -> bool:
    found = opened.execute(
        select(DedupeDecision.id).where(
            DedupeDecision.matched_item_id == item_id, DedupeDecision.reversed_at.is_(None),
            DedupeDecision.decided_by == "auto", DedupeDecision.tier.in_(CONFIRMING_TIERS),
        ).limit(1)
    ).first()
    return found is not None


def reverse(opened: Session, decision_id: int, now: float, by: DecidedBy) -> DedupeDecision:
    row = opened.get(DedupeDecision, decision_id)
    if row is None:
        raise LookupError("no such dedupe decision")
    if row.reversed_at is None:
        row.reversed_at = now
        opened.flush()
        events.record(opened, now, "dedupe_reversed", f"decision {row.id}", f"tier {row.tier}, by {by}")
    matched = None if row.matched_item_id is None else opened.get(MediaItem, row.matched_item_id)
    if matched is not None and matched.source == "phone":
        matched.confirmed_in_google = confirming(opened, matched.id)
    opened.flush()
    return row
