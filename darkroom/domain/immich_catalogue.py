from __future__ import annotations

import json
import re
from pathlib import PurePath
from typing import Final

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.domain import backfill, immich_reconcile, index, pixel_folder, pixel_source
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import ImmichAsset, ManagedAssetVersion, MediaItem, Person, PixelCopy, PixelPerson

FOLDER: Final = "immich-catalogue"
SUFFIX: Final = re.compile(r"\.[a-z0-9]{1,10}")
SAFE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
CATALOGED: Final = "cataloged"
KINDS: Final = ("IMAGE", "VIDEO")


class CatalogueRun(immich_reconcile.Run):
    done = ("stored", CATALOGED)

    def __init__(self, opened: Session, settings: config.Settings, person: Person, client: immich.Client, key: str, queue: immich_reconcile.PixelQueue, now: float,
                 read: index.Reader) -> None:
        super().__init__(opened, settings, person, client, key, queue, now, read)
        self.settings = settings

    def sized(self, half: immich.Asset) -> int:
        return sum(len(chunk) for chunk in self.client.original_chunks(self.key, half))

    def path_of(self, half: immich.Asset) -> str:
        if not SAFE_ID.fullmatch(half.id):
            raise immich_reconcile.CopyMismatch("the Immich asset has no id Darkroom can record")
        suffix = PurePath(half.file_name).suffix.lower()
        return f"{self.person.slug}/{FOLDER}/{half.id}{suffix if SUFFIX.fullmatch(suffix) else ''}"

    def item_of(self, half: immich.Asset, size: int) -> MediaItem:
        path = self.path_of(half)
        query = select(MediaItem).where(MediaItem.person_id == self.person.id, MediaItem.path == path).execution_options(populate_existing=True)
        found = self.opened.execute(query).scalar_one_or_none()
        if found is None:
            found = MediaItem(person_id=self.person.id, source="phone", path=path, sha1=half.sha1, bytes=size, indexed_at=self.now)
            self.opened.add(found)
            self.opened.flush()
        elif (found.sha1, found.bytes) != (half.sha1, size):
            raise immich_reconcile.CopyMismatch("the catalogued item holds other bytes than the Immich asset")
        return found

    def snapshot(self, asset: immich.Asset) -> str:
        return json.dumps({"origin": "phone", "live_video_id": asset.live_video_id} if asset.live_video_id else {"origin": "phone"}, sort_keys=True)

    def keep(self, asset: immich.Asset, halves: list[tuple[immich.Asset, str, str | None]]) -> None:
        snapshot = self.snapshot(asset)
        try:
            sizes = [None if self.present(half.sha1) is not None else self.sized(half) for half, _, _ in halves]
            db.write_lock(self.opened)
            for (half, kind, linked), size in zip(halves, sizes, strict=True):
                real = self.present(half.sha1, fresh=True)
                if real is not None:
                    self.mark(half, kind, "stored", linked).original_item_id = real.id
                    self.report.stored += 1
                    self.report.already_there += 1
                    continue
                if half.kind not in KINDS:
                    raise immich_reconcile.CopyMismatch("the Immich asset is neither a photo nor a video")
                if size is None:
                    raise immich_reconcile.CopyMismatch("the original this asset matched is gone")
                item = self.item_of(half, size)
                version = backfill.version_of(self.opened, self.person, half.id, half.sha1, size, half.kind, half.live_video_id, snapshot, self.now)
                backfill.origin(self.opened, version, "phone", item, None, self.now)
                self.mark(half, kind, CATALOGED, linked).original_item_id = item.id
                self.report.cataloged += 1
        except (immich.ImmichFailed, immich_reconcile.CopyMismatch, OSError) as error:
            if isinstance(error, immich.ImmichFailed) and error.status in (401, 403):
                raise
            self.opened.rollback()
            self.fail(asset, "photo", type(error).__name__)
            return
        self.opened.commit()

    def queue_row(self, row: ImmichAsset) -> None:
        item = None if row.original_item_id is None else self.opened.get(MediaItem, row.original_item_id)
        version = self.opened.execute(select(ManagedAssetVersion).where(
            ManagedAssetVersion.person_id == self.person.id, ManagedAssetVersion.owner_id == str(self.person.immich_user_id),
            ManagedAssetVersion.asset_id == row.asset_id, ManagedAssetVersion.sha1 == row.sha1)).scalar_one_or_none()
        if item is None or version is None:
            return
        try:
            self.report.queued += pixel_folder.queue_managed(self.opened, item, version.id, self.now)
        except pixel_source.NotManaged:
            self.opened.rollback()
            return
        row.queued_at = self.now
        self.opened.commit()

    def settle(self) -> None:
        if not self.settings.pixel_immich_source:
            return
        unserved = exists().where(PixelPerson.person_id == self.person.id,
                                  ~exists().where(PixelCopy.pixel_id == PixelPerson.pixel_id, PixelCopy.item_id == ImmichAsset.original_item_id))
        waiting = self.opened.execute(
            select(ImmichAsset).where(ImmichAsset.person_id == self.person.id, ImmichAsset.status == CATALOGED, ImmichAsset.original_item_id.is_not(None), unserved)
            .order_by(ImmichAsset.id)
        ).scalars().all()
        for row in waiting:
            self.queue_row(row)
        self.opened.commit()

    def summary(self) -> str:
        report = self.report
        return (f"{report.listed} listed, {report.cataloged} cataloged, {report.stored} already stored, {report.ignored} ignored, {report.waiting} waiting, "
                f"{report.failed} failed, {report.queued} queued for the Pixels")
