export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly data: Record<string, unknown> = {}) { super(message); }
}

export type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
export type Options = { method?: Method; body?: unknown; failed?: string };

export const SIGNED_OUT = "darkroom:signed-out";

declare global {
  interface WindowEventMap { "darkroom:signed-out": Event }
}

export const OFFLINE = "Can't reach Darkroom. Check your connection and try again.";
export const UNREACHABLE = "Darkroom is restarting or unreachable. Try again in a moment.";
export const SESSION_ENDED = "You're signed out. Sign in again to keep going.";

export function signInUrl(): string {
  return "/auth/login?next=" + encodeURIComponent(location.pathname + location.search);
}

function failure(status: number, data: Record<string, unknown>, failed?: string): string {
  if (typeof data.error === "string" && data.error) return data.error;
  if (failed) return `${failed} (${status})`;
  return [502, 503, 504].includes(status) ? UNREACHABLE : `Request failed (${status})`;
}

export async function api<T = unknown>(path: string, opts: Options = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res: Response;
  try { res = await fetch(path, init); }
  catch (err) { throw err instanceof TypeError ? new ApiError(OFFLINE, 0) : err; }
  if (res.status === 401) {
    window.dispatchEvent(new Event(SIGNED_OUT));
    throw new ApiError(SESSION_ENDED, 401);
  }
  const data: unknown = res.status === 204 ? {} : await res.json().catch(() => ({}));
  const fields = data !== null && typeof data === "object" ? data as Record<string, unknown> : {};
  if (!res.ok) throw new ApiError(failure(res.status, fields, opts.failed), res.status, fields);
  return data as T;
}
