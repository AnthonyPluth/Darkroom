from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from darkroom.domain import media
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


def pair(staging_root: Path, hashing: Digest = paths.plain_sha1) -> PairingReport:
    folders = scan(staging_root)
    report = PairingReport()
    found_copies: dict[str, list[tuple[Path, Path | None]]] = {}
    used: set[Path] = set()
    json_files: list[Path] = []
    for folder, files in folders.items():
        sidecars: Sidecars = {name: folder / name for name in files if name.lower().endswith(".json") and name not in NON_MEDIA_JSON}
        json_files.extend(sidecars.values())
        for name in files:
            path = folder / name
            if path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            try:
                sha1 = hashing(path)
            except paths.OutsideRoot:
                continue
            found = sidecar_for(path, sidecars)
            if found is not None:
                used.add(found)
            found_copies.setdefault(sha1, []).append((path, found))
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
