from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path
from typing import Annotated

from darkroom import monitoring, validate
from darkroom.domain import people
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db, secretbox
from darkroom.storage.paths import Photos

MAX_BODY = 16 * 1024
MAX_KEY = 500
NO_SECRET = "Darkroom has no DARKROOM_SECRET_KEY, so it cannot keep an Immich key"
NO_PERSON = "no such person"
NO_FOLDERS = "Darkroom could not create the person's folders"
FOLDER_CAUSES = {
    "EACCES": "Darkroom cannot write to its photo share (permission denied): check the share's ownership and permissions, because Darkroom runs as uid {uid} with group {gid} and the share and its _inbox and _browser folders must be writable by that user or group, for example group {gid} with mode 2770",
    "EPERM": "Darkroom cannot write to its photo share (permission denied): check the share's ownership and permissions, because Darkroom runs as uid {uid} with group {gid} and the share and its _inbox and _browser folders must be writable by that user or group, for example group {gid} with mode 2770",
    "EROFS": "Darkroom's photo share is read-only: mount it read-write",
    "ENOSPC": "Darkroom's photo share is out of space: free some space on it",
    "EDQUOT": "Darkroom's photo share is out of space: free some space on it",
    "ENOENT": "Darkroom's photo share or one of its folders is missing: check that the share is mounted",
}
OTHER_CAUSE = "see the Darkroom server log for the cause"


def folder_failure(error: OSError, root: Path) -> Response:
    name = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    folder = "unknown"
    if isinstance(error.filename, str):
        with contextlib.suppress(ValueError):
            parts = Path(error.filename).relative_to(root).parts
            folder = "/".join(parts[:2] if parts[:1] == ("_inbox",) else parts[:1]) or "unknown"
    monitoring.log("person folders could not be created", operation="create person", errno=name, folder=folder)
    cause = FOLDER_CAUSES.get(name, OTHER_CAUSE).format(uid=os.geteuid(), gid=os.getegid())
    return error_response(f"{NO_FOLDERS}: {cause}", 500)


def key_of(request: Request, body: dict[str, object]) -> str | None:
    if body.get("immich_api_key") is None:
        return None
    plain = validate.text(body, "immich_api_key", limit=MAX_KEY)
    return people.seal_key(plain, request.settings.secret_key)


def optional_email(body: dict[str, object]) -> str | None:
    return validate.email(body, "email") if body.get("email") is not None else None


def list_people(request: Request) -> Annotated[Response, contract.People]:
    with db.session(engine_of(request)) as opened:
        listed = [people.as_dict(person) for person in people.listing(opened)]
    return json_response({"people": listed})


def create_person(request: Annotated[Request, contract.PersonCreateBody]) -> Annotated[Response, contract.PersonReply]:
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        name = validate.text(body, "name", limit=100)
        slug = validate.text(body, "slug", required=False, limit=63)
        email = optional_email(body)
        user_id = validate.text_or_none(body, "immich_user_id")
        library_id = validate.text_or_none(body, "immich_library_id")
        sealed = key_of(request, body)
        with db.session(engine_of(request), writing=True) as opened:
            person = people.create(opened, Photos(request.settings.photos_root), name, slug, email, user_id, library_id, sealed, now())
            reply = people.as_dict(person)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except people.BadSlug as error:
        return error_response(str(error), 400)
    except (people.SlugTaken, people.FolderInUse) as error:
        return error_response(str(error), 409)
    except secretbox.Unreadable:
        return error_response(NO_SECRET, 503)
    except OSError as error:
        return folder_failure(error, Photos(request.settings.photos_root).root)
    return json_response({"person": reply}, 201)


def update_person(request: Annotated[Request, contract.PersonPatchBody]) -> Annotated[Response, contract.PersonReply]:
    person_id = path_id(request)
    if person_id is None:
        return error_response(NO_PERSON, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        changes: dict[str, str | None] = {}
        if validate.present(body, "name"):
            changes["name"] = validate.text(body, "name", limit=100)
        if validate.present(body, "email"):
            changes["email"] = optional_email(body)
        for field in ("immich_user_id", "immich_library_id"):
            if validate.present(body, field):
                changes[field] = validate.text_or_none(body, field)
        change_key = validate.present(body, "immich_api_key")
        sealed = key_of(request, body) if change_key else None
        with db.session(engine_of(request), writing=True) as opened:
            person = people.update(opened, person_id, changes, change_key, sealed, now())
            reply = people.as_dict(person)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except people.NoSuchPerson:
        return error_response(NO_PERSON, 404)
    except secretbox.Unreadable:
        return error_response(NO_SECRET, 503)
    return json_response({"person": reply})


def delete_person(request: Request) -> Annotated[Response, contract.Ok]:
    person_id = path_id(request)
    if person_id is None:
        return error_response(NO_PERSON, 404)
    try:
        with db.session(engine_of(request), writing=True) as opened:
            people.delete(opened, person_id, now())
    except people.NoSuchPerson:
        return error_response(NO_PERSON, 404)
    except people.InUse as error:
        return error_response(str(error), 409)
    return json_response({"ok": True})


