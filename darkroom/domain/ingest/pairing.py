from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from darkroom.domain import media
from darkroom.domain.ingest import sidecar
from darkroom.storage import paths

SIDECAR_NAME_LIMIT = 46
SUPPLEMENTAL = ".supplemental-metadata"
EDITED = "-edited"
YEAR_FOLDER = re.compile(r"^Photos from \d{4}$")
NUMBERED = re.compile(r"^(?P<stem>.*)\((?P<number>\d+)\)$")
STILL_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng", ".bmp"}
MEDIA_SUFFIXES = STILL_SUFFIXES | media.VIDEO_SUFFIXES
NON_MEDIA_JSON = {"metadata.json", "print-subscriptions.json", "user-generated-memory-titles.json", "shared_album_comments.json", "archive_browser.json"}

Sidecars = dict[str, Path]
Rule = Callable[[Path, Sidecars], Path | None]
Digest = Callable[[Path], str]
Reader = Callable[[Path], str | None]


@dataclass
class TakeoutItem:
    path: Path
    sha1: str
    copies: tuple[Path, ...]
    sidecar: Path | None
    albums: list[str] = field(default_factory=list)
    in_year_folder: bool = False
    edited_of: Path | None = None
    live_pair_of: Path | None = None


@dataclass
class PairingReport:
    items: list[TakeoutItem] = field(default_factory=list)
    orphan_sidecars: list[Path] = field(default_factory=list)
    unpaired_media: list[TakeoutItem] = field(default_factory=list)


def stem_of(sidecar: str) -> str:
    return sidecar[: -len(".json")]


def is_family_tail(tail: str) -> bool:
    return tail.startswith(".") and SUPPLEMENTAL.startswith(tail)


def exact(media_path: Path, sidecars: Sidecars) -> Path | None:
    return sidecars.get(f"{media_path.name}.json")


def supplemental(media_path: Path, sidecars: Sidecars) -> Path | None:
    name = media_path.name
    for sidecar_name, path in sorted(sidecars.items()):
        stem = stem_of(sidecar_name)
        if stem.startswith(name) and len(stem) > len(name) and is_family_tail(stem[len(name):]):
            return path
    return None


def counter(media_path: Path, sidecars: Sidecars) -> Path | None:
    found = NUMBERED.match(media_path.stem)
    if not found:
        return None
    base = f"{found.group('stem')}{media_path.suffix}"
    tag = f"({found.group('number')})"
    for sidecar_name, path in sorted(sidecars.items()):
        stem = stem_of(sidecar_name)
        if not stem.endswith(tag):
            continue
        rest = stem[: -len(tag)]
        if rest == base or (rest.startswith(base) and is_family_tail(rest[len(base):])):
            return path
    return None


def truncated(media_path: Path, sidecars: Sidecars) -> Path | None:
    full = f"{media_path.name}{SUPPLEMENTAL}"
    if len(media_path.name) <= SIDECAR_NAME_LIMIT - 1 and len(full) <= SIDECAR_NAME_LIMIT:
        return None
    for sidecar_name, path in sorted(sidecars.items()):
        stem = stem_of(sidecar_name)
        if len(stem) == SIDECAR_NAME_LIMIT and full.startswith(stem):
            return path
    return None


def original_name(media_path: Path) -> str | None:
    stem = media_path.stem
    base = NUMBERED.match(stem)
    number = f"({base.group('number')})" if base else ""
    bare = base.group("stem") if base else stem
    return f"{bare[: -len(EDITED)]}{number}{media_path.suffix}" if bare.endswith(EDITED) else None


RULES: tuple[Rule, ...] = (exact, supplemental, counter, truncated)


def own_sidecar(media_path: Path, sidecars: Sidecars) -> Path | None:
    for rule in RULES:
        found = rule(media_path, sidecars)
        if found is not None:
            return found
    return None


def edited_sidecar(media_path: Path, sidecars: Sidecars) -> Path | None:
    original = original_name(media_path)
    return None if original is None else own_sidecar(media_path.with_name(original), sidecars)


def sidecar_for(media_path: Path, sidecars: Sidecars) -> Path | None:
    return own_sidecar(media_path, sidecars) or edited_sidecar(media_path, sidecars)


def folded(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def plain_text(path: Path) -> str:
    with paths.open_plain_file(path) as handle:
        return handle.read().decode("utf-8", errors="replace")


def title_of(path: Path, reading: Reader | None) -> str | None:
    if reading is None:
        return None
    try:
        raw = reading(path)
    except (OSError, ValueError):
        return None
    parsed = sidecar.parse(raw) if raw else None
    return folded(parsed.title) if parsed is not None and parsed.title else None


def owner_of(path: Path, found: Path, sidecars: Sidecars) -> str:
    original = original_name(path)
    if original is not None and own_sidecar(path.with_name(original), sidecars) == found:
        return original
    return path.name


def variants_of(shared: Path, sidecars: Sidecars) -> list[Path]:
    stem = stem_of(shared.name)
    found = []
    for name, path in sorted(sidecars.items()):
        numbered = NUMBERED.match(stem_of(name))
        if stem_of(name) == stem or (numbered is not None and numbered.group("stem") == stem):
            found.append(path)
    return found


def by_title(candidates: dict[str, tuple[Path, ...]], reading: Reader | None) -> dict[str, Path]:
    titles = {path: title_of(path, reading) for path in {path for paths_of in candidates.values() for path in paths_of}}
    named: dict[tuple[Path, ...], dict[str, list[Path]]] = {}
    for paths_of in set(candidates.values()):
        index = named.setdefault(paths_of, {})
        for path in paths_of:
            title = titles[path]
            if title is not None:
                index.setdefault(title, []).append(path)
    picks: dict[str, Path] = {}
    for owner, paths_of in candidates.items():
        matching = named[paths_of].get(folded(owner), [])
        if len(matching) == 1:
            picks[owner] = matching[0]
    counts = Counter(picks.values())
    return {owner: path for owner, path in picks.items() if counts[path] == 1}


def folder_sidecars(media_paths: Iterable[Path], sidecars: Sidecars, reading: Reader | None) -> dict[Path, Path | None]:
    found = {path: sidecar_for(path, sidecars) for path in media_paths}
    owners: dict[Path, str] = {}
    claims: dict[Path, set[str]] = {}
    for path, match in found.items():
        if match is not None:
            owners[path] = owner_of(path, match, sidecars)
            claims.setdefault(match, set()).add(owners[path])
    shared = {match for match, names in claims.items() if len(names) > 1}
    if not shared:
        return found
    taken = set(claims) - shared
    variants = {match: tuple(c for c in variants_of(match, sidecars) if c not in taken) for match in shared}
    candidates = {owners[path]: variants[match] for path, match in found.items() if match in shared}
    settled = by_title(candidates, reading)
    return {path: settled.get(owners[path]) if match in shared else match for path, match in found.items()}


def album_of(folder: Path) -> str | None:
    return None if YEAR_FOLDER.match(folder.name) else folder.name


def scan(root: Path) -> dict[Path, list[str]]:
    folders: dict[Path, list[str]] = {}
    if os.path.islink(root):
        return folders
    for folder, names, files in os.walk(root):
        names[:] = sorted(name for name in names if not name.startswith("."))
        folders[Path(folder)] = paths.plain_entries(folder, files)
    return folders


def is_media(name: str) -> bool:
    return Path(name).suffix.lower() in MEDIA_SUFFIXES


def is_sidecar(name: str) -> bool:
    return name.lower().endswith(".json") and name not in NON_MEDIA_JSON


def digests_of(folders: dict[Path, list[str]], hashing: Digest) -> dict[Path, str]:
    found: dict[Path, str] = {}
    for folder, files in folders.items():
        for name in files:
            if not is_media(name):
                continue
            try:
                found[folder / name] = hashing(folder / name)
            except paths.OutsideRoot:
                continue
    return found


def pair(staging_root: Path, hashing: Digest = paths.plain_sha1, reading: Reader = plain_text) -> PairingReport:
    folders = scan(staging_root)
    return pair_names(folders.items(), digests_of(folders, hashing), reading=reading)


def pair_names(folders: Iterable[tuple[Path, Iterable[str]]], digests: Mapping[Path, str],
               earlier_sidecars: Mapping[Path, Iterable[str]] | None = None, *, reading: Reader | None = None) -> PairingReport:
    earlier = earlier_sidecars or {}
    report = PairingReport()
    found_copies: dict[str, list[tuple[Path, Path | None]]] = {}
    used: set[Path] = set()
    json_files: list[Path] = []
    for folder, listed in folders:
        files = list(listed)
        own: Sidecars = {name: folder / name for name in files if is_sidecar(name)}
        json_files.extend(own.values())
        sidecars: Sidecars = {name: folder / name for name in earlier.get(folder, ()) if is_sidecar(name)} | own
        media_paths = [folder / name for name in files if is_media(name) and folder / name in digests]
        for path, found in folder_sidecars(media_paths, sidecars, reading).items():
            if found is not None:
                used.add(found)
            found_copies.setdefault(digests[path], []).append((path, found))
    by_copy: dict[Path, TakeoutItem] = {}
    for digest, copies in sorted((digest, slot) for digest, found in found_copies.items() for slot in slots_of(found)):
        copies.sort(key=lambda c: (album_of(c[0].parent) is not None, str(c[0])))
        first = copies[0]
        item = TakeoutItem(
            path=first[0], sha1=digest, copies=tuple(c[0] for c in copies),
            sidecar=next((c[1] for c in copies if c[1] is not None), None),
            albums=sorted({a for c in copies if (a := album_of(c[0].parent))}),
            in_year_folder=any(album_of(c[0].parent) is None for c in copies),
        )
        report.items.append(item)
        for copy in copies:
            by_copy[copy[0]] = item
    for item in report.items:
        original = original_name(item.path)
        if original is not None:
            original_item = by_copy.get(item.path.with_name(original))
            item.edited_of = original_item.path if original_item else None
        if item.path.suffix.lower() in media.VIDEO_SUFFIXES:
            item.live_pair_of = live_still(item, by_copy)
    report.unpaired_media = [item for item in report.items if item.sidecar is None]
    report.orphan_sidecars = sorted(p for p in json_files if p not in used)
    return report


def slots_of(copies: list[tuple[Path, Path | None]]) -> list[list[tuple[Path, Path | None]]]:
    slots: list[list[tuple[Path, Path | None]]] = []
    for copy in sorted(copies, key=lambda c: str(c[0])):
        for slot in slots:
            if all(other[0].parent != copy[0].parent for other in slot):
                slot.append(copy)
                break
        else:
            slots.append([copy])
    return slots


def live_still(video: TakeoutItem, by_copy: dict[Path, TakeoutItem]) -> Path | None:
    stills = {
        item.path for sibling, item in by_copy.items()
        if sibling.parent == video.path.parent and sibling.stem.lower() == video.path.stem.lower() and sibling.suffix.lower() in STILL_SUFFIXES
    }
    return next(iter(stills)) if len(stills) == 1 else None
