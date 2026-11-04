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

export interface DoneBody {
  result?: Record<string, unknown>;
}

export interface EnqueueBody {
  account_id: number;
  kind: string;
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

export interface PersonStatus {
  slug: string;
  name: string;
  indexed: number;
  unmatched: number | null;
}

export interface ProgressBody {
  message: string;
}

export interface Status {
  worker_last_seen: number | null;
  people: PersonStatus[];
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
  "GET /api/browser/jobs": { body: never; reply: BrowserJobs };
  "POST /api/browser/jobs": { body: EnqueueBody; reply: BrowserJobReply };
  "POST /api/exports/{id}/import": { body: never; reply: ImportQueued };
  "GET /api/imports": { body: never; reply: Imports };
  "GET /api/imports/{id}": { body: never; reply: ImportReply };
  "POST /internal/browser/jobs/claim": { body: ClaimBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/heartbeat": { body: HeartbeatBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/progress": { body: ProgressBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/waiting": { body: WaitingBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/done": { body: DoneBody; reply: WorkerReply };
  "POST /internal/browser/jobs/{id}/failed": { body: FailedBody; reply: WorkerReply };
}
