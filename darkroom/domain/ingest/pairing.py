from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from darkroom.domain import media
from darkroom.domain.ingest import sidecar
from darkroom.storage import paths

SIDECAR_NAME_LIMIT = 46
SUPPLEMENTAL = ".supplemental-metadata"
METADATA_TAIL = ".metadata"
MOTION_CLIP_SUFFIX = ".mp"
EDITED_SUFFIXES = ("-edited", "-bearbeitet", "-modifié", "-edytowane", "-bewerkt", "-modificato", "-editat", "-ha editado", "-編集済み")
CREATION_SUFFIXES = ("-effects", "-smile", "-mix")
YEAR_FOLDER = re.compile(r"^Photos from \d{4}$")
SPECIAL_FOLDERS: dict[str, Literal["trash", "archive"]] = {"trash": "trash", "bin": "trash", "archive": "archive"}
TRASHED = "trash"
NUMBERED = re.compile(r"^(?P<stem>.*)\((?P<number>\d+)\)$")
STILL_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng", ".bmp", ".avif", ".jxl"} | media.CAMERA_RAW_SUFFIXES
MEDIA_SUFFIXES = STILL_SUFFIXES | media.VIDEO_SUFFIXES
NON_MEDIA_JSON = {"metadata.json", "print-subscriptions.json", "user-generated-memory-titles.json", "shared_album_comments.json", "archive_browser.json"}
KNOWN_NON_MEDIA = NON_MEDIA_JSON | {"archive_browser.html"}
NO_EXTENSION = "none"
OTHER_KIND = "other"
NAMED_KINDS = MEDIA_SUFFIXES | {
    ".txt", ".html", ".htm", ".pdf", ".csv", ".xml", ".zip", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".svg", ".psd", ".ico",
    ".jp2", ".jfif", ".mpo", ".crw", ".x3f", ".flv", ".vob", ".insp", ".insv", ".lrv", ".thm", ".aae", ".xmp",
}

Special = Literal["trash", "archive"]
Sidecars = dict[str, Path]
Names = frozenset[str]
Rule = Callable[[Path, Sidecars], Path | None]
FolderRule = Callable[[Path, Sidecars, Names], Path | None]
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
    sidecar_from_still: bool = False
    special: Special | None = None


@dataclass
class PairingReport:
    items: list[TakeoutItem] = field(default_factory=list)
    orphan_sidecars: list[Path] = field(default_factory=list)
    unpaired_media: list[TakeoutItem] = field(default_factory=list)
    not_imported: dict[str, int] = field(default_factory=dict)


def stem_of(sidecar: str) -> str:
    return sidecar[: -len(".json")]


def is_family_tail(tail: str) -> bool:
    return tail == METADATA_TAIL or (tail.startswith(".") and SUPPLEMENTAL.startswith(tail))


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


def folded(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def strip_suffix(bare: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        target = folded(suffix)
        for size in range(len(target), min(len(bare), 2 * len(target)) + 1):
            if folded(bare[-size:]) == target:
                return bare[:-size]
    return None


def derived_name(media_path: Path, suffixes: tuple[str, ...], media: Names | None) -> str | None:
    base = NUMBERED.match(media_path.stem)
    number = f"({base.group('number')})" if base else ""
    cut = strip_suffix(base.group("stem") if base else media_path.stem, suffixes)
    if cut is None:
        return None
    numbered = f"{cut}{number}{media_path.suffix}"
    return numbered if media is None or numbered in media else f"{cut}{media_path.suffix}"


def original_name(media_path: Path, media: Names | None = None) -> str | None:
    return derived_name(media_path, EDITED_SUFFIXES, media)


def base_name(media_path: Path, media: Names) -> str | None:
    original = original_name(media_path, media)
    return original if original is not None else derived_name(media_path, CREATION_SUFFIXES, media)


def exclusive(fits: Callable[[str, str], bool]) -> FolderRule:
    def rule(media_path: Path, sidecars: Sidecars, media: Names) -> Path | None:
        for sidecar_name, path in sorted(sidecars.items()):
            if fits(media_path.name, sidecar_name) and not any(other != media_path.name and fits(other, sidecar_name) for other in media):
                return path
        return None

    return rule


def fits_without_extension(media_name: str, sidecar_name: str) -> bool:
    stem = Path(media_name).stem
    named = stem_of(sidecar_name)
    return named == stem or (named.startswith(stem) and is_family_tail(named[len(stem):]))


def fits_cut_counter(media_name: str, sidecar_name: str) -> bool:
    media_path = Path(media_name)
    found = NUMBERED.match(media_path.stem)
    if not found:
        return False
    full = f"{found.group('stem')}{media_path.suffix}{SUPPLEMENTAL}"
    tag = f"({found.group('number')})"
    named = stem_of(sidecar_name)
    cut = named[: -len(tag)]
    return len(full) > SIDECAR_NAME_LIMIT and named.endswith(tag) and len(cut) in (SIDECAR_NAME_LIMIT, SIDECAR_NAME_LIMIT - len(tag)) and full.startswith(cut)


RULES: tuple[Rule, ...] = (exact, supplemental, counter, truncated)
FOLDER_RULES: tuple[FolderRule, ...] = (exclusive(fits_without_extension), exclusive(fits_cut_counter))


def own_sidecar(media_path: Path, sidecars: Sidecars, media: Names) -> Path | None:
    for rule in RULES:
        found = rule(media_path, sidecars)
        if found is not None:
            return found
    for folder_rule in FOLDER_RULES:
        found = folder_rule(media_path, sidecars, media)
        if found is not None:
            return found
    return None


def edited_sidecar(media_path: Path, sidecars: Sidecars, media: Names) -> Path | None:
    base = base_name(media_path, media)
    return None if base is None else own_sidecar(media_path.with_name(base), sidecars, media)


def sidecar_for(media_path: Path, sidecars: Sidecars, media: Names) -> Path | None:
    return own_sidecar(media_path, sidecars, media) or edited_sidecar(media_path, sidecars, media)


def plain_text(path: Path) -> str | None:
    with paths.open_plain_file(path) as handle:
        decoded = sidecar.decode(handle.read(sidecar.LIMIT + 1))
    return None if decoded is None else decoded.text


def title_of(path: Path, reading: Reader | None) -> str | None:
    if reading is None:
        return None
    try:
        raw = reading(path)
    except (OSError, ValueError):
        return None
    parsed = sidecar.parse(raw) if raw else None
    return folded(parsed.title) if parsed is not None and parsed.title else None


def owner_of(path: Path, found: Path, sidecars: Sidecars, media: Names) -> str:
    base = base_name(path, media)
    if base is not None and own_sidecar(path.with_name(base), sidecars, media) == found:
        return base
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
    listed = list(media_paths)
    media = frozenset(path.name for path in listed)
    found = {path: sidecar_for(path, sidecars, media) for path in listed}
    owners: dict[Path, str] = {}
    claims: dict[Path, set[str]] = {}
    for path, match in found.items():
        if match is not None:
            owners[path] = owner_of(path, match, sidecars, media)
            claims.setdefault(match, set()).add(owners[path])
    shared = {match for match, names in claims.items() if len(names) > 1}
    if not shared:
        return found
    taken = set(claims) - shared
    variants = {match: tuple(c for c in variants_of(match, sidecars) if c not in taken) for match in shared}
    candidates = {owners[path]: variants[match] for path, match in found.items() if match in shared}
    settled = by_title(candidates, reading)
    return {path: settled.get(owners[path]) if match in shared else match for path, match in found.items()}


def special_of(folder: Path) -> Special | None:
    return SPECIAL_FOLDERS.get(folded(folder.name))


def album_of(folder: Path) -> str | None:
    return None if YEAR_FOLDER.match(folder.name) or special_of(folder) is not None else folder.name


def special_of_copies(folders: list[Path]) -> Special | None:
    found = {special_of(folder) for folder in folders}
    if not found or None in found:
        return None
    return "archive" if "archive" in found else "trash"


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


def left_out_kind(name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem or not extension:
        return NO_EXTENSION
    suffix = f".{extension.lower()}"
    return suffix if suffix in NAMED_KINDS else OTHER_KIND


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
    media_in: dict[Path, Names] = {}
    used: set[Path] = set()
    json_files: list[Path] = []
    for folder, listed in folders:
        files = list(listed)
        own: Sidecars = {name: folder / name for name in files if is_sidecar(name)}
        json_files.extend(own.values())
        sidecars: Sidecars = {name: folder / name for name in earlier.get(folder, ()) if is_sidecar(name)} | own
        media_paths: list[Path] = []
        for name in files:
            path = folder / name
            if is_sidecar(name) or name in KNOWN_NON_MEDIA:
                continue
            if not is_media(name) or path not in digests:
                kind = left_out_kind(name)
                report.not_imported[kind] = report.not_imported.get(kind, 0) + 1
                continue
            media_paths.append(path)
        media_in[folder] = frozenset(path.name for path in media_paths)
        for path, found in folder_sidecars(media_paths, sidecars, reading).items():
            if found is not None:
                used.add(found)
            found_copies.setdefault(digests[path], []).append((path, found))
    by_copy: dict[Path, TakeoutItem] = {}
    for digest, copies in sorted((digest, slot) for digest, found in found_copies.items() for slot in slots_of(found)):
        copies.sort(key=lambda c: (not YEAR_FOLDER.match(c[0].parent.name), special_of(c[0].parent) is not None, str(c[0])))
        first = copies[0]
        item = TakeoutItem(
            path=first[0], sha1=digest, copies=tuple(c[0] for c in copies),
            sidecar=next((c[1] for c in copies if c[1] is not None), None),
            albums=sorted({a for c in copies if (a := album_of(c[0].parent))}),
            in_year_folder=any(YEAR_FOLDER.match(c[0].parent.name) for c in copies),
            special=special_of_copies([c[0].parent for c in copies]),
        )
        report.items.append(item)
        for copy in copies:
            by_copy[copy[0]] = item
    for item in report.items:
        original = original_name(item.path, media_in.get(item.path.parent, frozenset()))
        if original is not None:
            original_item = by_copy.get(item.path.with_name(original))
            item.edited_of = original_item.path if original_item else None
        if item.path.suffix.lower() in media.VIDEO_SUFFIXES:
            match = live_match(item, by_copy)
            item.live_pair_of = None if match is None else match[0]
            if item.sidecar is None and match is not None and lends_sidecar(item, match[1]):
                item.sidecar = by_copy[match[0]].sidecar
                item.sidecar_from_still = item.sidecar is not None
    report.unpaired_media = [item for item in report.items if item.sidecar is None and item.special != TRASHED]
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


def lends_sidecar(video: TakeoutItem, sibling: Path) -> bool:
    return sibling.stem in {video.path.stem, video.path.name if video.path.suffix.lower() == MOTION_CLIP_SUFFIX else video.path.stem}


def live_match(video: TakeoutItem, by_copy: dict[Path, TakeoutItem]) -> tuple[Path, Path] | None:
    stems = {video.path.stem.lower()} | ({video.path.name.lower()} if video.path.suffix.lower() == MOTION_CLIP_SUFFIX else set())
    stills = {
        item.path: sibling for sibling, item in by_copy.items()
        if sibling.parent == video.path.parent and sibling.stem.lower() in stems and sibling.suffix.lower() in STILL_SUFFIXES
    }
    return next(iter(stills.items())) if len(stills) == 1 else None
