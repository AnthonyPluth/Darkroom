import { chromium } from "@playwright/test";
import { createServer } from "node:http";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { CHROMIUM_ARGS, allowedRequest, checksAfter, contextOptions, DEFAULT_VIEWPORTS, findChromium, flowProblems, FROZEN_NOW, IMAGE_VIEWPORTS, launchOptions, loadedWhen, PAGES, readFlows, recheck, ROUTES, routeOf, screenshotFile, sessionCookie, SIGNED_OUT_PAGES, unknownPages, VIEWPORTS, visit, writesImage } from "./verify.mjs";

describe("flowProblems", () => {
  const ok = { name: "open-people", page: "status", steps: [{ goto: "/people" }, { click: "text=People", timeout: 500 }] };

  it("accepts a well formed flow", () => expect(flowProblems(ok)).toEqual([]));

  it("names what is wrong, step by step", () => {
    expect(flowProblems({ steps: [] })).toEqual(["needs a name of letters, digits, - or _", "needs a list of steps"]);
    const bad = flowProblems({ ...ok, steps: [{ click: "a", goto: "/b" }, { fill: "text" }, { drag: "x" }] });
    expect(bad).toHaveLength(3);
    expect(bad[0]).toMatch(/step 1 must have exactly one of/);
    expect(bad[1]).toBe("step 2: fill takes a object");
    expect(bad[2]).toMatch(/step 3 must have exactly one of/);
  });

  it("takes a click that only happens when the element shows up", () => {
    expect(flowProblems({ ...ok, steps: [{ click_if_present: "text=People", timeout: 500 }] })).toEqual([]);
    expect(flowProblems({ ...ok, steps: [{ click_if_present: { selector: "a" } }] })).toEqual(["step 1: click_if_present takes a string"]);
  });

  it("takes a selector to hover and an option to select by its position", () => {
    expect(flowProblems({ ...ok, steps: [{ hover: "li.row" }, { select: { selector: "select", index: 2 } }] })).toEqual([]);
    expect(flowProblems({ ...ok, steps: [{ hover: true }] })).toEqual(["step 1: hover takes a string"]);
    expect(flowProblems({ ...ok, steps: [{ select: "x" }] })).toEqual(["step 1: select takes a object"]);
  });

  it("needs a selector and a file name for a download", () => {
    expect(flowProblems({ ...ok, steps: [{ download: { selector: "button", name: "export" } }] })).toEqual([]);
    expect(flowProblems({ ...ok, steps: [{ download: { selector: "button" } }] })).toEqual(["step 1: download needs a selector and a name of letters, digits, - or _"]);
    expect(flowProblems({ ...ok, steps: [{ download: { selector: "button", name: "../x" } }] })).toHaveLength(1);
  });

  it("takes a file to upload, an element to scroll to, a sign out and a reload", () => {
    expect(flowProblems({ ...ok, steps: [{ upload: { selector: "input[type=file]", file: "tests/fixtures/a.json" } }, { scroll_to: "#x" }, { sign_out: true }, { reload: true }] })).toEqual([]);
    expect(flowProblems({ ...ok, steps: [{ sign_out: "yes" }] })).toEqual(["step 1: sign_out takes a boolean"]);
  });

  it("takes the laptop and wide viewports a flow asks for, and no other", () => {
    expect(flowProblems({ ...ok, viewports: ["laptop", "wide"] })).toEqual([]);
    expect(flowProblems({ ...ok, viewports: ["watch"] })).toEqual(["unknown viewport watch"]);
  });

  it("rejects a page the app doesn't have, so a typo can't pass for a clean run", () => {
    expect(flowProblems({ ...ok, page: "peple" })).toEqual(["unknown page peple"]);
    expect(unknownPages(["status", "peple"])).toEqual(["peple"]);
    expect(unknownPages(PAGES)).toEqual([]);
  });

  it("rejects a scheme and a host", () => {
    expect(flowProblems({ ...ok, scheme: "dark" })).toEqual(["scheme is not an option: the app is dark only"]);
    expect(flowProblems({ ...ok, host: "localhost" })).toHaveLength(1);
  });

  it("has the app's pages, each with a route", () => {
    expect(PAGES).toEqual(["sign-in", "status", "people"]);
    expect(Object.keys(ROUTES)).toEqual(PAGES);
    expect(SIGNED_OUT_PAGES).toEqual(["sign-in"]);
    expect([routeOf("status"), routeOf("people"), routeOf("sign-in"), routeOf("exports")]).toEqual(["/", "/people", "/", "/exports"]);
    expect([loadedWhen(true), loadedWhen(false)]).toEqual(["networkidle", "load"]);
  });
});

describe("screenshotFile", () => {
  it("names one image for a shot, with no viewport-only variant", () => {
    expect(screenshotFile("status", "phone")).toBe("status-phone.png");
    expect(screenshotFile("flow-open-people-people", "desktop")).toBe("flow-open-people-people-desktop.png");
  });

  it("writes images at phone and desktop only, and checks the extra widths without one", () => {
    expect(IMAGE_VIEWPORTS).toEqual(["phone", "desktop"]);
    expect(DEFAULT_VIEWPORTS).toEqual(["phone", "desktop"]);
    expect(Object.keys(VIEWPORTS).filter(writesImage)).toEqual(["phone", "desktop"]);
    expect(VIEWPORTS.phone).toEqual({ width: 440, height: 956 });
    expect(VIEWPORTS.desktop).toEqual({ width: 1280, height: 800 });
  });

  it("freezes the clock on a fixed date", () => expect(FROZEN_NOW).toBe("2026-06-15T12:00:00Z"));
});

describe("CHROMIUM_ARGS", () => {
  it("rasters in one thread, without partial raster and with a fixed colour profile and no sub-pixel text, so an edge comes out the same every run", () => {
    expect(CHROMIUM_ARGS).toEqual(["--force-color-profile=srgb", "--disable-lcd-text", "--num-raster-threads=1", "--disable-partial-raster"]);
  });
});

describe("findChromium", () => {
  const tree = (files) => ({ exists: (p) => files.some((f) => f === p || f.startsWith(`${p}/`)), list: () => ["ffmpeg-1011", "chromium-1194", "chromium-1200"] });

  it("uses the newest preinstalled Chromium, wherever its folder is", () => {
    const { exists, list } = tree(["/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]);
    expect(findChromium({}, exists, list)).toBe("/opt/pw-browsers/chromium-1194/chrome-linux/chrome");
    const both = tree(["/opt/pw-browsers/chromium-1194/chrome-linux/chrome", "/opt/pw-browsers/chromium-1200/chrome-linux64/chrome"]);
    expect(findChromium({}, both.exists, both.list)).toBe("/opt/pw-browsers/chromium-1200/chrome-linux64/chrome");
  });

  it("follows PLAYWRIGHT_BROWSERS_PATH, and leaves it to Playwright when there is no Chromium", () => {
    const { exists, list } = tree(["/x/chromium-1194/chrome-linux/chrome"]);
    expect(findChromium({ PLAYWRIGHT_BROWSERS_PATH: "/x" }, exists, list)).toBe("/x/chromium-1194/chrome-linux/chrome");
    expect(findChromium({ PLAYWRIGHT_BROWSERS_PATH: "/nowhere" }, exists, list)).toBeUndefined();
  });
});

describe("launchOptions and contextOptions", () => {
  it("launch the browser the same way for a run and for a control session", () => {
    expect(launchOptions().args).toEqual(CHROMIUM_ARGS);
  });

  it("make the dark UTC context a visit gets", () => {
    expect(contextOptions("desktop")).toEqual({ viewport: VIEWPORTS.desktop, colorScheme: "dark", timezoneId: "UTC", reducedMotion: "no-preference" });
    expect(contextOptions("phone", { reducedMotion: "reduce" }).reducedMotion).toBe("reduce");
  });
});

describe("the demo session", () => {
  const folder = mkdtempSync(join(tmpdir(), "darkroom-session-"));
  afterAll(() => rmSync(folder, { recursive: true, force: true }));

  it("is the cookie name and token the demo seed wrote", () => {
    writeFileSync(join(folder, "s.json"), JSON.stringify({ cookie: "darkroom_session", token: "t0k", sub: "demo:avery", name: "Avery Example" }));
    expect(sessionCookie(join(folder, "s.json"))).toEqual({ name: "darkroom_session", value: "t0k" });
  });

  it("is refused when the file has no cookie and token", () => {
    writeFileSync(join(folder, "bad.json"), JSON.stringify({ sub: "x" }));
    expect(() => sessionCookie(join(folder, "bad.json"))).toThrow(/no cookie and token/);
  });
});

describe("readFlows", () => {
  const folder = mkdtempSync(join(tmpdir(), "darkroom-flows-"));
  const write = (name, text) => writeFileSync(join(folder, name), text);
  afterAll(() => rmSync(folder, { recursive: true, force: true }));

  it("reads every flow of the repository", () => {
    const { flows, error } = readFlows(join(import.meta.dirname, "flows"));
    expect(error).toBeUndefined();
    expect(flows.map((f) => f.name)).toEqual(["open-people", "sign-out"]);
  });

  it("says which file is not JSON or not a flow, instead of ending the process", () => {
    write("a.json", JSON.stringify({ name: "a", steps: [{ goto: "/people" }] }));
    write("b.json", "{ nope");
    expect(readFlows(folder).error).toMatch(/^flow b\.json isn't valid JSON/);
    write("b.json", JSON.stringify({ name: "b", steps: [] }));
    expect(readFlows(folder).error).toBe("flow b.json: needs a list of steps");
    write("b.json", JSON.stringify({ name: "b", steps: [{ goto: "/people" }] }));
    expect(readFlows(folder).flows.map((f) => f.name)).toEqual(["a", "b"]);
    expect(readFlows(join(folder, "none"))).toEqual({ flows: [] });
  });
});

describe("the assertions a screenshot must not undo", () => {
  const expectText = { expect_text: { selector: "h1", text: "People" } };

  it("are the expect_text steps since the last action", () => {
    let checks = [];
    checks = checksAfter(checks, expectText);
    checks = checksAfter(checks, { wait_for: "ul" });
    checks = checksAfter(checks, { screenshot: "a" });
    checks = checksAfter(checks, { scroll_to: "#x" });
    checks = checksAfter(checks, { expect_text: { selector: "main", text: "Avery" }, timeout: 500 });
    expect(checks).toHaveLength(2);
    expect(checksAfter(checks, { click: "a" })).toEqual([]);
    expect(checksAfter(checks, { goto: "/people" })).toEqual([]);
    expect(checksAfter(checks, { sign_out: true })).toEqual([]);
  });

  const pageShowing = (text) => ({ waitForTimeout: async () => {}, locator: () => ({ first: () => ({ waitFor: async () => {}, innerText: async () => text }) }) });

  it("hold, or are named when the page no longer shows them", async () => {
    expect(await recheck(pageShowing("People and more"), [expectText])).toBeNull();
    expect(await recheck(pageShowing("Status"), [])).toBeNull();
    const lost = await recheck(pageShowing("Status"), [expectText], 1);
    expect(lost).toContain("no longer holds after the screenshot");
    expect(lost).toContain('expected "People" in h1, found "Status"');
  });
});

describe("a page's request to any other host", () => {
  let browser, home, other, base, otherBase;
  const hits = [];
  const listen = (handler) => new Promise((resolve) => { const server = createServer(handler); server.listen(0, "127.0.0.1", () => resolve(server)); });

  beforeAll(async () => {
    other = await listen((req, res) => { hits.push(req.url); res.end("hit"); });
    otherBase = `http://127.0.0.1:${other.address().port}`;
    home = await listen((req, res) => {
      res.setHeader("Content-Type", "text/html");
      res.end(`<!doctype html><title>t</title><main><h1>Home</h1></main><script>fetch(${JSON.stringify(`${otherBase}/leak`)}, { mode: "no-cors" }).catch(() => {});</script>`);
    });
    base = `http://127.0.0.1:${home.address().port}`;
    browser = await chromium.launch(launchOptions());
  });
  afterAll(async () => {
    await browser?.close();
    home?.close();
    other?.close();
  });

  it("is blocked before it is sent, and reported as a problem of the visit", async () => {
    const out = mkdtempSync(join(tmpdir(), "darkroom-visit-"));
    try {
      const result = await visit({ browser, now: FROZEN_NOW, images: out, out, base, cookie: null }, "leaky", "desktop", async (page) => {
        await page.goto(`${base}/`, { waitUntil: "load" });
        await page.waitForTimeout(500);
      }, { audit: false, signedIn: false });
      expect(hits).toEqual([]);
      expect(result.problems.some((p) => p.includes(`127.0.0.1:${other.address().port}`) && p.includes("blocked before it was sent"))).toBe(true);
    } finally { rmSync(out, { recursive: true, force: true }); }
  });

  it("lets the session's own server through, and nothing else", () => {
    expect(allowedRequest(`${base}/api/me`, base)).toBe(true);
    expect(allowedRequest(`${otherBase}/leak`, base)).toBe(false);
    expect(allowedRequest("https://example.com/", base)).toBe(false);
  });
});
