from __future__ import annotations

import math
import tempfile
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from darkroom.domain import dedupe, events, pixel_folder, xmp
from darkroom.domain.ingest import pairing
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, DuplicateRemoval, Export, ImmichAsset, MediaItem, Person, PixelCopy
from darkroom.storage.paths import Photos

IMMICH_FOLDER = "immich"
OPEN_STATES = ("pending", "refused")
TOMBSTONE = ("person_id", "kept_item_id", "removed_item_id", "kept_path", "kept_sha1", "removed_path", "removed_sha1", "removed_source", "rule", "tier", "created_at")


@dataclass
class Resolved:
    removed: int = 0
    kept_both: int = 0
    refused: int = 0
    failed: int = 0


@dataclass(frozen=True)
class Tools:
    read: xmp.Reader = exiftool.read
    write: xmp.Writer = exiftool.write_xmp


@dataclass(frozen=True)
class Pair:
    takeout: MediaItem
    decision: DedupeDecision
    phone: MediaItem
    tier: int


def pixels(item: MediaItem) -> int:
    return (item.width or 0) * (item.height or 0)


def rule_for(phone: MediaItem, takeout: MediaItem) -> str:
    if phone.sha1 == takeout.sha1:
        return "identical"
    return "takeout_better" if phone.width and phone.height and pixels(takeout) > pixels(phone) else "phone_better"


def resolved_away(opened: Session, decision_id: int) -> bool:
    return opened.execute(
        select(DuplicateRemoval.id).where(DuplicateRemoval.decision_id == decision_id, DuplicateRemoval.state == "removed").limit(1)
    ).first() is not None


def held_back(opened: Session, person: Person) -> set[int]:
    rows = opened.execute(
        select(DuplicateRemoval.kept_item_id, DuplicateRemoval.removed_item_id).where(DuplicateRemoval.person_id == person.id, DuplicateRemoval.state.in_(OPEN_STATES))
    ).all()
    return {item_id for row in rows for item_id in row if item_id is not None}


def placed_takeouts(opened: Session, person: Person) -> list[MediaItem]:
    return list(opened.execute(
        select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "takeout", MediaItem.path.startswith(f"{person.slug}/google-only/")).order_by(MediaItem.id)
    ).scalars())


def google_only(opened: Session, person: Person) -> list[tuple[MediaItem, DedupeDecision]]:
    return [(item, decision) for item in placed_takeouts(opened, person) if (decision := removable_takeout(opened, person, item)) is not None]


def removable_takeout(opened: Session, person: Person, item: MediaItem) -> DedupeDecision | None:
    if item.person_id != person.id or item.source != "takeout" or not item.path.startswith(f"{person.slug}/google-only/"):
        return None
    decisions = decisions_of(opened, item)
    matched = opened.execute(select(DedupeDecision.id).where(DedupeDecision.matched_item_id == item.id).limit(1)).first()
    qualifying = decisions and matched is None and all(
        decision.person_id == person.id and decision.tier == 5 and decision.decided_by == "auto" and decision.reversed_at is None and not edited(decision)
        for decision in decisions
    )
    return decisions[0] if qualifying else None


def decisions_of(opened: Session, item: MediaItem) -> list[DedupeDecision]:
    return list(opened.execute(select(DedupeDecision).where(DedupeDecision.takeout_item_id == item.id).order_by(DedupeDecision.id)).scalars())


def immich_originals(opened: Session, person: Person, item_id: int | None = None) -> dict[int, MediaItem]:
    query = select(MediaItem).join(ImmichAsset, ImmichAsset.original_item_id == MediaItem.id).where(
        ImmichAsset.person_id == person.id, ImmichAsset.status == "stored", MediaItem.person_id == person.id, MediaItem.source == "phone",
        MediaItem.path.startswith(f"{person.slug}/originals/{IMMICH_FOLDER}/"),
    )
    found = opened.execute(query if item_id is None else query.where(MediaItem.id == item_id)).scalars().unique()
    return {item.id: item for item in found}


def candidate_of(opened: Session, item: MediaItem) -> dedupe.TakeoutCandidate:
    shot = dedupe.shot_of(item)
    return dedupe.TakeoutCandidate(
        source_path=item.path, sha1=item.sha1, phash=item.phash, captured_utc=item.captured_utc, captured_local=item.captured_local,
        width=item.width, height=item.height, duration_ms=item.duration_ms, make=item.make, model=item.model, is_video=shot.is_video,
        is_edited=any(edited(decision) for decision in decisions_of(opened, item)),
    )


def edited(decision: DedupeDecision) -> bool:
    return decision.source_path is None or pairing.original_name(Path(decision.source_path)) is not None


def pairs(opened: Session, person: Person) -> list[Pair]:
    held = held_back(opened, person)
    takeouts = [(item, decision) for item, decision in google_only(opened, person) if item.id not in held]
    originals = {item_id: item for item_id, item in immich_originals(opened, person).items() if item_id not in held}
    if not takeouts or not originals:
        return []
    phone = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone").order_by(MediaItem.id)).scalars()
    index = dedupe.PersonIndex(0, person.id, frozenset({person.id}), tuple(dedupe.shot_of(item) for item in phone))
    by_path = {item.path: (item, decision) for item, decision in takeouts}
    decided = dedupe.decide_all(index, [candidate_of(opened, item) for item in placed_takeouts(opened, person)], dedupe.DedupeSettings())
    matched = [d for d in decided if d.tier in dedupe.CONFIRMING_TIERS and d.matched_source == "phone" and d.matched_item_id in originals and d.source_path in by_path]
    shared = Counter(d.matched_item_id for d in matched)
    return [Pair(*by_path[d.source_path], originals[d.matched_item_id], d.tier) for d in matched
            if d.matched_item_id is not None and (d.tier == 1 or shared[d.matched_item_id] == 1)]


def phone_may_go(opened: Session, person: Person, phone: MediaItem, takeout: MediaItem) -> bool:
    matched = opened.execute(select(DedupeDecision.id).where(DedupeDecision.matched_item_id == phone.id).limit(1)).first()
    sent = opened.execute(select(PixelCopy.id).where(PixelCopy.item_id == phone.id, PixelCopy.state != "queued").limit(1)).first()
    export = None if takeout.export_id is None else opened.get(Export, takeout.export_id)
    accounts = {pixel.google_account_id for pixel in pixel_folder.pixels_serving(opened, person.id)}
    return matched is None and sent is None and not phone.confirmed_in_google and export is not None and accounts <= {export.account_id}


def resolve(opened: Session, photos: Photos, person: Person, pair: Pair, now: float, resolved: Resolved, tools: Tools) -> None:
    rule = rule_for(pair.phone, pair.takeout)
    keeper, loser = (pair.takeout, pair.phone) if rule == "takeout_better" else (pair.phone, pair.takeout)
    if loser is pair.phone and not phone_may_go(opened, person, pair.phone, pair.takeout):
        resolved.kept_both += 1
        return
    row = DuplicateRemoval(
        person_id=person.id, decision_id=pair.decision.id if loser is pair.takeout else None, kept_item_id=keeper.id, removed_item_id=loser.id,
        kept_path=keeper.path, kept_sha1=keeper.sha1, removed_path=loser.path, removed_sha1=loser.sha1, removed_source=loser.source,
        rule=rule, tier=pair.tier, state="pending", created_at=now,
    )
    opened.add(row)
    opened.commit()
    finish(opened, photos, person, row, now, resolved, tools)


def refuse(opened: Session, photos: Photos, person: Person, row: DuplicateRemoval, reason: str, now: float, resolved: Resolved) -> None:
    with suppress(paths.NotALosingCopy, paths.OutsideRoot, OSError):
        paths.restore_losing_copy(photos, person.slug, row.removed_path, row.removed_sha1)
    at_name, claimed = loser_state(photos, person, row)
    if claimed or not at_name:
        fail(opened, person, row, "LoserNotBackAtItsName" if claimed else "LoserAlreadyGone", now, resolved)
        return
    row.state, row.reason = "refused", reason
    resolved.refused += 1
    events.record(opened, now, "duplicate_removal_refused", person.slug, f"{row.removed_source} copy, {row.rule}: {reason}")
    opened.commit()


def still_a_pair(opened: Session, person: Person, keeper: MediaItem, loser: MediaItem) -> bool:
    takeout, phone = (keeper, loser) if loser.source == "phone" else (loser, keeper)
    if removable_takeout(opened, person, takeout) is None or not immich_originals(opened, person, phone.id):
        return False
    return loser is not phone or phone_may_go(opened, person, phone, takeout)


def neighbours(opened: Session, person: Person, source: str, item: MediaItem) -> list[MediaItem]:
    when: list[ColumnElement[bool]] = [] if item.captured_utc is None else [MediaItem.captured_utc.between(math.floor(item.captured_utc) - 1, math.floor(item.captured_utc) + 2)]
    when += [MediaItem.captured_local.startswith(item.captured_local[:19])] if item.captured_local else []
    if not when:
        return []
    return list(opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == source, or_(*when))).scalars())


def still_matched(opened: Session, person: Person, takeout: MediaItem, phone: MediaItem, row: DuplicateRemoval) -> bool:
    if rule_for(phone, takeout) != row.rule:
        return False
    shots = {item.id: item for item in [*neighbours(opened, person, "phone", takeout), phone]}
    peers = [item for item in neighbours(opened, person, "takeout", takeout) if item.id != takeout.id and item.path.startswith(f"{person.slug}/google-only/")] + [takeout]
    index = dedupe.PersonIndex(0, person.id, frozenset({person.id}), tuple(dedupe.shot_of(item) for item in shots.values()))
    decided = dedupe.decide_all(index, [candidate_of(opened, item) for item in peers], dedupe.DedupeSettings())
    mine = [d for d in decided if d.source_path == takeout.path]
    rivals = [d for d in decided if d.source_path != takeout.path and d.tier == 2 and d.matched_item_id == phone.id]
    return bool(mine) and mine[0].tier == row.tier and mine[0].matched_item_id == phone.id and (row.tier == 1 or not rivals)


def carrier(loser: MediaItem, tools: Tools) -> Callable[[paths.Keeper], bytes | None] | None:
    if loser.source != "takeout":
        return None
    google = xmp.GoogleMetadata.of(loser.sidecar_json)

    def carry(pinned: paths.Keeper) -> bytes | None:
        try:
            with tempfile.TemporaryDirectory() as folder:
                before, staged = pinned.sidecar(), Path(folder) / f"{pinned.name}{paths.SIDECAR_SUFFIX}"
                if before is not None:
                    staged.write_bytes(before)
                if not gaps_left(pinned.path, staged, google, tools):
                    return before
                tools.write(staged, gaps_left(pinned.path, staged, google, tools))
                if gaps_left(pinned.path, staged, google, tools):
                    raise paths.NotALosingCopy("Google's metadata could not be carried to the kept copy")
                published = staged.read_bytes()
                pinned.write_sidecar(published, before)
                return published
        except (exiftool.WriteFailed, exiftool.Unreadable, paths.NotASidecar, OSError, ValueError) as error:
            raise paths.NotALosingCopy("the kept copy's sidecar could not be written") from error

    return carry


def gaps_left(media_path: Path, staged: Path, google: xmp.GoogleMetadata, tools: Tools) -> dict[str, str]:
    present = staged.exists()
    tags = tools.read([media_path, staged] if present else [media_path])
    own, existing = tags.get(media_path), tags.get(staged, {}) if present else {}
    if own is None or "Error" in own or "Error" in existing:
        raise ValueError("the kept copy's metadata could not be read")
    return xmp.gaps(own, existing, google, False)


def finish(opened: Session, photos: Photos, person: Person, row: DuplicateRemoval, now: float, resolved: Resolved, tools: Tools) -> None:
    opened.commit()
    opened.execute(update(DuplicateRemoval).where(DuplicateRemoval.id == row.id).values(id=DuplicateRemoval.id))
    opened.refresh(row)
    keeper = None if row.kept_item_id is None else opened.get(MediaItem, row.kept_item_id, populate_existing=True)
    loser = None if row.removed_item_id is None else opened.get(MediaItem, row.removed_item_id, populate_existing=True)
    if loser is not None and (loser.path, loser.sha1) == (row.removed_path, row.removed_sha1) and not loser_present(photos, person, row):
        finish_gone(opened, photos, person, row, loser, now, resolved, tools)
        return
    if keeper is None or loser is None or (keeper.path, keeper.sha1) != (row.kept_path, row.kept_sha1) or (loser.path, loser.sha1) != (row.removed_path, row.removed_sha1):
        refuse(opened, photos, person, row, "the records changed", now, resolved)
        return
    takeout, phone = (keeper, loser) if loser.source == "phone" else (loser, keeper)
    if not still_a_pair(opened, person, keeper, loser) or not still_matched(opened, person, takeout, phone, row):
        refuse(opened, photos, person, row, "the pair is no longer safe to resolve", now, resolved)
        return
    try:
        paths.remove_losing_copy(photos, person.slug, row.removed_path, row.removed_sha1, row.kept_path, row.kept_sha1, carrier(loser, tools))
    except paths.KeeperBusy:
        opened.rollback()
        fail(opened, person, row, "KeeperBusy", now, resolved)
        return
    except (paths.NotALosingCopy, paths.OutsideRoot) as error:
        refuse(opened, photos, person, row, str(error), now, resolved)
        return
    except OSError as error:
        opened.rollback()
        fail(opened, person, row, type(error).__name__, now, resolved)
        return
    complete(opened, person, row, loser, now, resolved)


def loser_state(photos: Photos, person: Person, row: DuplicateRemoval) -> tuple[bool, bool]:
    try:
        return paths.losing_copy_state(photos, person.slug, row.removed_path, row.removed_sha1)
    except (paths.NotALosingCopy, paths.OutsideRoot, OSError):
        return True, True


def loser_present(photos: Photos, person: Person, row: DuplicateRemoval) -> bool:
    return any(loser_state(photos, person, row))


def fail(opened: Session, person: Person, row: DuplicateRemoval, reason: str, now: float, resolved: Resolved) -> None:
    resolved.failed += 1
    events.record(opened, now, "duplicate_removal_failed", person.slug, f"{row.removed_source} copy, {row.rule}: {reason}")
    opened.commit()


def finish_gone(opened: Session, photos: Photos, person: Person, row: DuplicateRemoval, loser: MediaItem, now: float, resolved: Resolved,
                tools: Tools) -> None:
    try:
        paths.remove_losing_copy(photos, person.slug, row.removed_path, row.removed_sha1, row.kept_path, row.kept_sha1, carrier(loser, tools))
    except (paths.NotALosingCopy, paths.OutsideRoot, OSError) as error:
        fail(opened, person, row, type(error).__name__, now, resolved)
        return
    complete(opened, person, row, loser, now, resolved)


def complete(opened: Session, person: Person, row: DuplicateRemoval, loser: MediaItem, now: float, resolved: Resolved) -> None:
    for asset in opened.execute(select(ImmichAsset).where(ImmichAsset.original_item_id == loser.id)).scalars():
        asset.original_item_id = None
    for other in decisions_of(opened, loser) if loser.source == "takeout" else []:
        if other.id != row.decision_id:
            opened.add(DuplicateRemoval(**{column: getattr(row, column) for column in TOMBSTONE}, decision_id=other.id, state="removed", removed_at=now))
    opened.delete(loser)
    row.state, row.removed_at = "removed", now
    resolved.removed += 1
    events.record(opened, now, "duplicate_removed", person.slug, f"{row.removed_source} copy, {row.rule}, tier {row.tier}")
    opened.commit()


def resolve_person(opened: Session, photos: Photos, person: Person, now: float, tools: Tools | None = None) -> Resolved:
    tools = tools or Tools()
    resolved = Resolved()
    pending = opened.execute(
        select(DuplicateRemoval).where(DuplicateRemoval.person_id == person.id, DuplicateRemoval.state == "pending").order_by(DuplicateRemoval.id)
    ).scalars().all()
    for row in pending:
        finish(opened, photos, person, row, now, resolved, tools)
    for pair in pairs(opened, person):
        if pair.phone not in opened or pair.takeout not in opened:
            continue
        resolve(opened, photos, person, pair, now, resolved, tools)
    return resolved
