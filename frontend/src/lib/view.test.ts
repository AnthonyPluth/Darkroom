import { describe, expect, it } from "vitest";
import { seededStatus } from "../test/server";
import { downloadingExport, expiredExport, failedExport, importedExport, preparingExport, queuedConnect } from "../test/server";
import { accountExports, alertChip, checkNowSentence, currentExport, earlierExports, exportChip, failedCaptures, lastExportSentence, partChip, partsLabel, reviewCount, needsAttention, needsSentence, safeToDelete, sessionChip } from "./view";

describe("sessionChip", () => {
  it("names each session state with a tone", () => {
    expect(sessionChip("ok")).toEqual({ tone: "ok", label: "Session ok" });
    expect(sessionChip("needs_sign_in")).toEqual({ tone: "bad", label: "Sign-in needed" });
    expect(sessionChip("unknown")).toEqual({ tone: "neutral", label: "Session not checked yet" });
  });
});

describe("alertChip", () => {
  it("labels the alert kinds", () => {
    expect(alertChip("sign_in_needed")).toEqual({ tone: "bad", label: "Sign-in needed" });
    expect(alertChip("failure")).toEqual({ tone: "bad", label: "Failed" });
    expect(alertChip("originals_unmatched").label).toBe("Check backup");
    expect(alertChip("schedule_ending").label).toBe("Schedule ending");
    expect(alertChip("export_expiring").label).toBe("Export expiring");
    expect(alertChip("anything_else")).toEqual({ tone: "warn", label: "Needs a look" });
  });
});

describe("needsAttention and needsSentence", () => {
  it("flags a person with an open alert or an account that needs a sign-in", () => {
    const [jo, sam] = seededStatus.people;
    expect(needsAttention(jo)).toBe(false);
    expect(needsAttention(sam)).toBe(true);
    expect(needsAttention({ ...jo, alerts: [seededStatus.alerts[0]] })).toBe(true);
  });
  it("counts what needs the reader", () => {
    expect(needsSentence(0)).toBe("Nothing needs you right now.");
    expect(needsSentence(1)).toBe("One thing needs you.");
    expect(needsSentence(3)).toBe("3 things need you.");
  });
});

describe("safeToDelete", () => {
  const device = seededStatus.people[0].devices[0];
  it("adds up the iPhones' counts, and is unknown while any of them is", () => {
    expect(safeToDelete([])).toBeNull();
    expect(safeToDelete([{ ...device, eligible: 2 }, { ...device, eligible: 3 }])).toBe(5);
    expect(safeToDelete([{ ...device, eligible: 2 }, { ...device, eligible: null }])).toBeNull();
    expect(safeToDelete([device])).toBeNull();
  });
});

describe("exportChip", () => {
  it("names each state an export can be in", () => {
    expect(exportChip(preparingExport)).toEqual({ tone: "neutral", label: "Google is preparing it" });
    expect(exportChip({ ...preparingExport, ready_at: 5 })).toEqual({ tone: "neutral", label: "Waiting to download" });
    expect(exportChip(downloadingExport)).toEqual({ tone: "neutral", label: "Downloading" });
    expect(exportChip({ ...downloadingExport, status: "downloaded" }).label).toBe("Downloaded");
    expect(exportChip({ ...downloadingExport, status: "unpacking" }).label).toBe("Importing");
    expect(exportChip({ ...downloadingExport, status: "importing" }).label).toBe("Importing");
    expect(exportChip(importedExport)).toEqual({ tone: "ok", label: "Imported" });
    expect(exportChip(failedExport)).toEqual({ tone: "bad", label: "Failed" });
    expect(exportChip(expiredExport)).toEqual({ tone: "warn", label: "Expired" });
    expect(exportChip({ ...expiredExport, status: "mystery" })).toEqual({ tone: "warn", label: "Needs a look" });
  });
});

describe("partChip", () => {
  it("names each state a part can be in", () => {
    const part = downloadingExport.parts[0];
    expect(partChip({ ...part, status: "pending" }, "announced")).toEqual({ tone: "neutral", label: "Waiting" });
    expect(partChip({ ...part, status: "downloading" }, "downloading")).toEqual({ tone: "neutral", label: "Downloading" });
    expect(partChip({ ...part, status: "done" }, "downloading")).toEqual({ tone: "ok", label: "Downloaded" });
    expect(partChip({ ...part, status: "done" }, "imported")).toEqual({ tone: "ok", label: "Imported" });
    expect(partChip({ ...part, status: "failed" }, "failed")).toEqual({ tone: "bad", label: "Failed" });
    expect(partChip({ ...part, status: "corrupt" }, "failed")).toEqual({ tone: "bad", label: "Failed" });
  });
});

describe("the exports of an account", () => {
  const all = [preparingExport, downloadingExport, importedExport, { ...importedExport, id: 99, account_id: 11 }];
  it("keeps one account's exports, takes the newest unfinished one as current and the rest as earlier", () => {
    const mine = accountExports(all, 10);
    expect(mine.map((item) => item.id)).toEqual([41, 40, 38]);
    expect(currentExport(mine)?.id).toBe(41);
    expect(earlierExports(mine, currentExport(mine)).map((item) => item.id)).toEqual([40, 38]);
    expect(currentExport([importedExport, failedExport, expiredExport])).toBeUndefined();
    expect(earlierExports([importedExport], undefined)).toEqual([importedExport]);
  });
  it("counts parts and import reviews", () => {
    expect(partsLabel(null)).toBe("Parts not listed yet");
    expect(partsLabel(1)).toBe("1 part");
    expect(partsLabel(5)).toBe("5 parts");
    const run = { id: 1, export_id: 38, account_id: 10, started: 1, finished: 2, stage: null, status: "done", added: 0, skipped_t1: 0, skipped_t2: 0, skipped_t3: 0, review: 3, missing_gps: 0, failed: 0, unmatched_by_person: {}, error: null };
    expect(reviewCount([run], 38)).toBe(3);
    expect(reviewCount([run], 39)).toBe(0);
  });
});

describe("lastExportSentence", () => {
  it("sums up the newest export", () => {
    expect(lastExportSentence(undefined)).toBe("Nothing exported yet");
    expect(lastExportSentence(importedExport)).toBe("2 parts, all imported");
    expect(lastExportSentence(expiredExport)).toBe("Expired before it was downloaded");
    expect(lastExportSentence(failedExport)).toBe("2 parts, failed");
    expect(lastExportSentence(preparingExport)).toBe("Google is still preparing it");
    expect(lastExportSentence(downloadingExport)).toBe("3 parts, downloading");
  });
});

describe("checkNowSentence", () => {
  it("says what the check did", () => {
    expect(checkNowSentence(1, 0)).toBe("Checking one Google account for new exports.");
    expect(checkNowSentence(3, 1)).toBe("Checking 3 Google accounts for new exports.");
    expect(checkNowSentence(0, 2)).toBe("A check is already waiting or running.");
    expect(checkNowSentence(0, 0)).toBe("No Google account is connected, so there is nothing to check.");
  });
});

describe("failedCaptures", () => {
  it("includes a failed export check with a capture", () => {
    const job = { ...queuedConnect, id: 9, kind: "status", status: "failed", has_capture: true, capture_files: ["page.html"] };
    expect(failedCaptures([job], 10).map((each) => each.id)).toEqual([9]);
  });
});
