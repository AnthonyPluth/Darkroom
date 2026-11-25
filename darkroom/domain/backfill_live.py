from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import media
from darkroom.domain.ingest import pairing
from darkroom.storage.models import DedupeDecision, Export, ManagedCompound, ManagedCompoundMember, ManagedImportIntent, TakeoutEntry

LIVE_KEY: Final = "backfill-live:"
MEDIA_KEY: Final = "media-item:"


@dataclass(frozen=True)
class Pair:
    still: int
    video: int
    export: int

    @property
    def key(self) -> str:
        return f"{LIVE_KEY}{self.still}"

    @property
    def items(self) -> tuple[int, int]:
        return self.still, self.video


@dataclass(frozen=True)
class Groups:
    pairs: dict[int, Pair]
    holds: dict[int, str]


def item_id(key: str) -> int | None:
    value = key.removeprefix(MEDIA_KEY)
    return int(value) if value != key and value.isascii() and value.isdecimal() and len(value) < 20 else None


def frozen(opened: Session, person_id: int, owner_id: str, wanted: Collection[int]) -> Groups:
    member, compound, intent = ManagedCompoundMember, ManagedCompound, ManagedImportIntent
    pending = select(member.compound_id).join(intent, intent.id == member.intent_id).where(
        intent.kind == "backfill", intent.person_id == person_id, intent.owner_id == owner_id,
        member.resource_key.in_([f"{MEDIA_KEY}{item}" for item in wanted]))
    rows = opened.execute(select(member.compound_id, member.resource_key, member.role, intent.export_id)
                          .join(compound, compound.id == member.compound_id).join(intent, intent.id == compound.intent_id)
                          .where(compound.compound_key.startswith(LIVE_KEY), compound.id.in_(pending))).all()
    found: dict[tuple[int, int], dict[str, int]] = defaultdict(dict)
    holds: dict[int, str] = {}
    for compound_id, resource, role, export_id in rows:
        item = item_id(resource)
        if item is not None:
            holds[item] = "live_photo"
            if export_id is not None:
                found[(compound_id, export_id)][role] = item
    pairs = {}
    for (_, export_id), members in found.items():
        if set(members) == {"live_still", "live_video"}:
            pair = Pair(members["live_still"], members["live_video"], export_id)
            pairs.update(dict.fromkeys(pair.items, pair))
    return Groups(pairs, holds)


def recorded(opened: Session, person_id: int, candidates: Collection[int], sources: Collection[int], export_id: int | None = None) -> Groups:
    decision = DedupeDecision
    eligible = (select(decision.export_id).where(decision.takeout_item_id.in_(candidates), decision.person_id == person_id)
                if export_id is None else [export_id])
    finished = select(Export.id).where(Export.id.in_(eligible), Export.status == "imported")
    decisions: dict[int, dict[Path, DedupeDecision]] = defaultdict(dict)
    for row in opened.scalars(select(decision).where(decision.export_id.in_(finished), decision.source_path.is_not(None))
                              .execution_options(populate_existing=True)):
        decisions[row.export_id][Path(str(row.source_path))] = row
    names: dict[int, set[Path]] = defaultdict(set)
    for owner, name in opened.execute(select(TakeoutEntry.export_id, TakeoutEntry.name).where(TakeoutEntry.export_id.in_(finished),
                                                                                           TakeoutEntry.kind == "media")):
        names[owner].add(Path(name))
    proposals: dict[int, set[Pair]] = defaultdict(set)
    holds: dict[int, str] = {}
    for export, entries in decisions.items():
        by_path = {path: pairing.TakeoutItem(path, "", (path,), None) for path in sorted(set(entries) | names[export])
                   if pairing.special_of(path.parent) != "trash"}
        roles = {path: item for path, row in entries.items() if row.reversed_at is None and row.person_id == person_id and path in by_path
                 and (item := row.takeout_item_id) is not None and item in sources}
        index = pairing.stills_by_stem(by_path)
        claims: dict[Path, list[Path]] = defaultdict(list)
        for path, video in by_path.items():
            if path.suffix.lower() not in media.VIDEO_SUFFIXES:
                continue
            stills = pairing.live_candidates(video, index)
            if len(stills) > 1:
                for ambiguous in (*stills, path):
                    item = roles.get(ambiguous)
                    if item is not None:
                        holds[item] = "live_ambiguous"
            for still in stills:
                claims[still].append(path)
        for still, videos in claims.items():
            ids = [roles[path] for path in (still, *videos) if path in roles]
            if len(videos) != 1:
                holds.update(dict.fromkeys(ids, "live_ambiguous"))
            elif len(ids) == 2:
                still_id, video_id = roles[still], roles[videos[0]]
                if still_id != video_id:
                    pair = Pair(still_id, video_id, export)
                    for item in pair.items:
                        proposals[item].add(pair)
            elif still in entries and videos[0] in entries:
                for item in ids:
                    holds.setdefault(item, "live_partial")
    result = {}
    for item, possibilities in proposals.items():
        if len({pair.items for pair in possibilities}) != 1:
            holds[item] = "live_ambiguous"
    for item, possibilities in proposals.items():
        pair = min(possibilities, key=lambda candidate: candidate.export)
        if any(holds.get(half) == "live_ambiguous" for half in pair.items):
            continue
        result[item] = pair
    return Groups(result, holds)
