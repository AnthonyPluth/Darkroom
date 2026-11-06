import { vi } from "vitest";
import type { AccountView, BrowserJobView, DeviceView, EventView, ExportPartDetail, ExportView, Exports, Me, PersonView, PixelView, Status } from "$lib/api-types";

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

export const joAccount: AccountView = { id: 10, email: "jo.photos@example.com", default_person_id: 1, session_state: "ok", session_checked: 1000, schedule_created: null, schedule_end: null, served_people: [1], created: 1 };
export const familyAccount: AccountView = { id: 11, email: "family.photos@example.com", default_person_id: 2, session_state: "needs_sign_in", session_checked: 1000, schedule_created: 4_000_000, schedule_end: 5_000_000, served_people: [1, 2], created: 1 };

export const solo: PixelView = { id: 20, name: "Pixel 9a", syncthing_device_id: "AAA-111", google_account_id: 10, people_ids: [1], last_sync: 940, created: 1 };
export const shared: PixelView = { id: 21, name: "Pixel 8", syncthing_device_id: null, google_account_id: 11, people_ids: [1, 2], last_sync: null, created: 1 };

export const joPhone: DeviceView = { id: 30, person_id: 1, name: "Jo's iPhone", cleanup_enabled: false, last_checkin: 990, app_version: "0.1.0", pending_uploads: 0, created: 1, revoked_at: null };
export const samPhone: DeviceView = { id: 31, person_id: 2, name: "Sam's iPhone", cleanup_enabled: false, last_checkin: null, app_version: null, pending_uploads: 18, created: 1, revoked_at: null };
export const oldPhone: DeviceView = { id: 32, person_id: 2, name: "Old iPhone", cleanup_enabled: false, last_checkin: 100, app_version: "0.0.9", pending_uploads: 0, created: 1, revoked_at: 500 };

export const emptyStatus: Status = { worker_last_seen: null, people: [], alerts: [] };

export const signInAlert = { id: 1, kind: "sign_in_needed", subject: "family.photos@example.com", message: "family.photos@example.com needs someone to sign in to Google again.", link: null, opened: 900 };
export const unmatchedAlert = { id: 2, kind: "originals_unmatched", subject: "sam-example", message: "14 of Sam's originals have no match in Google yet.", link: null, opened: 800 };

export const seededStatus: Status = {
  worker_last_seen: 940,
  alerts: [signInAlert, unmatchedAlert],
  people: [
    {
      id: 1, slug: "jo-example", name: "Jo Example", indexed: 120, with_gps: 100, unknown_time: 3,
      accounts: [{ id: 10, email: "jo.photos@example.com", session_state: "ok", session_checked: 1000, schedule_end: null }],
      pixels: [{ id: 20, name: "Pixel 9a", google_account_id: 10 }],
      devices: [{ id: 30, name: "Jo's iPhone", last_checkin: 990, pending_uploads: 0, cleanup_enabled: false }],
      last_import: 700, unmatched: null, alerts: [],
    },
    {
      id: 2, slug: "sam-example", name: "Sam Example", indexed: 0, with_gps: 0, unknown_time: 0,
      accounts: [{ id: 11, email: "family.photos@example.com", session_state: "needs_sign_in", session_checked: 1000, schedule_end: 5_000_000 }],
      pixels: [],
      devices: [{ id: 31, name: "Sam's iPhone", last_checkin: null, pending_uploads: 18, cleanup_enabled: false }],
      last_import: null, unmatched: null, alerts: [signInAlert, unmatchedAlert],
    },
  ],
};

export const someEvents: EventView[] = [
  { id: 3, time: 995, type: "device_checkin", subject: "jo-example", detail: "Jo's iPhone" },
  { id: 2, time: 900, type: "mystery_event", subject: "sam-example", detail: null },
  { id: 1, time: 100, type: "person_created", subject: "jo-example", detail: null },
];

export const queuedConnect: BrowserJobView = { id: 5, account_id: 10, kind: "connect", status: "queued", created: 990, started: null, heartbeat: null, finished: null, message: "", needs_check: false, checked: null, has_capture: false, capture_files: [] };
export const waitingConnect: BrowserJobView = { ...queuedConnect, id: 6, account_id: 11, status: "waiting_for_user", started: 991, heartbeat: 995, message: "sign_in" };
export const failedSchedule: BrowserJobView = { ...queuedConnect, id: 4, account_id: 10, kind: "schedule", status: "failed", started: 900, finished: 910, message: "UnexpectedPage", has_capture: true, capture_files: ["screenshot.png", "page.html"] };

export const noExports: Exports = { exports: [], schedules: [] };

const part = (number: number, status: string, downloads: number): ExportPartDetail => ({ number, filename: status === "pending" ? null : `takeout-20261102-00${number}.zip`, bytes: 1000 * number, progress: status === "done" ? 100 : 0, downloads_used: downloads, attempts: downloads, status });

export const downloadingExport: ExportView = {
  id: 40, account_id: 10, kind: "initial", created_at: 900_000, ready_at: 905_000, expires_at: 1_500_000, part_count: 3, status: "downloading", stage: null,
  parts: [part(1, "done", 1), part(2, "downloading", 1), part(3, "pending", 0)],
};
export const preparingExport: ExportView = { id: 41, account_id: 10, kind: "incremental", created_at: 990_000, ready_at: null, expires_at: null, part_count: null, status: "announced", stage: null, parts: [] };
export const importedExport: ExportView = {
  id: 38, account_id: 10, kind: "incremental", created_at: 300_000, ready_at: 301_000, expires_at: 900_000, part_count: 2, status: "imported", stage: "done",
  parts: [part(1, "done", 1), part(2, "done", 1)],
};
export const failedExport: ExportView = { ...importedExport, id: 37, created_at: 200_000, status: "failed", parts: [part(1, "done", 1), part(2, "failed", 3)] };
export const expiredExport: ExportView = { id: 36, account_id: 10, kind: "incremental", created_at: 100_000, ready_at: null, expires_at: 150_000, part_count: 4, status: "expired", stage: null, parts: [] };
