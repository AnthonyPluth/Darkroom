from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom import oidc
from darkroom.config import Settings
from darkroom.domain import accounts, events
from darkroom.storage.models import DedupeDecision, Device, Export, MediaItem, Person, PhoneAsset, PhoneResource
from darkroom.storage.paths import OutsideRoot, Photos

KINDS = ("photo", "live_video", "adjusted")
CONFIRMING_TIERS = accounts.CONFIRMING_TIERS

CLEANUP_DISABLED = "cleanup_disabled"
DEVICE_REVOKED = "device_revoked"
ACCESS_LAPSED = "access_lapsed"
UNKNOWN_ASSET = "unknown_asset"
ALREADY_DELETED = "already_deleted"
RESOURCES_CHANGED = "resources_changed"
NO_ORIGINAL = "no_original"
NOT_UPLOADED = "not_uploaded"
NOT_ON_UNRAID = "not_on_unraid"
NOT_CONFIRMED = "not_confirmed"
NO_AUTOMATIC_MATCH = "no_automatic_match"
ACCOUNT_NOT_SERVING = "account_not_serving"
FILE_MISSING = "file_missing"
NOT_CONFIRMED_FOR_DELETION = "not_confirmed_for_deletion"

REASONS: dict[str, str] = {
    CLEANUP_DISABLED: "Cleanup is off for this iPhone.",
    DEVICE_REVOKED: "This iPhone is signed out of Darkroom.",
    ACCESS_LAPSED: "This iPhone's sign-in has lapsed; sign in again.",
    UNKNOWN_ASSET: "Darkroom has no record of this photo from this iPhone.",
    ALREADY_DELETED: "This photo was already removed from this iPhone.",
    RESOURCES_CHANGED: "This photo changed on the iPhone since Darkroom last checked it.",
    NO_ORIGINAL: "Darkroom has no original for this photo.",
    NOT_UPLOADED: "Part of this photo is not on Unraid yet.",
    NOT_ON_UNRAID: "Part of this photo is not on Unraid with a matching SHA-1.",
    NOT_CONFIRMED: "Part of this photo is not confirmed in Google Photos.",
    NO_AUTOMATIC_MATCH: "Part of this photo has no automatic match in a Takeout export.",
    ACCOUNT_NOT_SERVING: "The only Takeout match is from a Google account that does not serve this person.",
    FILE_MISSING: "A file of this photo is missing on Unraid or has changed size.",
    NOT_CONFIRMED_FOR_DELETION: "Darkroom did not confirm this photo for removal.",
}


class Busy(RuntimeError):
    pass


class NoSuchDevice(LookupError):
    pass


class NotAllowed(ValueError):
    pass


@dataclass(frozen=True)
class ReportedResource:
    kind: str
    sha1: str
    bytes: int
    filename: str


@dataclass(frozen=True)
class Reported:
    photokit_id: str
    resources: tuple[ReportedResource, ...]


@dataclass(frozen=True)
class Eligible:
    asset_id: str
    bytes: int
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    asset_id: str
    reason: str


@dataclass(frozen=True)
class Evaluation:
    cleanup_enabled: bool
    eligible: list[Eligible]
    rejected: list[Rejected]


def take_write_lock(opened: Session, device_id: int) -> Device:
    try:
        opened.execute(sql_update(Device).where(Device.id == device_id).values(id=Device.id))
    except OperationalError as error:
        if "database is locked" not in str(error.orig):
            raise
        raise Busy("Darkroom is busy with another change; try again in a moment") from None
    device = opened.get(Device, device_id)
    if device is None:
        raise NoSuchDevice("no such device")
    return device


def device_refusal(opened: Session, settings: Settings, device: Device, now: float) -> str | None:
    if device.revoked_at is not None:
        return DEVICE_REVOKED
    person = opened.get(Person, device.person_id)
    if person is None or oidc.device_access_lapsed(opened, settings, device.sub, person.email, now) is not None:
        return ACCESS_LAPSED
    if device.cleanup_enabled is not True:
        return CLEANUP_DISABLED
    return None


@dataclass(frozen=True)
class Facts:
    resources: dict[int, dict[str, PhoneResource]]
    items: dict[int, MediaItem]
    decisions: dict[int, list[DedupeDecision]]
    exports: dict[int, Export]
    served: dict[int, set[int]]

    def stored(self, asset_id: int) -> dict[str, PhoneResource]:
        return self.resources.get(asset_id, {})


def load_facts(opened: Session, assets: Select[int]) -> Facts:
    resources: dict[int, dict[str, PhoneResource]] = {}
    for row in opened.execute(select(PhoneResource).where(PhoneResource.asset_id.in_(assets)).order_by(PhoneResource.id)).scalars():
        resources.setdefault(row.asset_id, {})[row.kind] = row
    linked = select(PhoneResource.media_item_id).where(PhoneResource.asset_id.in_(assets))
    items = {item.id: item for item in opened.execute(select(MediaItem).where(MediaItem.id.in_(linked))).scalars()}
    backing = select(DedupeDecision).where(
        DedupeDecision.matched_item_id.in_(linked), DedupeDecision.reversed_at.is_(None),
        DedupeDecision.decided_by == "auto", DedupeDecision.tier.in_(CONFIRMING_TIERS),
    ).order_by(DedupeDecision.id)
    decisions: dict[int, list[DedupeDecision]] = {}
    for decision in opened.execute(backing).scalars():
        if decision.matched_item_id is not None:
            decisions.setdefault(decision.matched_item_id, []).append(decision)
    found = [decision for listed in decisions.values() for decision in listed]
    exports = {export.id: export for export in opened.execute(select(Export).where(Export.id.in_({d.export_id for d in found}))).scalars()}
    return Facts(resources, items, decisions, exports, accounts.served_people_of(opened, {decision.account_id for decision in found}))


def asset_query(device_id: int, photokit_ids: Sequence[str]) -> Select[int]:
    return select(PhoneAsset.id).where(PhoneAsset.device_id == device_id, PhoneAsset.photokit_id.in_(photokit_ids))


def asset_of(opened: Session, device_id: int, photokit_id: str) -> PhoneAsset | None:
    query = select(PhoneAsset).where(PhoneAsset.device_id == device_id, PhoneAsset.photokit_id == photokit_id)
    return opened.execute(query).scalar_one_or_none()


def same_resources(stored: dict[str, PhoneResource], reported: Reported) -> bool:
    declared = {resource.kind: (resource.sha1, resource.bytes, resource.filename) for resource in reported.resources}
    if len(declared) != len(reported.resources) or set(declared) != set(stored):
        return False
    return all((row.sha1, row.bytes, row.filename) == declared[kind] for kind, row in stored.items())


def backing_refusal(facts: Facts, item: MediaItem, person_id: int) -> str | None:
    decisions = facts.decisions.get(item.id, [])
    if not decisions:
        return NO_AUTOMATIC_MATCH
    for decision in decisions:
        export = facts.exports.get(decision.export_id)
        if (export is not None and export.account_id == decision.account_id and decision.person_id == person_id
                and person_id in facts.served.get(decision.account_id, set())):
            return None
    return ACCOUNT_NOT_SERVING


def file_intact(photos: Photos, slug: str, item: MediaItem) -> bool:
    try:
        path = photos.original(slug, item.path)
    except OutsideRoot:
        return False
    if path != photos.root / item.path:
        return False
    try:
        found = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(found.st_mode) and found.st_size == item.bytes


def resource_refusal(facts: Facts, photos: Photos, person: Person, row: PhoneResource) -> str | None:
    if row.uploaded_at is None or row.media_item_id is None:
        return NOT_UPLOADED
    item = facts.items.get(row.media_item_id)
    if item is None or item.source != "phone" or item.person_id != person.id or item.sha1 != row.sha1 or item.bytes != row.bytes:
        return NOT_ON_UNRAID
    if item.confirmed_in_google is not True:
        return NOT_CONFIRMED
    backing = backing_refusal(facts, item, person.id)
    if backing is not None:
        return backing
    if not file_intact(photos, person.slug, item):
        return FILE_MISSING
    return None


def asset_refusal(facts: Facts, photos: Photos, person: Person, asset: PhoneAsset | None, reported: Reported) -> str | None:
    if asset is None:
        return UNKNOWN_ASSET
    if asset.deleted_at is not None:
        return ALREADY_DELETED
    stored = facts.stored(asset.id)
    if not same_resources(stored, reported):
        return RESOURCES_CHANGED
    if "photo" not in stored:
        return NO_ORIGINAL
    for kind in KINDS:
        row = stored.get(kind)
        refusal = None if row is None else resource_refusal(facts, photos, person, row)
        if refusal is not None:
            return refusal
    return None


def as_known(asset: PhoneAsset, stored: dict[str, PhoneResource]) -> Reported:
    return Reported(asset.photokit_id, tuple(ReportedResource(row.kind, row.sha1, row.bytes, row.filename) for row in stored.values()))


def require_distinct(reported: Sequence[Reported]) -> None:
    if len({entry.photokit_id for entry in reported}) != len(reported):
        raise ValueError("the same asset is listed twice")


def judged(opened: Session, settings: Settings, photos: Photos, device: Device, reported: Sequence[Reported], now: float
           ) -> tuple[bool, Facts, list[tuple[Reported, PhoneAsset | None, str | None]]]:
    require_distinct(reported)
    refusal = device_refusal(opened, settings, device, now)
    person = opened.get(Person, device.person_id)
    wanted = asset_query(device.id, [entry.photokit_id for entry in reported])
    found = {asset.photokit_id: asset for asset in opened.execute(select(PhoneAsset).where(PhoneAsset.id.in_(wanted))).scalars()}
    facts = load_facts(opened, wanted)
    out: list[tuple[Reported, PhoneAsset | None, str | None]] = []
    for entry in reported:
        asset = found.get(entry.photokit_id)
        if refusal is None and person is not None:
            out.append((entry, asset, asset_refusal(facts, photos, person, asset, entry)))
        else:
            out.append((entry, asset, refusal or ACCESS_LAPSED))
    return refusal is None, facts, out


def eligible_of(facts: Facts, asset: PhoneAsset) -> Eligible:
    stored = facts.stored(asset.id)
    return Eligible(asset.photokit_id, sum(row.bytes for row in stored.values()), tuple(kind for kind in KINDS if kind in stored))


def settle(opened: Session, device: Device, facts: Facts, results: list[tuple[Reported, PhoneAsset | None, str | None]], now: float,
           confirming: bool) -> tuple[list[Eligible], list[Rejected]]:
    passed: list[Eligible] = []
    rejected: list[Rejected] = []
    for entry, asset, reason in results:
        if reason is None and asset is not None:
            asset.eligible_at = now
            if confirming:
                asset.eligible_confirmed_at = now
            passed.append(eligible_of(facts, asset))
            continue
        if asset is not None:
            forget(asset)
        rejected.append(Rejected(entry.photokit_id, reason or UNKNOWN_ASSET))
    device.eligibility_checked_at = now
    opened.flush()
    return passed, rejected


def eligible_assets(opened: Session, settings: Settings, photos: Photos, device_id: int, reported: Sequence[Reported], now: float) -> Evaluation:
    device = take_write_lock(opened, device_id)
    enabled, facts, results = judged(opened, settings, photos, device, reported, now)
    eligible, rejected = settle(opened, device, facts, results, now, confirming=False)
    return Evaluation(enabled, eligible, rejected)


def confirm(opened: Session, settings: Settings, photos: Photos, device_id: int, reported: Sequence[Reported], now: float
            ) -> tuple[list[Eligible], list[Rejected]]:
    device = take_write_lock(opened, device_id)
    _, facts, results = judged(opened, settings, photos, device, reported, now)
    confirmed, rejected = settle(opened, device, facts, results, now, confirming=True)
    events.record(opened, now, "cleanup_confirmed", subject_of(opened, device), f"{len(confirmed)} of {len(results)} confirmed")
    return confirmed, rejected


def record_deleted(opened: Session, device_id: int, asset_ids: Sequence[str], now: float) -> tuple[list[str], list[Rejected]]:
    device = take_write_lock(opened, device_id)
    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("the same asset is listed twice")
    recorded: list[str] = []
    rejected: list[Rejected] = []
    for photokit_id in asset_ids:
        asset = asset_of(opened, device.id, photokit_id)
        if asset is None:
            rejected.append(Rejected(photokit_id, UNKNOWN_ASSET))
        elif asset.deleted_at is not None:
            rejected.append(Rejected(photokit_id, ALREADY_DELETED))
        elif asset.eligible_confirmed_at is None:
            rejected.append(Rejected(photokit_id, NOT_CONFIRMED_FOR_DELETION))
        else:
            asset.deleted_at = now
            asset.eligible_at = None
            recorded.append(photokit_id)
    opened.flush()
    events.record(opened, now, "cleanup_deleted", subject_of(opened, device), f"{len(recorded)} recorded, {len(rejected)} refused")
    return recorded, rejected


def forget(asset: PhoneAsset) -> None:
    asset.eligible_at = None
    asset.eligible_confirmed_at = None


def subject_of(opened: Session, device: Device) -> str:
    person = opened.get(Person, device.person_id)
    return person.slug if person is not None else str(device.person_id)


def set_cleanup(opened: Session, device_id: int, enabled: bool, icloud_photos_off: bool, now: float) -> Device:
    device = take_write_lock(opened, device_id)
    if enabled and device.revoked_at is not None:
        raise NotAllowed("a signed-out iPhone cannot have cleanup turned on")
    if enabled and icloud_photos_off is not True:
        raise NotAllowed("confirm that iCloud Photos is off on this iPhone before turning cleanup on")
    if device.cleanup_enabled is enabled:
        return device
    opened.execute(sql_update(PhoneAsset).where(PhoneAsset.device_id == device.id).values(eligible_at=None, eligible_confirmed_at=None))
    device.cleanup_enabled = enabled
    device.eligibility_checked_at = None
    opened.flush()
    events.record(opened, now, "cleanup_enabled" if enabled else "cleanup_disabled", subject_of(opened, device), device.name)
    return device


def counts(opened: Session, settings: Settings, photos: Photos, device: Device, now: float) -> tuple[int | None, int | None]:
    if device.eligibility_checked_at is None:
        return None, None
    person = opened.get(Person, device.person_id)
    if device_refusal(opened, settings, device, now) is not None or person is None:
        return 0, 0
    marked = select(PhoneAsset.id).where(PhoneAsset.device_id == device.id, PhoneAsset.eligible_at.is_not(None), PhoneAsset.deleted_at.is_(None))
    facts = load_facts(opened, marked)
    number = size = 0
    for asset in opened.execute(select(PhoneAsset).where(PhoneAsset.id.in_(marked)).order_by(PhoneAsset.id)).scalars():
        stored = facts.stored(asset.id)
        if asset_refusal(facts, photos, person, asset, as_known(asset, stored)) is None:
            number += 1
            size += sum(row.bytes for row in stored.values())
    return number, size
