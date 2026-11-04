from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import events
from darkroom.storage import secretbox
from darkroom.storage.models import Device, GoogleAccount, MediaItem, Person, PixelPerson
from darkroom.storage.paths import SLUG, Photos


class SlugTaken(ValueError):
    pass


class FolderInUse(ValueError):
    pass


class BadSlug(ValueError):
    pass


class NoSuchPerson(LookupError):
    pass


class InUse(ValueError):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    return slug


def get(opened: Session, person_id: int) -> Person:
    person = opened.get(Person, person_id)
    if person is None:
        raise NoSuchPerson(f"no person {person_id}")
    return person


def listing(opened: Session) -> list[Person]:
    return list(opened.execute(select(Person).order_by(Person.id)).scalars())


def as_dict(person: Person) -> dict[str, object]:
    return {
        "id": person.id,
        "name": person.name,
        "slug": person.slug,
        "email": person.email,
        "immich_user_id": person.immich_user_id,
        "immich_library_id": person.immich_library_id,
        "has_immich_key": bool(person.immich_api_key_sealed),
        "created": person.created,
    }


def remove_empty(folders: list[Path]) -> None:
    for folder in folders:
        with contextlib.suppress(OSError):
            folder.rmdir()


def create_tree(photos: Photos, slug: str) -> None:
    person_folder = photos.inside(photos.person_part(slug))
    inbox_folder = photos.inbox(slug)
    inbox_root = inbox_folder.parent
    inbox_root_existed = inbox_root.exists()
    try:
        photos.ensure_person_tree(slug)
    except OSError:
        made = [photos.originals(slug), photos.inside(slug, "google-only"), person_folder, photos.manual_inbox(slug), inbox_folder]
        remove_empty([*made, *([] if inbox_root_existed else [inbox_root])])
        raise


def create(opened: Session, photos: Photos, name: str, slug: str, email: str | None, immich_user_id: str | None,
           immich_library_id: str | None, sealed_key: str | None, now: float) -> Person:
    slug = slug or slugify(name)
    if not SLUG.match(slug):
        raise BadSlug("slug must be lower-case letters, numbers and dashes, starting with a letter or number")
    if opened.execute(select(Person.id).where(Person.slug == slug)).first() is not None:
        raise SlugTaken(f"there is already a person with the folder name {slug}")
    if any(os.path.lexists(folder) for folder in (photos.inside(photos.person_part(slug)), photos.inbox(slug))):
        raise FolderInUse(f"the folder name {slug} is already in use on disk; a person's folders are never reused or deleted automatically, so choose another folder name")
    person = Person(name=name, slug=slug, email=email, immich_user_id=immich_user_id, immich_library_id=immich_library_id,
                    immich_api_key_sealed=sealed_key, originals_path=f"{slug}/originals", google_only_path=f"{slug}/google-only", created=now)
    opened.add(person)
    opened.flush()
    create_tree(photos, slug)
    events.record(opened, now, "person_created", slug)
    if sealed_key:
        events.record(opened, now, "immich_key_set", slug)
    return person


def seal_key(key: str, secret: str) -> str:
    return secretbox.seal(key, secret)


PLAIN_FIELDS = ("name", "email", "immich_user_id", "immich_library_id")


def update(opened: Session, person_id: int, changes: dict[str, str | None], change_key: bool, sealed_key: str | None, now: float) -> Person:
    person = get(opened, person_id)
    for field in PLAIN_FIELDS:
        if field in changes:
            setattr(person, field, changes[field])
    if change_key:
        had_key = bool(person.immich_api_key_sealed)
        person.immich_api_key_sealed = sealed_key
        if sealed_key:
            events.record(opened, now, "immich_key_set", person.slug)
        elif had_key:
            events.record(opened, now, "immich_key_removed", person.slug)
    opened.flush()
    events.record(opened, now, "person_updated", person.slug)
    return person


def blockers(opened: Session, person_id: int) -> list[str]:
    found = []
    counts = (
        ("media items", select(func.count(MediaItem.id)).where(MediaItem.person_id == person_id)),
        ("devices", select(func.count(Device.id)).where(Device.person_id == person_id)),
        ("Google accounts that list them as the default person", select(func.count(GoogleAccount.id)).where(GoogleAccount.default_person_id == person_id)),
        ("Pixels that serve them", select(func.count(PixelPerson.pixel_id)).where(PixelPerson.person_id == person_id)),
    )
    for label, query in counts:
        if opened.execute(query).scalar_one():
            found.append(label)
    return found


def delete(opened: Session, person_id: int, now: float) -> None:
    person = get(opened, person_id)
    found = blockers(opened, person_id)
    if found:
        raise InUse(f"{person.name} still has {', '.join(found)}; remove those first")
    slug = person.slug
    opened.delete(person)
    opened.flush()
    events.record(opened, now, "person_deleted", slug)
