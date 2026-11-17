import { describe, expect, it } from "vitest";
import { REFUSED_IMMICH_CONSOLE, SETUP_PAGE, UNANSWERED_IMMICH, isSetupPage, wantsSetup } from "./setup.mjs";
import { allowedFor, DEMO_PAGES, SETUP_PAGES, unknownPages } from "./verify.mjs";

describe("the setup pages", () => {
  it("run on a full verify or when asked for, never for flows alone", () => {
    expect(wantsSetup([], false, false)).toBe(true);
    expect(wantsSetup(["status", SETUP_PAGE], true, false)).toBe(true);
    expect(wantsSetup(["setup-done"], true, false)).toBe(true);
    expect(wantsSetup(["status"], true, false)).toBe(false);
    expect(wantsSetup([], true, true)).toBe(false);
  });

  it("are a page verify knows, with an image for every step and the closed screen", () => {
    expect(unknownPages([SETUP_PAGE, "nowhere"])).toEqual(["nowhere"]);
    expect(SETUP_PAGES).toEqual(["setup-welcome", "setup-address", "setup-sign-in", "setup-immich", "setup-immich-refused", "setup-people", "setup-accounts", "setup-pixels", "setup-alerts", "setup-done", "setup-closed"]);
    expect(DEMO_PAGES.some(isSetupPage)).toBe(false);
    expect(unknownPages(["setup-done"])).toEqual([]);
  });

  it("allow only the console line of the Immich check's refusal, at an address nothing answers on", () => {
    expect(REFUSED_IMMICH_CONSOLE).toBe("the server responded with a status of 422");
    expect(new URL(UNANSWERED_IMMICH).hostname).toBe("127.0.0.1");
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
