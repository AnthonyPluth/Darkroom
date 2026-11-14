from __future__ import annotations

import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from darkroom.storage.paths import MOUNTINFO, OCTAL_ESCAPE

SHARE_SENTINEL_FILE = ".darkroom-share"
SENTINEL_PREFIX = "darkroom-share "
SENTINEL_MODE = 0o600
LONGEST_SENTINEL = 256


class NotASentinel(OSError):
    pass


@dataclass(frozen=True)
class Mount:
    point: Path
    kind: str
    source: str


def unescaped(field: str) -> str:
    return OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def mount_at(root: Path) -> Mount | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        lines = MOUNTINFO.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        before, _, after = line.partition(" - ")
        fields, described = before.split(" "), after.split(" ")
        if len(fields) > 4 and len(described) > 1 and Path(unescaped(fields[4])) == root:
            found = Mount(root, unescaped(described[0]), unescaped(described[1]))
    return found


def device_of(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def separate_mount(root: Path) -> Mount | None:
    own = device_of(Path("/"))
    found = device_of(root)
    if root == Path("/") or found is None or own is None or found == own:
        return None
    return mount_at(root)


def sentinel_content(token: str) -> bytes:
    return f"{SENTINEL_PREFIX}{token}\n".encode("ascii")


def sentinel_present(root: Path) -> bool:
    try:
        os.lstat(root / SHARE_SENTINEL_FILE)
    except FileNotFoundError:
        return False
    return True


def read_sentinel(root: Path) -> bytes | None:
    try:
        fd = os.open(root / SHARE_SENTINEL_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1 or found.st_size > LONGEST_SENTINEL:
            raise NotASentinel(errno.EINVAL, "the sentinel is not a small regular file")
        return os.read(fd, LONGEST_SENTINEL + 1)
    finally:
        os.close(fd)


def remove_own(folder: int, created: os.stat_result) -> None:
    try:
        found = os.stat(SHARE_SENTINEL_FILE, dir_fd=folder, follow_symlinks=False)
    except OSError:
        return
    if os.path.samestat(found, created):
        os.unlink(SHARE_SENTINEL_FILE, dir_fd=folder)


def write_sentinel(root: Path, content: bytes) -> None:
    folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(SHARE_SENTINEL_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, SENTINEL_MODE, dir_fd=folder)
        try:
            written = 0
            while written < len(content):
                written += os.write(fd, content[written:])
            os.fsync(fd)
        except BaseException:
            remove_own(folder, os.fstat(fd))
            raise
        finally:
            os.close(fd)
        os.fsync(folder)
    finally:
        os.close(folder)
