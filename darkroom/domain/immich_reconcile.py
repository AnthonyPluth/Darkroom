from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Protocol

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, events, index, legacy, media, pixel_folder, takeout_copies
from darkroom.providers import exiftool, immich
from darkroom.storage import db, locks, paths, secretbox
from darkroom.storage.models import Event, ImmichAsset, MediaItem, Person, PixelCopy, PixelPerson
from darkroom.storage.paths import Photos

IMMICH_FOLDER = takeout_copies.IMMICH_FOLDER
OVERLAP_SECONDS = 300.0
MARGIN_SECONDS = 60.0
STEM_LIMIT = 100
SHA_PREFIX = 8
RETRIED = ("waiting", "failed")


class NotReady(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class CopyMismatch(RuntimeError):
    pass


class SetAside(CopyMismatch):
    pass


class PixelQueue(Protocol):
    def offer(self, opened: Session, person: Person, original: MediaItem, now: float) -> bool: ...


class NoQueue:
    def offer(self, opened: Session, person: Person, original: MediaItem, now: float) -> bool:
        return False


class PixelFolderQueue:
    def offer(self, opened: Session, person: Person, original: MediaItem, now: float) -> bool:
        if not pixel_folder.pixels_serving(opened, person.id):
            return False
        try:
            pixel_folder.queue(opened, original, now, always_send=True)
        except pixel_folder.NotInOriginals:
            return False
        return True


@dataclass
class Report:
    listed: int = 0
    stored: int = 0
    copied: int = 0
    already_there: int = 0
    queued: int = 0
    already_seen: int = 0
    ignored: int = 0
    waiting: int = 0
    failed: int = 0
    duplicates_removed: int = 0
    duplicates_refused: int = 0
    duplicates_failed: int = 0


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def copy_name(asset: immich.Asset) -> str:
    path = PurePath(asset.file_name)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem)[:STEM_LIMIT] or "asset"
    suffix = path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,6}", path.suffix.lower()) else ""
    return f"{stem}-{asset.sha1[:SHA_PREFIX]}{suffix}"


def person_key(settings: config.Settings, person: Person) -> str:
    if not person.immich_user_id or not person.immich_api_key_sealed:
        raise NotReady("this person has no Immich user and key")
    if not settings.secret_key:
        raise immich.ImmichFailed("Darkroom has no DARKROOM_SECRET_KEY, so it cannot read the Immich key")
    try:
        return secretbox.unseal(person.immich_api_key_sealed, settings.secret_key)
    except secretbox.Unreadable:
        raise immich.ImmichFailed("the stored Immich key cannot be read with DARKROOM_SECRET_KEY") from None


def require_earlier_imports(opened: Session, person: Person) -> None:
    try:
        legacy.require_takeout(opened, person)
    except legacy.NotReady:
        raise NotReady("the Immich reconcile runs only after this person's Takeout is imported") from None
    if person.legacy_path:
        done = opened.execute(select(Event.id).where(Event.type == "legacy_imported", Event.subject == person.slug).limit(1)).first()
        if done is None:
            raise NotReady("the Immich reconcile runs only after this person's legacy library is imported")


@contextmanager
def person_lock(settings: config.Settings, person: Person) -> Iterator[None]:
    try:
        with locks.exclusive(settings.data_dir / "locks" / f"immich-reconcile-{person.slug}.lock"):
            yield
    except locks.Held as error:
        raise Busy("an Immich reconcile is already running for this person") from error


class Run:
    def __init__(self, opened: Session, settings: config.Settings, person: Person, client: immich.Client, key: str, queue: PixelQueue, now: float,
                 read: index.Reader) -> None:
        self.opened, self.person, self.client, self.key, self.queue, self.now, self.read = opened, person, client, key, queue, now, read
        self.photos = Photos(settings.photos_root)
        self.report = Report()
        self.handled: set[str] = set()

    def row(self, asset_id: str) -> ImmichAsset | None:
        return self.opened.execute(select(ImmichAsset).where(ImmichAsset.person_id == self.person.id, ImmichAsset.asset_id == asset_id)).scalar_one_or_none()

    def mark(self, asset: immich.Asset, kind: str, status: str, linked: str | None = None) -> ImmichAsset:
        row = self.row(asset.id)
        if row is None:
            row = ImmichAsset(person_id=self.person.id, asset_id=asset.id, kind=kind, status=status, sha1=asset.sha1, seen_at=self.now)
            self.opened.add(row)
        row.kind, row.status, row.sha1, row.linked_asset_id, row.seen_at = kind, status, asset.sha1, linked, self.now
        if status != "stored":
            row.original_item_id = row.queued_at = None
        self.opened.flush()
        return row

    def store(self, asset: immich.Asset) -> tuple[MediaItem, bool]:
        db.write_lock(self.opened)
        present = self.opened.execute(
            select(MediaItem).where(MediaItem.person_id == self.person.id, MediaItem.source == "phone", MediaItem.sha1 == asset.sha1).order_by(MediaItem.id).limit(1)
        ).scalar_one_or_none()
        if present is not None:
            return present, False
        destination = self.photos.originals(self.person.slug) / IMMICH_FOLDER / copy_name(asset)
        with suppress(paths.AlreadyExists):
            paths.write_once(destination, self.client.original_chunks(self.key, asset))
        if media.sha1_of(destination)[0] != asset.sha1:
            raise CopyMismatch("a different file is already at the copy's name")
        report = index.index_file(self.opened, self.photos, self.person, destination, self.now, self.read)
        if report.skipped or report.set_aside:
            raise SetAside("the index set the copy in originals aside; it is tried again in a day")
        item = self.opened.execute(select(MediaItem).where(MediaItem.person_id == self.person.id, MediaItem.path == index.relative(self.photos, destination))).scalar_one_or_none()
        if item is None or report.failed or item.sha1 != asset.sha1:
            raise CopyMismatch("the copy in originals could not be checked against the Immich asset")
        return item, True

    def offer(self, row: ImmichAsset) -> None:
        item = None if row.original_item_id is None else self.opened.get(MediaItem, row.original_item_id)
        if item is not None and self.queue.offer(self.opened, self.person, item, self.now):
            row.queued_at = self.now
            self.report.queued += 1

    def photo(self, asset: immich.Asset) -> None:
        self.handled.add(asset.id)
        halves = [(asset, "photo", asset.live_video_id)]
        if asset.live_video_id:
            try:
                video = self.client.get_asset(self.key, asset.live_video_id)
            except immich.ImmichFailed as error:
                if error.status != 404:
                    raise
                self.mark(asset, "photo", "waiting", asset.live_video_id)
                self.opened.commit()
                return
            if video.owner_id != asset.owner_id:
                self.fail(asset, "photo", "LiveVideoOwner")
                return
            halves.append((video, "live_video", asset.id))
            self.handled.add(video.id)
        try:
            stored = [(half, kind, linked, *self.store(half)) for half, kind, linked in halves]
        except SetAside:
            self.opened.commit()
            self.fail(asset, "photo", SetAside.__name__)
            return
        except (immich.ImmichFailed, CopyMismatch, OSError) as error:
            if isinstance(error, immich.ImmichFailed) and error.status in (401, 403):
                raise
            self.opened.rollback()
            self.fail(asset, "photo", type(error).__name__)
            return
        for half, kind, linked, item, copied in stored:
            row = self.mark(half, kind, "stored", linked)
            row.original_item_id = item.id
            self.report.stored += 1
            self.report.copied += copied
            self.report.already_there += not copied
        self.opened.commit()

    def fail(self, asset: immich.Asset, kind: str, reason: str) -> None:
        self.mark(asset, kind, "failed")
        self.report.failed += 1
        events.record(self.opened, self.now, "immich_asset_failed", self.person.slug, reason)
        self.opened.commit()

    def listed(self, asset: immich.Asset) -> None:
        row = self.row(asset.id)
        if row is not None and row.status == "stored":
            self.report.already_seen += 1
        elif asset.owner_id != self.person.immich_user_id:
            return
        elif asset.library_id or asset.trashed or asset.offline:
            self.mark(asset, "photo", "ignored")
            self.report.ignored += 1
        elif asset.visibility == "hidden" and asset.kind == "VIDEO":
            if row is None:
                self.mark(asset, "live_video", "waiting")
        elif immich.PHONE_KEY not in self.client.metadata_keys(self.key, asset.id):
            self.mark(asset, "photo", "ignored")
            self.report.ignored += 1
        else:
            self.photo(asset)
        self.opened.commit()

    def retry(self) -> None:
        waiting = self.opened.execute(
            select(ImmichAsset.asset_id).where(ImmichAsset.person_id == self.person.id, ImmichAsset.kind == "photo", ImmichAsset.status.in_(RETRIED)).order_by(ImmichAsset.id)
        ).scalars().all()
        for asset_id in waiting:
            if asset_id in self.handled:
                continue
            try:
                self.photo(self.client.get_asset(self.key, asset_id))
            except immich.ImmichFailed as error:
                if error.status in (401, 403):
                    raise

    def settle(self) -> None:
        resolved = takeout_copies.resolve_person(self.opened, self.photos, self.person, self.now, takeout_copies.Tools(read=self.read))
        self.report.duplicates_removed, self.report.duplicates_refused, self.report.duplicates_failed = resolved.removed, resolved.refused, resolved.failed
        missing = exists().where(PixelPerson.person_id == self.person.id, ~exists().where(PixelCopy.pixel_id == PixelPerson.pixel_id, PixelCopy.item_id == ImmichAsset.original_item_id))
        unqueued = self.opened.execute(
            select(ImmichAsset).where(ImmichAsset.person_id == self.person.id, ImmichAsset.status == "stored", ImmichAsset.original_item_id.is_not(None), missing).order_by(ImmichAsset.id)
        ).scalars().all()
        for row in unqueued:
            self.offer(row)
        self.opened.commit()


def reconcile_person(opened: Session, settings: config.Settings, person: Person, now: float, client: immich.Client | None = None,
                     queue: PixelQueue | None = None, read: index.Reader = exiftool.read) -> Report:
    key = person_key(settings, person)
    require_earlier_imports(opened, person)
    run = Run(opened, settings, person, client or immich.Client.at(settings), key, queue or NoQueue(), now, read)
    until = now - MARGIN_SECONDS
    since = None if person.immich_reconciled_until is None else person.immich_reconciled_until - OVERLAP_SECONDS
    with person_lock(settings, person):
        if run.client.user_id(key) != person.immich_user_id:
            raise immich.ImmichFailed("the Immich key belongs to a different user than the person's")
        listing = run.client.search_stable(key, None if since is None else stamp(since), stamp(until))
        run.report.listed = len(listing)
        try:
            for asset in listing:
                run.listed(asset)
            run.retry()
        except Exception:
            opened.rollback()
            run.settle()
            raise
        run.settle()
        run.report.waiting = opened.query(ImmichAsset).filter(ImmichAsset.person_id == person.id, ImmichAsset.status == "waiting").count()
        person.immich_reconciled_until = max(until, person.immich_reconciled_until or 0.0)
        report = run.report
        events.record(opened, now, "immich_reconciled", person.slug,
                      f"{report.listed} listed, {report.stored} stored, {report.ignored} ignored, {report.waiting} waiting, {report.failed} failed, "
                      f"{report.duplicates_removed} duplicates removed, {report.duplicates_refused} refused, {report.duplicates_failed} failed to remove")
        opened.commit()
    monitoring.log("immich reconciled", person=person.slug, listed=report.listed, stored=report.stored, ignored=report.ignored, waiting=report.waiting, failed=report.failed,
                   duplicates_removed=report.duplicates_removed, duplicates_refused=report.duplicates_refused, duplicates_failed=report.duplicates_failed)
    return report


def reconcile_all(opened: Session, settings: config.Settings, now: float, client: immich.Client | None = None, queue: PixelQueue | None = None,
                  read: index.Reader = exiftool.read) -> dict[str, Report]:
    done: dict[str, Report] = {}
    for person in opened.execute(select(Person).where(Person.immich_api_key_sealed.is_not(None)).order_by(Person.id)).scalars().all():
        try:
            done[person.slug] = reconcile_person(opened, settings, person, now, client, queue, read)
        except (NotReady, Busy):
            opened.rollback()
        except immich.ImmichFailed as error:
            opened.rollback()
            latest = opened.execute(
                select(Event.type).where(Event.subject == person.slug, Event.type.in_(("immich_reconciled", "immich_reconcile_failed"))).order_by(Event.id.desc()).limit(1)
            ).scalar_one_or_none()
            events.record(opened, now, "immich_reconcile_failed", person.slug, str(error)[:200])
            if latest != "immich_reconcile_failed":
                alerts.open_alert(opened, settings, f"immich:reconcile:{person.id}", "failure", person.slug,
                                  alerts.failure(f"The Immich reconcile of {person.name}'s phone uploads"), None, now, about=alerts.person(person.id))
            opened.commit()
    return done
