from __future__ import annotations

from typing import NotRequired, TypedDict


class Health(TypedDict):
    ok: bool
    version: str


class Me(TypedDict):
    sub: str
    email: str
    name: str
    groups: list[str]


class PersonStatus(TypedDict):
    slug: str
    name: str
    indexed: int
    unmatched: int | None


class Status(TypedDict):
    worker_last_seen: float | None
    people: list[PersonStatus]


class BrowserJobView(TypedDict):
    id: int
    account_id: int
    kind: str
    status: str
    created: float
    started: float | None
    heartbeat: float | None
    finished: float | None
    message: str
    has_capture: bool


class BrowserJobs(TypedDict):
    jobs: list[BrowserJobView]


class BrowserJobReply(TypedDict):
    job: BrowserJobView


class WorkerJob(TypedDict):
    id: int
    kind: str
    account: str
    payload: dict[str, object]


class WorkerReply(TypedDict):
    job: WorkerJob | None


class EnqueueBody(TypedDict):
    account_id: int
    kind: str


class ClaimBody(TypedDict):
    worker: NotRequired[str]


class HeartbeatBody(TypedDict):
    pass


class ProgressBody(TypedDict):
    message: str


class WaitingBody(TypedDict):
    reason: str


class DoneBody(TypedDict):
    result: NotRequired[dict[str, object]]


class FailedBody(TypedDict):
    message: str
    failure_dir: NotRequired[str]
    files: NotRequired[list[str]]


class ImportView(TypedDict):
    id: int
    export_id: int
    account_id: int
    started: float
    finished: float | None
    stage: str | None
    status: str
    added: int
    skipped_t1: int
    skipped_t2: int
    skipped_t3: int
    review: int
    missing_gps: int
    failed: int
    unmatched_by_person: dict[str, int]
    error: str | None


class Imports(TypedDict):
    imports: list[ImportView]


class ImportReply(TypedDict):
    run: ImportView


class ImportQueued(TypedDict):
    export_id: int
    status: str
    stage: str | None
