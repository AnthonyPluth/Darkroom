from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from darkroom.storage import paths
from darkroom.storage.models import (Export, GoogleAccount, ManagedAssetOrigin, ManagedAssetVersion, ManagedImportIntent, ManagedPixelSource, MediaItem, Person,
                                     Pixel, PixelCopy)
from darkroom.storage.paths import Photos

VERIFIED = "bytes_verified"
SUFFIX = re.compile(r"\.[a-z0-9]{1,10}")


class NotManaged(ValueError):
    pass


def retire(opened: Session, copies: Select[int]) -> None:
    opened.execute(delete(ManagedPixelSource).where(ManagedPixelSource.copy_id.in_(copies)))


def stranded(opened: Session, account_id: int) -> bool:
    elsewhere = select(ManagedPixelSource.copy_id).where(ManagedPixelSource.google_account_id != account_id)
    return opened.execute(select(ManagedPixelSource.id).where(ManagedPixelSource.google_account_id == account_id, ManagedPixelSource.copy_id.not_in(elsewhere))
                          .limit(1)).first() is not None


def retire_account(opened: Session, account_id: int) -> None:
    opened.execute(delete(ManagedPixelSource).where(ManagedPixelSource.google_account_id == account_id))


def referenced(opened: Session, item_id: int) -> bool:
    return opened.execute(select(ManagedPixelSource.id).where(ManagedPixelSource.item_id == item_id).limit(1)).first() is not None


def still_sending(opened: Session, item_id: int) -> bool:
    return opened.execute(select(PixelCopy.id).join(ManagedPixelSource, ManagedPixelSource.copy_id == PixelCopy.id)
                          .where(PixelCopy.item_id == item_id, PixelCopy.state.in_(("queued", "placed"))).limit(1)).first() is not None


def converted(opened: Session, copy_id: int) -> bool:
    return opened.execute(select(ManagedPixelSource.id).where(ManagedPixelSource.copy_id == copy_id).limit(1)).first() is not None


def usable(opened: Session, version: ManagedAssetVersion | None, person_id: int, sha1: str, size: int) -> bool:
    person = opened.get(Person, person_id, populate_existing=True)
    return (version is not None and person is not None and version.person_id == person_id and person.immich_user_id == version.owner_id
            and version.state == VERIFIED and version.library_id is None and version.sha1 == sha1 and version.bytes == size)


def phone_origin(opened: Session, version_id: int) -> bool:
    return opened.execute(select(ManagedAssetOrigin.id).where(ManagedAssetOrigin.version_id == version_id, ManagedAssetOrigin.kind == "phone")
                          .limit(1)).first() is not None


def takeout_accounts(opened: Session, version_id: int) -> set[int]:
    return set(opened.execute(
        select(Export.account_id).select_from(ManagedAssetOrigin).join(ManagedImportIntent, ManagedImportIntent.id == ManagedAssetOrigin.intent_id)
        .join(Export, Export.id == ManagedImportIntent.export_id).where(ManagedAssetOrigin.version_id == version_id, ManagedAssetOrigin.kind == "takeout")
    ).scalars())


def returns_home(opened: Session, version_id: int, account_id: int) -> bool:
    return account_id in takeout_accounts(opened, version_id) and not phone_origin(opened, version_id)


def latest(opened: Session, copy_id: int) -> ManagedPixelSource | None:
    return opened.execute(select(ManagedPixelSource).where(ManagedPixelSource.copy_id == copy_id)
                          .order_by(ManagedPixelSource.generation.desc(), ManagedPixelSource.id.desc()).limit(1)
                          .execution_options(populate_existing=True)).scalar_one_or_none()


def sent_home(opened: Session, row: PixelCopy, account_id: int) -> bool:
    found = latest(opened, row.id)
    return found is not None and returns_home(opened, found.version_id, account_id)


def recorded(opened: Session, row: PixelCopy, account_id: int) -> ManagedPixelSource | None:
    return opened.execute(select(ManagedPixelSource).where(ManagedPixelSource.copy_id == row.id, ManagedPixelSource.generation == row.requeued,
                                                           ManagedPixelSource.google_account_id == account_id)
                          .execution_options(populate_existing=True)).scalar_one_or_none()


def current(opened: Session, row: PixelCopy, pixel: Pixel) -> ManagedPixelSource | None:
    source = recorded(opened, row, pixel.google_account_id)
    item = opened.get(MediaItem, row.item_id, populate_existing=True)
    if (source is None or item is None or row.pixel_id != pixel.id
            or (source.sha1, source.bytes) != (row.sha1, row.bytes) or (item.sha1, item.bytes) != (row.sha1, row.bytes)):
        return None
    version = opened.get(ManagedAssetVersion, source.version_id, populate_existing=True)
    if not usable(opened, version, row.person_id, row.sha1, row.bytes) or returns_home(opened, source.version_id, pixel.google_account_id):
        return None
    return source


def snapshot(row: PixelCopy) -> str:
    return json.dumps({"state": row.state, "requeued": row.requeued, "always_send": row.always_send, "queued_at": row.queued_at,
                       "placed_at": row.placed_at, "pruned_at": row.pruned_at}, sort_keys=True)


def bind(opened: Session, row: PixelCopy, pixel: Pixel, version: ManagedAssetVersion | None, now: float) -> ManagedPixelSource | None:
    account = opened.get(GoogleAccount, pixel.google_account_id, populate_existing=True)
    if (version is None or account is None or row.pixel_id != pixel.id or recorded(opened, row, account.id) is not None
            or not usable(opened, version, row.person_id, row.sha1, row.bytes) or returns_home(opened, version.id, account.id)):
        return None
    opened.add(ManagedPixelSource(copy_id=row.id, pixel_id=row.pixel_id, item_id=row.item_id, person_id=row.person_id, version_id=version.id,
                                  owner_id=version.owner_id, asset_id=version.asset_id, sha1=version.sha1, bytes=row.bytes, generation=row.requeued,
                                  google_account_id=account.id, destination_email=account.email, copy_snapshot=snapshot(row), recorded_at=now))
    opened.flush()
    return current(opened, row, pixel)


def carried(opened: Session, row: PixelCopy, pixel: Pixel, now: float) -> ManagedPixelSource | None:
    if recorded(opened, row, pixel.google_account_id) is not None:
        return current(opened, row, pixel)
    earlier = latest(opened, row.id)
    if earlier is None or (earlier.sha1, earlier.bytes) != (row.sha1, row.bytes):
        return None
    return bind(opened, row, pixel, opened.get(ManagedAssetVersion, earlier.version_id, populate_existing=True), now)


def copy_name(photos: Photos, slug: str, item_path: str, asset_id: str) -> str:
    try:
        return paths.pixel_copy_name(photos, slug, item_path)
    except (paths.OutsideRoot, ValueError):
        suffix = PurePosixPath(item_path).suffix.lower()
        return f"immich/{asset_id}{suffix if SUFFIX.fullmatch(suffix) else ''}"
