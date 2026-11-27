import type { HoldResolveBody, HoldResolution, HoldView } from "./api-types";
import { onDayAt } from "./format";
import { wallClock } from "./review";

export type HoldAction = HoldResolveBody["action"];

export const holdLabels: Record<HoldView["hold"], string> = {
  unknown_offset: "Missing UTC offset",
  no_instant: "Missing capture time",
  unwritable: "Description can’t be sent",
  not_enriched: "Missing metadata in Immich",
  not_settled: "Metadata still differs",
  unusable: "Copy unavailable in Immich",
};

export const actionLabels: Record<HoldAction, string> = {
  offset: "Supply the offset",
  capture_time: "Supply the capture time",
  drop_description: "Send without the description",
  accept: "Accept as it is in Immich",
  record_offset: "Record an offset in Darkroom",
  recheck: "Check again",
  skip: "Skip",
};

export const formLabels = {
  offset: "Save offset",
  capture_time: "Save capture time",
  record_offset: "Save offset",
};

export type FormAction = keyof typeof formLabels;

export function isHoldAction(action: string): action is HoldAction {
  return Object.hasOwn(actionLabels, action);
}

export function isFormAction(action: HoldAction): action is FormAction {
  return Object.hasOwn(formLabels, action);
}

export function holdSource(row: HoldView): string {
  const exportName = row.export_id === null ? "Takeout export" : `Takeout export #${row.export_id}`;
  switch (row.source) {
    case "import": return exportName;
    case "kept": return row.export_id === null ? "Kept from Review" : `Kept from Review · ${exportName}`;
    case "backfill": return row.export_id === null ? "Backfill" : `Backfill · ${exportName}`;
  }
}

type Fact = [string, string];
const UNAVAILABLE = "Not available";
const fields: Record<string, string> = { capture_time: "Capture time", offset: "Offset", place: "Place", description: "Description" };
const custody: Record<string, string> = {
  trashed: "In Immich’s trash",
  offline: "Offline in Immich",
  external: "In an external library",
  other_owner: "Belongs to another person",
  other_bytes: "The original bytes differ",
  not_visible: "Darkroom could not read the copy",
  unavailable: "Darkroom could not verify the copy",
};

function utcTime(seconds: number | null | undefined): string {
  if (seconds == null) return UNAVAILABLE;
  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.getTime())) return UNAVAILABLE;
  return `${wallClock(date.toISOString())} UTC`;
}

function capture(value: string | null | undefined): string {
  if (value === undefined) return UNAVAILABLE;
  if (value === null) return "Missing";
  return utcTime(Date.parse(value) / 1000);
}

function offset(value: number | null | undefined): string {
  if (value === undefined) return UNAVAILABLE;
  if (value === null) return "Missing";
  const abs = Math.abs(value);
  const hours = String(Math.floor(abs / 60)).padStart(2, "0");
  const minutes = String(abs % 60).padStart(2, "0");
  return `UTC${value < 0 ? "−" : "+"}${hours}:${minutes}`;
}

export function holdFacts(row: HoldView): Fact[] {
  const facts = row.facts ?? {};
  if (row.hold === "unknown_offset") {
    const clock = wallClock(facts.wall_clock ?? null);
    return [
      [clock === null && facts.file_utc != null ? "Photo time" : "Photo clock", clock ?? utcTime(facts.file_utc)],
      ["Google capture time", utcTime(facts.google_utc)],
      ["Place known", facts.place_known === undefined ? UNAVAILABLE : facts.place_known ? "Yes" : "No"],
    ];
  }
  if (row.hold === "unwritable") {
    const length = facts.description_length;
    return [["Description length", length == null ? UNAVAILABLE : `${length} ${length === 1 ? "character" : "characters"}`]];
  }
  if (row.hold === "unusable") {
    return [["Last checked", facts.kind && Object.hasOwn(custody, facts.kind) ? custody[facts.kind] : UNAVAILABLE]];
  }
  if (row.hold === "not_enriched" || row.hold === "not_settled") {
    const differs = (facts.differs ?? []).filter((field) => Object.hasOwn(fields, field));
    const labels = differs.map((field, index) => index === 0 ? fields[field] : fields[field].toLowerCase());
    const result: Fact[] = [["Fields that differ", labels.join(", ") || UNAVAILABLE]];
    if (differs.includes("capture_time")) {
      result.push(["Immich capture time", capture(facts.immich_taken)], ["Intended capture time", capture(facts.intended_taken)]);
    }
    if (differs.includes("offset")) {
      result.push(["Immich offset", offset(facts.immich_offset)], ["Intended offset", offset(facts.intended_offset)]);
    }
    if (row.hold === "not_settled") {
      result.push(["File verified", row.bytes_verified_at === null ? UNAVAILABLE : onDayAt(row.bytes_verified_at)]);
    }
    return result;
  }
  return [];
}

export function resolutionText(resolution: HoldResolution): string | null {
  if (resolution.result === "still_differs") return "Checked again. Immich still shows different metadata.";
  if (resolution.result === "still_unusable") return "Checked again. Immich still can’t provide a usable copy.";
  if (resolution.state !== "recorded" && resolution.state !== "applied") return null;
  switch (resolution.action) {
    case "skip": return "Skipped. Darkroom is keeping the copy.";
    case "accept":
    case "record_offset":
    case "recheck": return "Saved. Darkroom will check Immich on the next import tick.";
    default: return "Saved; it goes to Immich on the next import tick.";
  }
}

export function offsetMinutes(sign: string, hours: number | undefined, minutes: string): number | null {
  if ((sign !== "+" && sign !== "-") || hours === undefined || !Number.isInteger(hours) || hours < 0 ||
      !["00", "15", "30", "45"].includes(minutes)) return null;
  const value = (hours * 60 + Number(minutes)) * (sign === "-" ? -1 : 1);
  return value >= -720 && value <= 840 ? value : null;
}

export function tomorrowDate(): string {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  return `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, "0")}-${String(tomorrow.getDate()).padStart(2, "0")}`;
}
