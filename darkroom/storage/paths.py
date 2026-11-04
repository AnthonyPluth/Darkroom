from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NAME = re.compile(r"^[A-Za-z0-9._~ -]{1,200}$")
CHUNK = 1024 * 1024
SIDECAR_SUFFIX = ".xmp"


class OutsideRoot(ValueError):
    pass


class AlreadyExists(FileExistsError):
    pass


class NotASidecar(ValueError):
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

    def ensure_person_tree(self, person: str) -> list[Path]:
        folders = [self.originals(person), self.inside(self.person_part(person), "google-only"), self.inbox(person), self.manual_inbox(person)]
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
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
