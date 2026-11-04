import { describe, expect, it } from "vitest";
import { seededStatus } from "../test/server";
import { alertChip, needsAttention, needsSentence, safeToDelete, sessionChip } from "./view";

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
    expect(safeToDelete([])).toBe(0);
    expect(safeToDelete([{ ...device, eligible: 2 }, { ...device, eligible: 3 }])).toBe(5);
    expect(safeToDelete([{ ...device, eligible: 2 }, { ...device, eligible: null }])).toBeNull();
    expect(safeToDelete([device])).toBeNull();
  });
});
