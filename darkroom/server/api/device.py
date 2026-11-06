from __future__ import annotations

from typing import Annotated
from urllib.parse import unquote

from sqlalchemy.exc import IntegrityError

from darkroom import monitoring, validate
from darkroom.domain import devices, push_tokens, uploads
from darkroom.server import contract
from darkroom.server.common import Principal, Request, Response, engine_of, error_response, header, json_response, now
from darkroom.storage import db, paths, secretbox
from darkroom.storage.models import Person
from darkroom.storage.paths import Photos

MAX_BODY = 4 * 1024 * 1024
MAX_ASSETS = 500
MAX_VERSION = 50
MAX_LENGTH_DIGITS = 19
SIGN_IN = "sign in required"


def device_of(request: Request) -> Principal | None:
    who = request.user
    return who if who is not None and who.kind == "device" and who.device_id is not None and who.person_id is not None else None


def objects(value: object, name: str, most: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value or len(value) > most or not all(isinstance(entry, dict) for entry in value):
        raise validate.Invalid(f"{name} must be a list of 1 to {most} objects")
    return value


def resource_of(entry: dict[str, object]) -> uploads.Resource:
    return uploads.Resource(uploads.kind_of(validate.text(entry, "kind", limit=20)), uploads.sha1_of(validate.text(entry, "sha1", limit=40)),
                            validate.whole_number(entry, "bytes", maximum=uploads.MAX_BYTES),
                            uploads.filename_of(validate.text(entry, "filename", limit=uploads.MAX_FILENAME)))


def asset_of(entry: dict[str, object]) -> uploads.Asset:
    photokit_id = uploads.asset_id_of(validate.text(entry, "id", limit=uploads.MAX_ASSET_ID))
    resources = tuple(resource_of(item) for item in objects(entry.get("resources"), "resources", len(uploads.KINDS)))
    if len({resource.kind for resource in resources}) != len(resources):
        raise validate.Invalid("an asset lists the same kind twice")
    return uploads.Asset(photokit_id, resources)


def assets_of(body: dict[str, object]) -> list[uploads.Asset]:
    found = [asset_of(entry) for entry in objects(body.get("assets"), "assets", MAX_ASSETS)]
    if len({asset.photokit_id for asset in found}) != len(found):
        raise validate.Invalid("the same asset is listed twice")
    return found


def check_assets(request: Annotated[Request, contract.DeviceCheckBody]) -> Annotated[Response, contract.DeviceCheckReply]:
    who = device_of(request)
    if who is None or who.device_id is None or who.person_id is None:
        return error_response(SIGN_IN, 401)
    try:
        assets = assets_of(validate.json_object(request.body, limit=MAX_BODY))
    except (validate.Invalid, uploads.BadUpload) as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        needed = uploads.check(opened, who.device_id, who.person_id, assets, now())
    return json_response({"needed": [{"id": photokit_id, "kind": kind} for photokit_id, kind in needed]})


def content_length(request: Request) -> int | None:
    if header(request, "Transfer-Encoding") is not None:
        return None
    raw = (header(request, "Content-Length") or "").strip()
    return int(raw) if raw.isdigit() and len(raw) <= MAX_LENGTH_DIGITS else None


def upload_reply(photokit_id: str, kind: str, sha1: str, already: bool, indexed: bool) -> Response:
    return json_response({"id": photokit_id, "kind": kind, "sha1": sha1, "already": already, "indexed": indexed})


def upload_resource(request: Request) -> Annotated[Response, contract.UploadReply]:
    who = device_of(request)
    if who is None or who.device_id is None or who.person_id is None or request.stream is None:
        return error_response(SIGN_IN, 401)
    length = content_length(request)
    if length is None:
        return error_response("an upload needs a Content-Length", 411)
    try:
        photokit_id = uploads.asset_id_of(unquote(request.params.get("id", "")))
        kind = uploads.kind_of(request.params.get("kind", ""))
        sha1 = uploads.sha1_of(header(request, "X-Darkroom-SHA1") or "")
        filename = uploads.filename_of(header(request, "X-Darkroom-Filename") or "")
    except uploads.BadUpload as error:
        return error_response(str(error), 400)
    engine = engine_of(request)
    photos = Photos(request.settings.photos_root)
    try:
        with db.session(engine) as opened:
            row = uploads.expected(opened, who.device_id, photokit_id, kind, sha1, filename)
            uploaded, linked = row.uploaded_at is not None, row.media_item_id is not None
            person = opened.get(Person, who.person_id)
            slug = person.slug if person is not None else None
    except uploads.NotChecked as error:
        return error_response(str(error), 404)
    except uploads.NotWhatWasChecked as error:
        return error_response(str(error), 409)
    if slug is None:
        return error_response(SIGN_IN, 401)
    if uploaded:
        return upload_reply(photokit_id, kind, sha1, True, linked)
    try:
        target, already = uploads.save(photos, slug, filename, sha1, request.stream, length)
    except uploads.HashMismatch as error:
        monitoring.log("upload refused", reason="sha1 mismatch", kind=kind)
        return error_response(str(error), 422)
    except uploads.NameTaken as error:
        return error_response(str(error), 409)
    except paths.IncompleteUpload as error:
        return error_response(str(error), 400)
    except TimeoutError:
        monitoring.log("upload refused", reason="read timeout", kind=kind)
        return error_response("the upload stopped sending", 408)
    try:
        with db.session(engine) as opened:
            current = uploads.expected(opened, who.device_id, photokit_id, kind, sha1, filename)
            person = opened.get(Person, who.person_id)
            indexed = uploads.record(opened, photos, person, current, target, now()) if person is not None else False
    except (uploads.NotChecked, uploads.NotWhatWasChecked):
        monitoring.log("upload not recorded", reason="changed while uploading", kind=kind)
        return error_response("the asset changed while this upload was sent; check it again", 409)
    monitoring.log("upload received", kind=kind, already=already, indexed=indexed)
    return upload_reply(photokit_id, kind, sha1, already, indexed)


def device_me(request: Request) -> Annotated[Response, contract.DeviceMeReply]:
    who = device_of(request)
    if who is None or who.person_id is None or who.name is None:
        return error_response(SIGN_IN, 401)
    with db.session(engine_of(request)) as opened:
        person = opened.get(Person, who.person_id)
        if person is None:
            return error_response(SIGN_IN, 401)
        return json_response({"person": person.name, "device": who.name})


def checkin(request: Annotated[Request, contract.DeviceCheckinBody]) -> Annotated[Response, contract.Ok]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        body = validate.json_object(request.body, limit=16 * 1024)
        pending = validate.whole_number(body, "pending_uploads")
        version = validate.text_or_none(body, "app_version", limit=MAX_VERSION)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        devices.check_in(opened, who.device_id, pending, version, now())
    return json_response({"ok": True})


def register_push_token(request: Annotated[Request, contract.DevicePushTokenBody]) -> Annotated[Response, contract.Ok]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        body = validate.json_object(request.body, limit=4 * 1024)
        token = push_tokens.normalized(validate.text(body, "token", limit=push_tokens.MAX_RAW))
    except (validate.Invalid, push_tokens.BadToken) as error:
        return error_response(str(error), 400)
    try:
        changed = register_once_more_if_raced(request, who.device_id, token)
    except push_tokens.DeviceGone:
        return error_response(SIGN_IN, 401)
    except push_tokens.HeldByAnotherPerson as error:
        return error_response(str(error), 409)
    except secretbox.Unreadable:
        return error_response("push alerts need DARKROOM_SECRET_KEY to be set on the server", 503)
    monitoring.log("push token registered", changed=changed)
    return json_response({"ok": True})


def register_once_more_if_raced(request: Request, device_id: int, token: str) -> bool:
    for attempt in range(2):
        try:
            with db.session(engine_of(request)) as opened:
                return push_tokens.register(opened, request.settings, device_id, token, now())
        except IntegrityError:
            if attempt:
                raise
    raise AssertionError("unreachable")


def forget_push_token(request: Request) -> Annotated[Response, contract.Ok]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        with db.session(engine_of(request)) as opened:
            changed = push_tokens.forget_own(opened, who.device_id, now())
    except push_tokens.DeviceGone:
        return error_response(SIGN_IN, 401)
    monitoring.log("push token cleared", changed=changed)
    return json_response({"ok": True})
