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
  schedule_created: number | null;
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
  needs_check: boolean;
  has_capture: boolean;
  capture_files: string[];
}

export interface BrowserJobs {
  jobs: BrowserJobView[];
}

export interface BrowserView {
  vnc_url: string | null;
  worker_last_seen: number | null;
}

export interface ClaimBody {
  worker?: string;
}

export interface DeviceAssetDecl {
  id: string;
  resources: DeviceResourceDecl[];
}

export interface DeviceCheckBody {
  assets: DeviceAssetDecl[];
}

export interface DeviceCheckReply {
  needed: NeededResource[];
}

export interface DeviceCheckinBody {
  pending_uploads: number;
  app_version?: string | null;
}

export interface DeviceReply {
  device: DeviceView;
}

export interface DeviceResourceDecl {
  kind: string;
  sha1: string;
  bytes: number;
  filename: string;
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
  account_id: number;
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

export interface Me {
  sub: string;
  email: string;
  name: string;
  groups: string[];
}

export interface NeededResource {
  id: string;
  kind: string;
}

export interface Ok {
  ok: boolean;
}

export interface People {
  people: PersonView[];
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

export interface Status {
  worker_last_seen: number | null;
  people: PersonStatus[];
  alerts: AlertView[];
}

export interface UploadReply {
  id: string;
  kind: string;
  sha1: string;
  already: boolean;
  indexed: boolean;
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
  "POST /api/accounts/{id}/connect": { body: never; reply: BrowserJobReply };
  "POST /api/accounts/{id}/schedule": { body: never; reply: BrowserJobReply };
  "POST /api/accounts/{id}/schedule/checked": { body: never; reply: BrowserJobReply };
  "GET /api/pixels": { body: never; reply: Pixels };
  "POST /api/pixels": { body: PixelCreateBody; reply: PixelReply };
  "PATCH /api/pixels/{id}": { body: PixelPatchBody; reply: PixelReply };
  "DELETE /api/pixels/{id}": { body: never; reply: Ok };
  "GET /api/browser/jobs": { body: never; reply: BrowserJobs };
  "POST /api/browser/jobs": { body: EnqueueBody; reply: BrowserJobReply };
  "GET /api/browser/view": { body: never; reply: BrowserView };
  "POST /api/exports/{id}/import": { body: never; reply: ImportQueued };
  "GET /api/imports": { body: never; reply: Imports };
  "GET /api/imports/{id}": { body: never; reply: ImportReply };
  "POST /api/device/assets/check": { body: DeviceCheckBody; reply: DeviceCheckReply };
  "PUT /api/device/assets/{id}/resources/{kind}": { body: never; reply: UploadReply };
  "POST /api/device/checkin": { body: DeviceCheckinBody; reply: Ok };
  "POST /internal/browser/jobs/claim": { body: ClaimBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/heartbeat": { body: HeartbeatBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/progress": { body: ProgressBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/waiting": { body: WaitingBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/done": { body: DoneBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/failed": { body: FailedBody; reply: WorkerReply };
}
