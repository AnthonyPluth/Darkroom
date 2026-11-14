from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024
LOCKS = frozenset({"SingletonLock", "SingletonSocket", "SingletonCookie"})
COPYING = ".copying-"
BEFORE = ".before-"

Entry = tuple[str, int, int, str]


class CopyRefused(OSError):
    pass


class CopyDiffers(OSError):
    pass


@dataclass(frozen=True)
class Copied:
    files: int
    folders: int
    locks_left_out: int
    set_aside: Path | None


def digest(path: Path) -> str:
    found = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            found.update(chunk)
    return found.hexdigest()


def walk(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    for current, folders, files in os.walk(root, followlinks=False):
        here = Path(current)
        for name in sorted(folders) + sorted(files):
            path = here / name
            yield path, path.lstat()


def is_lock(path: Path, found: os.stat_result) -> bool:
    return stat.S_ISLNK(found.st_mode) and path.name in LOCKS


def listing(root: Path) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for path, found in walk(root):
        if is_lock(path, found):
            continue
        relative = str(path.relative_to(root))
        mode = stat.S_IMODE(found.st_mode)
        if stat.S_ISDIR(found.st_mode):
            entries[relative] = ("folder", mode, 0, "")
        elif stat.S_ISREG(found.st_mode):
            entries[relative] = ("file", mode, found.st_size, digest(path))
        else:
            raise CopyRefused(f"{relative} is neither a folder nor a regular file, so the browser profiles were not copied")
    return entries


def copy_file(source: Path, target: Path, found: os.stat_result) -> None:
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with source.open("rb") as reading, os.fdopen(handle, "wb") as writing:
        while chunk := reading.read(CHUNK):
            writing.write(chunk)
        writing.flush()
        os.fsync(writing.fileno())
    os.chmod(target, stat.S_IMODE(found.st_mode))
    os.utime(target, ns=(found.st_atime_ns, found.st_mtime_ns))


def copy_tree(source: Path, into: Path) -> tuple[int, int, int]:
    files = folders = locks = 0
    modes: list[tuple[Path, os.stat_result]] = []
    for path, found in walk(source):
        target = into / path.relative_to(source)
        if is_lock(path, found):
            locks += 1
        elif stat.S_ISDIR(found.st_mode):
            os.mkdir(target, 0o700)
            modes.append((target, found))
            folders += 1
        elif stat.S_ISREG(found.st_mode):
            copy_file(path, target, found)
            files += 1
        else:
            raise CopyRefused(f"{path.relative_to(source)} is neither a folder nor a regular file, so the browser profiles were not copied")
    for target, found in reversed(modes):
        os.chmod(target, stat.S_IMODE(found.st_mode))
        os.utime(target, ns=(found.st_atime_ns, found.st_mtime_ns))
    return files, folders, locks


def leftovers(target: Path) -> list[Path]:
    if not target.parent.is_dir():
        return []
    return sorted(each for each in target.parent.iterdir() if each.name.startswith(f".{target.name}{COPYING}"))


def checked(source: Path, target: Path, set_aside: bool) -> None:
    if source.is_symlink() or not source.is_dir():
        raise CopyRefused("the folder to copy the browser profiles from is not a folder (or is a link)")
    if target.is_symlink() or not target.parent.is_dir() or target.parent.is_symlink():
        raise CopyRefused("the folder to copy the browser profiles into must not be a link, and the folder that holds it must exist")
    real_source, real_target = source.resolve(), target.parent.resolve() / target.name
    if real_target.is_relative_to(real_source) or real_source.is_relative_to(real_target):
        raise CopyRefused("the browser profiles cannot be copied into themselves")
    if target.exists() and not set_aside:
        raise CopyRefused("the folder to copy the browser profiles into already exists; nothing was copied")


def copy_browser_state(source: Path, target: Path, stamp: str, set_aside: bool = False) -> Copied:
    checked(source, target, set_aside)
    expected = listing(source)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}{COPYING}", dir=target.parent))
    files, folders, locks = copy_tree(source, temporary)
    os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
    if listing(temporary) != expected:
        raise CopyDiffers("the copy of the browser profiles does not match the original, so it was not put in place")
    if listing(source) != expected:
        raise CopyDiffers("the browser profiles changed while they were copied (is a worker still running?), so the copy was not put in place")
    aside = None
    if target.exists():
        aside = target.with_name(f"{target.name}{BEFORE}{stamp}")
        if aside.exists() or aside.is_symlink():
            raise CopyRefused("the name to set the existing browser profiles aside under is taken; nothing was renamed")
        os.rename(target, aside)
    os.rename(temporary, target)
    folder = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)
    return Copied(files, folders, locks, aside)
