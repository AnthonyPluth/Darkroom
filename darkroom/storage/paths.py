from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NAME = re.compile(r"^[A-Za-z0-9._~ -]{1,200}$")
CHUNK = 1024 * 1024
SIDECAR_SUFFIX = ".xmp"
DROPPING = ".dropping-"


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
        return self.inside(self.person_part(person), "originals", ".uploads_tmp")

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
        except OSError:
            for path in reversed(made):
                with contextlib.suppress(OSError):
                    path.rmdir()
            raise
        return folders


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


def drop_library_copy(photos: Photos, relative: str, kept: Path, with_sidecar: bool = True) -> None:
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
        for path, aside in reversed(staged):
            os.rename(aside, path)
        raise
    for _, aside in staged:
        with contextlib.suppress(OSError):
            aside.unlink()


def sweep_dropping(photos: Photos, older_than: float, copy_kept: Callable[[Path, str], bool]) -> int:
    swept = 0
    for folder in photos.kept_folders():
        if folder.name != "google-only" or folder.is_symlink() or not folder.is_dir():
            continue
        for path in sorted(folder.rglob(f"{DROPPING}*")):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
                continue
            if path.stat().st_ctime >= older_than or not copy_kept(path, file_sha1(path)):
                continue
            path.unlink()
            swept += 1
    return swept
