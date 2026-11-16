from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from darkroom_init.plan import SOCKET_FOLDER
from darkroom_init.supervisor import become_quietly, log


class Refused(Exception):
    pass


def private_folder(folder: Path) -> bool:
    found = os.lstat(folder)
    return stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() and stat.S_IMODE(found.st_mode) & 0o077 == 0


def in_folder(socket_path: Path, folder: Path = SOCKET_FOLDER) -> bool:
    text = str(socket_path)
    return socket_path.is_absolute() and os.path.normpath(text) == text and socket_path.parent == folder and socket_path.name not in ("", ".", "..")


def clear_stale_socket(socket_path: Path) -> None:
    try:
        found = os.lstat(socket_path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(found.st_mode):
        raise Refused("something other than a socket is where an internal socket goes")
    socket_path.unlink()


def prepare(socket_path: Path, folder: Path = SOCKET_FOLDER) -> None:
    if not in_folder(socket_path, folder):
        raise Refused("an internal socket was asked for outside the private socket folder")
    try:
        private = private_folder(folder)
    except FileNotFoundError:
        private = False
    if not private:
        raise Refused("the folder for the internal sockets is not private to the container's user")
    clear_stale_socket(socket_path)


def main(argv: list[str], folder: Path = SOCKET_FOLDER) -> int:
    if len(argv) < 3 or not argv[2].startswith("/"):
        log("listener not started", reason="the init gives the wrapper a socket and an absolute program")
        return 2
    try:
        prepare(Path(argv[1]), folder)
    except Refused as refused:
        log("listener not started", reason=str(refused))
        return 1
    except OSError as error:
        log("listener not started", reason="the private socket folder could not be checked", error=type(error).__name__)
        return 1
    become_quietly(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
