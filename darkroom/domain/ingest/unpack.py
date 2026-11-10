from __future__ import annotations

import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.storage import paths
from darkroom.storage.models import Export, ExportPart, GoogleAccount, Person
from darkroom.storage.paths import Photos

PART_NAME = re.compile(r"^(?P<stamp>.+?)-(?P<number>\d{1,4})\.zip$", re.IGNORECASE)
UNPACKED_MARKER_FOLDER = ".darkroom-unpacked"
CHUNK = 1024 * 1024
SYMLINK_MODE = 0o120000
LINK_IN_THE_INBOX = "a link is in the export's inbox folder"
OUTSIDE_THE_INBOX = "the export's staging folder is not inside the inbox"


class UnpackFailed(Exception):
    def __init__(self, message: str, public: str | None = None) -> None:
        super().__init__(message)
        self.public = public or message


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


def part_path(export: Export, part: ExportPart) -> Path:
    return Path(export.inbox_dir or "") / (part.filename or "")


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
            handle = paths.open_import_part(photos, path)
        except FileNotFoundError as error:
            part.status = "failed"
            raise UnpackFailed(f"part {part.number} ({path.name}) is not in the inbox", f"part {part.number} is not in the inbox") from error
        except paths.OutsideRoot as error:
            part.status = "failed"
            raise UnpackFailed(f"part {part.number} ({path.name}) is a link or not a plain file", f"part {part.number} is a link or not a plain file") from error
        with handle:
            if os.fstat(handle.fileno()).st_size == 0:
                part.status = "corrupt"
                raise UnpackFailed(f"part {part.number} ({path.name}) is empty", f"part {part.number} is empty")
            try:
                with zipfile.ZipFile(handle) as archive:
                    bad = archive.testzip()
            except zipfile.BadZipFile as error:
                part.status = "corrupt"
                raise UnpackFailed(f"part {part.number} ({path.name}) is not a valid zip", f"part {part.number} is not a valid zip") from error
        if bad is not None:
            part.status = "corrupt"
            raise UnpackFailed(f"part {part.number} ({path.name}) has a damaged file inside", f"part {part.number} has a damaged file inside")
        checked.append((part, path))
    return checked


def safe_target(staging: Path, name: str) -> Path:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise UnpackFailed(f"an entry would be written outside the staging folder: {pure.name}", "an entry would be written outside the staging folder")
    target = (staging / pure).resolve()
    if not target.is_relative_to(staging.resolve()):
        raise UnpackFailed(f"an entry would be written outside the staging folder: {pure.name}", "an entry would be written outside the staging folder")
    return target


def entry_parts(name: str) -> tuple[str, ...]:
    return PurePosixPath(name.replace("\\", "/")).parts


def extract(photos: Photos, path: Path, staging: Path, staged: int) -> int:
    written = 0
    with paths.open_import_part(photos, path) as handle, zipfile.ZipFile(handle) as archive:
        for info in archive.infolist():
            if (info.external_attr >> 16) & 0o170000 == SYMLINK_MODE:
                raise UnpackFailed(f"an entry is a symbolic link: {PurePosixPath(info.filename).name}", "an entry is a symbolic link")
            safe_target(staging, info.filename)
            parts = entry_parts(info.filename)
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


def group_manual_zips(folder: Path) -> dict[tuple[Path, str], dict[int, Path]]:
    groups: dict[tuple[Path, str], dict[int, Path]] = {}
    for path in sorted(folder.rglob("*.zip")) if folder.is_dir() and not folder.is_symlink() else []:
        if path.is_symlink() or path.resolve() != path:
            continue
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


def register_zips(opened: Session, person: Person, folder: Path, now: float, account_email: str | None = None) -> list[Export]:
    account = chosen_account(opened, person, account_email)
    registered = []
    for (inbox, stamp), numbered in group_manual_zips(folder).items():
        export = opened.execute(select(Export).where(Export.account_id == account.id, Export.google_id == stamp, Export.inbox_dir == str(inbox))).scalars().first()
        if export is None:
            export = Export(account_id=account.id, kind="manual", google_id=stamp, created_at=now, ready_at=now, status="downloaded",
                            inbox_dir=str(inbox), part_count=max(numbered), owner_person_id=person.id)
            opened.add(export)
            opened.flush()
        known = {part.number for part in opened.execute(select(ExportPart).where(ExportPart.export_id == export.id)).scalars()}
        for number, path in sorted(numbered.items()):
            if number not in known:
                opened.add(ExportPart(export_id=export.id, number=number, filename=path.name, bytes=path.stat().st_size, status="done"))
        registered.append(export)
    opened.flush()
    return registered


def inspect_manual_inbox(opened: Session, photos: Photos, person: Person, now: float, account_email: str | None = None) -> list[Export]:
    try:
        folder = photos.manual_inbox(person.slug)
    except paths.LinkRefused as error:
        raise UnpackFailed(f"{person.slug}'s manual inbox folder is a link, so nothing in it is imported") from error
    return register_zips(opened, person, folder, now, account_email)
