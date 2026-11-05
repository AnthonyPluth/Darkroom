from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

STALL_SECONDS = 300.0
SETTLE_SECONDS = 4.0
POLL_MS = 2000
IN_PROGRESS_SUFFIX = ".crdownload"


class Verdict(Enum):
    WAIT = "wait"
    FINISHED = "finished"
    STALLED = "stalled"


@dataclass(frozen=True)
class FolderState:
    size: int
    files: int
    in_progress: bool


def folder_state(folder: Path) -> FolderState:
    size = 0
    files = 0
    in_progress = False
    for each in folder.iterdir():
        if each.is_symlink() or not each.is_file():
            continue
        files += 1
        size += each.stat().st_size
        in_progress = in_progress or each.name.endswith(IN_PROGRESS_SUFFIX)
    return FolderState(size, files, in_progress)


class StallWatch:
    def __init__(self, started: float, stall_seconds: float = STALL_SECONDS, settle_seconds: float = SETTLE_SECONDS) -> None:
        self.stall_seconds = stall_seconds
        self.settle_seconds = settle_seconds
        self.last_change = started
        self.last: FolderState | None = None

    def judge(self, now: float, state: FolderState) -> Verdict:
        if state != self.last:
            self.last = state
            self.last_change = now
            return Verdict.WAIT
        quiet = now - self.last_change
        if state.files > 0 and not state.in_progress and quiet >= self.settle_seconds:
            return Verdict.FINISHED
        if quiet >= self.stall_seconds:
            return Verdict.STALLED
        return Verdict.WAIT
