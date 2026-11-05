import os
import shutil
from pathlib import Path


def sweep_a_listing(folder: Path):
    for leftover in folder.iterdir():
        if leftover.is_file():
            # ruleid: darkroom-no-sweep-outside-guarded-helpers
            leftover.unlink()


def sweep_a_glob(folder: Path):
    for leftover in folder.glob("*.zip"):
        # ruleid: darkroom-no-sweep-outside-guarded-helpers
        shutil.rmtree(leftover)


def sweep_a_listdir(folder: str):
    for name in os.listdir(folder):
        # ruleid: darkroom-no-sweep-outside-guarded-helpers
        os.remove(os.path.join(folder, name))


def read_a_listing(folder: Path):
    total = 0
    for each in folder.iterdir():
        # ok: darkroom-no-sweep-outside-guarded-helpers
        total += each.stat().st_size
    return total


def remove_one_named_file(path: Path):
    # ok: darkroom-no-sweep-outside-guarded-helpers
    path.unlink()
