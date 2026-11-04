import type { AccountState, BrowserJobView, DeviceState, PersonStatus } from "./api-types";
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
    case "wrong_account": return { tone: "bad", label: "Wrong account" };
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

const OPEN_JOB = ["queued", "running", "waiting_for_user"];

export function openJob(jobs: readonly BrowserJobView[], accountId: number, kind: string): BrowserJobView | undefined {
  return jobs.find((job) => job.account_id === accountId && job.kind === kind && OPEN_JOB.includes(job.status));
}

export function latestJob(jobs: readonly BrowserJobView[], accountId: number, kind: string): BrowserJobView | undefined {
  return jobs.find((job) => job.account_id === accountId && job.kind === kind);
}

export function failureReason(job: BrowserJobView): string {
  const message = job.message;
  const at = message.indexOf(": ");
  return message === "" ? "" : at === -1 ? ` (${message})` : `, ${message.slice(at + 2).replace(/[.]$/, "")}`;
}

export function failedCaptures(jobs: readonly BrowserJobView[], accountId: number): BrowserJobView[] {
  const latest = [latestJob(jobs, accountId, "connect"), latestJob(jobs, accountId, "schedule")];
  return latest.flatMap((job) => (job !== undefined && job.status === "failed" && job.capture_files.length > 0 ? [job] : []));
}

export function maybeCreated(jobs: readonly BrowserJobView[], accountId: number): BrowserJobView | undefined {
  const latest = latestJob(jobs, accountId, "schedule");
  return latest !== undefined && latest.needs_check ? latest : undefined;
}

export function jobSentence(job: BrowserJobView | undefined, doing: string): string | null {
  if (job === undefined) return null;
  switch (job.status) {
    case "queued": return `${doing}: waiting for the browser worker to pick it up.`;
    case "running": return `${doing}: running${job.message ? `, ${job.message}` : ""}.`;
    case "waiting_for_user": return job.message === "wrong_account" ? `${doing}: waiting for you to switch to the right Google account.` : `${doing}: waiting for you to sign in.`;
    case "failed": return `${doing}: failed${failureReason(job)}.`;
    default: return `${doing}: done.`;
  }
}

export function workerChip(lastSeen: number | null, now: number): { tone: Tone; label: string } {
  if (lastSeen === null) return { tone: "neutral", label: "Worker not seen yet" };
  return now - lastSeen <= 120 ? { tone: "ok", label: "Connected" } : { tone: "bad", label: "Worker not responding" };
}
