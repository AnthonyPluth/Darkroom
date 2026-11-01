from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

MEDIA = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".dng", ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}
VIDEO = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}
STILL = MEDIA - VIDEO
CHUNK = 1024 * 1024
YEAR_FOLDER = re.compile(r"^Photos from \d{4}$")
SIDECAR_NAME_LIMIT = 46
SUPPLEMENTAL = ".supplemental-metadata"
NUMBERED = re.compile(r"^(?P<stem>.*)\((?P<number>\d+)\)$")
EDITED = "-edited"


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    opener: Callable[[], IO[bytes]]

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str:
        return Path(self.basename).suffix.lower()

    @property
    def folder(self) -> str:
        return self.name.rsplit("/", 1)[0] if "/" in self.name else ""


def sha1_stream(handle: IO[bytes]) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    while chunk := handle.read(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def directory_entries(root: Path) -> Iterator[Entry]:
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        yield Entry(path.relative_to(root).as_posix(), path.stat().st_size, lambda path=path: path.open("rb"))


def zip_entries(path: Path) -> Iterator[Entry]:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            yield Entry(info.filename, info.file_size, lambda info=info, path=path: open_member(path, info.filename))


def open_member(zip_path: Path, member: str) -> IO[bytes]:
    archive = zipfile.ZipFile(zip_path)
    handle = archive.open(member)
    original_close = handle.close

    def close() -> None:
        original_close()
        archive.close()

    handle.close = close
    return handle


def export_entries(export: Path) -> list[Entry]:
    if export.is_file() and export.suffix.lower() == ".zip":
        return list(zip_entries(export))
    entries: list[Entry] = []
    for zip_path in sorted(export.rglob("*.zip")):
        entries += zip_entries(zip_path)
    extracted = directory_entries(export)
    return entries + [e for e in extracted if not e.name.lower().endswith(".zip")]


def is_media(entry: Entry) -> bool:
    return entry.suffix in MEDIA


def sidecar_bases(basename: str) -> list[str]:
    path = Path(basename)
    stem, extension = path.stem, path.suffix
    names = [basename]
    numbered = NUMBERED.match(stem)
    if numbered:
        names.append(f"{numbered.group('stem')}{extension}({numbered.group('number')})")
        stem = numbered.group("stem")
    if stem.endswith(EDITED):
        names.append(f"{stem[:-len(EDITED)]}{extension}")
    return names + [name[:SIDECAR_NAME_LIMIT] for name in names if len(name) > SIDECAR_NAME_LIMIT]


def sidecar_of(entry: Entry, sidecars: dict[str, list[Entry]]) -> Entry | None:
    candidates = sidecars.get(entry.folder, [])
    for base in sidecar_bases(entry.basename):
        for candidate in candidates:
            name = candidate.basename
            if name.startswith(base) and name.endswith(".json") and SUPPLEMENTAL.startswith(name[len(base):-len(".json")]):
                return candidate
    return None


def read_json(entry: Entry) -> dict[str, object]:
    with contextlib.closing(entry.opener()) as handle:
        try:
            value = json.load(handle)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def scrubbed(value: object, depth: int = 0) -> object:
    if isinstance(value, dict):
        return {key: scrubbed(item, depth + 1) for key, item in value.items()} if depth < 3 else "{...}"
    if isinstance(value, list):
        return [scrubbed(value[0], depth + 1)] if value else []
    return type(value).__name__


def exiftool_tags(source: IO[bytes] | Path) -> dict[str, object]:
    program = shutil.which("exiftool")
    if program is None:
        return {}
    if isinstance(source, Path):
        done = subprocess.run([program, "-j", "-G1", "-n", str(source)], capture_output=True, text=True, timeout=120)
    else:
        with subprocess.Popen([program, "-j", "-G1", "-n", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            assert process.stdin is not None and process.stdout is not None
            try:
                shutil.copyfileobj(source, process.stdin, CHUNK)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
            output = process.stdout.read().decode(errors="replace")
        return parse_tags(output)
    return parse_tags(done.stdout)


def parse_tags(text: str) -> dict[str, object]:
    try:
        return dict(json.loads(text)[0])
    except (ValueError, IndexError, TypeError):
        return {}


IGNORED_TAGS = ("SourceFile", "System:", "ExifTool:", "File:FileName", "File:Directory", "File:FileSize")


def differing_tags(left: dict[str, object], right: dict[str, object]) -> list[str]:
    names = {k for k in left.keys() | right.keys() if not k.startswith(IGNORED_TAGS)}
    return sorted(k for k in names if left.get(k) != right.get(k))


@dataclass
class Originals:
    by_sha1: dict[str, list[Path]]
    by_name_size: dict[tuple[str, int], list[Path]]
    by_name: dict[str, list[Path]]
    stills_with_video: list[Path]
    count: int


def index_originals(root: Path) -> Originals:
    by_sha1: dict[str, list[Path]] = {}
    by_name_size: dict[tuple[str, int], list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA and not any(part.startswith(".") for part in p.relative_to(root).parts))
    for path in files:
        with path.open("rb") as handle:
            by_sha1.setdefault(sha1_stream(handle), []).append(path)
        by_name_size.setdefault((path.name.lower(), path.stat().st_size), []).append(path)
        by_name.setdefault(path.name.lower(), []).append(path)
    stems = {(p.parent, p.stem.lower()): p for p in files if p.suffix.lower() in VIDEO}
    stills = [p for p in files if p.suffix.lower() in STILL and (p.parent, p.stem.lower()) in stems]
    return Originals(by_sha1, by_name_size, by_name, stills, len(files))


def unambiguous_stems(stills: list[Path]) -> set[str]:
    counts = Counter(p.stem.lower() for p in stills)
    return {stem for stem, number in counts.items() if number == 1}


def percent(part: int, whole: int) -> str:
    return f"{part} ({100 * part / whole:.1f}%)" if whole else f"{part}"


def digest_of(entry: Entry) -> str:
    with contextlib.closing(entry.opener()) as handle:
        return sha1_stream(handle)


def in_year_folder(entry: Entry) -> bool:
    return bool(YEAR_FOLDER.match(entry.folder.rsplit("/", 1)[-1]))


def takeout_report(export: Path, originals_root: Path, out: IO[str]) -> int:
    entries = export_entries(export)
    media = [e for e in entries if is_media(e)]
    sidecars: dict[str, list[Entry]] = {}
    for e in entries:
        if e.suffix == ".json":
            sidecars.setdefault(e.folder, []).append(e)
    originals = index_originals(originals_root)
    digests = {e.name: digest_of(e) for e in media}
    identical = different = resized = missing = 0
    tag_counts: Counter[str] = Counter()
    for entry in media:
        if digests[entry.name] in originals.by_sha1:
            identical += 1
            continue
        same_name = originals.by_name.get(entry.basename.lower())
        if not same_name:
            missing += 1
            continue
        if originals.by_name_size.get((entry.basename.lower(), entry.size)):
            different += 1
        else:
            resized += 1
        with contextlib.closing(entry.opener()) as handle:
            tag_counts.update(differing_tags(exiftool_tags(handle), exiftool_tags(same_name[0])))
    year_digests = {digests[e.name] for e in media if in_year_folder(e)}
    album_only = [e for e in media if digests[e.name] not in year_digests]
    lacking = []
    for entry in media:
        meta = sidecar_of(entry, sidecars)
        origin = read_json(meta).get("googlePhotosOrigin") if meta else None
        if not (isinstance(origin, dict) and "mobileUpload" in origin):
            lacking.append(entry)
    export_hashes = set(digests.values())
    export_video_stems = {Path(e.basename).stem.lower() for e in media if e.suffix in VIDEO}
    safe_stems = unambiguous_stems(originals.stills_with_video)
    present = absent = 0
    for still in originals.stills_with_video:
        video = next(p for p in still.parent.iterdir() if p.stem.lower() == still.stem.lower() and p.suffix.lower() in VIDEO)
        with video.open("rb") as handle:
            found = sha1_stream(handle) in export_hashes or (still.stem.lower() in safe_stems and still.stem.lower() in export_video_stems)
        present, absent = present + found, absent + (not found)
    total = len(media)
    print(f"Media files in the export: {total}; originals indexed: {originals.count}", file=out)
    print(f"Byte-identical to an original: {percent(identical, total)}", file=out)
    print(f"Same name and size, different bytes: {percent(different, total)}", file=out)
    print(f"Same name, different size: {percent(resized, total)}", file=out)
    for name, count in tag_counts.most_common(10):
        print(f"  tag that differs in these: {name} ({count})", file=out)
    print(f"No counterpart in the originals: {percent(missing, total)}", file=out)
    print(f"Live Photos in the originals (a still with a video of the same name): {len(originals.stills_with_video)}; video in the export: {present}, missing: {absent}", file=out)
    print(f"Items found only under album folders: {percent(len(album_only), total)}", file=out)
    print(f"Items whose sidecar lacks googlePhotosOrigin.mobileUpload (shared into an album, or no sidecar): {percent(len(lacking), total)}", file=out)
    for entry in lacking[:5]:
        meta = sidecar_of(entry, sidecars)
        print(f"  {entry.basename}: {json.dumps(scrubbed(read_json(meta)), sort_keys=True) if meta else 'no sidecar'}", file=out)
    return 0


def phone_report(phone_files: Path, originals_root: Path, out: IO[str]) -> int:
    originals = index_originals(originals_root)
    files = sorted(p for p in phone_files.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA)
    identical = different = missing = 0
    for path in files:
        with path.open("rb") as handle:
            digest = sha1_stream(handle)
        if digest in originals.by_sha1:
            identical += 1
        elif any(p.name.lower() == path.name.lower() for found in originals.by_name_size.values() for p in found):
            different += 1
            print(f"  different bytes: {path.name}", file=out)
        else:
            missing += 1
            print(f"  not on Unraid: {path.name}", file=out)
    print(f"Files exported from the iPhone: {len(files)}", file=out)
    print(f"Byte-identical on Unraid: {percent(identical, len(files))}", file=out)
    print(f"Same name, different bytes: {percent(different, len(files))}", file=out)
    print(f"Not found on Unraid: {percent(missing, len(files))}", file=out)
    return 0


def main(argv: list[str] | None = None, out: IO[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only comparison of Takeout or iPhone files with the originals on Unraid.")
    commands = parser.add_subparsers(dest="mode", required=True)
    takeout = commands.add_parser("takeout")
    takeout.add_argument("--export", type=Path, required=True)
    takeout.add_argument("--originals", type=Path, required=True)
    phone = commands.add_parser("phone")
    phone.add_argument("--phone-files", type=Path, required=True)
    phone.add_argument("--originals", type=Path, required=True)
    args = parser.parse_args(argv)
    out = out or sys.stdout
    if args.mode == "takeout":
        return takeout_report(args.export, args.originals, out)
    return phone_report(args.phone_files, args.originals, out)


if __name__ == "__main__":
    sys.exit(main())
