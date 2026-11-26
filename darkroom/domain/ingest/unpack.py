from __future__ import annotations

import lzma
import os
import re
import shutil
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.storage import db, paths
from darkroom.storage.models import Export, ExportPart, GoogleAccount, Person
from darkroom.storage.paths import Photos

DECOMPRESS_ERRORS = (zlib.error, lzma.LZMAError, OSError, EOFError, NotImplementedError, RuntimeError)
PART_NAME = re.compile(r"^(?P<stamp>.+?)-(?P<number>\d{1,4})\.zip$", re.IGNORECASE)
UNPACKED_MARKER_FOLDER = ".darkroom-unpacked"
CHUNK = 1024 * 1024
SYMLINK_MODE = 0o120000
LINK_IN_THE_INBOX = "a link is in the export's inbox folder"
SYMLINK_ENTRY = "an entry is a symbolic link"
OUTSIDE_STAGING = "an entry would be written outside the staging folder"
OUTSIDE_THE_INBOX = "the export's staging folder is not inside the inbox"


class UnpackFailed(Exception):
    def __init__(self, message: str, public: str | None = None, status: str | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.public = public or message
        self.status = status
        self.reason = reason


@dataclass
class UnpackReport:
    parts_unpacked: int = 0
    parts_skipped: int = 0
    files: int = 0
    problems: list[str] = field(default_factory=list)
    public: list[str] = field(default_factory=list)


def staging_name(export: Export) -> str:
    return f"export-{export.id}"


def staging_dir(photos: Photos, person: Person, export: Export) -> Path:
    return photos.staging(person.slug, staging_name(export))


def part_folder(export: Export, photos: Photos | None = None, person: Person | None = None) -> Path:
    if export.inbox_dir:
        return Path(export.inbox_dir)
    if photos is None or person is None:
        return Path("")
    try:
        return photos.root / paths.INBOX / photos.person_part(person.slug) / photos.name_part(staging_name(export))
    except paths.OutsideRoot:
        return Path("")


def part_path(export: Export, part: ExportPart, photos: Photos | None = None, person: Person | None = None) -> Path:
    return part_folder(export, photos, person) / (part.filename or "")


def check_parts(photos: Photos, export: Export, parts: list[ExportPart]) -> list[tuple[ExportPart, Path]]:
    numbers = {part.number for part in parts}
    expected = export.part_count or (max(numbers) if numbers else 0)
    for number in range(1, expected + 1):
        if number not in numbers:
            raise UnpackFailed(f"part {number} of {expected} is missing")
    checked = []
    for part in sorted(parts, key=lambda p: p.number):
        path = part_path(export, part)
        try:
            with open_part(photos, path, part.number) as handle, zip_of(handle, path, part.number) as archive:
                test_archive(archive, path, part.number)
        except UnpackFailed as error:
            part.status = error.status or part.status
            raise
        checked.append((part, path))
    return checked


def open_part(photos: Photos, path: Path, number: int) -> BinaryIO:
    try:
        handle = paths.open_import_part(photos, path)
    except FileNotFoundError as error:
        raise UnpackFailed(f"part {number} ({path.name}) is not in the inbox", f"part {number} is not in the inbox", "failed", "missing") from error
    except paths.OutsideRoot as error:
        raise UnpackFailed(f"part {number} ({path.name}) is a link or not a plain file", f"part {number} is a link or not a plain file", "failed", "not_plain") from error
    if os.fstat(handle.fileno()).st_size == 0:
        handle.close()
        raise UnpackFailed(f"part {number} ({path.name}) is empty", f"part {number} is empty", "corrupt", "empty")
    return handle


def not_a_zip(path: Path, number: int) -> UnpackFailed:
    return UnpackFailed(f"part {number} ({path.name}) is not a valid zip", f"part {number} is not a valid zip", "corrupt", "not_a_zip")


def damaged(path: Path, number: int) -> UnpackFailed:
    return UnpackFailed(f"part {number} ({path.name}) has a damaged file inside", f"part {number} has a damaged file inside", "corrupt", "damaged")


def zip_of(handle: BinaryIO, path: Path, number: int) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(handle)
    except zipfile.BadZipFile as error:
        raise not_a_zip(path, number) from error


def test_archive(archive: zipfile.ZipFile, path: Path, number: int) -> None:
    try:
        bad = archive.testzip()
    except zipfile.BadZipFile as error:
        raise not_a_zip(path, number) from error
    except DECOMPRESS_ERRORS as error:
        raise damaged(path, number) from error
    if bad is not None:
        raise damaged(path, number)


def symlink_entry(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == SYMLINK_MODE


def plain_entry(name: str) -> PurePosixPath:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise UnpackFailed(f"{OUTSIDE_STAGING}: {pure.name}", OUTSIDE_STAGING, reason="outside")
    return pure


def safe_target(staging: Path, name: str) -> Path:
    pure = plain_entry(name)
    target = (staging / pure).resolve()
    if not target.is_relative_to(staging.resolve()):
        raise UnpackFailed(f"{OUTSIDE_STAGING}: {pure.name}", OUTSIDE_STAGING, reason="outside")
    return target


def extract(photos: Photos, path: Path, staging: Path, staged: int) -> int:
    written = 0
    with paths.open_import_part(photos, path) as handle, zipfile.ZipFile(handle) as archive:
        for info in archive.infolist():
            if symlink_entry(info):
                raise UnpackFailed(f"{SYMLINK_ENTRY}: {PurePosixPath(info.filename).name}", SYMLINK_ENTRY)
            parts = plain_entry(info.filename).parts
            try:
                safe_target(staging, info.filename)
            except UnpackFailed as error:
                raise paths.LinkRefused("a link is inside the staging folder") from error
            if info.is_dir():
                with paths.inbox_subfolder(staged, parts, create=True):
                    continue
            with paths.inbox_subfolder(staged, parts[:-1], create=True) as folder:
                out = paths.open_inbox_entry(parts[-1], folder, info.file_size)
                if out is None:
                    continue
                with archive.open(info) as source, out:
                    shutil.copyfileobj(source, out, CHUNK)
            written += 1
    return written


def marker_name(part: ExportPart) -> str:
    return f"{part.number:04d}"


def failure_text(error: UnpackFailed | OSError | paths.OutsideRoot) -> tuple[str, str]:
    if isinstance(error, UnpackFailed):
        return str(error), error.public
    if isinstance(error, paths.LinkRefused):
        return LINK_IN_THE_INBOX, LINK_IN_THE_INBOX
    if isinstance(error, paths.OutsideRoot):
        return OUTSIDE_THE_INBOX, OUTSIDE_THE_INBOX
    public = f"{type(error).__name__} while unpacking"
    return public, public


def unpack(opened: Session, photos: Photos, person: Person, export: Export, now: float) -> UnpackReport:
    report = UnpackReport()
    parts = list(opened.execute(select(ExportPart).where(ExportPart.export_id == export.id)).scalars())
    export.status = "unpacking"
    export.stage = "unpack"
    try:
        staging = staging_dir(photos, person, export)
        checked = check_parts(photos, export, parts)
        with (paths.inbox_folder(photos, staging, create=True) as staged,
              paths.inbox_folder(photos, staging.parent / UNPACKED_MARKER_FOLDER, create=True) as markers):
            for part, path in checked:
                if paths.inbox_marker_set(marker_name(part), markers):
                    report.parts_skipped += 1
                    continue
                report.files += extract(photos, path, staging, staged)
                paths.set_inbox_marker(marker_name(part), markers)
                report.parts_unpacked += 1
    except (UnpackFailed, OSError, paths.OutsideRoot) as error:
        export.status = "failed"
        problem, public = failure_text(error)
        report.problems.append(problem)
        report.public.append(public)
        events.record(opened, now, "unpack_failed", person.slug, public[:200])
        opened.flush()
        return report
    export.stage = "unpacked"
    events.record(opened, now, "unpacked", person.slug, f"{report.parts_unpacked} parts, {report.files} files")
    opened.flush()
    return report


def listed_zips(folder: Path) -> list[Path]:
    return sorted(folder.rglob("*.zip")) if folder.is_dir() else []


def plain_zip(folder: Path, path: Path) -> bool:
    return not paths.linked_on_the_way(folder, path.relative_to(folder).parts) and paths.plain_file(path.lstat())


def skipped_zips(folder: Path) -> int:
    return sum(1 for path in listed_zips(folder) if not plain_zip(folder, path))


def group_manual_zips(folder: Path, photos: Photos | None = None) -> dict[tuple[Path, str], dict[int, Path]]:
    groups: dict[tuple[Path, str], dict[int, Path]] = {}
    found_zips = [path for path in listed_zips(folder) if plain_zip(folder, path)] if photos is None else paths.inbox_files(photos, folder, ".zip")
    for path in found_zips:
        found = PART_NAME.match(path.name)
        stamp, number = (found.group("stamp"), int(found.group("number"))) if found else (path.stem, 1)
        groups.setdefault((path.parent, stamp), {})[number] = path
    return groups


def accounts_of(opened: Session, person: Person) -> list[GoogleAccount]:
    return list(opened.execute(select(GoogleAccount).where(GoogleAccount.default_person_id == person.id).order_by(GoogleAccount.id)).scalars())


def chosen_account(opened: Session, person: Person, email: str | None) -> GoogleAccount:
    found = accounts_of(opened, person)
    if email:
        found = [account for account in found if account.email == email]
        if not found:
            raise UnpackFailed(f"{person.slug} has no Google account {email}")
    if not found:
        raise UnpackFailed(f"{person.slug} has no Google account to attach the export to")
    if len(found) > 1:
        raise UnpackFailed(f"{person.slug} has several Google accounts; say which one with --account")
    return found[0]


def recorded_folder(photos: Photos | None, inbox: Path) -> str:
    if photos is None:
        return str(inbox.resolve())
    if paths.linked_on_the_way(photos.root, inbox.relative_to(photos.root).parts):
        raise UnpackFailed("a link is in the manual inbox folder, so nothing in it is imported")
    return str(inbox)


def register_zips(opened: Session, person: Person, folder: Path, now: float, account_email: str | None = None,
                  photos: Photos | None = None, route: str | None = None, guard_photos: Photos | None = None) -> list[Export]:
    from darkroom.domain import managed_switch
    from darkroom.domain.ingest import part_ingest, run

    db.write_lock(opened)
    account = chosen_account(opened, person, account_email)
    registered = []
    try:
        groups = group_manual_zips(folder, photos)
    except paths.LinkRefused as error:
        raise UnpackFailed("a link is in the manual inbox folder, so nothing in it is imported") from error
    for (inbox, stamp), numbered in groups.items():
        canonical = recorded_folder(photos, inbox)
        export = opened.execute(select(Export).where(Export.account_id == account.id, Export.google_id == stamp,
                                                     Export.inbox_dir.in_([canonical, str(inbox)]))).scalars().first()
        if route == run.WHOLE:
            managed_switch.hold(opened, guard_photos or photos, person, "whole_registration" if export is None else "whole_import", export)
        if export is None:
            export = Export(account_id=account.id, kind="manual", google_id=stamp, created_at=now, ready_at=now, status="downloaded",
                            inbox_dir=canonical, part_count=max(numbered), owner_person_id=person.id, ingest_route=route)
            opened.add(export)
            opened.flush()
        export.inbox_dir = canonical
        if export.ingest_route is None and route is not None:
            export.ingest_route = route
        known = {part.number for part in opened.execute(select(ExportPart).where(ExportPart.export_id == export.id)).scalars()}
        for number, path in sorted(numbered.items()):
            if number not in known:
                part = ExportPart(export_id=export.id, number=number, filename=path.name, bytes=path.lstat().st_size, status="done")
                part_ingest.queue(opened, photos, export, part, now)
                opened.add(part)
        registered.append(export)
    opened.flush()
    return registered


def inspect_manual_inbox(opened: Session, photos: Photos, person: Person, now: float, account_email: str | None = None,
                         route: str | None = None) -> list[Export]:
    try:
        folder = photos.manual_inbox(person.slug)
    except paths.LinkRefused as error:
        raise UnpackFailed(f"{person.slug}'s manual inbox folder is a link, so nothing in it is imported") from error
    return register_zips(opened, person, folder, now, account_email, photos, route)
