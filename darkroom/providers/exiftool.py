from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

BATCH = 100
TIMEOUT = 300


class Missing(RuntimeError):
    pass


class Unreadable(RuntimeError):
    pass


def executable() -> str:
    path = shutil.which("exiftool")
    if path is None:
        raise Missing("exiftool is not installed; install it (libimage-exiftool-perl) and try again")
    return path


def version() -> str:
    done = subprocess.run([executable(), "-ver"], capture_output=True, text=True, check=True, timeout=30)
    return done.stdout.strip()


def batches(paths: list[Path]) -> Iterable[list[Path]]:
    for start in range(0, len(paths), BATCH):
        yield paths[start:start + BATCH]


def read(paths: list[Path]) -> dict[Path, dict[str, object]]:
    found: dict[Path, dict[str, object]] = {}
    program = executable()
    for group in batches(paths):
        done = subprocess.run([program, "-j", "-n", "-G1", "-api", "largefilesupport=1", "-fast2", *map(str, group)],
                              capture_output=True, text=True, timeout=TIMEOUT)
        if not done.stdout.strip():
            if done.returncode != 0:
                raise Unreadable(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "exiftool failed")
            continue
        for entry in json.loads(done.stdout):
            found[Path(entry["SourceFile"])] = entry
    return found
