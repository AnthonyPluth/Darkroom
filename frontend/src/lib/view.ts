import type { AccountState, AccountView, BrowserJobView, ExportPartDetail, ExportView, ImportView, PersonStatus } from "./api-types";
import type { Tone } from "../components/Chip.svelte";
import { onDay } from "./format";

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
    case "schedule_renewal_failed": return { tone: "bad", label: "Not renewed" };
    case "schedule_renewal_unconfirmed": return { tone: "warn", label: "Renewal unconfirmed" };
    case "export_expiring": return { tone: "warn", label: "Export expiring" };
    default: return { tone: "warn", label: "Needs a look" };
  }
}

export function needsSentence(count: number): string {
  if (count === 0) return "Nothing needs you right now.";
  return count === 1 ? "One thing needs you." : `${count} things need you.`;
}

export const NOT_CHECKED = "Not checked yet";

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
  const latest = [latestJob(jobs, accountId, "connect"), latestJob(jobs, accountId, "schedule"), latestJob(jobs, accountId, "status"), latestJob(jobs, accountId, "download"), latestJob(jobs, accountId, "renew")];
  return latest.flatMap((job) => (job !== undefined && job.status === "failed" && job.capture_files.length > 0 ? [job] : []));
}

export function maybeCreated(jobs: readonly BrowserJobView[], accountId: number): BrowserJobView | undefined {
  const latest = latestJob(jobs, accountId, "schedule");
  return latest !== undefined && latest.needs_check ? latest : undefined;
}

export function maybeRenewed(jobs: readonly BrowserJobView[], accountId: number): BrowserJobView | undefined {
  const latest = latestJob(jobs, accountId, "renew");
  return latest !== undefined && latest.needs_check ? latest : undefined;
}

export function scheduleLive(account: AccountView, now: number): boolean {
  return account.schedule_created !== null && (account.schedule_end === null || account.schedule_end > now);
}

export type RenewNow = { enabled: boolean; sentence: string };

export function renewNow(account: AccountView, jobs: readonly BrowserJobView[], now: number): RenewNow | null {
  const end = account.schedule_end;
  const from = account.renew_from;
  if (account.schedule_created === null || end === null || from === null) return null;
  if (now < from) return { enabled: false, sentence: `Darkroom asks for the renewal itself from ${onDay(from)}, three weeks before the schedule ends. Renew now is available from then.` };
  if (account.session_state === "needs_sign_in") return { enabled: false, sentence: "This account needs to sign in to Google again before its schedule can be renewed. Press Connect first." };
  if (maybeCreated(jobs, account.id) !== undefined || maybeRenewed(jobs, account.id) !== undefined) return { enabled: false, sentence: "Answer the Takeout question above first." };
  if (openJob(jobs, account.id, "schedule") !== undefined || openJob(jobs, account.id, "renew") !== undefined) return { enabled: false, sentence: "A schedule or renew job is already open for this account." };
  const last = latestJob(jobs, account.id, "renew");
  if (last !== undefined && last.status === "failed" && last.created >= from) return { enabled: true, sentence: "The last renewal failed. Darkroom does not retry a renewal on its own: press Renew now to try again." };
  if (end <= now) return { enabled: true, sentence: `This schedule ended on ${onDay(end)}, so scheduled exports have stopped. Renew now or Create schedule sets up a new yearly schedule in Takeout; both run the same steps.` };
  return { enabled: true, sentence: "Renew now creates next year's schedule in Takeout without waiting for the daily check. Darkroom does not retry a renewal on its own." };
}

function waitingSentence(reason: string): string {
  switch (reason) {
    case "wrong_account": return "waiting for you to switch to the right Google account.";
    case "verify": return "waiting for you to confirm your identity in the remote browser.";
    default: return "waiting for you to sign in.";
  }
}

export function jobSentence(job: BrowserJobView | undefined, doing: string): string | null {
  if (job === undefined) return null;
  switch (job.status) {
    case "queued": return `${doing}: waiting for the browser worker to pick it up.`;
    case "running": return `${doing}: running${job.message ? `, ${job.message}` : ""}.`;
    case "waiting_for_user": return `${doing}: ${waitingSentence(job.message)}`;
    case "failed":
      if (job.needs_check) return `${doing}: not confirmed by Takeout; say below what you found.`;
      if (job.checked === "found") return `${doing}: not confirmed by Takeout, then you found it there.`;
      if (job.checked === "missing") return `${doing}: not confirmed by Takeout, and you found none there.`;
      return `${doing}: failed${failureReason(job)}.`;
    default: return `${doing}: done.`;
  }
}

export function workerChip(lastSeen: number | null, now: number): { tone: Tone; label: string } {
  if (lastSeen === null) return { tone: "neutral", label: "Worker not seen yet" };
  return now - lastSeen <= 120 ? { tone: "ok", label: "Connected" } : { tone: "bad", label: "Worker not responding" };
}

export const JOB_NAMES: Record<string, string> = { connect: "Connect", schedule: "Create schedule", status: "Check exports", download: "Download", renew: "Renew schedule" };

const ACTIVE = ["announced", "downloading", "downloaded", "unpacking", "importing"];

export function exportChip(item: ExportView): { tone: Tone; label: string } {
  switch (item.status) {
    case "announced": return { tone: "neutral", label: item.ready_at === null ? "Google is preparing it" : "Waiting to download" };
    case "downloading": return { tone: "neutral", label: "Downloading" };
    case "downloaded": return { tone: "neutral", label: "Downloaded" };
    case "unpacking":
    case "importing": return { tone: "neutral", label: "Importing" };
    case "imported": return { tone: "ok", label: "Imported" };
    case "failed": return { tone: "bad", label: "Failed" };
    case "expired": return { tone: "warn", label: "Expired" };
    default: return { tone: "warn", label: "Needs a look" };
  }
}

export function partChip(part: ExportPartDetail, exportStatus: string): { tone: Tone; label: string } {
  switch (part.status) {
    case "pending": return { tone: "neutral", label: "Waiting" };
    case "downloading": return { tone: "neutral", label: "Downloading" };
    case "done": return exportStatus === "imported" ? { tone: "ok", label: "Imported" } : { tone: "ok", label: "Downloaded" };
    default: return { tone: "bad", label: "Failed" };
  }
}

export function accountExports(exports: readonly ExportView[], accountId: number): ExportView[] {
  return exports.filter((item) => item.account_id === accountId);
}

export function currentExport(exports: readonly ExportView[]): ExportView | undefined {
  return exports.find((item) => ACTIVE.includes(item.status));
}

export function earlierExports(exports: readonly ExportView[], current: ExportView | undefined): ExportView[] {
  return exports.filter((item) => item !== current);
}

export function partsLabel(count: number | null): string {
  return count === null ? "Parts not listed yet" : count === 1 ? "1 part" : `${count} parts`;
}

export function lastExportSentence(item: ExportView | undefined): string {
  if (item === undefined) return "Nothing exported yet";
  if (item.status === "imported") return `${partsLabel(item.part_count)}, all imported`;
  if (item.status === "expired") return "Expired before it was downloaded";
  if (item.status === "failed") return `${partsLabel(item.part_count)}, failed`;
  if (item.status === "announced" && item.ready_at === null) return "Google is still preparing it";
  return `${partsLabel(item.part_count)}, ${exportChip(item).label.toLowerCase()}`;
}

export function reviewCount(runs: readonly ImportView[], exportId: number): number {
  return runs.find((run) => run.export_id === exportId)?.review ?? 0;
}

export function checkNowSentence(queued: number, skipped: number): string {
  if (queued > 0) return queued === 1 ? "Checking one Google account for new exports." : `Checking ${queued} Google accounts for new exports.`;
  return skipped > 0 ? "A check is already waiting or running." : "No Google account is connected, so there is nothing to check.";
}
