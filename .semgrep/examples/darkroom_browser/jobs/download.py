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
        # ruleid: darkroom-no-sweep-outside-guarded-helpers
        leftover.unlink()
