from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from darkroom.domain import dedupe, media
from darkroom.domain.ingest import pairing
from darkroom.storage.models import TakeoutEntry, TakeoutSidecar

READY: Final = "ready"
WAITING_SIDECAR: Final = "waiting_sidecar"
WAITING_EXPORT: Final = "waiting_export"
COPY: Final = "copy"
LISTED: Final = "listed"

Verdict = Literal["ready", "waiting_sidecar", "waiting_export"]


@dataclass(frozen=True)
class PartPairing:
    ready: tuple[TakeoutEntry, ...]
    newly_paired: tuple[TakeoutEntry, ...]
    copies: tuple[TakeoutEntry, ...]


def folder_of(entry: TakeoutEntry) -> Path:
    return Path(entry.name).parent


def candidate_of(entry: TakeoutEntry) -> dedupe.TakeoutCandidate:
    return dedupe.TakeoutCandidate(
        source_path=entry.name, sha1=entry.sha1 or "", phash=entry.phash, captured_utc=entry.captured_utc, captured_local=entry.captured_local,
        width=entry.width, height=entry.height, duration_ms=entry.duration_ms, make=entry.make, model=entry.model, is_video=entry.is_video,
        is_raw=Path(entry.name).suffix.lower() in media.CAMERA_RAW_SUFFIXES, is_edited=entry.is_edited, edited_of=entry.edited_of,
    )


def is_tier_1(index: dedupe.PersonIndex, item: dedupe.TakeoutCandidate) -> bool:
    return dedupe.by_sha1(index, item) is not None


def is_tier_2_candidate(index: dedupe.PersonIndex, item: dedupe.TakeoutCandidate) -> bool:
    return any(dedupe.same_shot(item, shot) for shot in index.phone_times.near(item, 0))


def is_trashed(members: Sequence[TakeoutEntry]) -> bool:
    return pairing.special_of_copies([folder_of(member) for member in members]) == pairing.TRASHED


def verdict_for(index: dedupe.PersonIndex, root: TakeoutEntry, members: Sequence[TakeoutEntry], paired: bool) -> Verdict:
    if is_trashed(members):
        return WAITING_EXPORT
    item = candidate_of(root)
    if is_tier_1(index, item):
        return READY
    if root.is_edited:
        return WAITING_EXPORT
    if not paired:
        return WAITING_SIDECAR
    return WAITING_EXPORT if is_tier_2_candidate(index, item) else READY


def attach_copies(rows: Sequence[TakeoutEntry], part_number: int) -> tuple[dict[int, list[TakeoutEntry]], list[TakeoutEntry]]:
    roots_of: dict[str, list[TakeoutEntry]] = {}
    members: dict[int, list[TakeoutEntry]] = {}
    attached: list[TakeoutEntry] = []
    for row in rows:
        if row.copy_of_id is not None and row.copy_of_id in members:
            members[row.copy_of_id].append(row)
            continue
        fresh = row.part_number == part_number and row.outcome == LISTED and row.copy_of_id is None
        if row.sha1 is not None and fresh:
            home = next((root for root in roots_of.get(row.sha1, ()) if folder_of(row) not in {folder_of(m) for m in members[root.id]}), None)
            if home is not None:
                row.outcome, row.copy_of_id = COPY, home.id
                members[home.id].append(row)
                attached.append(row)
                continue
        members[row.id] = [row]
        if row.sha1 is not None:
            roots_of.setdefault(row.sha1, []).append(row)
    return members, attached


def listed_media(entries: Iterable[TakeoutEntry]) -> list[TakeoutEntry]:
    return sorted((entry for entry in entries if entry.kind == "media"), key=lambda entry: entry.id)


def sidecar_choices(rows: Iterable[TakeoutEntry], sidecars: Sequence[TakeoutSidecar]) -> dict[int, int | None]:
    by_path: dict[Path, list[TakeoutEntry]] = {}
    for row in rows:
        by_path.setdefault(Path(row.name), []).append(row)
    folders: dict[Path, list[str]] = {}
    for path in by_path:
        folders.setdefault(path.parent, []).append(path.name)
    known: dict[Path, list[str]] = {}
    texts: dict[Path, str] = {}
    owners: dict[Path, int] = {}
    for sidecar in sorted(sidecars, key=lambda s: (s.part_number, s.id)):
        path = Path(sidecar.folder) / sidecar.name
        known.setdefault(path.parent, []).append(path.name)
        texts.setdefault(path, sidecar.text)
        owners.setdefault(path, sidecar.entry_id)
    digests = {path: str(found[0].id) for path, found in by_path.items()}
    report = pairing.pair_names(sorted(folders.items()), digests, known, reading=texts.get)
    chosen: dict[int, int | None] = {row.id: None for found in by_path.values() for row in found}
    for item in report.items:
        if item.sidecar is not None and not item.sidecar_from_still:
            for row in by_path[item.path]:
                chosen[row.id] = owners.get(item.sidecar)
    return chosen


def own_sidecar(root: TakeoutEntry, group: Sequence[TakeoutEntry]) -> int | None:
    if root.sidecar_entry_id is not None:
        return root.sidecar_entry_id
    return next((member.sidecar_entry_id for member in group if member.sidecar_entry_id is not None), None)


def pair_part(entries: Sequence[TakeoutEntry], sidecars: Sequence[TakeoutSidecar], part_number: int, index: dedupe.PersonIndex) -> PartPairing:
    rows = listed_media(entries)
    members, attached = attach_copies(rows, part_number)
    here = [row for row in rows if row.part_number == part_number and row.sha1 is not None]
    here_ids = {row.id for row in here}
    brings_sidecars = any(sidecar.part_number == part_number for sidecar in sidecars)
    roots = [group[0] for group in members.values()]
    fresh = [root for root in roots if root.part_number == part_number and root.copy_of_id is None and root.outcome == LISTED and root.sha1 is not None]
    waiting = [
        root for root in roots
        if root.copy_of_id is None and root.sidecar_entry_id is None and root.sha1 is not None and root.id not in {f.id for f in fresh}
        and (root.outcome in {WAITING_SIDECAR, WAITING_EXPORT} or root.metadata_pending)
        and (brings_sidecars or any(member.id in here_ids for member in members[root.id]))
    ]
    pairing_rows = {row.id: row for row in here}
    for root in waiting:
        pairing_rows.update((member.id, member) for member in members[root.id] if member.sha1 is not None)
    chosen = sidecar_choices(rows, sidecars)
    for row in pairing_rows.values():
        if row.sidecar_entry_id is None:
            row.sidecar_entry_id = chosen[row.id]
    ready: list[TakeoutEntry] = []
    released: list[TakeoutEntry] = []
    for root in fresh:
        group = members[root.id]
        root.sidecar_entry_id = own_sidecar(root, group)
        ready.extend(decide_state(index, root, group))
    for root in waiting:
        group = members[root.id]
        found = own_sidecar(root, group)
        if found is None:
            continue
        was_waiting = root.outcome == WAITING_SIDECAR
        root.sidecar_entry_id = found
        released.append(root)
        if was_waiting:
            ready.extend(decide_state(index, root, group))
    return PartPairing(
        ready=tuple(sorted(ready, key=lambda r: r.id)), newly_paired=tuple(sorted(released, key=lambda r: r.id)),
        copies=tuple(sorted(attached, key=lambda r: r.id)),
    )


def decide_state(index: dedupe.PersonIndex, root: TakeoutEntry, group: Sequence[TakeoutEntry]) -> list[TakeoutEntry]:
    paired = root.sidecar_entry_id is not None
    verdict = verdict_for(index, root, group, paired)
    root.outcome = LISTED if verdict == READY else verdict
    if verdict != READY:
        return []
    if not paired and is_tier_1(index, candidate_of(root)):
        root.metadata_pending = True
    return [root]
