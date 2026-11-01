from __future__ import annotations

from typing import TypedDict


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


REPLIES: dict[str, type] = {
    "GET /healthz": Health,
    "GET /api/me": Me,
    "GET /api/status": Status,
    "GET /api/browser/jobs": BrowserJobs,
    "POST /api/browser/jobs": BrowserJobReply,
    "POST /internal/browser/jobs/claim": WorkerReply,
    "POST /internal/browser/jobs/{id}/heartbeat": WorkerReply,
    "POST /internal/browser/jobs/{id}/progress": WorkerReply,
    "POST /internal/browser/jobs/{id}/waiting": WorkerReply,
    "POST /internal/browser/jobs/{id}/done": WorkerReply,
    "POST /internal/browser/jobs/{id}/failed": WorkerReply,
}
