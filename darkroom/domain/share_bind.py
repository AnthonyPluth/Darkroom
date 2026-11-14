from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.domain.share_check import BOUND, SUBJECT
from darkroom.storage import db, share, stored_settings
from darkroom.storage.models import PixelCopy
from darkroom.storage.paths import PIXELS

NOT_WIRED = "this build does not run the placed-copy check before tending the Pixel folders, so a replacement share would never be refilled; nothing was written"
NOT_A_MOUNT = "the share root is not a mount separate from the container's own filesystem; map the working share to it and start again; nothing was written"
ANOTHER_SENTINEL = f"the share root already holds a sentinel ({share.SHARE_SENTINEL_FILE}); Darkroom never replaces one, so nothing was written. If this really is a replacement share, check what that file is before you remove it by hand"
CHANGED_SINCE = "the share root changed while you were confirming; nothing was written, so run the command again"


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Found:
    root: Path
    mount: share.Mount
    device: int
    entries: int
    pixel_files: int
    placed: int


def count_files(folder: Path) -> int:
    counted = 0
    for _, _, files in os.walk(folder):
        counted += len(files)
    return counted


def survey(opened: Session, root: Path, wired: bool) -> Found:
    if not wired:
        raise Refused(NOT_WIRED)
    mount = share.separate_mount(root)
    device = share.device_of(root)
    if mount is None or device is None:
        raise Refused(NOT_A_MOUNT)
    if share.sentinel_present(root):
        raise Refused(ANOTHER_SENTINEL)
    placed = int(opened.execute(select(func.count()).select_from(PixelCopy).where(PixelCopy.state == "placed")).scalar_one())
    opened.commit()
    return Found(root, mount, device, len(os.listdir(root)), count_files(root / PIXELS), placed)


def bind_share(opened: Session, found: Found, now: float) -> None:
    if share.separate_mount(found.root) != found.mount or share.device_of(found.root) != found.device or share.sentinel_present(found.root):
        raise Refused(CHANGED_SINCE)
    token = secrets.token_hex(16)
    db.write_lock(opened)
    stored_settings.record_share(opened, token, now)
    opened.commit()
    share.write_sentinel(found.root, share.sentinel_content(token))
    db.write_lock(opened)
    events.record(opened, now, BOUND, SUBJECT)
    opened.commit()
