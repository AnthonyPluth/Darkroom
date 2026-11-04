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


class AlertView(TypedDict):
    id: int
    kind: str
    subject: str
    message: str
    link: str | None
    opened: float


class AccountState(TypedDict):
    id: int
    email: str
    session_state: str
    session_checked: float | None
    schedule_end: float | None


class PixelState(TypedDict):
    id: int
    name: str
    google_account_id: int


class DeviceState(TypedDict):
    id: int
    name: str
    last_checkin: float | None
    pending_uploads: int
    eligible: int | None


class PersonStatus(TypedDict):
    id: int
    slug: str
    name: str
    indexed: int
    with_gps: int
    unknown_time: int
    accounts: list[AccountState]
    pixels: list[PixelState]
    devices: list[DeviceState]
    last_import: float | None
    unmatched: int | None
    alerts: list[AlertView]


class Status(TypedDict):
    worker_last_seen: float | None
    people: list[PersonStatus]
    alerts: list[AlertView]


class Ok(TypedDict):
    ok: bool


class PersonView(TypedDict):
    id: int
    name: str
    slug: str
    email: str | None
    immich_user_id: str | None
    immich_library_id: str | None
    has_immich_key: bool
    created: float


class People(TypedDict):
    people: list[PersonView]


class PersonReply(TypedDict):
    person: PersonView


class PersonCreateBody(TypedDict):
    name: str
    slug: NotRequired[str]
    email: NotRequired[str | None]
    immich_user_id: NotRequired[str | None]
    immich_library_id: NotRequired[str | None]
    immich_api_key: NotRequired[str | None]


class PersonPatchBody(TypedDict):
    name: NotRequired[str]
    email: NotRequired[str | None]
    immich_user_id: NotRequired[str | None]
    immich_library_id: NotRequired[str | None]
    immich_api_key: NotRequired[str | None]


class AccountView(TypedDict):
    id: int
    email: str
    default_person_id: int
    session_state: str
    session_checked: float | None
    schedule_end: float | None
    served_people: list[int]
    created: float


class Accounts(TypedDict):
    accounts: list[AccountView]


class AccountReply(TypedDict):
    account: AccountView


class AccountCreateBody(TypedDict):
    email: str
    default_person_id: int


class AccountPatchBody(TypedDict):
    email: NotRequired[str]
    default_person_id: NotRequired[int]


class PixelView(TypedDict):
    id: int
    name: str
    syncthing_device_id: str | None
    google_account_id: int
    people_ids: list[int]
    last_sync: float | None
    created: float


class Pixels(TypedDict):
    pixels: list[PixelView]


class PixelReply(TypedDict):
    pixel: PixelView


class PixelCreateBody(TypedDict):
    name: str
    google_account_id: int
    syncthing_device_id: NotRequired[str | None]
    people_ids: NotRequired[list[int]]


class PixelPatchBody(TypedDict):
    name: NotRequired[str]
    google_account_id: NotRequired[int]
    syncthing_device_id: NotRequired[str | None]
    people_ids: NotRequired[list[int]]


class DeviceView(TypedDict):
    id: int
    person_id: int
    name: str
    cleanup_enabled: bool
    last_checkin: float | None
    app_version: str | None
    pending_uploads: int
    created: float
    revoked_at: float | None


class Devices(TypedDict):
    devices: list[DeviceView]


class DeviceReply(TypedDict):
    device: DeviceView


class EventView(TypedDict):
    id: int
    time: float
    type: str
    subject: str
    detail: str | None


class Events(TypedDict):
    events: list[EventView]


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
