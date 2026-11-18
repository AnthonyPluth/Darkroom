import { ApiError } from "./api";
import { apiCall } from "./contract";

export const POLL_MS = 5000;

export const NOT_A_DEVICE_ID = "not a Syncthing device ID";

export const PAIRING_FAILED = "Couldn't load Pixel pairing";

export type OwnDevice = { kind: "loading" } | { kind: "ready"; id: string } | { kind: "not_running" };

export function idLines(id: string): string[] {
  const groups = id.split("-");
  return groups.length > 4 ? [groups.slice(0, 4).join("-"), groups.slice(4).join("-")] : [id];
}

export function sentence(message: string): string {
  return /[.!?]$/.test(message) ? message : `${message}.`;
}

export async function readPairing(): Promise<void> {
  try {
    await apiCall<"GET /api/pairing/device">("/api/pairing/device", { failed: PAIRING_FAILED });
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 503)) throw err;
  }
}

export async function ownDevice(): Promise<OwnDevice> {
  try {
    const found = await apiCall<"GET /api/pairing/device">("/api/pairing/device");
    return { kind: "ready", id: found.device_id };
  } catch {
    return { kind: "not_running" };
  }
}
