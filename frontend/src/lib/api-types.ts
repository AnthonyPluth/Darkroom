export interface AccountCreateBody {
  email: string;
  default_person_id: number;
}

export interface AccountPatchBody {
  email?: string;
  default_person_id?: number;
}

export interface AccountReply {
  account: AccountView;
}

export interface AccountState {
  id: number;
  email: string;
  session_state: string;
  session_checked: number | null;
  schedule_end: number | null;
}

export interface AccountView {
  id: number;
  email: string;
  default_person_id: number;
  session_state: string;
  session_checked: number | null;
  schedule_end: number | null;
  served_people: number[];
  created: number;
}

export interface Accounts {
  accounts: AccountView[];
}

export interface AlertView {
  id: number;
  kind: string;
  subject: string;
  message: string;
  link: string | null;
  opened: number;
}

export interface BrowserJobReply {
  job: BrowserJobView;
}

export interface BrowserJobView {
  id: number;
  account_id: number;
  kind: string;
  status: string;
  created: number;
  started: number | null;
  heartbeat: number | null;
  finished: number | null;
  message: string;
  has_capture: boolean;
}

export interface BrowserJobs {
  jobs: BrowserJobView[];
}

export interface ClaimBody {
  worker?: string;
}

export interface DecisionReply {
  decision: DecisionView;
}

export interface DecisionView {
  id: number;
  export_id: number;
  account_id: number;
  name: string | null;
  path: string | null;
  person_id: number | null;
  tier: number;
  reason: string;
  decided_by: string;
  decided_at: number;
  reversed_at: number | null;
  matched_item_id: number | null;
  takeout_item_id: number | null;
  review_status: string | null;
}

export interface Decisions {
  decisions: DecisionView[];
  total: number;
  page: number;
  page_size: number;
}

export interface DeviceReply {
  device: DeviceView;
}

export interface DeviceState {
  id: number;
  name: string;
  last_checkin: number | null;
  pending_uploads: number;
  eligible: number | null;
}

export interface DeviceView {
  id: number;
  person_id: number;
  name: string;
  cleanup_enabled: boolean;
  last_checkin: number | null;
  app_version: string | null;
  pending_uploads: number;
  created: number;
  revoked_at: number | null;
}

export interface Devices {
  devices: DeviceView[];
}

export interface DoneBody {
  result?: Record<string, unknown>;
}

export interface EnqueueBody {
  account_id: number;
  kind: string;
}

export interface EventView {
  id: number;
  time: number;
  type: string;
  subject: string;
  detail: string | null;
}

export interface Events {
  events: EventView[];
}

export interface ExportPartView {
  number: number;
  status: string;
}

export interface ExportSide {
  name: string | null;
  path: string | null;
  bytes: number | null;
  captured_utc: number | null;
  captured_local: string | null;
  offset_minutes: number | null;
  make: string | null;
  model: string | null;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  is_video: boolean;
  held: boolean;
}

export interface FailedBody {
  message: string;
  failure_dir?: string;
  files?: string[];
}

export interface Health {
  ok: boolean;
  version: string;
}

export type HeartbeatBody = Record<string, never>;

export interface ImportQueued {
  export_id: number;
  status: string;
  stage: string | null;
}

export interface ImportReply {
  run: ImportView;
}

export interface ImportView {
  id: number;
  export_id: number;
  account_id: number | null;
  export_kind: string | null;
  export_created_at: number | null;
  part_count: number | null;
  parts: ExportPartView[];
  started: number;
  finished: number | null;
  stage: string | null;
  status: string;
  added: number;
  skipped_t1: number;
  skipped_t2: number;
  skipped_t3: number;
  review: number;
  missing_gps: number;
  failed: number;
  unmatched_by_person: Record<string, number>;
  error: string | null;
}

export interface Imports {
  imports: ImportView[];
}

export interface LibrarySide {
  name: string | null;
  path: string | null;
  bytes: number | null;
  captured_utc: number | null;
  captured_local: string | null;
  offset_minutes: number | null;
  make: string | null;
  model: string | null;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  is_video: boolean;
  item_id: number;
  person_id: number;
  source: string;
}

export interface Me {
  sub: string;
  email: string;
  name: string;
  groups: string[];
}

export interface MediaList {
  media: MediaView[];
  total: number;
  page: number;
  page_size: number;
}

export interface MediaReply {
  media: MediaView;
}

export interface MediaView {
  name: string | null;
  path: string | null;
  bytes: number | null;
  captured_utc: number | null;
  captured_local: string | null;
  offset_minutes: number | null;
  make: string | null;
  model: string | null;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  is_video: boolean;
  item_id: number;
  person_id: number;
  source: string;
  id: number;
  export_id: number | null;
  lat: number | null;
  lon: number | null;
}

export interface Ok {
  ok: boolean;
}

export interface People {
  people: PersonView[];
}

export interface PersonChoice {
  id: number;
  name: string;
}

export interface PersonCreateBody {
  name: string;
  slug?: string;
  email?: string | null;
  immich_user_id?: string | null;
  immich_library_id?: string | null;
  immich_api_key?: string | null;
}

export interface PersonPatchBody {
  name?: string;
  email?: string | null;
  immich_user_id?: string | null;
  immich_library_id?: string | null;
  immich_api_key?: string | null;
}

export interface PersonReply {
  person: PersonView;
}

export interface PersonStatus {
  id: number;
  slug: string;
  name: string;
  indexed: number;
  with_gps: number;
  unknown_time: number;
  accounts: AccountState[];
  pixels: PixelState[];
  devices: DeviceState[];
  last_import: number | null;
  unmatched: number | null;
  alerts: AlertView[];
}

export interface PersonView {
  id: number;
  name: string;
  slug: string;
  email: string | null;
  immich_user_id: string | null;
  immich_library_id: string | null;
  has_immich_key: boolean;
  created: number;
}

export interface PixelCreateBody {
  name: string;
  google_account_id: number;
  syncthing_device_id?: string | null;
  people_ids?: number[];
}

export interface PixelPatchBody {
  name?: string;
  google_account_id?: number;
  syncthing_device_id?: string | null;
  people_ids?: number[];
}

export interface PixelReply {
  pixel: PixelView;
}

export interface PixelState {
  id: number;
  name: string;
  google_account_id: number;
}

export interface PixelView {
  id: number;
  name: string;
  syncthing_device_id: string | null;
  google_account_id: number;
  people_ids: number[];
  last_sync: number | null;
  created: number;
}

export interface Pixels {
  pixels: PixelView[];
}

export interface ProgressBody {
  message: string;
}

export interface ReassignBody {
  person_id: number;
}

export interface ResolveBody {
  action: "keep_both" | "skip";
}

export interface ReviewDetail {
  review: ReviewSummary;
  library: LibrarySide | null;
  export: ExportSide;
  phash_distance: number | null;
  served_people: PersonChoice[];
  default_person_id: number | null;
}

export interface ReviewReply {
  review: ReviewSummary;
}

export interface ReviewSummary {
  id: number;
  status: string;
  decision_id: number;
  export_id: number;
  account_id: number;
  tier: number;
  reason: string;
  decided_at: number;
  reversed: boolean;
  resolved_at: number | null;
  undo_until: number | null;
  name: string | null;
  library_name: string | null;
  person_id: number | null;
  kept_item_id: number | null;
  kept_person_id: number | null;
}

export interface Reviews {
  reviews: ReviewSummary[];
  pending: number;
}

export interface Status {
  worker_last_seen: number | null;
  people: PersonStatus[];
  alerts: AlertView[];
}

export interface WaitingBody {
  reason: string;
}

export interface WorkerJob {
  id: number;
  kind: string;
  account: string;
  payload: Record<string, unknown>;
}

export interface WorkerReply {
  job: WorkerJob | null;
}

export interface Endpoints {
  "GET /healthz": { body: never; reply: Health };
  "GET /api/me": { body: never; reply: Me };
  "GET /api/status": { body: never; reply: Status };
  "GET /api/events": { body: never; reply: Events };
  "GET /api/people": { body: never; reply: People };
  "POST /api/people": { body: PersonCreateBody; reply: PersonReply };
  "PATCH /api/people/{id}": { body: PersonPatchBody; reply: PersonReply };
  "DELETE /api/people/{id}": { body: never; reply: Ok };
  "GET /api/people/{id}/devices": { body: never; reply: Devices };
  "DELETE /api/devices/{id}": { body: never; reply: DeviceReply };
  "GET /api/accounts": { body: never; reply: Accounts };
  "POST /api/accounts": { body: AccountCreateBody; reply: AccountReply };
  "PATCH /api/accounts/{id}": { body: AccountPatchBody; reply: AccountReply };
  "DELETE /api/accounts/{id}": { body: never; reply: Ok };
  "GET /api/pixels": { body: never; reply: Pixels };
  "POST /api/pixels": { body: PixelCreateBody; reply: PixelReply };
  "PATCH /api/pixels/{id}": { body: PixelPatchBody; reply: PixelReply };
  "DELETE /api/pixels/{id}": { body: never; reply: Ok };
  "GET /api/browser/jobs": { body: never; reply: BrowserJobs };
  "POST /api/browser/jobs": { body: EnqueueBody; reply: BrowserJobReply };
  "POST /api/exports/{id}/import": { body: never; reply: ImportQueued };
  "GET /api/imports": { body: never; reply: Imports };
  "GET /api/imports/{id}": { body: never; reply: ImportReply };
  "GET /api/review": { body: never; reply: Reviews };
  "GET /api/review/{id}": { body: never; reply: ReviewDetail };
  "POST /api/review/{id}/resolve": { body: ResolveBody; reply: ReviewReply };
  "POST /api/review/{id}/undo": { body: never; reply: ReviewReply };
  "GET /api/decisions": { body: never; reply: Decisions };
  "POST /api/decisions/{id}/reverse": { body: never; reply: DecisionReply };
  "GET /api/media": { body: never; reply: MediaList };
  "POST /api/media/{id}/reassign": { body: ReassignBody; reply: MediaReply };
  "POST /internal/browser/jobs/claim": { body: ClaimBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/heartbeat": { body: HeartbeatBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/progress": { body: ProgressBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/waiting": { body: WaitingBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/done": { body: DoneBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/failed": { body: FailedBody; reply: WorkerReply };
}
