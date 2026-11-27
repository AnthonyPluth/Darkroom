import { vi } from "vitest";
import type { AccountView, BrowserJobView, EventView, ExportPartDetail, ExportView, Exports, Me, PersonView, PixelSyncthing, PixelView, Status, SyncthingCard, Version } from "$lib/api-types";

export type Answer = { status: number; body: unknown };
export type Handler = unknown | ((body: unknown, url: string) => unknown);
export type Call = { key: string; body: unknown };

export const reply = (status: number, body: unknown): Answer => ({ status, body });
const isAnswer = (value: unknown): value is Answer => typeof value === "object" && value !== null && "status" in value && "body" in value;

export function serve(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const path = url.split("?")[0];
    const key = `${method} ${url}` in routes ? `${method} ${url}` : `${method} ${path}`;
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ key: `${method} ${url}`, body });
    const handler = routes[key];
    if (handler === undefined) return Promise.resolve(new Response(JSON.stringify({ error: "no such route in the test" }), { status: 404 }));
    const produced = typeof handler === "function" ? (handler as (b: unknown, u: string) => unknown)(body, url) : handler;
    const answer = isAnswer(produced) ? produced : reply(200, produced);
    return Promise.resolve(new Response(JSON.stringify(answer.body), { status: answer.status }));
  });
  return { mock, calls, sent: (key: string) => calls.filter((c) => c.key === key) };
}

export const me: Me = { sub: "u-1", email: "sam@example.com", name: "Sam Example", groups: ["parents"] };

export const jo: PersonView = { id: 1, name: "Jo Example", slug: "jo-example", email: "jo@example.com", immich_user_id: "immich-jo", immich_library_id: null, has_immich_key: true, created: 1 };
export const sam: PersonView = { id: 2, name: "Sam Example", slug: "sam-example", email: null, immich_user_id: null, immich_library_id: null, has_immich_key: false, created: 2 };

export const joAccount: AccountView = { id: 10, email: "jo.photos@example.com", default_person_id: 1, session_state: "ok", session_checked: 1000, schedule_created: null, schedule_end: null, renew_from: null, served_people: [1], created: 1 };
export const familyAccount: AccountView = { id: 11, email: "family.photos@example.com", default_person_id: 2, session_state: "needs_sign_in", session_checked: 1000, schedule_created: 4_000_000, schedule_end: 5_000_000, renew_from: 3_185_600, served_people: [1, 2], created: 1 };

export const solo: PixelView = { id: 20, name: "Pixel 9a", syncthing_device_id: "AAA-111", google_account_id: 10, people_ids: [1], last_sync: 940, created: 1 };
export const shared: PixelView = { id: 21, name: "Pixel 8", syncthing_device_id: null, google_account_id: 11, people_ids: [1, 2], last_sync: null, created: 1 };

export const devVersion: Version = { version: "dev", commit: null };

export const idleCard: SyncthingCard = { state: "running", reason: null, since: null, pixels: [] };

export const emptyStatus: Status = { worker_last_seen: null, people: [], alerts: [], syncthing: idleCard };

export const signInAlert = { id: 1, kind: "sign_in_needed", subject: "family.photos@example.com", message: "family.photos@example.com needs someone to sign in to Google again.", link: null, opened: 900 };
export const unmatchedAlert = { id: 2, kind: "originals_unmatched", subject: "sam-example", message: "14 of Sam's originals have no match in Google yet.", link: null, opened: 800 };

export const seededStatus: Status = {
  worker_last_seen: 940,
  syncthing: idleCard,
  alerts: [signInAlert, unmatchedAlert],
  people: [
    {
      id: 1, slug: "jo-example", name: "Jo Example", indexed: 120, with_gps: 100, unknown_time: 3, index_skipped: 0,
      accounts: [{ id: 10, email: "jo.photos@example.com", session_state: "ok", session_checked: 1000, schedule_end: null }],
      pixels: [{ id: 20, name: "Pixel 9a", google_account_id: 10, last_sync: 940, syncthing: null }],
      last_import: 700, unmatched: null, alerts: [],
    },
    {
      id: 2, slug: "sam-example", name: "Sam Example", indexed: 0, with_gps: 0, unknown_time: 0, index_skipped: 0,
      accounts: [{ id: 11, email: "family.photos@example.com", session_state: "needs_sign_in", session_checked: 1000, schedule_end: 5_000_000 }],
      pixels: [],
      last_import: null, unmatched: null, alerts: [signInAlert, unmatchedAlert],
    },
  ],
};

export const someEvents: EventView[] = [
  { id: 3, time: 995, type: "originals_indexed", subject: "jo-example", detail: "new=4 failed=0" },
  { id: 2, time: 900, type: "mystery_event", subject: "sam-example", detail: null },
  { id: 1, time: 100, type: "person_created", subject: "jo-example", detail: null },
];

export const queuedConnect: BrowserJobView = { id: 5, account_id: 10, kind: "connect", status: "queued", created: 990, started: null, heartbeat: null, finished: null, message: "", needs_check: false, checked: null, has_capture: false, capture_files: [] };
export const waitingConnect: BrowserJobView = { ...queuedConnect, id: 6, account_id: 11, status: "waiting_for_user", started: 991, heartbeat: 995, message: "sign_in" };
export const failedSchedule: BrowserJobView = { ...queuedConnect, id: 4, account_id: 10, kind: "schedule", status: "failed", started: 900, finished: 910, message: "UnexpectedPage", has_capture: true, capture_files: ["screenshot.png", "page.html"] };

export const noExports: Exports = { exports: [], schedules: [] };

const part = (number: number, status: string, downloads: number): ExportPartDetail => ({ number, filename: status === "pending" ? null : `takeout-20261102-00${number}.zip`, bytes: 1000 * number, progress: status === "done" ? 100 : 0, downloads_used: downloads, attempts: downloads, status, ingest_state: "not_queued", entries: 0, entries_done: 0, held: 0, held_waiting_sidecar: 0, held_waiting_export: 0, failed_items: 0, kept_reason: null, import_failure: null, freed: false, bytes_freed: 0 });

export const downloadingExport: ExportView = {
  id: 40, account_id: 10, kind: "initial", created_at: 900_000, ready_at: 905_000, expires_at: 1_500_000, part_count: 3, status: "downloading", stage: null, parts_ingested: 0, held_waiting_sidecar: 0, held_waiting_export: 0, bytes_freed: 0, retry_allowed: false, retry_refusal: null, import_retry_allowed: false, failed_items: 0, item_retry_allowed: false,
  parts: [part(1, "done", 1), part(2, "downloading", 1), part(3, "pending", 0)],
};
export const preparingExport: ExportView = { id: 41, account_id: 10, kind: "incremental", created_at: 990_000, ready_at: null, expires_at: null, part_count: null, status: "announced", stage: null, parts: [], parts_ingested: 0, held_waiting_sidecar: 0, held_waiting_export: 0, bytes_freed: 0, retry_allowed: false, retry_refusal: null, import_retry_allowed: false, failed_items: 0, item_retry_allowed: false };
export const importedExport: ExportView = {
  id: 38, account_id: 10, kind: "incremental", created_at: 300_000, ready_at: 301_000, expires_at: 900_000, part_count: 2, status: "imported", stage: "done", parts_ingested: 0, held_waiting_sidecar: 0, held_waiting_export: 0, bytes_freed: 0, retry_allowed: false, retry_refusal: null, import_retry_allowed: false, failed_items: 0, item_retry_allowed: false,
  parts: [part(1, "done", 1), part(2, "done", 1)],
};
export const failedExport: ExportView = { ...importedExport, id: 37, created_at: 200_000, status: "failed", import_retry_allowed: true, failed_items: 0, item_retry_allowed: false, parts: [part(1, "done", 1), part(2, "failed", 3)], retry_refusal: "This export was downloaded and its import failed; Retry import on this page tries the import again." };
export const failedDownload: ExportView = {
  ...downloadingExport, id: 42, status: "failed", retry_allowed: true,
  parts: [part(1, "done", 1), { ...part(2, "failed", 3), filename: null }, part(3, "pending", 0)],
};
export const olderFailedDownload: ExportView = { ...failedDownload, id: 44, created_at: 200_000 };
export const SPENT = "Part 2 has used all 5 of Google's downloads, so Takeout won't let Darkroom download it again. Ask Takeout for a new export of Google Photos instead.";
export const spentDownload: ExportView = { ...failedDownload, id: 43, created_at: 250_000, retry_allowed: false, retry_refusal: SPENT };
export const expiredExport: ExportView = { id: 36, account_id: 10, kind: "incremental", created_at: 100_000, ready_at: null, expires_at: 150_000, part_count: 4, status: "expired", stage: null, parts: [], parts_ingested: 0, held_waiting_sidecar: 0, held_waiting_export: 0, bytes_freed: 0, retry_allowed: false, retry_refusal: null, import_retry_allowed: false, failed_items: 0, item_retry_allowed: false };

const ingested = (number: number, ingest: Partial<ExportPartDetail>): ExportPartDetail => ({ ...part(number, "done", 1), ...ingest });
export const failedItemsExport: ExportView = {
  ...importedExport, id: 47, created_at: 250_000, failed_items: 3, item_retry_allowed: true,
  parts: [ingested(1, { ingest_state: "ingested", entries: 900, entries_done: 900 }), ingested(2, { ingest_state: "ingested", entries: 900, entries_done: 900, failed_items: 3, kept_reason: "failed_item" })],
};
export const perPartExport: ExportView = {
  ...downloadingExport, id: 45, part_count: 6, parts_ingested: 2, held_waiting_sidecar: 21, held_waiting_export: 2,
  parts: [
    ingested(1, { ingest_state: "ingested", entries: 1517, entries_done: 1517 }),
    ingested(2, { ingest_state: "ingested", entries: 1402, entries_done: 1402, held: 23, held_waiting_sidecar: 21, held_waiting_export: 2 }),
    ingested(3, { ingest_state: "ingesting", entries: 1488, entries_done: 812 }),
    ingested(4, { ingest_state: "queued" }),
    { ...part(5, "downloading", 1), progress: 62 },
    part(6, "pending", 0),
  ],
};
export const keptPartsExport: ExportView = {
  ...downloadingExport, id: 46, part_count: 4, parts_ingested: 2, import_retry_allowed: true,
  parts: [
    ingested(1, { ingest_state: "ingested", entries: 1517, entries_done: 1517 }),
    ingested(2, { ingest_state: "ingested", entries: 1402, entries_done: 1402, failed_items: 2, kept_reason: "failed_item" }),
    ingested(3, { ingest_state: "refused" }),
    ingested(4, { ingest_state: "failed", entries: 900, entries_done: 300, import_failure: { code: "files", error: "OSError" } }),
  ],
};

export const transport = (pixel_id: number, name: string, state: PixelSyncthing["state"], extra: Partial<PixelSyncthing> = {}): PixelSyncthing => ({
  pixel_id, name, state, copies: 0, files: 0, days: 0, completion: null, last_seen: null, others: [], ...extra,
});

export const runningCard: SyncthingCard = {
  state: "running", reason: null, since: null,
  pixels: [
    transport(20, "Pixel 9a", "connected", { completion: 100, last_seen: 990 }),
    transport(22, "Pixel 7", "sending", { completion: 82.6, last_seen: 990 }),
    transport(23, "Pixel 6", "not_seen", { days: 4, last_seen: 1000 - 4 * 86_400 }),
    transport(24, "Pixel 5", "held", { copies: 1, files: 3 }),
    transport(25, "Pixel 4a", "not_connected", { last_seen: 1000 - 5 * 3600 }),
  ],
};

export const syncthingStatus: Status = {
  ...seededStatus,
  syncthing: runningCard,
  people: seededStatus.people.map((person) => person.id === 1
    ? { ...person, pixels: [
      { id: 20, name: "Pixel 9a", google_account_id: 10, last_sync: 1000 - 360, syncthing: transport(20, "Pixel 9a", "connected", { completion: 100 }) },
      { id: 24, name: "Pixel 5", google_account_id: 10, last_sync: 1000 - 2 * 86_400, syncthing: transport(24, "Pixel 5", "held", { files: 3 }) },
      { id: 26, name: "Pixel 4a", google_account_id: 10, last_sync: null, syncthing: transport(26, "Pixel 4a", "waiting") },
    ] }
    : person),
};
