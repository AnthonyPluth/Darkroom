// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SetupSetting, SetupState } from "$lib/api-types";
import {
  alertsOn, arrivedFromTest, failed, firstStep, linked, markOf, rememberedMarks, rememberedStep, rememberMarks, rememberStep, splitList, startTesting, stepLine,
} from "./wizard";

const setting = (variable: string, source: SetupSetting["source"], value: string | null = null): SetupSetting => ({ variable, source, set: source !== "default", value });

function state(phase: SetupState["phase"], ...settings: SetupSetting[]): SetupState {
  return { settings, redirect_uri: null, proposal: null, phase, tested_email: phase === "after_test" ? "jo@example.com" : null };
}

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("the wizard's wording", () => {
  it("puts the API's message after what failed, with one full stop", () => {
    expect(failed("save the address", "public_url must be just the address, without a path")).toBe("Couldn't save the address: public_url must be just the address, without a path.");
    expect(failed("start setup", "Can't reach Darkroom. Check your connection and try again.")).toBe("Couldn't start setup: Can't reach Darkroom. Check your connection and try again.");
  });

  it("numbers the steps out of eight", () => {
    expect(stepLine("people")).toBe("Step 4 of 8: People");
    expect(stepLine("accounts")).toBe("Step 5 of 8: Google accounts");
  });

  it("splits a typed list on commas and trims it", () => {
    expect(splitList(" parents, ,family ,")).toEqual(["parents", "family"]);
    expect(splitList("")).toEqual([]);
  });
});

describe("where the wizard opens", () => {
  const counts = (people: number, accounts = 0, pixels = 0) => ({ people, accounts, pixels });

  it("opens at Address, then Sign-in, before the test", () => {
    expect(firstStep(state("before_test"), null, null)).toBe("address");
    expect(firstStep(state("before_test", setting("DARKROOM_PUBLIC_URL", "stored", "http://192.168.1.20:8000")), null, "people")).toBe("sign-in");
    expect(firstStep(state("before_test", setting("DARKROOM_PUBLIC_URL", "environment")), null, null)).toBe("sign-in");
  });

  it("opens at Immich, then People, then the step this browser was on, then Google accounts, else Done after the test", () => {
    const immich = setting("IMMICH_URL", "stored", "http://192.168.1.20:2283");
    expect(firstStep(state("after_test"), counts(2), "pixels")).toBe("immich");
    expect(firstStep(state("after_test", immich), counts(0), "pixels")).toBe("people");
    expect(firstStep(state("after_test", immich), counts(1), "pixels")).toBe("pixels");
    expect(firstStep(state("after_test", immich), counts(1), "sign-in")).toBe("accounts");
    expect(firstStep(state("after_test", immich), counts(1, 1), null)).toBe("done");
  });
});

describe("the step list", () => {
  it("marks settled steps done and only links the steps that can still change", () => {
    const before = state("before_test", setting("DARKROOM_PUBLIC_URL", "stored", "http://192.168.1.20:8000"));
    expect(markOf("address", before, null, {})).toBe("done");
    expect(linked("address", before, "done")).toBe(true);
    expect(markOf("sign-in", before, null, {})).toBeNull();
    const after = state("after_test", setting("IMMICH_URL", "stored", "http://192.168.1.20:2283"));
    expect(markOf("sign-in", after, null, {})).toBe("done");
    expect(linked("address", after, "done")).toBe(false);
    expect(linked("sign-in", after, "done")).toBe(false);
    expect(linked("immich", after, "done")).toBe(true);
    expect(linked("people", after, null)).toBe(false);
  });

  it("says Skipped for a skipped optional step until it holds something", () => {
    const after = state("after_test");
    expect(markOf("accounts", after, { people: 1, accounts: 0, pixels: 0 }, { accounts: "skipped" })).toBe("skipped");
    expect(markOf("accounts", after, { people: 1, accounts: 1, pixels: 0 }, { accounts: "skipped" })).toBe("done");
    expect(markOf("pixels", after, { people: 1, accounts: 1, pixels: 0 }, { pixels: "skipped" })).toBe("skipped");
    expect(markOf("alerts", after, null, { alerts: "skipped" })).toBe("skipped");
    expect(markOf("people", after, { people: 0, accounts: 0, pixels: 0 }, {})).toBeNull();
    expect(markOf("done", after, null, {})).toBeNull();
  });

  it("calls alert emails on only when all three settings are set", () => {
    const two = state("after_test", setting("RESEND_API_KEY", "stored"), setting("ALERT_EMAIL_FROM", "stored", "alerts@example.com"));
    expect(alertsOn(two)).toBe(false);
    expect(alertsOn({ ...two, settings: [...two.settings, setting("ALERT_EMAIL_TO", "environment")] })).toBe(true);
  });
});

describe("what the wizard keeps in this tab", () => {
  it("remembers the step and the marks, and ignores anything else", () => {
    rememberStep("pixels");
    rememberMarks({ accounts: "skipped" });
    expect(rememberedStep()).toBe("pixels");
    expect(rememberedMarks()).toEqual({ accounts: "skipped" });
    sessionStorage.setItem("darkroom.setup.step", "nowhere");
    sessionStorage.setItem("darkroom.setup.marks", JSON.stringify({ accounts: "maybe", pixels: "done", other: "done" }));
    expect(rememberedStep()).toBeNull();
    expect(rememberedMarks()).toEqual({ pixels: "done" });
    sessionStorage.setItem("darkroom.setup.marks", "not json");
    expect(rememberedMarks()).toEqual({});
  });

  it("says once that the browser came back from the test sign-in", () => {
    expect(arrivedFromTest()).toBe(false);
    startTesting();
    expect(arrivedFromTest()).toBe(true);
    expect(arrivedFromTest()).toBe(false);
  });

  it("works without storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    rememberStep("people");
    expect(rememberedStep()).toBeNull();
    expect(rememberedMarks()).toEqual({});
  });
});
