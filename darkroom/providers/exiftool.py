from __future__ import annotations

import json
import re
import select
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from darkroom.storage import paths

BATCH = 100
TIMEOUT = 300
FILE_TIMEOUT = 60
PREVIEW_TIMEOUT = 30
PREVIEW_TAGS = ("PreviewImage", "ThumbnailImage", "CoverArt")
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG")
XMP_TAG = re.compile(r"^XMP-[A-Za-z]+:[A-Za-z]+$")


class Missing(RuntimeError):
    pass


class Unreadable(RuntimeError):
    pass


class TimedOut(Unreadable):
    pass


class WriteFailed(RuntimeError):
    pass


def executable() -> str:
    path = shutil.which("exiftool")
    if path is None:
        raise Missing("exiftool is not installed; install it (libimage-exiftool-perl) and try again")
    return path


def run(command: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess[str]:
    done = run_binary(command, timeout)
    return subprocess.CompletedProcess(command, done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace"))


def run_binary(command: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                select.select([], [], [], 0.02)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(command, process.returncode, out.read(), err.read())


def version() -> str:
    command = [executable(), "-ver"]
    done = run(command, 30)
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, command, done.stdout, done.stderr)
    return done.stdout.strip()


def batches(paths: list[Path]) -> Iterable[list[Path]]:
    for start in range(0, len(paths), BATCH):
        yield paths[start:start + BATCH]


def read(paths: list[Path]) -> dict[Path, dict[str, object]]:
    found: dict[Path, dict[str, object]] = {}
    program = executable()
    for group in batches(paths):
        try:
            done = run([program, "-j", "-n", "-G1", "-api", "largefilesupport=1", "-fast2", *map(str, group)], min(TIMEOUT, FILE_TIMEOUT * len(group)))
        except subprocess.TimeoutExpired as error:
            raise TimedOut("exiftool took too long to read the files") from error
        if not done.stdout.strip():
            if done.returncode != 0:
                raise Unreadable(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "exiftool failed")
            continue
        try:
            entries = json.loads(done.stdout)
        except ValueError as error:
            raise Unreadable("exiftool wrote output that is not JSON") from error
        if not isinstance(entries, list):
            raise Unreadable("exiftool wrote output that is not a list")
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("SourceFile"), str):
                found[Path(entry["SourceFile"])] = entry
    return found


def embedded_preview(path: Path) -> bytes | None:
    program = executable()
    for name in PREVIEW_TAGS:
        try:
            done = run_binary([program, "-b", f"-{name}", "-api", "largefilesupport=1", str(path)], PREVIEW_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise Unreadable("exiftool took too long to read the preview") from error
        if done.returncode == 0 and done.stdout.startswith(IMAGE_MAGIC):
            return done.stdout
    return None


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
        done = run([program, "-q", "-n", "-o", str(temp), *assignments, *sources])
        if done.returncode != 0:
            raise WriteFailed("exiftool could not write the sidecar")

    paths.write_sidecar(sidecar_path, render)
    return True
