from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_metadata, media, xmp
from darkroom.domain.ingest import pairing
from darkroom.storage import db
from darkroom.storage.models import ManagedCompound, ManagedCompoundMember, ManagedImportReceipt, TakeoutEntry

Hold = Literal["live_partial", "live_ambiguous", "embedded_motion"]
MemberRole = Literal["live_still", "live_video"]
KIND: Final = "live_photo"
CONFLICT: Final = "compound_conflict"
UNUSABLE: Final = "unusable"
TAKEOUT_LIVE: Final = "takeout-live:"
TAKEOUT_ENTRY: Final = "takeout-entry:"
EMBEDDED_MOTION_TAGS: Final = frozenset({"MotionPhoto", "MicroVideo", "MotionPhotoVideo", "EmbeddedVideoFile", "EmbeddedVideoType"})
REASONS: Final[dict[Hold, str]] = {
    "live_partial": "one half of the Live Photo is not going to Immich, so the other half is kept in the inbox",
    "live_ambiguous": "the Live Photo's halves cannot be told apart, so they are kept in the inbox",
    "embedded_motion": "the still carries its own motion video, which Immich's handling of is not verified, so the Live Photo is kept in the inbox",
}


class Refused(RuntimeError):
    pass


class Member(Protocol):
    @property
    def item(self) -> pairing.TakeoutItem: ...

    @property
    def root(self) -> TakeoutEntry: ...


class Canonical(Protocol):
    @property
    def report(self) -> pairing.PairingReport: ...

    @property
    def units(self) -> Sequence[Member]: ...

    @property
    def trashed(self) -> Sequence[Member]: ...


@dataclass(frozen=True)
class Pair:
    still: Member
    video: Member

    @property
    def key(self) -> str:
        return f"{TAKEOUT_LIVE}{self.still.root.id}"


@dataclass(frozen=True)
class Grouping:
    pairs: tuple[Pair, ...]
    held: Mapping[int, Hold] = field(default_factory=dict)


@dataclass(frozen=True)
class Planned:
    resource_key: str
    role: MemberRole
    sha1: str
    bytes: int
    intended: managed_metadata.Intended = field(repr=False)


@dataclass(frozen=True)
class Frozen:
    compound_id: int
    hold: str | None
    receipts: Mapping[str, int]


def is_video(member: Member) -> bool:
    return member.item.path.suffix.lower() in media.VIDEO_SUFFIXES


def ambiguous(canon: Canonical) -> set[int]:
    by_copy = {copy: item for item in canon.report.items for copy in item.copies}
    index = pairing.stills_by_stem(by_copy)
    by_path = {unit.item.path: unit for unit in canon.units}
    found: set[int] = set()
    for unit in canon.units:
        if not is_video(unit) or unit.item.live_pair_of is not None:
            continue
        stills = pairing.live_candidates(unit.item, index)
        if len(stills) > 1:
            found |= {unit.root.id, *(by_path[path].root.id for path in stills if path in by_path)}
    return found


def groups(canon: Canonical) -> Grouping:
    by_path = {unit.item.path: unit for unit in canon.units}
    trashed = {unit.root.id for unit in canon.trashed}
    claims: dict[Path, list[Member]] = {}
    for unit in (*canon.units, *canon.trashed):
        if is_video(unit) and unit.item.live_pair_of is not None:
            claims.setdefault(unit.item.live_pair_of, []).append(unit)
    tangled = ambiguous(canon)
    partial: set[int] = set()
    candidates: list[Pair] = []
    for path, videos in sorted(claims.items()):
        still = by_path.get(path)
        alive = [video for video in videos if video.root.id not in trashed]
        if len(videos) > 1:
            tangled |= {video.root.id for video in alive} | ({still.root.id} if still is not None else set())
        elif still is None:
            partial |= {video.root.id for video in alive}
        elif not alive:
            partial.add(still.root.id)
        else:
            candidates.append(Pair(still, alive[0]))
    pairs = []
    for pair in candidates:
        if {pair.still.root.id, pair.video.root.id} & tangled:
            tangled |= {pair.still.root.id, pair.video.root.id}
        else:
            pairs.append(pair)
    held: dict[int, Hold] = {root: "live_partial" for root in partial - tangled}
    held |= {root: "live_ambiguous" for root in tangled}
    return Grouping(tuple(sorted(pairs, key=lambda pair: pair.still.root.id)), held)


def embedded_motion(tags: Mapping[str, object]) -> bool:
    for key, value in tags.items():
        group, _, name = key.rpartition(":")
        if group == "Trailer" or (name in EMBEDDED_MOTION_TAGS and value not in (None, 0, "0", "", False)):
            return True
    return False


def takeout_key(entry: TakeoutEntry) -> str:
    return f"{TAKEOUT_ENTRY}{entry.id}"


def planned(member: Member, role: MemberRole, tags: dict[str, object] | None, sidecar: str | None, borrowed: bool) -> Planned | None:
    if tags is None or "Error" in tags or member.root.sha1 is None:
        return None
    kind: managed_metadata.Kind = "VIDEO" if is_video(member) else "IMAGE"
    intended = managed_metadata.intend(tags, xmp.GoogleMetadata.of(sidecar), kind, role, corrects=not borrowed)
    return Planned(takeout_key(member.root), role, member.root.sha1, member.root.bytes, intended)


def checked(members: Sequence[Planned]) -> tuple[Planned, Planned]:
    by_role = {member.role: member for member in members}
    if len(members) != 2 or set(by_role) != {"live_still", "live_video"} or len({member.resource_key for member in members}) != 2:
        raise Refused("a Live Photo is exactly one still and one video")
    still, video = by_role["live_still"], by_role["live_video"]
    for member, kind in ((still, "IMAGE"), (video, "VIDEO")):
        if member.intended.role != member.role or member.intended.kind != kind or len(member.sha1) != 40 or member.bytes <= 0 or not member.resource_key:
            raise Refused("a half of the Live Photo does not match its role")
    return still, video


def frozen(opened: Session, compound: ManagedCompound) -> Frozen:
    member = ManagedCompoundMember
    keys = list(opened.execute(select(member.resource_key).where(member.compound_id == compound.id)).scalars())
    receipt = ManagedImportReceipt
    found = opened.execute(select(receipt.resource_key, receipt.id).where(receipt.intent_id == compound.intent_id, receipt.compound_key == compound.compound_key,
                                                                          receipt.resource_key.in_(keys))).all()
    return Frozen(compound.id, compound.hold, {key: receipt_id for key, receipt_id in found})


def freeze(opened: Session, intent_id: int, compound_key: str, members: Sequence[Planned], now: float, wanted: Callable[[], bool]) -> Frozen:
    still, video = checked(members)
    if not compound_key:
        raise Refused("a Live Photo needs a compound key")
    db.write_lock(opened)
    existing = opened.execute(select(ManagedCompound).where(ManagedCompound.intent_id == intent_id, ManagedCompound.compound_key == compound_key)
                              .execution_options(populate_existing=True)).scalar_one_or_none()
    if existing is not None:
        result = frozen(opened, existing)
        opened.commit()
        return result
    keys = [still.resource_key, video.resource_key]
    receipt, member = ManagedImportReceipt, ManagedCompoundMember
    taken = opened.execute(select(receipt.id).where(receipt.intent_id == intent_id, or_(receipt.resource_key.in_(keys), receipt.compound_key.in_([*keys, compound_key])))
                           .limit(1)).first()
    named = opened.execute(select(member.id).where(member.intent_id == intent_id, member.resource_key.in_(keys)).limit(1)).first()
    if taken is not None or named is not None:
        opened.rollback()
        raise Refused("a half of the Live Photo already has a receipt or belongs to another compound")
    if not wanted():
        opened.rollback()
        raise Refused("a half of the Live Photo is no longer wanted")
    hold = next((half.intended.hold for half in (still, video) if half.intended.hold in managed_metadata.HELD_BEFORE_UPLOAD), None)
    compound = ManagedCompound(intent_id=intent_id, compound_key=compound_key, kind=KIND, hold=hold, frozen_at=now)
    opened.add(compound)
    opened.flush()
    for half in (still, video):
        opened.add(ManagedCompoundMember(compound_id=compound.id, intent_id=intent_id, resource_key=half.resource_key, role=half.role, source_sha1=half.sha1,
                                         expected_bytes=half.bytes))
        opened.add(ManagedImportReceipt(intent_id=intent_id, resource_key=half.resource_key, compound_key=compound_key, source_sha1=half.sha1,
                                        expected_bytes=half.bytes, expected_metadata=managed_metadata.snapshot(half.intended), updated_at=now))
    opened.flush()
    result = frozen(opened, compound)
    opened.commit()
    return result


def settled(opened: Session, intent_id: int, compound_key: str) -> ManagedCompound | None:
    query = select(ManagedCompound).where(ManagedCompound.intent_id == intent_id, ManagedCompound.compound_key == compound_key)
    found = opened.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    if found is not None:
        opened.expunge(found)
    opened.commit()
    return found


def set_hold(opened: Session, intent_id: int, compound_key: str, reason: str) -> None:
    opened.commit()
    db.write_lock(opened)
    opened.connection().execute(update(ManagedCompound).where(ManagedCompound.intent_id == intent_id, ManagedCompound.compound_key == compound_key,
                                                              ManagedCompound.committed_at.is_(None), ManagedCompound.hold.is_(None)).values(hold=reason))
    opened.commit()
