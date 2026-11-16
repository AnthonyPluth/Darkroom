from __future__ import annotations

from typing import Annotated

from darkroom import config, validate
from darkroom.domain import accounts, pixels, transport
from darkroom.providers import syncthing
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now, path_id
from darkroom.storage import db

MAX_BODY = 16 * 1024
NO_PIXEL = "no such Pixel"
TRANSPORT_OFF = "Darkroom's Syncthing is off"
UNREACHABLE = "Darkroom's Syncthing could not be reached; try again in a moment, or type the Pixel's device ID instead"
NOT_WAITING = "that device is no longer waiting; ask the Pixel to connect again"
UNCONFIRMED = "the approval was not confirmed"
RELEASE_UNCONFIRMED = "confirmed must be true to let Darkroom share this Pixel's folder again"
MOST_WAITING = 50
LONGEST_NAME = 100
LONGEST_DEVICE_ID = 80


def typed_device(body: dict[str, object], name: str) -> str | None:
    value = body.get(name)
    if isinstance(value, str) and len(value) > LONGEST_DEVICE_ID:
        raise pixels.InvalidDevice(syncthing.NOT_A_DEVICE_ID)
    return validate.text_or_none(body, name, limit=LONGEST_DEVICE_ID)


def syncthing_id(body: dict[str, object]) -> str | None:
    return typed_device(body, "syncthing_device_id")


def transport_client(request: Request) -> syncthing.Client:
    return syncthing.Client.on_socket(str(config.SYNCTHING_SOCKET), request.settings.syncthing_key)


def own_or_none(request: Request) -> str | None:
    if not request.settings.syncthing:
        return None
    try:
        return syncthing.device_id(transport_client(request).own_device_id())
    except (syncthing.SyncthingFailed, syncthing.InvalidDeviceId):
        return None


def shown_name(name: str) -> str:
    return "".join(character for character in name if character.isprintable())[:LONGEST_NAME]


def confirmed(body: dict[str, object], refusal: str) -> None:
    if validate.boolean(body, "confirmed") is not True:
        raise validate.Invalid(refusal)


def own_device(request: Request) -> Annotated[Response, contract.OwnDevice]:
    if not request.settings.syncthing:
        return error_response(TRANSPORT_OFF, 409)
    try:
        found = transport_client(request).own_device_id()
    except syncthing.SyncthingFailed:
        return error_response(UNREACHABLE, 503)
    return json_response({"device_id": found})


def waiting_devices(request: Request) -> Annotated[Response, contract.WaitingDevices]:
    if not request.settings.syncthing:
        return error_response(TRANSPORT_OFF, 409)
    try:
        found = transport_client(request).pending_devices()
    except syncthing.SyncthingFailed:
        return error_response(UNREACHABLE, 503)
    return json_response({"devices": [{"device_id": device.device_id, "name": shown_name(device.name)} for device in found[:MOST_WAITING]]})


def pair_pixel(request: Annotated[Request, contract.PixelPairBody]) -> Annotated[Response, contract.PixelReply]:
    if not request.settings.syncthing:
        return error_response(TRANSPORT_OFF, 409)
    pixel_id = path_id(request)
    if pixel_id is None:
        return error_response(NO_PIXEL, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        device = pixels.canonical_device(typed_device(body, "device_id") or validate.text(body, "device_id"))
        if body.get("confirmed") is not True:
            raise validate.Invalid(UNCONFIRMED)
    except (validate.Invalid, pixels.InvalidDevice) as error:
        return error_response(str(error), 400)
    try:
        client = transport_client(request)
        own = syncthing.device_id(client.own_device_id())
        waiting = {found.device_id for found in client.pending_devices()}
    except (syncthing.SyncthingFailed, syncthing.InvalidDeviceId):
        return error_response(UNREACHABLE, 503)
    if device == own:
        return error_response(pixels.OWN_DEVICE, 400)
    if device not in waiting:
        return error_response(NOT_WAITING, 409)
    try:
        with db.session(engine_of(request)) as opened:
            pixel = pixels.update(opened, pixel_id, {"syncthing_device_id": device}, now(), own)
            reply = pixels.as_dict(opened, pixel)
    except pixels.NoSuchPixel:
        return error_response(NO_PIXEL, 404)
    except pixels.OwnDevice as error:
        return error_response(str(error), 400)
    except (pixels.DeviceTaken, accounts.StillConfirming, pixels.StillHasCopies) as error:
        return error_response(str(error), 409)
    except accounts.Busy as error:
        return error_response(str(error), 503)
    return json_response({"pixel": reply})


def release_folder(request: Annotated[Request, contract.ReleaseFolderBody]) -> Annotated[Response, contract.Ok]:
    if not request.settings.syncthing:
        return error_response(TRANSPORT_OFF, 409)
    pixel_id = path_id(request)
    if pixel_id is None:
        return error_response(NO_PIXEL, 404)
    try:
        confirmed(validate.json_object(request.body, limit=MAX_BODY), RELEASE_UNCONFIRMED)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    try:
        with db.session(engine_of(request), writing=True) as opened:
            pixels.get(opened, pixel_id)
            transport.release_folder(opened, pixel_id, now())
    except pixels.NoSuchPixel:
        return error_response(NO_PIXEL, 404)
    except transport.NotRemoved as error:
        return error_response(str(error), 409)
    return json_response({"ok": True})


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
        own = own_or_none(request) if device_id is not None else None
        with db.session(engine_of(request), writing=True) as opened:
            pixel = pixels.create(opened, name, device_id, account_id, people_ids, now(), own)
            reply = pixels.as_dict(opened, pixel)
    except (validate.Invalid, pixels.InvalidDevice, pixels.OwnDevice, pixels.NoPeople) as error:
        return error_response(str(error), 400)
    except pixels.DeviceTaken as error:
        return error_response(str(error), 409)
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
        own = own_or_none(request) if fields.get("syncthing_device_id") is not None else None
        with db.session(engine_of(request)) as opened:
            pixel = pixels.update(opened, pixel_id, fields, now(), own)
            reply = pixels.as_dict(opened, pixel)
    except (validate.Invalid, pixels.InvalidDevice, pixels.OwnDevice, pixels.NoPeople) as error:
        return error_response(str(error), 400)
    except pixels.DeviceTaken as error:
        return error_response(str(error), 409)
    except pixels.NoSuchPixel:
        return error_response(NO_PIXEL, 404)
    except accounts.NoSuchAccount:
        return error_response("no such Google account", 404)
    except accounts.NoSuchPerson:
        return error_response("no such person", 404)
    except (accounts.StillConfirming, pixels.StillHasCopies) as error:
        return error_response(str(error), 409)
    except accounts.Busy as error:
        return error_response(str(error), 503)
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
    except (accounts.StillConfirming, pixels.StillHasCopies) as error:
        return error_response(str(error), 409)
    except accounts.Busy as error:
        return error_response(str(error), 503)
    return json_response({"ok": True})
