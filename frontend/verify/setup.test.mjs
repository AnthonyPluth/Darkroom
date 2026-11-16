import { describe, expect, it } from "vitest";
import { SETUP_PAGE, SETUP_SHOTS, wantsSetup } from "./setup.mjs";
import { allowedFor, unknownPages } from "./verify.mjs";

describe("the setup pages", () => {
  it("run on a full verify or when asked for, never for flows alone", () => {
    expect(wantsSetup([], false, false)).toBe(true);
    expect(wantsSetup(["status", SETUP_PAGE], true, false)).toBe(true);
    expect(wantsSetup(["status"], true, false)).toBe(false);
    expect(wantsSetup([], true, true)).toBe(false);
  });

  it("are a page verify knows, with an image for every step and the closed screen", () => {
    expect(unknownPages([SETUP_PAGE, "nowhere"])).toEqual(["nowhere"]);
    expect(SETUP_SHOTS).toEqual(["setup-welcome", "setup-address", "setup-sign-in", "setup-immich", "setup-people", "setup-accounts", "setup-pixels", "setup-alerts", "setup-done", "setup-closed"]);
  });

  it("let the browser reach the made-up sign-in provider and nothing else", () => {
    const base = "http://127.0.0.1:4000";
    const provider = "http://127.0.0.1:4100";
    expect(allowedFor(`${provider}/authorize?state=x`, base, [provider])).toBe(true);
    expect(allowedFor(`${provider}/authorize`, base)).toBe(false);
    expect(allowedFor("https://accounts.google.com/", base, [provider])).toBe(false);
    expect(allowedFor(`${base}/api/setup`, base, [provider])).toBe(true);
  });
});
