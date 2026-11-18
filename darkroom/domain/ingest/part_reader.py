from __future__ import annotations

import re
import stat
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, BinaryIO, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain.ingest import pairing, unpack
from darkroom.storage import db, paths
from darkroom.storage.models import Export, ExportPart, Person, TakeoutEntry, TakeoutSidecar
from darkroom.storage.paths import Photos

BATCH_FILES = 100
BATCH_BYTES = 4 << 30
MAX_ENTRIES = 500_000
MAX_UNPACKED_BYTES = 1 << 40
RATIO_FLOOR = 1 << 20
MAX_RATIO = 100
MAX_COMPONENT_BYTES = 255
MAX_NAME_BYTES = 4096
SIDECAR_MAX_BYTES = 1 << 20
MAX_SIDECAR_BYTES = 256 << 20
FLUSH_ROWS = 500
FLUSH_TEXT_BYTES = 16 << 20
ENCRYPTED = 0x1
READABLE = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA})
PLAIN_TYPES = frozenset({0, stat.S_IFREG, stat.S_IFDIR})
SCRATCH_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")
READ_ERRORS = (zipfile.BadZipFile, *unpack.DECOMPRESS_ERRORS)

Refusal = Literal["missing", "not_plain", "empty", "not_a_zip", "damaged", "symlink_entry", "outside", "file_and_folder", "device_entry",
                  "unreadable_entry", "name_too_long", "duplicate_name", "too_many_entries", "too_large", "zip_bomb", "sidecars_too_big",
                  "size_lies", "listing_differs", "bytes_differ"]
FROM_UNPACK: dict[str, Refusal] = {"missing": "missing", "not_plain": "not_plain", "empty": "empty", "not_a_zip": "not_a_zip",
                                   "damaged": "damaged", "outside": "outside"}
DEVICE_ENTRY = "an entry is a device, a pipe or a socket"
UNREADABLE_ENTRY = "an entry is encrypted or packed in a way Darkroom can't read"
NAME_TOO_LONG = "an entry's name is too long"
DUPLICATE_NAME = "two entries have the same name"
ZIP_BOMB = "an entry is compressed far more than a photo or video can be"
SIZE_LIES = "an entry's size doesn't match what the zip says"
SIDECARS_TOO_BIG = "the part's sidecars are too big in all to store"


class PartRefused(Exception):
    def __init__(self, reason: Refusal, public: str) -> None:
        super().__init__(public)
        self.reason: Refusal = reason
        self.public = public


@dataclass(frozen=True)
class Listed:
    name: str
    kind: str
    bytes: int
    crc32: int
    info: zipfile.ZipInfo

    def key(self) -> tuple[str, str, int, int]:
        return self.name, self.kind, self.bytes, self.crc32


@dataclass(frozen=True)
class Streamed:
    entry_id: int
    name: str
    path: Path
    sha1: str
    bytes: int


@dataclass
class PartRead:
    entries: int = 0
    media: int = 0
    sidecars: int = 0
    sidecars_unread: int = 0
    other: int = 0
    relisted: bool = False
    streamed: int = 0
    batches: int = 0


EachBatch = Callable[[Session, Sequence[Streamed]], None]


def refused_by_unpack(error: unpack.UnpackFailed) -> PartRefused:
    return PartRefused(FROM_UNPACK.get(error.reason or "", "damaged"), error.public)


def central_checks(infos: Sequence[zipfile.ZipInfo], number: int) -> None:
    if len(infos) > MAX_ENTRIES:
        raise PartRefused("too_many_entries", f"part {number} has more than {MAX_ENTRIES} entries")
    total = sidecars = 0
    for info in infos:
        if info.flag_bits & ENCRYPTED or info.compress_type not in READABLE:
            raise PartRefused("unreadable_entry", UNREADABLE_ENTRY)
        if info.file_size > RATIO_FLOOR and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise PartRefused("zip_bomb", ZIP_BOMB)
        total += info.file_size
        if may_be_read_as_sidecar(info):
            sidecars += info.file_size
    if total > MAX_UNPACKED_BYTES:
        raise PartRefused("too_large", f"part {number} would unpack to more than {MAX_UNPACKED_BYTES >> 40} TiB")
    if sidecars > MAX_SIDECAR_BYTES:
        raise PartRefused("sidecars_too_big", SIDECARS_TOO_BIG)


def may_be_read_as_sidecar(info: zipfile.ZipInfo) -> bool:
    return not info.is_dir() and info.file_size <= SIDECAR_MAX_BYTES and pairing.is_sidecar(PurePosixPath(info.filename.replace("\\", "/")).name)


def damaged(number: int) -> PartRefused:
    return PartRefused("damaged", unpack.damaged(Path(), number).public)


def file_type(info: zipfile.ZipInfo) -> int:
    return stat.S_IFMT(info.external_attr >> 16)


def kind_of(parts: tuple[str, ...], folder: bool) -> str:
    if folder or any(part.startswith(".") for part in parts[:-1]):
        return "other"
    if pairing.is_media(parts[-1]):
        return "media"
    return "sidecar" if pairing.is_sidecar(parts[-1]) else "other"


def too_long(parts: tuple[str, ...]) -> bool:
    return any(len(part.encode()) > MAX_COMPONENT_BYTES for part in parts) or len("/".join(parts).encode()) > MAX_NAME_BYTES


class Names:
    def __init__(self) -> None:
        self.files: set[tuple[str, ...]] = set()
        self.folders: set[tuple[str, ...]] = set()
        self.seen: set[str] = set()

    def add(self, name: str, parts: tuple[str, ...], folder: bool) -> None:
        if name in self.seen:
            raise PartRefused("duplicate_name", DUPLICATE_NAME)
        self.seen.add(name)
        above = {parts[:end] for end in range(1, len(parts))}
        if above & self.files or (parts in self.folders if not folder else parts in self.files):
            raise PartRefused("file_and_folder", unpack.LINK_IN_THE_INBOX)
        self.folders |= above | ({parts} if folder else set())
        if not folder:
            self.files.add(parts)


def entry_listing(infos: Sequence[zipfile.ZipInfo]) -> list[Listed]:
    names = Names()
    listed = []
    for info in infos:
        if unpack.symlink_entry(info):
            raise PartRefused("symlink_entry", unpack.SYMLINK_ENTRY)
        if file_type(info) not in PLAIN_TYPES:
            raise PartRefused("device_entry", DEVICE_ENTRY)
        try:
            parts = unpack.plain_entry(info.filename).parts
        except unpack.UnpackFailed as error:
            raise refused_by_unpack(error) from error
        if too_long(parts):
            raise PartRefused("name_too_long", NAME_TOO_LONG)
        folder = info.is_dir()
        name = "/".join(parts) + ("/" if folder else "")
        names.add(name, parts, folder)
        listed.append(Listed(name, kind_of(parts, folder), info.file_size, info.CRC, info))
    return listed


def sidecar_text(archive: zipfile.ZipFile, entry: Listed, number: int) -> str | None:
    if entry.bytes > SIDECAR_MAX_BYTES:
        return None
    try:
        with archive.open(entry.info) as source:
            raw = source.read(SIDECAR_MAX_BYTES + 1)
    except READ_ERRORS as error:
        raise damaged(number) from error
    if len(raw) != entry.bytes:
        raise PartRefused("size_lies", SIZE_LIES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def folder_and_name(name: str) -> tuple[str, str]:
    folder, _, leaf = name.rpartition("/")
    return folder, leaf


def recorded(opened: Session, export: Export, part: ExportPart) -> list[TakeoutEntry]:
    query = select(TakeoutEntry).where(TakeoutEntry.export_id == export.id, TakeoutEntry.part_number == part.number).order_by(TakeoutEntry.id)
    return list(opened.execute(query).scalars())


def record_listing(opened: Session, archive: zipfile.ZipFile, export: Export, part: ExportPart, listed: list[Listed], now: float,
                   read: PartRead) -> None:
    db.write_lock(opened)
    known = recorded(opened, export, part)
    if known:
        if sorted((row.name, row.kind, row.bytes, row.crc32) for row in known) != sorted(entry.key() for entry in listed):
            opened.rollback()
            raise PartRefused("listing_differs", f"part {part.number} doesn't match the part listed before")
        read.relisted = True
        return
    for start in range(0, len(listed), FLUSH_ROWS):
        record_rows(opened, archive, export, part, listed[start:start + FLUSH_ROWS], now)
    part.entries = len(listed)
    opened.commit()


def record_rows(opened: Session, archive: zipfile.ZipFile, export: Export, part: ExportPart, chunk: list[Listed], now: float) -> None:
    rows = [TakeoutEntry(export_id=export.id, part_number=part.number, name=entry.name, kind=entry.kind, bytes=entry.bytes, crc32=entry.crc32,
                         listed_at=now) for entry in chunk]
    opened.add_all(rows)
    opened.flush()
    ids = [row.id for row in rows]
    for row in rows:
        opened.expunge(row)
    held: list[TakeoutSidecar] = []
    pending = 0
    for entry, entry_id in zip(chunk, ids, strict=True):
        text = sidecar_text(archive, entry, part.number) if entry.kind == "sidecar" else None
        if text is None:
            continue
        folder, name = folder_and_name(entry.name)
        held.append(TakeoutSidecar(export_id=export.id, part_number=part.number, entry_id=entry_id, folder=folder, name=name, text=text))
        opened.add(held[-1])
        pending += len(text)
        if pending >= FLUSH_TEXT_BYTES:
            flush_out(opened, held)
            pending = 0
    flush_out(opened, held)


def flush_out(opened: Session, held: list[TakeoutSidecar]) -> None:
    opened.flush()
    for row in held:
        opened.expunge(row)
    held.clear()


def count(read: PartRead, opened: Session, export: Export, part: ExportPart) -> None:
    rows = recorded(opened, export, part)
    stored = set(opened.execute(select(TakeoutSidecar.entry_id).where(TakeoutSidecar.export_id == export.id,
                                                                      TakeoutSidecar.part_number == part.number)).scalars())
    read.entries = len(rows)
    read.media = sum(1 for row in rows if row.kind == "media")
    read.sidecars = sum(1 for row in rows if row.kind == "sidecar")
    read.sidecars_unread = sum(1 for row in rows if row.kind == "sidecar" and row.id not in stored)
    read.other = read.entries - read.media - read.sidecars


def scratch_name(row: TakeoutEntry) -> str:
    suffix = PurePosixPath(row.name).suffix.lower()
    return f"{row.id}{suffix if SCRATCH_SUFFIX.match(suffix) else ''}"


def batches_of(rows: list[TakeoutEntry], files: int, size: int) -> Iterator[list[TakeoutEntry]]:
    batch: list[TakeoutEntry] = []
    held = 0
    for row in rows:
        if batch and (len(batch) >= files or held + row.bytes > size):
            yield batch
            batch, held = [], 0
        batch.append(row)
        held += row.bytes
    if batch:
        yield batch


def read_chunk(source: IO[bytes], number: int) -> bytes:
    try:
        return source.read(unpack.CHUNK)
    except READ_ERRORS as error:
        raise damaged(number) from error


def entry_chunks(archive: zipfile.ZipFile, info: zipfile.ZipInfo, number: int, seen: list[int]) -> Iterator[bytes]:
    try:
        source = archive.open(info)
    except READ_ERRORS as error:
        raise damaged(number) from error
    with source:
        while chunk := read_chunk(source, number):
            seen[0] += len(chunk)
            if seen[0] > info.file_size:
                raise PartRefused("size_lies", SIZE_LIES)
            yield chunk


def stream_one(photos: Photos, person: Person, export: Export, archive: zipfile.ZipFile, row: TakeoutEntry, info: zipfile.ZipInfo) -> Streamed:
    target = paths.scratch_file(photos, person.slug, unpack.staging_name(export), scratch_name(row))
    seen = [0]
    sha1 = paths.write_inbox_once(photos, target, entry_chunks(archive, info, row.part_number, seen))
    if seen[0] != row.bytes:
        raise PartRefused("size_lies", SIZE_LIES)
    return Streamed(row.id, row.name, target, sha1, row.bytes)


def stream_batch(opened: Session, photos: Photos, person: Person, export: Export, archive: zipfile.ZipFile, batch: list[TakeoutEntry],
                 infos: dict[str, zipfile.ZipInfo], part: ExportPart) -> list[Streamed]:
    streamed = []
    for row in batch:
        found = stream_one(photos, person, export, archive, row, infos[row.name])
        if row.sha1 is not None and row.sha1 != found.sha1:
            opened.rollback()
            raise PartRefused("bytes_differ", f"part {part.number}'s files don't match the part read before")
        row.sha1 = found.sha1
        streamed.append(found)
    opened.commit()
    return streamed


def stream_media(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart, archive: zipfile.ZipFile,
                 listed: list[Listed], each_batch: EachBatch | None, files: int, size: int, read: PartRead) -> None:
    rows = [row for row in recorded(opened, export, part) if row.kind == "media" and row.outcome == "listed"]
    infos = {entry.name: entry.info for entry in listed}
    folder = unpack.staging_name(export)
    for batch in batches_of(rows, files, size):
        streamed = stream_batch(opened, photos, person, export, archive, batch, infos, part)
        if each_batch is not None:
            each_batch(opened, streamed)
        paths.sweep_scratch(photos, person.slug, folder)
        read.streamed += len(streamed)
        read.batches += 1


def read_part(opened: Session, photos: Photos, person: Person, export: Export, part: ExportPart, now: float, each_batch: EachBatch | None = None,
              batch_files: int = BATCH_FILES, batch_bytes: int = BATCH_BYTES) -> PartRead:
    read = PartRead()
    path = unpack.part_path(export, part)
    folder = unpack.staging_name(export)
    try:
        with unpack.open_part(photos, path, part.number) as handle, zip_of(handle, path, part.number) as archive:
            infos = archive.infolist()
            central_checks(infos, part.number)
            unpack.test_archive(archive, path, part.number)
            listed = entry_listing(infos)
            paths.sweep_scratch(photos, person.slug, folder)
            try:
                record_listing(opened, archive, export, part, listed, now, read)
                stream_media(opened, photos, person, export, part, archive, listed, each_batch, batch_files, batch_bytes, read)
            finally:
                paths.sweep_scratch(photos, person.slug, folder)
    except unpack.UnpackFailed as error:
        opened.rollback()
        raise refused_by_unpack(error) from error
    except BaseException:
        opened.rollback()
        raise
    count(read, opened, export, part)
    return read


def zip_of(handle: BinaryIO, path: Path, number: int) -> zipfile.ZipFile:
    try:
        return unpack.zip_of(handle, path, number)
    except (ValueError, OverflowError) as error:
        raise unpack.not_a_zip(path, number) from error
