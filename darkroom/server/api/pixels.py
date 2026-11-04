from __future__ import annotations

import re
from typing import Annotated

from darkroom import validate
from darkroom.domain import accounts, pixels
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db

MAX_BODY = 16 * 1024
SYNCTHING_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
NO_PIXEL = "no such Pixel"


def syncthing_id(body: dict[str, object]) -> str | None:
    value = validate.text_or_none(body, "syncthing_device_id", limit=80)
    if value is not None and not SYNCTHING_ID.match(value):
        raise validate.Invalid("syncthing_device_id may hold only letters, numbers and dashes")
    return value


def list_pixels(request: Request) -> Annotated[Response, contract.Pixels]:
    with db.session(engine_of(request)) as opened:
        listed = [pixels.as_dict(opened, pixel) for pixel in pixels.listing(opened)]
    return json_response({"pixels": listed})


def create_pixel(request: Annotated[Request, contract.PixelCreateBody]) -> Annotated[Response, contract.PixelReply]:
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        name = validate.text(body, "name", limit=100)
        account_id = validate.whole_number(body, "google_account_id", minimum=1)
        device_id = syncthing_id(body)
        people_ids = validate.whole_numbers(body, "people_ids") if validate.present(body, "people_ids") else None
        with db.session(engine_of(request)) as opened:
            pixel = pixels.create(opened, name, device_id, account_id, people_ids, now())
            reply = pixels.as_dict(opened, pixel)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except pixels.NoPeople as error:
        return error_response(str(error), 400)
    except accounts.NoSuchAccount:
        return error_response("no such Google account", 404)
    except accounts.NoSuchPerson:
        return error_response("no such person", 404)
    return json_response({"pixel": reply}, 201)


def update_pixel(request: Annotated[Request, contract.PixelPatchBody]) -> Annotated[Response, contract.PixelReply]:
    pixel_id = path_id(request)
    if pixel_id is None:
        return error_response(NO_PIXEL, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        fields: dict[str, object] = {}
        if validate.present(body, "name"):
            fields["name"] = validate.text(body, "name", limit=100)
        if validate.present(body, "google_account_id"):
            fields["google_account_id"] = validate.whole_number(body, "google_account_id", minimum=1)
        if validate.present(body, "syncthing_device_id"):
            fields["syncthing_device_id"] = syncthing_id(body)
        if validate.present(body, "people_ids"):
            fields["people_ids"] = validate.whole_numbers(body, "people_ids")
        with db.session(engine_of(request)) as opened:
            pixel = pixels.update(opened, pixel_id, fields, now())
            reply = pixels.as_dict(opened, pixel)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except pixels.NoPeople as error:
        return error_response(str(error), 400)
    except pixels.NoSuchPixel:
        return error_response(NO_PIXEL, 404)
    except accounts.NoSuchAccount:
        return error_response("no such Google account", 404)
    except accounts.NoSuchPerson:
        return error_response("no such person", 404)
    return json_response({"pixel": reply})


def delete_pixel(request: Request) -> Annotated[Response, contract.Ok]:
    pixel_id = path_id(request)
    if pixel_id is None:
        return error_response(NO_PIXEL, 404)
    try:
        with db.session(engine_of(request)) as opened:
            pixels.delete(opened, pixel_id, now())
    except pixels.NoSuchPixel:
        return error_response(NO_PIXEL, 404)
    return json_response({"ok": True})
