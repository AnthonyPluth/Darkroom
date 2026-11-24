from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class Health(TypedDict):
    ok: bool
    version: str


class Version(TypedDict):
    version: str
    commit: str | None


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


class PixelSyncthing(TypedDict):
    pixel_id: int
    name: str
    state: Literal["not_syncing", "not_paired", "device_id_invalid", "device_id_shared", "folder_removed", "folder_path_refused", "checking", "held", "waiting", "not_seen",
                   "sending", "connected", "not_connected"]
    copies: int
    files: int
    days: int
    completion: float | None
    last_seen: float | None
    others: list[str]


class SyncthingCard(TypedDict):
    state: Literal["starting", "running", "down", "settings_failed", "version", "key", "share_check_failed"]
    reason: str | None
    since: float | None
    pixels: list[PixelSyncthing]


class PixelState(TypedDict):
    id: int
    name: str
    google_account_id: int
    last_sync: float | None
    syncthing: PixelSyncthing | None


class PersonStatus(TypedDict):
    id: int
    slug: str
    name: str
    indexed: int
    with_gps: int
    unknown_time: int
    index_skipped: int
    accounts: list[AccountState]
    pixels: list[PixelState]
    last_import: float | None
    unmatched: int | None
    alerts: list[AlertView]


class Status(TypedDict):
    worker_last_seen: float | None
    people: list[PersonStatus]
    alerts: list[AlertView]
    syncthing: SyncthingCard


class Ok(TypedDict):
    ok: bool


class DemoSyncthingBody(TypedDict):
    state: Literal["running", "down", "share_check_failed"]


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
    renew_from: float | None
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


class ScheduleCheckedBody(TypedDict):
    found: NotRequired[bool]


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
    syncthing: list[PixelSyncthing]


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


class PixelPairBody(TypedDict):
    device_id: str
    confirmed: bool


class ReleaseFolderBody(TypedDict):
    confirmed: bool


class OwnDevice(TypedDict):
    device_id: str


class WaitingDevice(TypedDict):
    device_id: str
    name: str


class WaitingDevices(TypedDict):
    devices: list[WaitingDevice]


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
    checked: str | None
    has_capture: bool
    capture_files: list[str]


class BrowserView(TypedDict):
    vnc_url: str | None
    worker_last_seen: float | None
    on_screen_job: int | None


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
    lane: NotRequired[Literal["any", "interactive"]]


class HeartbeatBody(TypedDict):
    pass


class ProgressBody(TypedDict):
    message: str


class OnScreenBody(TypedDict):
    pass


class WaitingBody(TypedDict):
    reason: str


class DoneBody(TypedDict):
    result: NotRequired[dict[str, object]]


class FailedBody(TypedDict):
    message: str
    failure_dir: NotRequired[str]
    files: NotRequired[list[str]]


class DownloadPlanBody(TypedDict):
    pass


class DownloadPartPlan(TypedDict):
    number: int
    bytes: int | None
    status: str
    filename: str | None
    downloads_used: int
    attempts: int
    max_downloads: int
    max_attempts: int
    sign_in_prompts: int


class DownloadPlan(TypedDict):
    export_id: int
    google_id: str
    person: str
    folder: str
    part_count: int
    parts: list[DownloadPartPlan]


class PartBody(TypedDict):
    event: Literal["space", "attempt", "progress", "done", "failed", "clicked", "prompted"]
    bytes: NotRequired[int]
    filename: NotRequired[str]
    reason: NotRequired[Literal["corrupt", "wrong_size", "not_started", "interrupted", "bad_name", "asked_to_verify"]]
    downloading: NotRequired[int]
    since_click: NotRequired[int]
    since_answer: NotRequired[int]
    clicks: NotRequired[int]
    google_downloads: NotRequired[int | None]


class PartReply(TypedDict):
    allowed: bool
    gave_up: bool
    paused_for_space: bool
    part: DownloadPartPlan


KeptReason = Literal["failed_item", "missing", "not_plain", "empty", "not_a_zip", "damaged", "symlink_entry", "outside", "file_and_folder", "device_entry",
                     "unreadable_entry", "name_too_long", "duplicate_name", "too_many_entries", "too_large", "zip_bomb", "sidecars_too_big", "size_lies",
                     "listing_differs", "bytes_differ"]


ImportFailureCode = Literal["files", "database", "metadata", "placing", "memory", "unexpected"]


class ImportFailure(TypedDict):
    code: ImportFailureCode
    error: str


class ExportPartDetail(TypedDict):
    number: int
    filename: str | None
    bytes: int | None
    progress: int
    downloads_used: int
    attempts: int
    status: str
    ingest_state: str
    entries: int
    entries_done: int
    held: int
    held_waiting_sidecar: int
    held_waiting_export: int
    failed_items: int
    kept_reason: KeptReason | None
    import_failure: ImportFailure | None
    freed: bool
    bytes_freed: int


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
    parts_ingested: int
    held_waiting_sidecar: int
    held_waiting_export: int
    bytes_freed: int
    retry_allowed: bool
    retry_refusal: str | None
    import_retry_allowed: bool


class ExportSchedule(TypedDict):
    account_id: int
    last_export_at: float | None
    next_export_at: float | None


class Exports(TypedDict):
    exports: list[ExportView]
    schedules: list[ExportSchedule]


class ExportReply(TypedDict):
    export: ExportView


class RetryDownloadReply(TypedDict):
    export: ExportView
    retried: bool


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
    sidecars_unreadable: int | None
    sidecars_repaired: int | None
    unmatched_by_person: dict[str, int]
    not_imported: dict[str, int] | None
    orphan_sidecars: int | None
    unpaired_media: int | None
    error: str | None


class Imports(TypedDict):
    imports: list[ImportView]


class ImportReply(TypedDict):
    run: ImportView


class ImportQueued(TypedDict):
    export_id: int
    status: str
    stage: str | None
    per_part: bool
    parts_requeued: int
    run_requeued: bool


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
    kept_in_immich: bool
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


class HoldFacts(TypedDict, total=False):
    wall_clock: str | None
    file_utc: float | None
    google_utc: float | None
    place_known: bool
    description_length: int | None
    description_unwritable: bool
    kind: str
    differs: list[str]
    immich_taken: str | None
    immich_offset: int | None
    intended_taken: str | None
    intended_offset: int | None


class HoldResolution(TypedDict):
    id: int
    receipt_id: int
    hold: Literal["unknown_offset", "no_instant", "unwritable", "not_enriched", "not_settled", "unusable"]
    action: Literal["offset", "capture_time", "drop_description", "accept", "record_offset", "recheck", "skip"]
    offset_minutes: int | None
    taken_local: str | None
    state: Literal["recorded", "applied", "undone", "superseded"]
    result: str | None
    decided_by_person_id: int | None
    decided_at: float
    applied_at: float | None
    undone_at: float | None
    can_undo: bool


class HoldView(TypedDict):
    id: int
    source: Literal["import", "kept", "backfill"]
    person_id: int
    export_id: int | None
    name: str | None
    hold: Literal["unknown_offset", "no_instant", "unwritable", "not_enriched", "not_settled", "unusable"]
    reason: str
    facts: HoldFacts | None
    actions: list[str]
    bytes_verified_at: float | None
    updated_at: float
    resolution: HoldResolution | None


class Holds(TypedDict):
    holds: list[HoldView]
    total: int


class HoldResolveBody(TypedDict):
    action: Literal["offset", "capture_time", "drop_description", "accept", "record_offset", "recheck", "skip"]
    offset_minutes: NotRequired[int]
    taken_local: NotRequired[str]


class HoldReply(TypedDict):
    resolution: HoldResolution


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


class SetupSetting(TypedDict):
    variable: str
    source: Literal["environment", "stored", "default"]
    set: bool
    value: str | None


class SetupProposal(TypedDict):
    oidc_issuer: str
    oidc_client_id: str
    has_client_secret: bool
    oidc_allowed_groups: list[str]
    oidc_allowed_emails: list[str]


class SetupState(TypedDict):
    settings: list[SetupSetting]
    redirect_uri: str | None
    proposal: SetupProposal | None
    phase: Literal["before_test", "after_test"]
    tested_email: str | None


class SetupAddressBody(TypedDict):
    public_url: str


class SetupImmichBody(TypedDict):
    immich_url: str


class SetupAlertsBody(TypedDict):
    resend_api_key: NotRequired[str]
    alert_email_from: str
    alert_email_to: str


class SetupSignInBody(TypedDict):
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: NotRequired[str]
    oidc_allowed_groups: list[str]
    oidc_allowed_emails: list[str]


class SetupTestStart(TypedDict):
    location: str
