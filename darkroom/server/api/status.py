from __future__ import annotations

from typing import Annotated

from darkroom import VERSION
from darkroom.domain import browser_jobs, index
from darkroom.domain.ingest import run
from darkroom.server import contract
from darkroom.server.common import Request, Response, json_response
from darkroom.storage import db


def healthz(request: Request) -> Annotated[Response, contract.Health]:
    return json_response({"ok": True, "version": VERSION})


def status(request: Request) -> Annotated[Response, contract.Status]:
    if request.engine is None:
        raise RuntimeError("this route needs the database")
    with db.session(request.engine) as opened:
        seen = browser_jobs.worker_last_seen(opened)
        unmatched = run.unmatched_by_person(opened)
        people = [{"slug": slug, "name": name, "indexed": count, "unmatched": unmatched.get(slug)} for slug, name, count in index.counts_by_person(opened)]
    return json_response({"worker_last_seen": seen, "people": people})
