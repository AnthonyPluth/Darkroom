from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, events, pixel_folder, xmp
from darkroom.domain.ingest import pairing
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, DuplicateRemoval, Export, ImmichAsset, MediaItem, Person, PixelCopy
from darkroom.storage.paths import Photos

IMMICH_FOLDER = "immich"
OPEN_STATES = ("pending", "refused")


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


def google_only(opened: Session, person: Person) -> list[tuple[MediaItem, DedupeDecision]]:
    found = opened.execute(
        select(MediaItem, DedupeDecision).join(DedupeDecision, DedupeDecision.takeout_item_id == MediaItem.id).where(
            MediaItem.person_id == person.id, MediaItem.source == "takeout", MediaItem.path.startswith(f"{person.slug}/google-only/"),
            DedupeDecision.person_id == person.id, DedupeDecision.tier == 5, DedupeDecision.decided_by == "auto", DedupeDecision.reversed_at.is_(None),
        ).order_by(MediaItem.id, DedupeDecision.id)
    ).all()
    first: dict[int, tuple[MediaItem, DedupeDecision]] = {}
    for item, decision in found:
        first.setdefault(item.id, (item, decision))
    return [(item, decision) for item, decision in first.values() if not doubted(opened, person, item)]


def doubted(opened: Session, person: Person, item: MediaItem) -> bool:
    owner = opened.execute(select(DedupeDecision.id).where(DedupeDecision.matched_item_id == item.id, DedupeDecision.decided_by == "owner").limit(1)).first()
    return owner is not None or not all(
        decision.person_id == person.id and decision.tier == 5 and decision.decided_by == "auto" and decision.reversed_at is None and not edited(decision)
        for decision in decisions_of(opened, item)
    )


def decisions_of(opened: Session, item: MediaItem) -> list[DedupeDecision]:
    return list(opened.execute(select(DedupeDecision).where(DedupeDecision.takeout_item_id == item.id)).scalars())


def immich_originals(opened: Session, person: Person) -> dict[int, MediaItem]:
    found = opened.execute(
        select(MediaItem).join(ImmichAsset, ImmichAsset.original_item_id == MediaItem.id).where(
            ImmichAsset.person_id == person.id, ImmichAsset.status == "stored", MediaItem.person_id == person.id, MediaItem.source == "phone",
            MediaItem.path.startswith(f"{person.slug}/originals/{IMMICH_FOLDER}/"),
        )
    ).scalars().unique()
    return {item.id: item for item in found}


def candidate_of(item: MediaItem) -> dedupe.TakeoutCandidate:
    shot = dedupe.shot_of(item)
    return dedupe.TakeoutCandidate(
        source_path=item.path, sha1=item.sha1, phash=item.phash, captured_utc=item.captured_utc, captured_local=item.captured_local,
        width=item.width, height=item.height, duration_ms=item.duration_ms, make=item.make, model=item.model, is_video=shot.is_video,
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
    decided = dedupe.decide_all(index, [candidate_of(item) for item, _ in takeouts], dedupe.DedupeSettings())
    matched = [d for d in decided if d.tier in dedupe.CONFIRMING_TIERS and d.matched_source == "phone" and d.matched_item_id in originals]
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


def refuse(opened: Session, person: Person, row: DuplicateRemoval, reason: str, now: float, resolved: Resolved) -> None:
    row.state, row.reason = "refused", reason
    resolved.refused += 1
    events.record(opened, now, "duplicate_removal_refused", person.slug, f"{row.removed_source} copy, {row.rule}: {reason}")
    opened.commit()


def still_a_pair(opened: Session, person: Person, keeper: MediaItem, loser: MediaItem) -> bool:
    takeout, phone = (keeper, loser) if loser.source == "phone" else (loser, keeper)
    eligible = {item.id for item, _ in google_only(opened, person)}
    if takeout.id not in eligible or phone.id not in immich_originals(opened, person):
        return False
    return loser is not phone or phone_may_go(opened, person, phone, takeout)


def carry_metadata(photos: Photos, keeper: MediaItem, loser: MediaItem, tools: Tools) -> str | None:
    if loser.source != "takeout":
        return None
    google = xmp.GoogleMetadata.of(loser.sidecar_json)
    try:
        media_path = photos.inside(*keeper.path.split("/"))
        xmp.fill(media_path, google, False, tools.read, tools.write)
        sidecar = paths.sidecar_of(media_path)
        present = sidecar.exists() or sidecar.is_symlink()
        if present:
            paths.checked_sidecar(sidecar)
        tags = tools.read([media_path, sidecar] if present else [media_path])
    except (exiftool.WriteFailed, exiftool.Unreadable, paths.NotASidecar, paths.OutsideRoot, OSError):
        return "the kept copy's sidecar could not be written"
    own, existing = tags.get(media_path), tags.get(sidecar, {}) if present else {}
    if own is None or "Error" in own or "Error" in existing or xmp.gaps(own, existing, google, False):
        return "Google's metadata could not be carried to the kept copy"
    return None


def finish(opened: Session, photos: Photos, person: Person, row: DuplicateRemoval, now: float, resolved: Resolved, tools: Tools) -> None:
    opened.commit()
    opened.execute(update(DuplicateRemoval).where(DuplicateRemoval.id == row.id).values(id=DuplicateRemoval.id))
    opened.refresh(row)
    keeper = None if row.kept_item_id is None else opened.get(MediaItem, row.kept_item_id, populate_existing=True)
    loser = None if row.removed_item_id is None else opened.get(MediaItem, row.removed_item_id, populate_existing=True)
    if keeper is None or loser is None or (keeper.path, keeper.sha1) != (row.kept_path, row.kept_sha1) or (loser.path, loser.sha1) != (row.removed_path, row.removed_sha1):
        refuse(opened, person, row, "the records changed", now, resolved)
        return
    if not still_a_pair(opened, person, keeper, loser):
        refuse(opened, person, row, "the pair is no longer safe to resolve", now, resolved)
        return
    unsaved = carry_metadata(photos, keeper, loser, tools)
    if unsaved is not None:
        refuse(opened, person, row, unsaved, now, resolved)
        return
    try:
        paths.remove_losing_copy(photos, person.slug, row.removed_path, row.removed_sha1, row.kept_path, row.kept_sha1)
    except (paths.NotALosingCopy, paths.OutsideRoot) as error:
        refuse(opened, person, row, str(error), now, resolved)
        return
    except OSError as error:
        opened.rollback()
        resolved.failed += 1
        events.record(opened, now, "duplicate_removal_failed", person.slug, f"{row.removed_source} copy, {row.rule}: {type(error).__name__}")
        opened.commit()
        return
    for asset in opened.execute(select(ImmichAsset).where(ImmichAsset.original_item_id == loser.id)).scalars():
        asset.original_item_id = None
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
