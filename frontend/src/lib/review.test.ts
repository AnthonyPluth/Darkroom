import { describe, expect, it } from "vitest";
import type { ImportView, ReviewSummary } from "./api-types";
import {
  camera, canUndo, compared, counted, decidedBy, decisionChip, decisionDetail, frame, pairLabel, partsSentence, personName, rangeStart, runChip, savedSentence, size, skipped,
  undoUnavailable, unmatchedNames, unmatchedTotal, wallClock,
} from "./review";

const people = [{ id: 1, name: "Jo", slug: "jo" }, { id: 2, name: "Sam", slug: "sam" }];
const summary: ReviewSummary = {
  id: 1, status: "kept_both", decision_id: 6, export_id: 1, account_id: 1, tier: 4, reason: "close", decided_at: 1, reversed: false,
  resolved_at: 100, undo_until: 200, name: "IMG_2102.jpg", library_name: "IMG_2102.HEIC", person_id: 1, kept_item_id: 9, kept_person_id: 1, decided_by_person_id: 1,
};
const run = { stage: "done", status: "done", skipped_t1: 3, skipped_t2: 1, skipped_t3: 1, unmatched_by_person: { jo: 2, sam: 0 }, parts: [], part_count: null } as unknown as ImportView;
const side = { name: "a", bytes: 1, captured_local: null, make: "Apple", model: "iPhone 15", width: 4032, height: 3024, duration_ms: null, is_video: false };

describe("sizes and times", () => {
  it("writes sizes the way people read them", () => {
    expect(size(null)).toBe("Unknown");
    expect(size(512)).toBe("512 B");
    expect(size(640 * 1024)).toBe("640 KB");
    expect(size(3.2 * 1024 * 1024)).toBe("3.2 MB");
    expect(size(2 * 1024 ** 3)).toBe("2.0 GB");
    expect(compared(1024 * 1024, 3 * 1024 * 1024)).toBe("1.0 MB, smaller");
    expect(compared(3 * 1024 * 1024, 1024 * 1024)).toBe("3.0 MB, larger");
    expect(compared(10, 10)).toBe("10 B");
    expect(compared(10, null)).toBe("10 B");
  });

  it("shows a wall-clock time as written, never converting it", () => {
    expect(wallClock("2026-09-12T14:03:22")).toBe("12 Sep 2026, 14:03:22");
    expect(wallClock("2026-09-12T14:03")).toBe("12 Sep 2026, 14:03:00");
    expect(wallClock("2026-10-29T17:42:10", false)).toBe("29 Oct 2026, 17:42");
    expect(wallClock(null)).toBeNull();
    expect(wallClock("yesterday")).toBeNull();
  });

  it("names the camera and the frame", () => {
    expect(camera(side)).toBe("iPhone 15");
    expect(camera({ make: "Google", model: null })).toBe("Google");
    expect(camera({ make: null, model: " " })).toBe("Unknown");
    expect(frame(side)).toBe("4032 × 3024");
    expect(frame({ ...side, width: null })).toBe("Unknown");
    expect(frame({ ...side, is_video: true, duration_ms: 72_400 })).toBe("1:12");
    expect(frame({ ...side, is_video: true })).toBe("Unknown length");
  });
});

describe("review rows", () => {
  it("labels a pair by its library name and the export's extension", () => {
    expect(pairLabel(summary)).toBe("IMG_2102.HEIC / .jpg");
    expect(pairLabel({ name: "PXL_1.jpg", library_name: "IMG_9.HEIC" })).toBe("IMG_9.HEIC / PXL_1.jpg");
    expect(pairLabel({ name: "PXL_1.jpg", library_name: null })).toBe("PXL_1.jpg");
    expect(pairLabel({ name: null, library_name: null })).toBe("Unknown file");
    expect(pairLabel({ name: "noext", library_name: "noext" })).toBe("noext / noext");
  });

  it("chips each decision and says who a kept copy moved to", () => {
    expect(decisionChip(summary, people)).toEqual({ tone: "ok", label: "Kept both" });
    expect(decisionChip({ ...summary, kept_person_id: 2 }, people)).toEqual({ tone: "ok", label: "Reassigned to Sam" });
    expect(decisionChip({ ...summary, kept_person_id: 2 }, people, true)).toEqual({ tone: "ok", label: "Reassigned" });
    expect(decisionChip({ ...summary, status: "pending" }, people)).toEqual({ tone: "warn", label: "Waiting" });
    expect(decisionChip({ ...summary, status: "undone" }, people)).toEqual({ tone: "neutral", label: "Undone" });
    expect(personName(people, 9)).toBe("Unknown");
  });

  it("names who decided and builds the phone line the board shows", () => {
    const moved = { ...summary, kept_person_id: 2 };
    expect(decidedBy(summary, people)).toBe("Jo");
    expect(decidedBy({ ...summary, decided_by_person_id: null }, people)).toBeNull();
    expect(decisionDetail(summary, people, "Today 10:05")).toBe("Today 10:05, Jo");
    expect(decisionDetail(moved, people, "Yesterday 20:39")).toBe("Yesterday 20:39, Jo, moved to Sam");
    expect(decisionDetail({ ...moved, decided_by_person_id: null }, people, "Yesterday 20:39")).toBe("Yesterday 20:39, moved to Sam");
    expect(decisionDetail({ ...summary, decided_by_person_id: null }, people, "")).toBe("");
  });

  it("offers undo only inside the window", () => {
    expect(canUndo(summary, 199)).toBe(true);
    expect(canUndo(summary, 200)).toBe(false);
    expect(canUndo({ ...summary, undo_until: null }, 0)).toBe(false);
    expect(canUndo({ ...summary, reversed: true }, 0)).toBe(false);
    expect(undoUnavailable({ ...summary, reversed: true })).toBe("Reversed, so it can't be undone");
    expect(undoUnavailable(summary)).toBe("Too late to undo");
    expect(savedSentence("a.jpg")).toBe("Decision saved: kept both for a.jpg.");
    expect(savedSentence(null)).toBe("Decision saved: kept both for the pair.");
  });
});

describe("import runs", () => {
  it("counts only once the run has counted", () => {
    expect(counted(run)).toBe(true);
    expect(counted({ stage: "placed", status: "failed" })).toBe(false);
    expect(counted({ stage: "rescanned", status: "running" })).toBe(true);
    expect(counted({ stage: null, status: "running" })).toBe(false);
    expect(skipped(run)).toBe(5);
  });

  it("adds up the unmatched originals and names who has some", () => {
    expect(unmatchedTotal(run)).toBe(2);
    expect(unmatchedTotal({ unmatched_by_person: {} })).toBeNull();
    expect(unmatchedNames(run, people)).toEqual(["Jo 2"]);
    expect(unmatchedNames({ unmatched_by_person: { kim: 1 } }, people)).toEqual(["kim 1"]);
  });

  it("describes the parts and the run's state", () => {
    expect(partsSentence({ parts: [], part_count: null })).toBeNull();
    expect(partsSentence({ parts: [{ number: 1, status: "done" }], part_count: 1 })).toBe("1 part");
    expect(partsSentence({ parts: [{ number: 1, status: "done" }, { number: 2, status: "done" }], part_count: null })).toBe("2 parts");
    expect(partsSentence({ parts: [{ number: 1, status: "done" }, { number: 2, status: "pending" }], part_count: 5 })).toBe("1 of 5 parts downloaded");
    expect(runChip({ status: "failed" })).toEqual({ tone: "bad", label: "Failed" });
    expect(runChip({ status: "running" })).toEqual({ tone: "neutral", label: "Importing" });
    expect(runChip({ status: "done" })).toBeNull();
  });

  it("turns a range into a start time", () => {
    expect(rangeStart("year", 400 * 86_400)).toBe(35 * 86_400);
    expect(rangeStart("month", 40 * 86_400)).toBe(10 * 86_400);
    expect(rangeStart("all", 5)).toBeNull();
  });
});
