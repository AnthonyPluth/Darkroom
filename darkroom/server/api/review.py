from __future__ import annotations

from typing import Annotated

from sqlalchemy.orm import Session

from darkroom import config, validate
from darkroom.domain import review, thumbs
from darkroom.domain.ingest import run
from darkroom.server import contract
from darkroom.server.common import Request, Response, engine_of, error_response, first_query, json_response, now, path_id
from darkroom.storage import db, paths
from darkroom.storage.paths import Photos

MAX_BODY = 4 * 1024
MAX_PAGE = 100_000
NUMBER_DIGITS = 12
NO_REVIEW = "no such review item"
NO_DECISION = "no such dedupe decision"
NO_MEDIA = "no such photo"
NO_THUMB = "no such picture"
THUMB_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"}


class BadQuery(ValueError):
    pass


def photos_of(request: Request) -> Photos:
    return Photos(request.settings.photos_root)


def who(request: Request) -> tuple[str, str | None]:
    return ("", None) if request.user is None else (request.user.sub, request.user.email)


def whole(request: Request, name: str, maximum: int = 2**31 - 1) -> int | None:
    raw = first_query(request, name)
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or len(raw) > NUMBER_DIGITS or int(raw) > maximum:
        raise BadQuery(f"{name} must be a whole number")
    return int(raw)


def instant(request: Request, name: str) -> float | None:
    raw = first_query(request, name)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise BadQuery(f"{name} must be a time in seconds") from error
    if value != value or abs(value) > 1e11:
        raise BadQuery(f"{name} must be a time in seconds")
    return value


def rescan(opened: Session, settings: config.Settings, changed: review.Changed, at: float) -> None:
    if changed.people:
        run.rescan_people(opened, settings, sorted(changed.people), changed.export, at, run.Tools())


def list_reviews(request: Request) -> Annotated[Response, contract.Reviews]:
    status = first_query(request, "status") or None
    if status is not None and status not in review.STATUSES:
        return error_response(f"status is one of {', '.join(review.STATUSES)}", 400)
    try:
        after = whole(request, "after")
    except BadQuery as error:
        return error_response(str(error), 400)
    if after is not None and status != "pending":
        return error_response("after is only for status=pending", 400)
    with db.session(engine_of(request)) as opened:
        reply = {"reviews": review.listing(opened, status, now(), after), "pending": review.pending_count(opened),
                 "before": 0 if after is None else review.pending_count(opened, after)}
    return json_response(reply)


def review_one(request: Request) -> Annotated[Response, contract.ReviewDetail]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_REVIEW, 404)
    try:
        with db.session(engine_of(request)) as opened:
            reply = review.detail(opened, photos_of(request), wanted)
    except review.NoSuchReview:
        return error_response(NO_REVIEW, 404)
    return json_response(reply)


def thumb(request: Request) -> Response:
    wanted, side = path_id(request), request.params.get("side", "")
    if wanted is None or side not in review.SIDES:
        return error_response(NO_THUMB, 404)
    try:
        with db.session(engine_of(request)) as opened:
            data = review.thumb(opened, photos_of(request), wanted, side)
    except (review.NoSuchReview, thumbs.NotAPhoto, paths.OutsideRoot):
        return error_response(NO_THUMB, 404)
    return Response(200, data, "image/jpeg", THUMB_HEADERS)


def resolve(request: Annotated[Request, contract.ResolveBody]) -> Annotated[Response, contract.ReviewReply]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_REVIEW, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        action = validate.text(body, "action", limit=20)
        if action not in review.ACTIONS:
            raise validate.Invalid("action is keep_both; skip leaves the pair for later and saves nothing")
        at = now()
        with db.session(engine_of(request), writing=True) as opened:
            with review.saving(opened) as effects:
                changed = review.resolve(opened, photos_of(request), wanted, action, at, effects, by=review.person_of_session(opened, *who(request)))
                hit = review.found(opened, wanted)
                reply = review.summary(opened, hit.review, hit.decision)
            rescan(opened, request.settings, changed, at)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except review.NoSuchReview:
        return error_response(NO_REVIEW, 404)
    except review.WrongState as error:
        return error_response(str(error), 409)
    return json_response({"review": reply})


def undo(request: Request) -> Annotated[Response, contract.ReviewReply]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_REVIEW, 404)
    try:
        at = now()
        with db.session(engine_of(request), writing=True) as opened:
            with review.saving(opened) as effects:
                changed = review.undo(opened, photos_of(request), wanted, at, effects, by=review.person_of_session(opened, *who(request)))
                hit = review.found(opened, wanted)
                reply = review.summary(opened, hit.review, hit.decision)
            rescan(opened, request.settings, changed, at)
    except review.NoSuchReview:
        return error_response(NO_REVIEW, 404)
    except (review.WrongState, review.TooLate) as error:
        return error_response(str(error), 409)
    return json_response({"review": reply})


def list_decisions(request: Request) -> Annotated[Response, contract.Decisions]:
    try:
        person, tier, export = whole(request, "person"), whole(request, "tier", 5), whole(request, "export")
        page = whole(request, "page", MAX_PAGE) or 0
    except BadQuery as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        found, total = review.decisions(opened, person, tier, export, page)
    return json_response({"decisions": found, "total": total, "page": page, "page_size": review.PAGE_SIZE})


def reverse(request: Request) -> Annotated[Response, contract.DecisionReply]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_DECISION, 404)
    try:
        with db.session(engine_of(request), writing=True) as opened:
            row = review.reverse(opened, wanted, now())
            reply = review.decision_view(opened, row)
    except review.NoSuchDecision:
        return error_response(NO_DECISION, 404)
    except review.WrongState as error:
        return error_response(str(error), 409)
    return json_response({"decision": reply})


def reassign(request: Annotated[Request, contract.ReassignBody]) -> Annotated[Response, contract.MediaReply]:
    wanted = path_id(request)
    if wanted is None:
        return error_response(NO_MEDIA, 404)
    try:
        body = validate.json_object(request.body, limit=MAX_BODY)
        person_id = validate.whole_number(body, "person_id", minimum=1)
        at = now()
        with db.session(engine_of(request), writing=True) as opened:
            with review.saving(opened) as effects:
                item, changed = review.reassign(opened, photos_of(request), wanted, person_id, at, effects, by=review.person_of_session(opened, *who(request)))
                reply = review.media_view(item)
            rescan(opened, request.settings, changed, at)
    except validate.Invalid as error:
        return error_response(str(error), 400)
    except review.NoSuchItem:
        return error_response(NO_MEDIA, 404)
    except (review.WrongState, review.NotServed) as error:
        return error_response(str(error), 409)
    return json_response({"media": reply})


def list_media(request: Request) -> Annotated[Response, contract.MediaList]:
    if first_query(request, "gps") != "none":
        return error_response("gps=none is the only filter so far", 400)
    try:
        person, page = whole(request, "person"), whole(request, "page", MAX_PAGE) or 0
        start, end = instant(request, "from"), instant(request, "to")
    except BadQuery as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        found, total = review.missing_gps(opened, person, start, end, page)
    return json_response({"media": found, "total": total, "page": page, "page_size": review.PAGE_SIZE})
