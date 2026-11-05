from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NAME = re.compile(r"^[A-Za-z0-9._~ -]{1,200}$")
CHUNK = 1024 * 1024
SIDECAR_SUFFIX = ".xmp"
DROPPING = ".dropping-"
SET_ASIDE = re.compile(rf"^{re.escape(DROPPING)}[0-9a-f]{{16}}-(.+)$")
SET_ASIDE_RECORD = re.compile(rf"^{re.escape(DROPPING)}([0-9a-f]{{16}})\.json$")
TOKEN = len(DROPPING) + 16
PLACING = ".placing-"
PLACING_RECORD = re.compile(rf"^{re.escape(PLACING)}([0-9a-f]{{16}})\.json$")
OLD_PLACING_MARKER = re.compile(rf"^{re.escape(PLACING)}([0-9a-f]{{16}})-(.+)$")
UPLOADS_TMP = ".uploads_tmp"
DOWNLOADS = ".downloads"
SHARED_FOLDER_MODE = 0o2770


class OutsideRoot(ValueError):
    pass


class AlreadyExists(FileExistsError):
    pass


class NotASidecar(ValueError):
    pass


class ProfileNotRemoved(OSError):
    pass


class NotInTheInbox(ValueError):
    pass


class NotALibraryCopy(ValueError):
    pass


class Photos:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def inside(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise OutsideRoot(f"{'/'.join(parts)} is outside the photo folder")
        return path

    def person_part(self, slug: str) -> str:
        if not SLUG.match(slug):
            raise OutsideRoot(f"{slug!r} is not a valid folder name")
        return slug

    def originals(self, person: str) -> Path:
        return self.inside(self.person_part(person), "originals")

    def original(self, person: str, relative: str) -> Path:
        folder = self.originals(person)
        path = self.inside(relative)
        if not path.is_relative_to(folder) or path == folder:
            raise OutsideRoot("not a file in the person's originals folder")
        return path

    def google_only(self, person: str, year: int, month: int) -> Path:
        if not (1900 <= year <= 2200 and 1 <= month <= 12):
            raise OutsideRoot("not a valid year and month")
        return self.inside(self.person_part(person), "google-only", f"{year:04d}", f"{month:02d}")

    def google_only_undated(self, person: str) -> Path:
        return self.inside(self.person_part(person), "google-only", "undated")

    def inbox(self, person: str) -> Path:
        return self.inside("_inbox", self.person_part(person))

    def manual_inbox(self, person: str) -> Path:
        return self.inside("_inbox", self.person_part(person), "manual")

    def staging(self, person: str, export: str) -> Path:
        return self.inside("_inbox", self.person_part(person), self.name_part(export), "staging")

    def review_hold(self, person: str, export: str) -> Path:
        return self.inside("_inbox", self.person_part(person), "review", self.name_part(export))

    def uploads_tmp(self, person: str) -> Path:
        return self.inside(self.person_part(person), "originals", UPLOADS_TMP)

    def thumbs(self, person: str) -> Path:
        return self.inside("_inbox", self.person_part(person), ".thumbs")

    def browser_profile(self, account: str) -> Path:
        return self.inside("_browser", self.name_part(account), "profile")

    def failures(self, account: str, job: str) -> Path:
        return self.inside("_browser", self.name_part(account), "failures", self.name_part(job))

    def name_part(self, name: str) -> str:
        if not NAME.match(name) or name in {".", ".."} or name.startswith("."):
            raise OutsideRoot(f"{name!r} is not a valid folder name")
        return name

    def retire_browser_account(self, account: str) -> None:
        name = self.name_part(account)
        parent = self.root / "_browser"
        target = parent / name
        if parent.is_symlink() or target.is_symlink():
            raise ProfileNotRemoved("the account's browser folder is a link, so it was not removed")
        if not target.exists():
            return
        if target.resolve() != self.browser_profile(name).parent:
            raise ProfileNotRemoved("the account's browser folder is not where it should be, so it was not removed")
        try:
            shutil.rmtree(target)
        except OSError as error:
            raise ProfileNotRemoved("the account's browser folder could not be removed") from error

    def retire_account_downloads(self, account: str) -> None:
        name = self.name_part(account)
        inbox = self.root / "_inbox"
        target = inbox / DOWNLOADS / name
        if any(each.is_symlink() for each in (inbox, inbox / DOWNLOADS, target)):
            raise ProfileNotRemoved("the account's downloads folder is a link, so it was not removed")
        if not target.exists():
            return
        if not target.is_dir() or target.resolve() != target:
            raise ProfileNotRemoved("the account's downloads folder is not where it should be, so it was not removed")
        try:
            shutil.rmtree(target)
        except OSError as error:
            raise ProfileNotRemoved("the account's downloads folder could not be removed") from error

    def share_inbox(self) -> list[Path]:
        inbox = self.root / "_inbox"
        made = [make_shared(inbox), make_shared(inbox / DOWNLOADS)]
        for each in sorted(inbox.iterdir()):
            if SLUG.match(each.name) and not each.is_symlink() and each.is_dir():
                share_folder(each)
                made.append(each)
        return made

    def make_export_folder(self, person: str, export: str) -> Path:
        inbox = self.root / "_inbox"
        make_shared(inbox)
        make_shared(inbox / self.person_part(person))
        return make_shared(inbox / self.person_part(person) / self.name_part(export))

    def inbox_root(self) -> Path:
        return self.inside("_inbox")

    def export_tree(self, person: str, export: str) -> Path:
        return self.inside("_inbox", self.person_part(person), self.name_part(export))

    def google_only_file(self, relative: str) -> Path:
        parts = relative.split("/")
        if len(parts) < 3 or parts[1] != "google-only" or any(part in {"", ".", ".."} for part in parts):
            raise NotALibraryCopy("not a file in a person's google-only folder")
        folder = self.inside(self.person_part(parts[0]), "google-only")
        path = self.root.joinpath(*parts)
        if path.is_symlink() or not path.is_file() or path.resolve() != path or not path.is_relative_to(folder):
            raise NotALibraryCopy("not a file in a person's google-only folder")
        return path

    def kept_folders(self) -> list[Path]:
        found = []
        for child in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if child.name.startswith("_"):
                continue
            found += [child / "originals", child / "google-only"]
        return found

    def removable(self, path: Path) -> Path:
        if path.is_symlink():
            raise NotInTheInbox("a link is never removed through")
        resolved = path.resolve()
        inbox = self.inbox_root()
        if not resolved.is_relative_to(inbox) or resolved == inbox or resolved.parent == inbox:
            raise NotInTheInbox("only what is inside a person's inbox folder is removed")
        for kept in self.kept_folders():
            if resolved.is_relative_to(kept.resolve()) or kept.resolve().is_relative_to(resolved):
                raise NotInTheInbox("originals and google-only folders are never removed")
        return resolved

    def ensure_person_tree(self, person: str) -> list[Path]:
        folders = [self.originals(person), self.inside(self.person_part(person), "google-only"), self.inbox(person), self.manual_inbox(person)]
        made: list[Path] = []
        try:
            for folder in folders:
                missing: list[Path] = []
                ancestor = folder
                while ancestor != self.root and not ancestor.exists():
                    missing.append(ancestor)
                    ancestor = ancestor.parent
                for path in reversed(missing):
                    path.mkdir()
                    made.append(path)
                    if path.is_relative_to(self.root / "_inbox"):
                        share_folder(path)
        except OSError:
            for path in reversed(made):
                with contextlib.suppress(OSError):
                    path.rmdir()
            raise
        return folders


def share_folder(path: Path) -> None:
    found = path.lstat()
    if not stat.S_ISDIR(found.st_mode):
        raise NotInTheInbox(f"{path.name} is not a folder")
    if found.st_uid != os.geteuid():
        return
    if found.st_gid != os.getegid():
        os.chown(path, -1, os.getegid(), follow_symlinks=False)
    if stat.S_IMODE(path.lstat().st_mode) != SHARED_FOLDER_MODE:
        os.chmod(path, SHARED_FOLDER_MODE)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the shared inbox group (gid 10010) needs rwx, others get nothing


def make_shared(path: Path) -> Path:
    with contextlib.suppress(FileExistsError):
        path.mkdir()
    share_folder(path)
    return path


def copy_exclusive(source: Path, destination: Path) -> None:
    try:
        target = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as error:
        raise AlreadyExists(destination.name) from error
    try:
        with os.fdopen(target, "wb") as out, source.open("rb") as origin:
            while chunk := origin.read(CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def write_once(destination: Path, chunks: Iterable[bytes]) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".write-once-")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as out:
            for chunk in chunks:
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if destination.exists():
            raise AlreadyExists(destination.name)
        try:
            os.link(temp, destination)
        except FileExistsError as error:
            raise AlreadyExists(destination.name) from error
        except OSError:
            copy_exclusive(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return digest.hexdigest()


def placing_token() -> str:
    return secrets.token_hex(8)


def placing_record(destination: Path, token: str) -> Path:
    return destination.with_name(f"{PLACING}{token}.json")


@dataclass(frozen=True)
class Placing:
    name: str
    sha1: str
    made: bool
    new_sidecar: bool
    sidecar_sha1: str | None = None


def mark_placing(destination: Path, token: str, placing: Placing) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = {"name": placing.name, "sha1": placing.sha1, "made": placing.made, "sidecar": placing.new_sidecar, "sidecar_sha1": placing.sidecar_sha1}
    handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".write-once-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(json.dumps(data).encode())
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_name, placing_record(destination, token))
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_placing(record: Path) -> Placing | None:
    try:
        data = json.loads(record.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"name", "sha1", "made", "sidecar", "sidecar_sha1"}:
        return None
    name, sha1, made, new_sidecar, sidecar_sha1 = data["name"], data["sha1"], data["made"], data["sidecar"], data["sidecar_sha1"]
    if not (isinstance(name, str) and name and not name.startswith(".") and "/" not in name and "\0" not in name
            and isinstance(sha1, str) and re.fullmatch(r"[0-9a-f]{40}", sha1) and type(made) is bool and type(new_sidecar) is bool
            and (sidecar_sha1 is None or (new_sidecar and isinstance(sidecar_sha1, str) and re.fullmatch(r"[0-9a-f]{40}", sidecar_sha1)))):
        return None
    return Placing(name, sha1, made, new_sidecar, sidecar_sha1)


def present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def clear_placing(destination: Path, token: str, committed: bool) -> None:
    record = placing_record(destination, token)
    found = None if committed else read_placing(record)
    if found is not None and ((found.made and present(destination)) or (found.new_sidecar and present(sidecar_of(destination)))):
        return
    with contextlib.suppress(OSError):
        record.unlink(missing_ok=True)


class Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class IncompleteUpload(OSError):
    pass


class NotAnUpload(ValueError):
    pass


def upload_temp(path: Path) -> Path:
    if path.is_symlink() or path.parent.name != UPLOADS_TMP or path.parent.parent.name != "originals":
        raise NotAnUpload("only a file in a person's upload folder is an upload")
    return path


def receive_upload(folder: Path, stream: Readable, length: int) -> tuple[Path, str]:
    if folder.name != UPLOADS_TMP or folder.parent.name != "originals" or folder.is_symlink():
        raise NotAnUpload("uploads are received only into a person's upload folder")
    folder.mkdir(parents=False, exist_ok=True)
    temp = folder / uuid.uuid4().hex
    digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(handle, "wb") as out:
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    raise IncompleteUpload("the upload ended before its Content-Length")
                digest.update(chunk)
                out.write(chunk)
                remaining -= len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp, digest.hexdigest()


def discard_upload(temp: Path) -> None:
    upload_temp(temp).unlink(missing_ok=True)


def adopt_upload(temp: Path, destination: Path) -> None:
    upload_temp(temp)
    if destination.exists() or destination.is_symlink():
        raise AlreadyExists(destination.name)
    try:
        os.link(temp, destination)
    except FileExistsError as error:
        raise AlreadyExists(destination.name) from error
    except OSError:
        copy_exclusive(temp, destination)
    temp.unlink(missing_ok=True)


def sidecar_of(media: Path) -> Path:
    return media.with_name(f"{media.name}{SIDECAR_SUFFIX}")


def checked_sidecar(destination: Path) -> Path:
    if destination.suffix != SIDECAR_SUFFIX or destination.name == SIDECAR_SUFFIX:
        raise NotASidecar("a sidecar's name ends in .xmp")
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise NotASidecar("the sidecar's place holds something that is not a sidecar")
    return destination


def write_sidecar(destination: Path, render: Callable[[Path, Path | None], None]) -> None:
    checked_sidecar(destination)
    source = destination if destination.exists() else None
    before = None if source is None else source.read_bytes()
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".xmp-") as folder:
        temp = Path(folder) / destination.name
        render(temp, source)
        if not temp.is_file():
            raise NotASidecar("no sidecar was written")
        checked_sidecar(destination)
        if source is not None:
            if not source.is_file() or source.read_bytes() != before:
                raise AlreadyExists(destination.name)
            os.replace(temp, destination)
            return
        try:
            os.link(temp, destination)
        except FileExistsError as error:
            raise AlreadyExists(destination.name) from error
        except OSError:
            copy_exclusive(temp, destination)


def write_new_sidecar(destination: Path, write: Callable[[Path], bool], recording: Callable[[str], None]) -> bool:
    checked_sidecar(destination)
    if present(destination):
        raise AlreadyExists(destination.name)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".xmp-") as folder:
        temp = Path(folder) / destination.name
        if not write(temp):
            return False
        if not regular(temp):
            raise NotASidecar("no sidecar was written")
        recording(file_sha1(temp))
        try:
            os.link(temp, destination)
        except FileExistsError as error:
            raise AlreadyExists(destination.name) from error
        except OSError:
            copy_exclusive(temp, destination)
    return True


def remove_inbox_tree(photos: Photos, path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    target = photos.removable(path)
    if not target.is_dir():
        raise NotInTheInbox("not a folder")
    shutil.rmtree(target)
    return True


def remove_inbox_file(photos: Photos, path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    target = photos.removable(path)
    if not target.is_file():
        raise NotInTheInbox("not a file")
    target.unlink()
    return True


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def carry_sidecar(source: Path, destination: Path) -> bool:
    found = sidecar_of(source)
    if found.is_symlink() or not found.is_file():
        return False
    target = checked_sidecar(sidecar_of(destination))
    data = found.read_bytes()
    if target.exists():
        if target.read_bytes() == data:
            return True
        raise AlreadyExists(target.name)
    write_once(target, [data])
    return True


class SetAside:
    def __init__(self, staged: list[tuple[Path, Path]], records: list[Path] | None = None) -> None:
        self.staged = staged
        self.records = records or []

    def finish(self) -> None:
        for _, aside in self.staged:
            with contextlib.suppress(OSError):
                aside.unlink()
        if any(aside.exists() or aside.is_symlink() for _, aside in self.staged):
            return
        for record in self.records:
            with contextlib.suppress(OSError):
                record.unlink()

    def restore(self) -> None:
        taken = [path for path, aside in reversed(self.staged) if not move_without_replacing(aside, path)]
        if taken:
            raise AlreadyExists(taken[0].name)
        for record in self.records:
            with contextlib.suppress(OSError):
                record.unlink()


def record_of(aside: Path) -> Path:
    return aside.with_name(f"{aside.name[:TOKEN]}.json")


@dataclass(frozen=True)
class SidecarRecord:
    sha1: str
    review_id: int
    undone_before: int


def read_record(record: Path) -> SidecarRecord | None:
    try:
        data = json.loads(record.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"sha1", "review", "undone"}:
        return None
    sha1, review_id, undone_before = data["sha1"], data["review"], data["undone"]
    if not (isinstance(sha1, str) and re.fullmatch(r"[0-9a-f]{40}", sha1) and type(review_id) is int and type(undone_before) is int
            and review_id > 0 and undone_before >= 0):
        return None
    return SidecarRecord(sha1, review_id, undone_before)


def drop_new_sidecar(photos: Photos, relative: str) -> None:
    sidecar = sidecar_of(photos.google_only_file(relative))
    if sidecar.is_file() and not sidecar.is_symlink():
        sidecar.unlink()


def restore_sidecar(destination: Path, data: bytes) -> None:
    checked_sidecar(destination)
    if destination.is_file() and destination.read_bytes() == data:
        return
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".xmp-") as folder:
        temp = Path(folder) / destination.name
        temp.write_bytes(data)
        checked_sidecar(destination)
        os.replace(temp, destination)


def set_sidecar_aside(photos: Photos, relative: str, review_id: int, undone_before: int) -> SetAside:
    photo = photos.google_only_file(relative)
    sidecar = sidecar_of(photo)
    if sidecar.is_symlink() or not sidecar.is_file():
        return SetAside([])
    aside = sidecar.with_name(f"{DROPPING}{secrets.token_hex(8)}-{sidecar.name}")
    record = record_of(aside)
    write_once(record, [json.dumps({"sha1": file_sha1(photo), "review": review_id, "undone": undone_before}).encode()])
    try:
        os.rename(sidecar, aside)
    except OSError:
        record.unlink(missing_ok=True)
        raise
    return SetAside([(sidecar, aside)], [record])


def drop_library_copy(photos: Photos, relative: str, kept: Path, with_sidecar: bool = True) -> None:
    set_library_copy_aside(photos, relative, kept, with_sidecar).finish()


def set_library_copy_aside(photos: Photos, relative: str, kept: Path, with_sidecar: bool = True) -> SetAside:
    target = photos.google_only_file(relative)
    if kept.is_symlink() or not kept.is_file() or kept.resolve() == target or not kept.resolve().is_relative_to(photos.root):
        raise NotALibraryCopy("no other copy of the file is kept")
    if file_sha1(kept) != file_sha1(target):
        raise NotALibraryCopy("the kept copy is not the same file")
    sidecar = sidecar_of(target)
    going = [target, sidecar] if with_sidecar and sidecar.is_file() and not sidecar.is_symlink() else [target]
    token = secrets.token_hex(8)
    staged: list[tuple[Path, Path]] = []
    try:
        for path in going:
            aside = path.with_name(f"{DROPPING}{token}-{path.name}")
            os.rename(path, aside)
            staged.append((path, aside))
    except OSError:
        SetAside(staged).restore()
        raise
    return SetAside(staged)


@dataclass
class Swept:
    dropped: int = 0
    restored: int = 0


def set_aside_name(path: Path) -> Path | None:
    match = SET_ASIDE.match(path.name)
    return None if match is None else path.with_name(match.group(1))


def move_without_replacing(path: Path, destination: Path) -> bool:
    try:
        os.link(path, destination)
    except OSError:
        return False
    path.unlink()
    return True


def put_back(path: Path, original: Path) -> bool:
    if not move_without_replacing(path, original):
        return False
    sidecar = sidecar_of(path)
    if sidecar.is_file() and not sidecar.is_symlink():
        move_without_replacing(sidecar, sidecar_of(original))
    return True


def put_sidecar_back(path: Path, recorded: Callable[[Path, str], bool], undo_committed: Callable[[int, int], bool | None]) -> str | None:
    original, record = set_aside_name(path), record_of(path)
    found = None if original is None or record.is_symlink() or not record.is_file() else read_record(record)
    if original is None or found is None:
        return None
    committed = undo_committed(found.review_id, found.undone_before)
    if committed:
        path.unlink()
        record.unlink()
        return "dropped"
    photo = original.with_name(original.name.removesuffix(SIDECAR_SUFFIX))
    if committed is None or photo.is_symlink() or not photo.is_file() or file_sha1(photo) != found.sha1 or not recorded(photo, found.sha1):
        return None
    if not move_without_replacing(path, original):
        return None
    record.unlink()
    return "restored"


def leftovers(photos: Photos, prefix: str) -> Iterator[list[Path]]:
    for folder in photos.kept_folders():
        if folder.name != "google-only" or folder.is_symlink() or not folder.is_dir():
            continue
        yield [path for path in sorted(folder.rglob(f"{prefix}*"))
               if not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(folder.resolve())]


def settled(path: Path, older_than: float) -> bool:
    return path.stat().st_ctime < older_than


def written_before(record: Path, older_than: float) -> bool:
    found = record.stat()
    return max(found.st_mtime, found.st_ctime) < older_than


def sweep_dropping(photos: Photos, older_than: float, copy_kept: Callable[[Path, str], bool], recorded: Callable[[Path, str], bool],
                   undo_committed: Callable[[int, int], bool | None]) -> Swept:
    swept = Swept()
    for found in leftovers(photos, DROPPING):
        records = [path for path in found if SET_ASIDE_RECORD.match(path.name)]
        for path in [path for path in found if path.suffix != SIDECAR_SUFFIX and path not in records and settled(path, older_than)]:
            sha1, original = file_sha1(path), set_aside_name(path)
            if original is not None and recorded(original, sha1) and put_back(path, original):
                swept.restored += 1
            elif copy_kept(path, sha1):
                path.unlink()
                swept.dropped += 1
        for path in [path for path in found if path.suffix == SIDECAR_SUFFIX and path.is_file()]:
            if record_of(path).is_file():
                if written_before(record_of(path), older_than):
                    outcome = put_sidecar_back(path, recorded, undo_committed)
                    swept.restored += outcome == "restored"
                    swept.dropped += outcome == "dropped"
                continue
            if not settled(path, older_than):
                continue
            photo = path.with_name(path.name.removesuffix(SIDECAR_SUFFIX))
            original = set_aside_name(photo)
            if photo.exists() or photo.is_symlink() or (original is not None and (original.exists() or original.is_symlink())):
                continue
            path.unlink()
            swept.dropped += 1
        for record in records:
            if record.is_file() and written_before(record, older_than) and not any(record.parent.glob(f"{record.name.removesuffix('.json')}-*")):
                record.unlink(missing_ok=True)
    return swept


def regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def regular_sha1(path: Path) -> str | None:
    return file_sha1(path) if regular(path) else None


def take_back(record: Path, placing: Placing, copy_kept: Callable[[Path, str], bool], recorded: Callable[[Path, str], bool]) -> bool:
    made = record.with_name(placing.name)
    sidecar = sidecar_of(made)
    sha1 = regular_sha1(made)
    kept_by_a_row = sha1 is not None and recorded(made, sha1)
    ours = placing.made and sha1 == placing.sha1 and not kept_by_a_row
    if ours and not copy_kept(made, placing.sha1):
        return False
    if placing.sidecar_sha1 is not None and sha1 == placing.sha1 and regular_sha1(sidecar) == placing.sidecar_sha1 and not kept_by_a_row:
        sidecar.unlink()
    if ours:
        made.unlink()
    record.unlink()
    return ours


def take_back_old_copy(marker: Path, token: str, made: Path, copy_kept: Callable[[Path, str], bool], recorded: Callable[[Path, str], bool]) -> bool:
    sha1, new_sidecar = file_sha1(marker), marker.with_name(f"{PLACING}{token}-{sidecar_of(made).name}")
    if not regular(made) or not os.path.samefile(marker, made):
        if copy_kept(marker, sha1):
            marker.unlink()
        return False
    if recorded(made, sha1):
        marker.unlink()
        new_sidecar.unlink(missing_ok=True)
        return False
    if not copy_kept(made, sha1):
        return False
    if new_sidecar.is_file():
        if regular(sidecar_of(made)):
            sidecar_of(made).unlink()
        new_sidecar.unlink()
    made.unlink()
    marker.unlink()
    return True


def take_back_old_sidecar(marker: Path, token: str, made: Path, recorded: Callable[[Path, str], bool]) -> None:
    photo = made.with_name(made.name.removesuffix(SIDECAR_SUFFIX))
    if marker.with_name(f"{PLACING}{token}-{photo.name}").exists():
        return
    if regular(made) and not (regular(photo) and recorded(photo, file_sha1(photo))):
        made.unlink()
    marker.unlink()


def sweep_old_marker(marker: Path, older_than: float, committed: Callable[[str], bool], copy_kept: Callable[[Path, str], bool],
                     recorded: Callable[[Path, str], bool]) -> bool:
    match = OLD_PLACING_MARKER.match(marker.name)
    if match is None or not regular(marker) or not settled(marker, older_than):
        return False
    token, made = match.group(1), marker.with_name(match.group(2))
    if committed(token):
        marker.unlink()
        return False
    if marker.suffix == SIDECAR_SUFFIX:
        take_back_old_sidecar(marker, token, made, recorded)
        return False
    return take_back_old_copy(marker, token, made, copy_kept, recorded)


def sweep_placing(photos: Photos, older_than: float, committed: Callable[[str], bool], copy_kept: Callable[[Path, str], bool],
                  recorded: Callable[[Path, str], bool]) -> int:
    taken_back = 0
    for found in leftovers(photos, PLACING):
        for record in sorted(found, key=lambda path: path.suffix == SIDECAR_SUFFIX):
            match = PLACING_RECORD.match(record.name)
            if match is None:
                taken_back += sweep_old_marker(record, older_than, committed, copy_kept, recorded)
                continue
            placing = None if not written_before(record, older_than) else read_placing(record)
            if placing is None:
                continue
            if committed(match.group(1)):
                record.unlink()
            elif take_back(record, placing, copy_kept, recorded):
                taken_back += 1
    return taken_back
