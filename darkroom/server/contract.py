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


class Status(TypedDict):
    worker_last_seen: float | None


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
