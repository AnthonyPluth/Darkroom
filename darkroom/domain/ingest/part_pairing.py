from __future__ import annotations

import dataclasses
from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from darkroom.domain import dedupe, media
from darkroom.domain.ingest import metadata, pairing, sidecar
from darkroom.storage.models import TakeoutEntry, TakeoutSidecar

READY: Final = "ready"
WAITING_SIDECAR: Final = "waiting_sidecar"
WAITING_EXPORT: Final = "waiting_export"
COPY: Final = "copy"
LISTED: Final = "listed"
UNDECIDED: Final = frozenset({LISTED, WAITING_SIDECAR, WAITING_EXPORT})
NAME_ONLY_RULES: Final = (pairing.exact, pairing.supplemental, pairing.counter)

ANY_ZONE_MINUTES: Final = 16 * 60

Verdict = Literal["ready", "waiting_sidecar", "waiting_export"]
Moment = tuple[float | None, str | None]


@dataclass(frozen=True)
class Choice:
    entry_id: int | None
    settled: bool


NO_CHOICE: Final = Choice(None, settled=False)


@dataclass(frozen=True)
class PartPairing:
    ready: tuple[TakeoutEntry, ...]
    newly_paired: tuple[TakeoutEntry, ...]
    copies: tuple[TakeoutEntry, ...]
    metadata_due: tuple[TakeoutEntry, ...]


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


def wall_clock(utc: float, found: sidecar.Sidecar) -> str | None:
    return media.wall_clock_at(utc, found.lat, found.lon)[0]


def sidecar_moments(entry: TakeoutEntry, text: str | None, walls: Container[datetime] = frozenset()) -> list[Moment]:
    found = sidecar.parse(text) if text else None
    if found is None:
        return []
    moments: list[Moment] = []
    if entry.captured_utc is not None:
        moments.append((entry.captured_utc, wall_clock(entry.captured_utc, found)))
    moment = metadata.moment_of(found)
    if moment is None or found.photo_taken_utc is None:
        return moments
    if entry.captured_local is not None:
        derived = metadata.zone_offset(entry.captured_local, moment)
        utc = None if derived is None else media.utc_or_none(datetime.fromisoformat(entry.captured_local), derived)
        if utc is not None:
            moments.append((utc, entry.captured_local))
    moments.extend([(found.photo_taken_utc, entry.captured_local), (found.photo_taken_utc, wall_clock(found.photo_taken_utc, found))])
    if found.lat is None or found.lon is None:
        moments.extend(any_zone(walls, found.photo_taken_utc, moment))
    return moments


def any_zone(walls: Container[datetime], utc: float, moment: datetime) -> list[Moment]:
    start = moment.replace(tzinfo=None, microsecond=0)
    steps = (start + timedelta(minutes=minutes) for minutes in range(-ANY_ZONE_MINUTES, ANY_ZONE_MINUTES + 1))
    return [(utc, wall.isoformat(timespec="seconds")) for wall in steps if wall in walls]


def is_tier_2_candidate(index: dedupe.PersonIndex, item: dedupe.TakeoutCandidate, moments: Iterable[Moment] = ()) -> bool:
    timed = [item] + [dataclasses.replace(item, captured_utc=utc, captured_local=local) for utc, local in moments]
    return any(dedupe.same_shot(each, shot) for each in timed for shot in index.phone_times.near(each, 0))


def is_trashed(members: Sequence[TakeoutEntry]) -> bool:
    return pairing.special_of_copies([folder_of(member) for member in members]) == pairing.TRASHED


def verdict_for(index: dedupe.PersonIndex, root: TakeoutEntry, members: Sequence[TakeoutEntry], choice: Choice, text: str | None) -> Verdict:
    if is_trashed(members):
        return WAITING_EXPORT
    item = candidate_of(root)
    if is_tier_1(index, item):
        return READY
    if root.is_edited:
        return WAITING_EXPORT
    if choice.entry_id is None:
        return WAITING_SIDECAR
    if is_tier_2_candidate(index, item, sidecar_moments(root, text, index.phone_times.by_local)):
        return WAITING_EXPORT
    return READY if choice.settled else WAITING_SIDECAR


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


def named_alone(media_path: Path, found: Path) -> bool:
    return found.parent == media_path.parent and any(rule(media_path, {found.name: found}) == found for rule in NAME_ONLY_RULES)


def sidecar_choices(rows: Iterable[TakeoutEntry], sidecars: Sequence[TakeoutSidecar]) -> dict[int, Choice]:
    by_path: dict[Path, list[TakeoutEntry]] = {}
    for row in rows:
        by_path.setdefault(Path(row.name), []).append(row)
    folders: dict[Path, list[str]] = {}
    for path in by_path:
        folders.setdefault(path.parent, []).append(path.name)
    known: dict[Path, list[str]] = {}
    texts: dict[Path, str] = {}
    owners: dict[Path, int] = {}
    for found in sorted(sidecars, key=lambda s: (s.part_number, s.id)):
        path = Path(found.folder) / found.name
        known.setdefault(path.parent, []).append(path.name)
        texts.setdefault(path, found.text)
        owners.setdefault(path, found.entry_id)
    digests = {path: str(found[0].id) for path, found in by_path.items()}
    report = pairing.pair_names(sorted(folders.items()), digests, known, reading=texts.get)
    chosen: dict[int, Choice] = {row.id: NO_CHOICE for found in by_path.values() for row in found}
    for item in report.items:
        if item.sidecar is not None and not item.sidecar_from_still:
            for row in by_path[item.path]:
                chosen[row.id] = Choice(owners.get(item.sidecar), named_alone(item.path, item.sidecar))
    return chosen


def choice_for(group: Sequence[TakeoutEntry], chosen: dict[int, Choice]) -> Choice:
    return next((chosen[member.id] for member in group if chosen[member.id].entry_id is not None), NO_CHOICE)


def is_undecided(root: TakeoutEntry, part_number: int) -> bool:
    if root.decision_id is not None or root.outcome not in UNDECIDED:
        return False
    return root.part_number == part_number or was_seen(root)


def was_seen(root: TakeoutEntry) -> bool:
    return root.outcome != LISTED or root.sidecar_entry_id is not None or root.metadata_pending


def pair_part(entries: Sequence[TakeoutEntry], sidecars: Sequence[TakeoutSidecar], part_number: int, index: dedupe.PersonIndex) -> PartPairing:
    rows = listed_media(entries)
    members, attached = attach_copies(rows, part_number)
    chosen = sidecar_choices(rows, sidecars)
    texts = {found.entry_id: found.text for found in sidecars}
    for row in rows:
        if row.part_number == part_number and row.outcome == COPY:
            row.sidecar_entry_id = chosen[row.id].entry_id
    ready: list[TakeoutEntry] = []
    paired: list[TakeoutEntry] = []
    due: list[TakeoutEntry] = []
    for group in members.values():
        root = group[0]
        undecided = is_undecided(root, part_number)
        if root.copy_of_id is not None or root.sha1 is None or not (undecided or root.metadata_pending):
            continue
        choice = choice_for(group, chosen)
        if choice.entry_id is not None and choice.entry_id != root.sidecar_entry_id and was_seen(root):
            paired.append(root)
        for member in group[1:]:
            member.sidecar_entry_id = chosen[member.id].entry_id
        root.sidecar_entry_id = choice.entry_id
        if undecided:
            ready.extend(decide_state(index, root, group, choice, texts.get(choice.entry_id) if choice.entry_id is not None else None))
        elif choice.settled:
            due.append(root)
    return PartPairing(
        ready=by_id(ready), newly_paired=by_id(paired), copies=by_id(attached), metadata_due=by_id(due),
    )


def by_id(found: Iterable[TakeoutEntry]) -> tuple[TakeoutEntry, ...]:
    return tuple(sorted(found, key=lambda row: row.id))


def decide_state(index: dedupe.PersonIndex, root: TakeoutEntry, group: Sequence[TakeoutEntry], choice: Choice, text: str | None) -> list[TakeoutEntry]:
    verdict = verdict_for(index, root, group, choice, text)
    root.outcome = LISTED if verdict == READY else verdict
    if verdict != READY:
        return []
    root.metadata_pending = is_tier_1(index, candidate_of(root)) and not choice.settled
    return [root]
