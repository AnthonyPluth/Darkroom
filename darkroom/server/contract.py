from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


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
    cleanup_enabled: bool
    eligible: int | None
    eligible_bytes: int | None
    eligible_checked: float | None


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
    schedule_created: float | None
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
    needs_check: bool
    has_capture: bool
    capture_files: list[str]


class BrowserView(TypedDict):
    vnc_url: str | None
    worker_last_seen: float | None


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


class ExportPartDetail(TypedDict):
    number: int
    filename: str | None
    bytes: int | None
    progress: int
    downloads_used: int
    attempts: int
    status: str


class ExportPartView(TypedDict):
    number: int
    status: str


class ExportView(TypedDict):
    id: int
    account_id: int
    kind: str
    created_at: float | None
    ready_at: float | None
    expires_at: float | None
    part_count: int | None
    status: str
    stage: str | None
    parts: list[ExportPartDetail]


class ExportSchedule(TypedDict):
    account_id: int
    last_export_at: float | None
    next_export_at: float | None


class Exports(TypedDict):
    exports: list[ExportView]
    schedules: list[ExportSchedule]


class ExportReply(TypedDict):
    export: ExportView


class CheckNowReply(TypedDict):
    jobs: list[BrowserJobView]
    skipped: int


class ImportView(TypedDict):
    id: int
    export_id: int
    account_id: int | None
    export_kind: str | None
    export_created_at: float | None
    part_count: int | None
    parts: list[ExportPartView]
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


class ReviewSummary(TypedDict):
    id: int
    status: str
    decision_id: int
    export_id: int
    account_id: int
    tier: int
    reason: str
    decided_at: float
    reversed: bool
    resolved_at: float | None
    undo_until: float | None
    name: str | None
    library_name: str | None
    person_id: int | None
    kept_item_id: int | None
    kept_person_id: int | None
    decided_by_person_id: int | None


class Reviews(TypedDict):
    reviews: list[ReviewSummary]
    pending: int
    before: int


class ReviewSide(TypedDict):
    name: str | None
    path: str | None
    bytes: int | None
    captured_utc: float | None
    captured_local: str | None
    offset_minutes: int | None
    make: str | None
    model: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    is_video: bool


class LibrarySide(ReviewSide):
    item_id: int
    person_id: int
    source: str


class ExportSide(ReviewSide):
    held: bool


class PersonChoice(TypedDict):
    id: int
    name: str


class ReviewDetail(TypedDict):
    review: ReviewSummary
    library: LibrarySide | None
    export: ExportSide
    phash_distance: int | None
    served_people: list[PersonChoice]
    default_person_id: int | None


class ReviewReply(TypedDict):
    review: ReviewSummary


class ResolveBody(TypedDict):
    action: Literal["keep_both"]


class DecisionView(TypedDict):
    id: int
    export_id: int
    account_id: int
    name: str | None
    path: str | None
    person_id: int | None
    tier: int
    reason: str
    decided_by: str
    decided_at: float
    reversed_at: float | None
    matched_item_id: int | None
    takeout_item_id: int | None
    review_status: str | None


class Decisions(TypedDict):
    decisions: list[DecisionView]
    total: int
    page: int
    page_size: int


class DecisionReply(TypedDict):
    decision: DecisionView


class MediaView(LibrarySide):
    id: int
    export_id: int | None
    lat: float | None
    lon: float | None


class MediaList(TypedDict):
    media: list[MediaView]
    total: int
    page: int
    page_size: int


class MediaReply(TypedDict):
    media: MediaView


class ReassignBody(TypedDict):
    person_id: int


class DeviceResourceDecl(TypedDict):
    kind: str
    sha1: str
    bytes: int
    filename: str


class DeviceAssetDecl(TypedDict):
    id: str
    resources: list[DeviceResourceDecl]


class DeviceCheckBody(TypedDict):
    assets: list[DeviceAssetDecl]


class NeededResource(TypedDict):
    id: str
    kind: str


class DeviceCheckReply(TypedDict):
    needed: list[NeededResource]


class UploadReply(TypedDict):
    id: str
    kind: str
    sha1: str
    already: bool
    indexed: bool


class EligibleBody(TypedDict):
    assets: list[DeviceAssetDecl]


class EligibleAsset(TypedDict):
    asset_id: str
    bytes: int
    resources: list[str]


class Refusal(TypedDict):
    asset_id: str
    reason: str
    message: str


class EligibleReply(TypedDict):
    cleanup_enabled: bool
    eligible: list[EligibleAsset]
    rejected: list[Refusal]


class ConfirmReply(TypedDict):
    confirmed: list[str]
    rejected: list[Refusal]


class DeletedBody(TypedDict):
    asset_ids: list[str]


class DeletedReply(TypedDict):
    recorded: list[str]
    rejected: list[Refusal]


class DevicePatchBody(TypedDict):
    cleanup_enabled: bool
    icloud_photos_off: NotRequired[bool]


class DeviceCheckinBody(TypedDict):
    pending_uploads: int
    app_version: NotRequired[str | None]


class DevicePushTokenBody(TypedDict):
    token: str
