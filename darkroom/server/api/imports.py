from __future__ import annotations

import json
from typing import Annotated

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain.ingest import run
from darkroom.server import contract
from darkroom.server.api.browser_internal import engine_of
from darkroom.server.common import Request, Response, error_response, json_response
from darkroom.storage import db
from darkroom.storage.models import Export, ExportPart, ImportRun

ID_DIGITS = 12
LIST_LIMIT = 200


def id_of(request: Request) -> int | None:
    raw = request.params.get("id", "")
    return int(raw) if raw.isdigit() and len(raw) <= ID_DIGITS else None


def counts(raw: str | None) -> dict[str, int]:
    try:
        found = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(found, dict):
        return {}
    return {slug: count for slug, count in found.items() if isinstance(slug, str) and isinstance(count, int)}


def view(opened: Session, row: ImportRun) -> dict[str, object]:
    export = opened.get(Export, row.export_id)
    parts = opened.execute(select(ExportPart).where(ExportPart.export_id == row.export_id).order_by(ExportPart.number)).scalars()
    return {
        "id": row.id, "export_id": row.export_id, "account_id": None if export is None else export.account_id,
        "export_kind": None if export is None else export.kind, "export_created_at": None if export is None else export.created_at,
        "part_count": None if export is None else export.part_count, "parts": [{"number": part.number, "status": part.status} for part in parts],
        "started": row.started, "finished": row.finished, "stage": row.stage, "status": row.status,
        "added": row.added, "skipped_t1": row.skipped_t1, "skipped_t2": row.skipped_t2, "skipped_t3": row.skipped_t3,
        "review": row.review, "missing_gps": row.missing_gps, "failed": row.failed,
        "unmatched_by_person": counts(row.unmatched_by_person), "error": row.error,
    }


def list_imports(request: Request) -> Annotated[Response, contract.Imports]:
    with db.session(engine_of(request)) as opened:
        rows = opened.execute(select(ImportRun).order_by(ImportRun.started.desc(), ImportRun.id.desc()).limit(LIST_LIMIT)).scalars()
        found = [view(opened, row) for row in rows]
    return json_response({"imports": found})


def one(request: Request) -> Annotated[Response, contract.ImportReply]:
    wanted = id_of(request)
    if wanted is None:
        return error_response("no such import", 404)
    with db.session(engine_of(request)) as opened:
        row = opened.get(ImportRun, wanted)
        if row is None:
            return error_response("no such import", 404)
        reply = view(opened, row)
    return json_response({"run": reply})


def request(request: Request) -> Annotated[Response, contract.ImportQueued]:
    wanted = id_of(request)
    if wanted is None:
        return error_response("no such export", 404)
    try:
        with db.session(engine_of(request)) as opened:
            export = run.request_import(opened, wanted)
            reply = {"export_id": export.id, "status": export.status, "stage": export.stage}
    except LookupError:
        return error_response("no such export", 404)
    except (run.AlreadyImported, run.NotReady) as error:
        return error_response(str(error), 409)
    return json_response(reply, 202)
