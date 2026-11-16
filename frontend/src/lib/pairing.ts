import { ApiError } from "./api";
import { apiCall } from "./contract";

export const POLL_MS = 5000;

export type Pairing = { kind: "off" } | { kind: "on" };

export type OwnDevice = { kind: "loading" } | { kind: "ready"; id: string } | { kind: "not_running" };

export function idLines(id: string): string[] {
  const groups = id.split("-");
  return groups.length > 4 ? [groups.slice(0, 4).join("-"), groups.slice(4).join("-")] : [id];
}

export function sentence(message: string): string {
  return /[.!?]$/.test(message) ? message : `${message}.`;
}

export async function pairingMode(): Promise<Pairing> {
  try {
    await apiCall<"GET /api/pairing/device">("/api/pairing/device");
    return { kind: "on" };
  } catch (err) {
    return err instanceof ApiError && err.status === 503 ? { kind: "on" } : { kind: "off" };
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
