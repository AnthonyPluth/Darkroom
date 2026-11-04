from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from darkroom.storage import paths

BATCH = 100
TIMEOUT = 300
XMP_TAG = re.compile(r"^XMP-[A-Za-z]+:[A-Za-z]+$")


class Missing(RuntimeError):
    pass


class Unreadable(RuntimeError):
    pass


class WriteFailed(RuntimeError):
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


def write_xmp(sidecar_path: Path, fields: Mapping[str, str]) -> bool:
    paths.checked_sidecar(sidecar_path)
    if not fields:
        return False
    for name, value in fields.items():
        if not XMP_TAG.match(name) or not value or "\x00" in value:
            raise WriteFailed("only XMP tags with a value are written to a sidecar")
    program = executable()

    def render(temp: Path, source: Path | None) -> None:
        assignments = [f"-{name}={value}" for name, value in sorted(fields.items())]
        sources = [] if source is None else [str(source)]
        done = subprocess.run([program, "-q", "-n", "-o", str(temp), *assignments, *sources], capture_output=True, text=True, timeout=TIMEOUT)
        if done.returncode != 0:
            raise WriteFailed("exiftool could not write the sidecar")

    paths.write_sidecar(sidecar_path, render)
    return True
