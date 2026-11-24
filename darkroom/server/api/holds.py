from __future__ import annotations

from typing import Annotated

from darkroom.domain import holds
from darkroom.server import contract
from darkroom.server.api.review import BadQuery, whole
from darkroom.server.common import Request, Response, engine_of, error_response, first_query, json_response
from darkroom.storage import db


def list_holds(request: Request) -> Annotated[Response, contract.Holds]:
    source = first_query(request, "source") or None
    if source is not None and source not in holds.SOURCES:
        return error_response(f"source is one of {', '.join(holds.SOURCES)}", 400)
    try:
        person, after = whole(request, "person"), whole(request, "after")
    except BadQuery as error:
        return error_response(str(error), 400)
    with db.session(engine_of(request)) as opened:
        found, total = holds.listing(opened, person, source, after)
    return json_response({"holds": found, "total": total})
