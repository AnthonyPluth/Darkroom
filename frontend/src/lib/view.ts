import type { AccountState, DeviceState, PersonStatus } from "./api-types";
import type { Tone } from "../components/Chip.svelte";

export function sessionChip(state: string): { tone: Tone; label: string } {
  if (state === "ok") return { tone: "ok", label: "Session ok" };
  if (state === "needs_sign_in") return { tone: "bad", label: "Sign-in needed" };
  return { tone: "neutral", label: "Session not checked yet" };
}

export function needsAttention(person: PersonStatus): boolean {
  return person.alerts.length > 0 || person.accounts.some((a: AccountState) => a.session_state === "needs_sign_in");
}

export function alertChip(kind: string): { tone: Tone; label: string } {
  switch (kind) {
    case "sign_in_needed": return { tone: "bad", label: "Sign-in needed" };
    case "failure": return { tone: "bad", label: "Failed" };
    case "originals_unmatched": return { tone: "warn", label: "Check backup" };
    case "schedule_ending": return { tone: "warn", label: "Schedule ending" };
    case "export_expiring": return { tone: "warn", label: "Export expiring" };
    default: return { tone: "warn", label: "Needs a look" };
  }
}

export function needsSentence(count: number): string {
  if (count === 0) return "Nothing needs you right now.";
  return count === 1 ? "One thing needs you." : `${count} things need you.`;
}

export const NOT_CHECKED = "Not checked yet";

export function safeToDelete(devices: readonly DeviceState[]): number | null {
  if (devices.length === 0) return null;
  let total = 0;
  for (const device of devices) {
    if (device.eligible === null) return null;
    total += device.eligible;
  }
  return total;
}
