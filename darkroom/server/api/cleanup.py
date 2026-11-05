from __future__ import annotations

from typing import Annotated

from darkroom import monitoring, validate
from darkroom.domain import eligibility, uploads
from darkroom.server import contract
from darkroom.server.api.device import MAX_ASSETS, MAX_BODY, SIGN_IN, assets_of, device_of
from darkroom.server.common import Request, Response, engine_of, error_response, json_response, now
from darkroom.storage import db
from darkroom.storage.paths import Photos

BUSY = "Darkroom is busy with another change; try again in a moment"


def reported_of(request: Request) -> list[eligibility.Reported]:
    found = assets_of(validate.json_object(request.body, limit=MAX_BODY))
    return [eligibility.Reported(asset.photokit_id, tuple(eligibility.ReportedResource(r.kind, r.sha1, r.bytes, r.filename) for r in asset.resources))
            for asset in found]


def ids_of(request: Request) -> list[str]:
    value = validate.json_object(request.body, limit=MAX_BODY).get("ids")
    if not isinstance(value, list) or not value or len(value) > MAX_ASSETS or not all(isinstance(entry, str) for entry in value):
        raise validate.Invalid(f"ids must be a list of 1 to {MAX_ASSETS} asset ids")
    ids = [uploads.asset_id_of(entry) for entry in value]
    if len(set(ids)) != len(ids):
        raise validate.Invalid("the same asset is listed twice")
    return ids


def asset_view(entry: eligibility.Eligible) -> dict[str, object]:
    return {"id": entry.asset_id, "bytes": entry.bytes, "resources": list(entry.resources)}


def refusal_view(entry: eligibility.Rejected) -> dict[str, object]:
    return {"id": entry.asset_id, "reason": entry.reason, "message": eligibility.REASONS[entry.reason]}


def eligible(request: Annotated[Request, contract.EligibleBody]) -> Annotated[Response, contract.EligibleReply]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        reported = reported_of(request)
    except (validate.Invalid, uploads.BadUpload) as error:
        return error_response(str(error), 400)
    try:
        with db.session(engine_of(request)) as opened:
            found = eligibility.eligible_assets(opened, request.settings, Photos(request.settings.photos_root), who.device_id, reported, now())
    except eligibility.Busy:
        return error_response(BUSY, 503)
    except eligibility.NoSuchDevice:
        return error_response(SIGN_IN, 401)
    monitoring.log("eligibility checked", asked=len(reported), eligible=len(found.eligible), cleanup_enabled=found.cleanup_enabled)
    return json_response({"cleanup_enabled": found.cleanup_enabled, "eligible": [asset_view(entry) for entry in found.eligible],
                          "rejected": [refusal_view(entry) for entry in found.rejected]})


def confirm(request: Annotated[Request, contract.EligibleBody]) -> Annotated[Response, contract.ConfirmReply]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        reported = reported_of(request)
    except (validate.Invalid, uploads.BadUpload) as error:
        return error_response(str(error), 400)
    try:
        with db.session(engine_of(request)) as opened:
            confirmed, rejected = eligibility.confirm(opened, request.settings, Photos(request.settings.photos_root), who.device_id, reported, now())
    except eligibility.Busy:
        return error_response(BUSY, 503)
    except eligibility.NoSuchDevice:
        return error_response(SIGN_IN, 401)
    monitoring.log("cleanup confirmed", asked=len(reported), confirmed=len(confirmed))
    return json_response({"confirmed": [asset_view(entry) for entry in confirmed], "rejected": [refusal_view(entry) for entry in rejected]})


def deleted(request: Annotated[Request, contract.DeletedBody]) -> Annotated[Response, contract.DeletedReply]:
    who = device_of(request)
    if who is None or who.device_id is None:
        return error_response(SIGN_IN, 401)
    try:
        ids = ids_of(request)
    except (validate.Invalid, uploads.BadUpload) as error:
        return error_response(str(error), 400)
    try:
        with db.session(engine_of(request)) as opened:
            recorded, rejected = eligibility.record_deleted(opened, who.device_id, ids, now())
    except eligibility.Busy:
        return error_response(BUSY, 503)
    except eligibility.NoSuchDevice:
        return error_response(SIGN_IN, 401)
    monitoring.log("cleanup deleted", reported=len(ids), recorded=len(recorded))
    return json_response({"recorded": recorded, "rejected": [refusal_view(entry) for entry in rejected]})
