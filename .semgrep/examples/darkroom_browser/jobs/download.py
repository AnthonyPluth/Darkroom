import os
from pathlib import Path


def sweep_downloads(folder: Path) -> int:
    removed = 0
    for leftover in folder.iterdir():
        if leftover.is_symlink() or leftover.is_file():
            # ok: darkroom-no-sweep-outside-guarded-helpers
            leftover.unlink()
            removed += 1
    return removed


def sweep_somewhere_else(folder: Path):
    for leftover in folder.iterdir():
        # ruleid: darkroom-no-sweep-outside-guarded-helpers,darkroom-no-removal-of-listed-names
        leftover.unlink()


def remove_download(folder: Path, handle: str) -> int:
    removed = 0
    for name in (handle, handle + ".crdownload"):
        own = folder / name
        # ok: darkroom-no-removal-of-listed-names
        own.unlink()
        removed += 1
    return removed


def drop_leftovers(folder: Path, owned: set[str]) -> None:
    leftovers = {each.name for each in folder.iterdir()} - owned
    for name in leftovers:
        # ruleid: darkroom-no-removal-of-listed-names
        (folder / name).unlink()


def drop_listed_stems(folder: Path) -> None:
    stems = [each.name.removesuffix(".crdownload") for each in folder.glob("*")]
    for stem in stems:
        # ruleid: darkroom-no-removal-of-listed-names
        os.remove(folder / stem)


def remove_a_fixed_name(folder: Path) -> None:
    # ok: darkroom-no-removal-of-listed-names
    (folder / "known").unlink()
