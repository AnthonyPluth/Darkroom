import type { AccountState, AccountView, BrowserJobView, ExportPartDetail, ExportView, ImportView, PersonStatus, PixelSyncthing, SyncthingCard } from "./api-types";
import type { Tone } from "../components/Chip.svelte";
import { ago, count, joined, lasted, onDay } from "./format";

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
    case "syncthing_down": return { tone: "bad", label: "Syncthing down" };
    case "syncthing_settings_failed": return { tone: "bad", label: "Settings not applied" };
    case "syncthing_version": return { tone: "bad", label: "Untested Syncthing" };
    case "share_check_failed": return { tone: "bad", label: "Share check failed" };
    case "pixel_held": return { tone: "warn", label: "Folder held" };
    default: return { tone: "warn", label: "Needs a look" };
  }
}

export type Chipped = { tone: Tone; label: string };
export type Described = Chipped & { line: string };

export function syncthingCard(card: SyncthingCard, now: number): Described {
  const reason = card.reason ?? "";
  switch (card.state) {
    case "starting": return { tone: "neutral", label: "Starting", line: "Darkroom is checking Syncthing's settings before anything syncs." };
    case "running": return { tone: "ok", label: "Running", line: "Syncthing is running with the settings Darkroom set." };
    case "down": return { tone: "bad", label: "Down", line: `Syncthing has not answered for ${lasted(now - (card.since ?? now))}. Darkroom keeps restarting it. Placing and pruning carry on, and the Pixels catch up once it is back.` };
    case "settings_failed": return { tone: "bad", label: "Paused", line: `Darkroom could not apply its settings to Syncthing (${reason}), so nothing is syncing. It tries again before the next hourly run.` };
    case "version": return { tone: "bad", label: "Not started", line: `${reason}, so Darkroom has not started Syncthing.` };
    case "key": return { tone: "bad", label: "Not started", line: `${reason}, so Darkroom has not started Syncthing. Restart the container.` };
    case "share_check_failed": return { tone: "bad", label: "Paused", line: `The photo share failed Darkroom's check (${reason}), so placing, pruning and syncing are paused, and nothing on the share is changed.` };
  }
}

function heldLines(pixel: PixelSyncthing): string[] {
  const lines: string[] = [];
  if (pixel.copies > 0) lines.push(`${count(pixel.copies, "photo")} could not be written again, so its folder is not shared.`);
  if (pixel.files > 0) lines.push(`Its folder has ${count(pixel.files, "file")} Darkroom did not put there, so it is not shared.`);
  return lines;
}

export function pixelSyncthing(pixel: PixelSyncthing, now: number): Chipped & { lines: string[] } {
  switch (pixel.state) {
    case "not_syncing": return { tone: "neutral", label: "Not syncing", lines: ["Nothing syncs until Syncthing is running again."] };
    case "not_paired": return { tone: "neutral", label: "Not paired", lines: ["Pair it to start sending photos."] };
    case "device_id_invalid": return { tone: "warn", label: "Device ID not valid", lines: ["Its saved device ID is not a Syncthing device ID, so nothing is shared with it. Pair it again."] };
    case "device_id_shared": return { tone: "warn", label: "Same device ID", lines: [`${joined(pixel.others)} ${pixel.others.length === 1 ? "has" : "have"} the same device ID, so Darkroom shares neither folder. Pair again whichever one is wrong.`] };
    case "folder_removed": return { tone: "warn", label: "Folder removed", lines: ["Darkroom removed its folder because it was changed outside Darkroom. It stays unshared until you share it again."] };
    case "folder_path_refused": return { tone: "warn", label: "Folder refused", lines: ["Its folder leads outside the photo share, so Darkroom does not share it."] };
    case "checking": return { tone: "neutral", label: "Checking", lines: ["Darkroom is checking its folder before sharing it."] };
    case "held": return { tone: "warn", label: "Folder held", lines: heldLines(pixel) };
    case "waiting": return { tone: "neutral", label: "Waiting for the Pixel", lines: ["Paired; the Pixel has not connected yet."] };
    case "not_seen": return { tone: "warn", label: `Not seen for ${pixel.days} days`, lines: ["Darkroom reaches the Pixel only on the home network. Check it is on, on the Wi-Fi, with Syncthing running."] };
    case "sending": return { tone: "ok", label: "Sending", lines: [`${Math.floor(pixel.completion ?? 0)}% sent`] };
    case "connected": return { tone: "ok", label: "Connected", lines: ["Up to date"] };
    case "not_connected": return { tone: "neutral", label: "Not connected", lines: [`Last seen ${ago(pixel.last_seen, now)}.`] };
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

const NOT_QUEUED = "not_queued";
const whole = new Intl.NumberFormat("en-GB");
const items = (n: number) => `${whole.format(n)} ${n === 1 ? "item" : "items"}`;
const wait = (n: number) => (n === 1 ? "waits" : "wait");

export function importedPerPart(item: ExportView): boolean {
  return item.parts.some((part) => part.ingest_state !== NOT_QUEUED);
}

export type PartState = { tone: Tone; label: string; detail: string | null };

function heldDetail(part: ExportPartDetail): string {
  const halves = [];
  if (part.held_waiting_sidecar > 0) halves.push(`${whole.format(part.held_waiting_sidecar)} ${wait(part.held_waiting_sidecar)} for a sidecar`);
  if (part.held_waiting_export > 0) halves.push(`${whole.format(part.held_waiting_export)}${halves.length === 0 ? ` ${wait(part.held_waiting_export)}` : ""} for the whole export`);
  return halves.length === 0 ? items(part.entries) : `${items(part.entries)}. ${halves.join(", ")}.`;
}

export function partImportState(part: ExportPartDetail, exportStatus: string): PartState {
  if (part.status !== "done" || part.ingest_state === NOT_QUEUED) return { ...partChip(part, exportStatus), detail: null };
  switch (part.ingest_state) {
    case "queued": return { tone: "neutral", label: "Downloaded", detail: "Waiting to import" };
    case "ingesting": return { tone: "neutral", label: "Importing", detail: part.entries === 0 ? "Reading the file" : `${whole.format(part.entries_done)} of ${items(part.entries)}` };
    case "ingested":
      if (part.kept_reason === "failed_item") return { tone: "warn", label: "Kept", detail: `${items(part.entries)}, ${whole.format(part.failed_items)} failed` };
      if (part.held > 0) return { tone: "neutral", label: `Holding ${items(part.held)}`, detail: heldDetail(part) };
      return { tone: "ok", label: "Imported", detail: items(part.entries) };
    case "refused": return { tone: "bad", label: "Couldn't read", detail: null };
    case "failed": return { tone: "bad", label: "Import failed", detail: null };
    default: return { ...partChip(part, exportStatus), detail: null };
  }
}

export function keptSentence(part: ExportPartDetail): string | null {
  if (part.status !== "done") return null;
  if (part.ingest_state === "refused") return `Part ${part.number}: Darkroom couldn't safely read this part, so it imported nothing from it and keeps the file. It doesn't try again on its own.`;
  if (part.ingest_state === "failed") return `Part ${part.number}: importing this part stopped with an error, so Darkroom keeps the file. It doesn't try again on its own.`;
  if (part.ingest_state === "ingested" && part.kept_reason === "failed_item") return `Part ${part.number}: ${items(part.failed_items)} couldn't be imported, so Darkroom keeps the part's file.`;
  return null;
}

export function importedTotal(item: ExportView): string {
  const total = item.part_count ?? item.parts.length;
  return `${item.parts_ingested} of ${total === 1 ? "1 part" : `${total} parts`}`;
}

export function heldTotal(item: ExportView): { value: string; note: string | null } {
  const held = item.held_waiting_sidecar + item.held_waiting_export;
  if (held === 0) return { value: "None", note: null };
  const sidecar = item.held_waiting_sidecar;
  const waiting = sidecar === 0 ? "" : ` ${whole.format(sidecar)} of them ${sidecar === 1 ? "is" : "are"} waiting for a sidecar.`;
  return { value: items(held), note: `Until the whole export is in.${waiting}` };
}

export function accountExports(exports: readonly ExportView[], accountId: number): ExportView[] {
  return exports.filter((item) => item.account_id === accountId);
}

export function currentExport(exports: readonly ExportView[]): ExportView | undefined {
  return exports.find((item) => ACTIVE.includes(item.status) || (item.status === "failed" && item.retry_allowed));
}

const DOWNLOAD_JOBS = 3;
const RETRY_DOES = "Retry download tries the parts that are not downloaded yet again and keeps the ones that are. Google's downloads already used still count.";

export function failedDownloadSentence(item: ExportView): string | null {
  if (item.status !== "failed" || !item.retry_allowed) return null;
  const stopped = item.parts.find((part) => part.status === "failed");
  const why = stopped === undefined ? `the download failed ${DOWNLOAD_JOBS} times` : `part ${stopped.number} failed after ${stopped.attempts === 1 ? "1 attempt" : `${stopped.attempts} attempts`}`;
  return `Download stopped: ${why}. ${RETRY_DOES}`;
}

export function retryDownloadSentence(retried: boolean, email: string): string {
  return retried ? `Download queued again for ${email}.` : "This export is already downloading.";
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
