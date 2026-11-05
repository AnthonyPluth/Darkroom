from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import eligibility, index, media
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import Device, MediaItem, Person, PhoneAsset, PhoneResource
from darkroom.storage.paths import Photos, Readable

KINDS = ("photo", "live_video", "adjusted")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
MAX_FILENAME = 200
MAX_ASSET_ID = 200
MAX_BYTES = 2**53


class BadUpload(ValueError):
    pass


class NotChecked(LookupError):
    pass


class NotWhatWasChecked(ValueError):
    pass


class HashMismatch(ValueError):
    pass


class NameTaken(FileExistsError):
    pass


@dataclass(frozen=True)
class Resource:
    kind: str
    sha1: str
    bytes: int
    filename: str


@dataclass(frozen=True)
class Asset:
    photokit_id: str
    resources: tuple[Resource, ...]


def kind_of(value: str) -> str:
    if value not in KINDS:
        raise BadUpload(f"kind must be one of {', '.join(KINDS)}")
    return value


def sha1_of(value: str) -> str:
    lowered = value.strip().lower()
    if not SHA1.match(lowered):
        raise BadUpload("sha1 must be 40 hexadecimal characters")
    return lowered


def asset_id_of(value: str) -> str:
    if not value or len(value) > MAX_ASSET_ID or not value.isprintable():
        raise BadUpload("the asset id must be 1 to 200 printable characters")
    return value


def filename_of(value: str) -> str:
    if not value or len(value) > MAX_FILENAME or not value.isprintable():
        raise BadUpload("the filename must be 1 to 200 printable characters")
    if any(char in value for char in "/\\") or value.startswith(".") or value.lower().endswith(paths.SIDECAR_SUFFIX):
        raise BadUpload("the filename must be a plain file name")
    return value


def asset_row(opened: Session, device_id: int, photokit_id: str, now: float) -> PhoneAsset:
    found = opened.execute(select(PhoneAsset).where(PhoneAsset.device_id == device_id, PhoneAsset.photokit_id == photokit_id)).scalar_one_or_none()
    if found is None:
        found = PhoneAsset(device_id=device_id, photokit_id=photokit_id, created=now)
        opened.add(found)
        opened.flush()
    return found


def resources_of(opened: Session, asset_id: int) -> dict[str, PhoneResource]:
    return {row.kind: row for row in opened.execute(select(PhoneResource).where(PhoneResource.asset_id == asset_id)).scalars()}


def phone_item(opened: Session, person_id: int, sha1: str) -> MediaItem | None:
    query = select(MediaItem).where(MediaItem.person_id == person_id, MediaItem.source == "phone", MediaItem.sha1 == sha1).order_by(MediaItem.id).limit(1)
    return opened.execute(query).scalar_one_or_none()


def uploaded_twin(opened: Session, person_id: int, row: PhoneResource) -> PhoneResource | None:
    query = (select(PhoneResource).join(PhoneAsset, PhoneAsset.id == PhoneResource.asset_id).join(Device, Device.id == PhoneAsset.device_id)
             .where(Device.person_id == person_id, PhoneResource.sha1 == row.sha1, PhoneResource.uploaded_at.is_not(None), PhoneResource.id != row.id)
             .order_by(PhoneResource.id).limit(1))
    return opened.execute(query).scalar_one_or_none()


def link(opened: Session, person_id: int, row: PhoneResource, now: float) -> None:
    item = phone_item(opened, person_id, row.sha1)
    if item is not None:
        row.media_item_id, row.uploaded_at = item.id, row.uploaded_at or now
        return
    if row.uploaded_at is None:
        twin = uploaded_twin(opened, person_id, row)
        if twin is not None:
            row.media_item_id, row.uploaded_at = twin.media_item_id, now


def check(opened: Session, device_id: int, person_id: int, assets: list[Asset], now: float) -> list[tuple[str, str]]:
    needed: list[tuple[str, str]] = []
    for declared in assets:
        asset = asset_row(opened, device_id, declared.photokit_id, now)
        current = resources_of(opened, asset.id)
        wanted = {resource.kind for resource in declared.resources}
        changed = False
        for kind, row in current.items():
            if kind not in wanted:
                opened.delete(row)
                changed = True
        for resource in declared.resources:
            existing = current.get(resource.kind)
            if existing is None:
                row = PhoneResource(asset_id=asset.id, kind=resource.kind, sha1=resource.sha1, bytes=resource.bytes, filename=resource.filename)
                opened.add(row)
                opened.flush()
                changed = True
            else:
                row = existing
            if (row.sha1, row.filename) != (resource.sha1, resource.filename):
                row.sha1, row.bytes, row.filename, row.uploaded_at, row.media_item_id = resource.sha1, resource.bytes, resource.filename, None, None
                changed = True
            if row.media_item_id is None:
                link(opened, person_id, row, now)
            if row.uploaded_at is None:
                needed.append((declared.photokit_id, resource.kind))
        if changed:
            eligibility.forget(asset)
    opened.flush()
    return needed


def expected(opened: Session, device_id: int, photokit_id: str, kind: str, sha1: str, filename: str) -> PhoneResource:
    asset = opened.execute(select(PhoneAsset).where(PhoneAsset.device_id == device_id, PhoneAsset.photokit_id == photokit_id)).scalar_one_or_none()
    row = resources_of(opened, asset.id).get(kind) if asset is not None else None
    if row is None:
        raise NotChecked("check this asset before uploading it")
    if row.sha1 != sha1 or row.filename != filename:
        raise NotWhatWasChecked("this upload is not what the last check declared; check the asset again")
    return row


def receive(photos: Photos, slug: str, stream: Readable, length: int, sha1: str) -> Path:
    temp, received = paths.receive_upload(photos.uploads_tmp(slug), stream, length)
    if received != sha1:
        paths.discard_upload(temp)
        raise HashMismatch("the upload's SHA-1 does not match X-Darkroom-SHA1")
    return temp


def candidates(filename: str, sha1: str) -> Iterator[str]:
    yield filename
    stem, dot, extension = filename.rpartition(".")
    yield f"{stem}~{sha1[:8]}.{extension}" if dot and stem else f"{filename}~{sha1[:8]}"


def same_bytes(path: Path, size: int, sha1: str) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        return False
    return media.sha1_of(path)[0] == sha1


def place(photos: Photos, slug: str, filename: str, sha1: str, temp: Path) -> tuple[Path, bool]:
    size = temp.stat().st_size
    try:
        for name in candidates(filename, sha1):
            target = photos.original(slug, str(Path(slug, "originals", name)))
            try:
                paths.adopt_upload(temp, target)
            except paths.AlreadyExists:
                if same_bytes(target, size, sha1):
                    return target, True
                continue
            return target, False
        raise NameTaken("another file already holds this name")
    finally:
        paths.discard_upload(temp)


def record(opened: Session, photos: Photos, person: Person, row: PhoneResource, target: Path, now: float, read: index.Reader = exiftool.read) -> bool:
    key = index.relative(photos, target)
    item = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.path == key)).scalar_one_or_none()
    if item is None or item.sha1 != row.sha1:
        index.index_file(opened, photos, person, target, now, read)
        item = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.path == key)).scalar_one_or_none()
    row.uploaded_at = now
    row.media_item_id = item.id if item is not None and item.source == "phone" and item.sha1 == row.sha1 else None
    opened.flush()
    return row.media_item_id is not None


def save(photos: Photos, slug: str, filename: str, sha1: str, stream: Readable, length: int) -> tuple[Path, bool]:
    temp = receive(photos, slug, stream, length, sha1)
    return place(photos, slug, filename, sha1, temp)
