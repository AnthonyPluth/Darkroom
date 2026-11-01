from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.storage.models import Export, ExportPart, GoogleAccount, Person
from darkroom.storage.paths import Photos

PART_NAME = re.compile(r"^(?P<stamp>.+?)-(?P<number>\d{1,4})\.zip$", re.IGNORECASE)
UNPACKED_MARKER_FOLDER = ".darkroom-unpacked"
CHUNK = 1024 * 1024
SYMLINK_MODE = 0o120000


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


def staging_name(export: Export) -> str:
    return f"export-{export.id}"


def staging_dir(photos: Photos, person: Person, export: Export) -> Path:
    return photos.staging(person.slug, staging_name(export))


def part_path(export: Export, part: ExportPart) -> Path:
    return Path(export.inbox_dir or "") / (part.filename or "")


def check_parts(export: Export, parts: list[ExportPart]) -> list[tuple[ExportPart, Path]]:
    numbers = {part.number for part in parts}
    expected = export.part_count or (max(numbers) if numbers else 0)
    for number in range(1, expected + 1):
        if number not in numbers:
            raise UnpackFailed(f"part {number} of {expected} is missing")
    checked = []
    for part in sorted(parts, key=lambda p: p.number):
        path = part_path(export, part)
        if not path.is_file():
            part.status = "failed"
            raise UnpackFailed(f"part {part.number} ({path.name}) is not in the inbox", f"part {part.number} is not in the inbox")
        if path.stat().st_size == 0:
            part.status = "corrupt"
            raise UnpackFailed(f"part {part.number} ({path.name}) is empty", f"part {part.number} is empty")
        try:
            with zipfile.ZipFile(path) as archive:
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


def extract(path: Path, staging: Path) -> int:
    written = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if (info.external_attr >> 16) & 0o170000 == SYMLINK_MODE:
                raise UnpackFailed(f"an entry is a symbolic link: {PurePosixPath(info.filename).name}", "an entry is a symbolic link")
            target = safe_target(staging, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size == info.file_size:
                continue
            with archive.open(info) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out, CHUNK)
            written += 1
    return written


def marker(staging: Path, part: ExportPart) -> Path:
    return staging.parent / UNPACKED_MARKER_FOLDER / f"{part.number:04d}"


def unpack(opened: Session, photos: Photos, person: Person, export: Export, now: float) -> UnpackReport:
    report = UnpackReport()
    parts = list(opened.execute(select(ExportPart).where(ExportPart.export_id == export.id)).scalars())
    export.status = "unpacking"
    export.stage = "unpack"
    staging = staging_dir(photos, person, export)
    try:
        checked = check_parts(export, parts)
        staging.mkdir(parents=True, exist_ok=True)
        for part, path in checked:
            flag = marker(staging, part)
            if flag.exists():
                report.parts_skipped += 1
                continue
            report.files += extract(path, staging)
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("done")
            report.parts_unpacked += 1
    except (UnpackFailed, OSError) as error:
        export.status = "failed"
        public = error.public if isinstance(error, UnpackFailed) else f"{type(error).__name__} while unpacking"
        report.problems.append(str(error) if isinstance(error, UnpackFailed) else public)
        events.record(opened, now, "unpack_failed", person.slug, public[:200])
        opened.flush()
        return report
    export.stage = "unpacked"
    events.record(opened, now, "unpacked", person.slug, f"{report.parts_unpacked} parts, {report.files} files")
    opened.flush()
    return report


def group_manual_zips(folder: Path) -> dict[tuple[Path, str], dict[int, Path]]:
    groups: dict[tuple[Path, str], dict[int, Path]] = {}
    for path in sorted(folder.rglob("*.zip")) if folder.is_dir() else []:
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
                            inbox_dir=str(inbox), part_count=max(numbered))
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
    return register_zips(opened, person, photos.manual_inbox(person.slug), now, account_email)
