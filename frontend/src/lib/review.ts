import type { ImportView, PersonView, ReviewSummary } from "./api-types";
import type { Tone } from "../components/Chip.svelte";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WALL_CLOCK = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;
const COUNTED = new Set(["counted", "rescanned", "reported", "done"]);
const DAY = 86_400;

export type Side = {
  name: string | null;
  bytes: number | null;
  captured_local: string | null;
  make: string | null;
  model: string | null;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  is_video: boolean;
};

export function size(bytes: number | null): string {
  if (bytes === null) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

export function compared(mine: number | null, theirs: number | null): string {
  const text = size(mine);
  if (mine === null || theirs === null || mine === theirs) return text;
  return `${text}, ${mine < theirs ? "smaller" : "larger"}`;
}

export function wallClock(local: string | null, seconds = true): string | null {
  const found = local === null ? null : WALL_CLOCK.exec(local);
  if (!found) return null;
  const [, year, month, day, hour, minute, second] = found;
  const time = seconds ? `${hour}:${minute}:${second ?? "00"}` : `${hour}:${minute}`;
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}, ${time}`;
}

export function camera(side: Pick<Side, "make" | "model">): string {
  return side.model?.trim() || side.make?.trim() || "Unknown";
}

export function frame(side: Side): string {
  if (side.is_video) {
    if (side.duration_ms === null) return "Unknown length";
    const total = Math.round(side.duration_ms / 1000);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }
  return side.width !== null && side.height !== null ? `${side.width} × ${side.height}` : "Unknown";
}

function extension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot) : name;
}

function stem(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

export function pairLabel(review: Pick<ReviewSummary, "name" | "library_name">): string {
  const exported = review.name ?? "Unknown file";
  if (review.library_name === null) return exported;
  if (stem(review.library_name) === stem(exported)) return `${review.library_name} / ${extension(exported)}`;
  return `${review.library_name} / ${exported}`;
}

export function personName(people: readonly Pick<PersonView, "id" | "name">[], id: number | null): string {
  return people.find((person) => person.id === id)?.name ?? "Unknown";
}

export function decisionChip(review: ReviewSummary, people: readonly Pick<PersonView, "id" | "name">[]): { tone: Tone; label: string } {
  if (review.status === "kept_both") {
    if (review.kept_person_id !== null && review.kept_person_id !== review.person_id) {
      return { tone: "ok", label: `Kept both, moved to ${personName(people, review.kept_person_id)}` };
    }
    return { tone: "ok", label: "Kept both" };
  }
  if (review.status === "pending") return { tone: "warn", label: "Waiting" };
  return { tone: "neutral", label: "Undone" };
}

export function canUndo(review: ReviewSummary, now: number): boolean {
  return !review.reversed && review.undo_until !== null && now < review.undo_until;
}

export function undoUnavailable(review: ReviewSummary): string {
  return review.reversed ? "Reversed, so it can't be undone" : "Too late to undo";
}

export function savedSentence(name: string | null): string {
  return `Decision saved: kept both for ${name ?? "the pair"}.`;
}

export function counted(run: Pick<ImportView, "stage" | "status">): boolean {
  return run.status === "done" || (run.stage !== null && COUNTED.has(run.stage));
}

export function skipped(run: Pick<ImportView, "skipped_t1" | "skipped_t2" | "skipped_t3">): number {
  return run.skipped_t1 + run.skipped_t2 + run.skipped_t3;
}

export function unmatchedTotal(run: Pick<ImportView, "unmatched_by_person">): number | null {
  const counts = Object.values(run.unmatched_by_person);
  return counts.length === 0 ? null : counts.reduce((sum, n) => sum + n, 0);
}

export function unmatchedNames(run: Pick<ImportView, "unmatched_by_person">, people: readonly Pick<PersonView, "slug" | "name">[]): string[] {
  return Object.entries(run.unmatched_by_person)
    .filter(([, n]) => n > 0)
    .map(([slug, n]) => `${people.find((person) => person.slug === slug)?.name ?? slug} ${n}`);
}

export function partsSentence(run: Pick<ImportView, "parts" | "part_count">): string | null {
  const total = run.part_count ?? run.parts.length;
  if (total === 0) return null;
  const done = run.parts.filter((part) => part.status === "done").length;
  if (done === total) return total === 1 ? "1 part" : `${total} parts`;
  return `${done} of ${total} parts downloaded`;
}

export function runChip(run: Pick<ImportView, "status">): { tone: Tone; label: string } | null {
  if (run.status === "failed") return { tone: "bad", label: "Failed" };
  if (run.status === "running" || run.status === "queued") return { tone: "neutral", label: "Importing" };
  return null;
}

export const RANGES = [
  { key: "year", label: "Last 12 months", days: 365 },
  { key: "month", label: "Last 30 days", days: 30 },
  { key: "all", label: "All time", days: null },
] as const;

export type RangeKey = (typeof RANGES)[number]["key"];

export function rangeStart(key: RangeKey, now: number): number | null {
  const found = RANGES.find((range) => range.key === key);
  return found?.days ? Math.floor(now - found.days * DAY) : null;
}
