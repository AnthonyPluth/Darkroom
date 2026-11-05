import { chromium } from "@playwright/test";
import { createServer } from "node:http";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { CHROMIUM_ARGS, SIGNED_OUT_CONSOLE, allowedRequest, browsersRoot, checksAfter, commitImages, runStep, contextOptions, DEFAULT_VIEWPORTS, findChromium, flowProblems, FROZEN_NOW, IMAGE_VIEWPORTS, launchOptions, loadedWhen, PAGES, readFlows, recheck, ROUTES, routeOf, screenshotFile, sessionCookie, SIGNED_OUT_PAGES, signedInNow, signedOutConsoleAllowed, unknownPages, VIEWPORTS, visit, writesImage } from "./verify.mjs";

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

  it("takes only a file-name-safe screenshot name, so a flow can't write outside its images", () => {
    expect(flowProblems({ ...ok, steps: [{ screenshot: "people-open" }] })).toEqual([]);
    for (const name of ["x/../../../docs/screenshots/status", "../status", "a/b", "a.b", ""]) expect(flowProblems({ ...ok, steps: [{ screenshot: name }] })).toHaveLength(1);
  });

  it("takes the laptop and wide viewports a flow asks for, and no other", () => {
    expect(flowProblems({ ...ok, viewports: ["laptop", "wide"] })).toEqual([]);
    expect(flowProblems({ ...ok, viewports: ["watch"] })).toEqual(["unknown viewport watch"]);
  });

  it("rejects a flow that lists no widths, which would never run", () => {
    expect(flowProblems({ ...ok, viewports: [] })).toEqual(["viewports is a list of at least one width"]);
    expect(flowProblems({ ...ok, viewports: "phone" })).toEqual(["viewports is a list of at least one width"]);
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

  it("has a page for every one the app's navigation lists, so a new page can't be skipped", () => {
    const nav = readFileSync(join(import.meta.dirname, "../src/lib/nav.svelte.ts"), "utf8");
    const paths = [...nav.matchAll(/\{ path: "([^"]+)", label: "[^"]+" \}/g)].map((m) => m[1]);
    expect(paths.length).toBeGreaterThan(0);
    for (const path of paths) expect(Object.values(ROUTES), `no page visits ${path}: add it to PAGES in verify.mjs and ROUTES in routes.mjs`).toContain(path);
    expect(Object.keys(ROUTES)).toEqual(PAGES);
  });

  it("has the app's pages, each with a route", () => {
    expect(PAGES).toEqual(["sign-in", "status", "people", "exports", "imports", "review", "browser"]);
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
    const home = { HOME: "/home/dev" };
    expect(browsersRoot(home)).toBe("/home/dev/.cache/ms-playwright");
    const { exists, list } = tree(["/home/dev/.cache/ms-playwright/chromium-1194/chrome-linux/chrome"]);
    expect(findChromium(home, exists, list)).toBe("/home/dev/.cache/ms-playwright/chromium-1194/chrome-linux/chrome");
    const both = tree(["/home/dev/.cache/ms-playwright/chromium-1194/chrome-linux/chrome", "/home/dev/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome"]);
    expect(findChromium(home, both.exists, both.list)).toBe("/home/dev/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome");
  });

  it("does not take a Chromium under /opt/pw-browsers unless the variable names it", () => {
    const { exists, list } = tree(["/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]);
    expect(findChromium({ HOME: "/home/dev" }, exists, list)).toBeUndefined();
    expect(findChromium({ HOME: "/home/dev", PLAYWRIGHT_BROWSERS_PATH: "/opt/pw-browsers" }, exists, list)).toBe("/opt/pw-browsers/chromium-1194/chrome-linux/chrome");
  });

  it("follows PLAYWRIGHT_BROWSERS_PATH, and leaves it to Playwright when there is no Chromium", () => {
    const { exists, list } = tree(["/x/chromium-1194/chrome-linux/chrome"]);
    expect(findChromium({ PLAYWRIGHT_BROWSERS_PATH: "/x" }, exists, list)).toBe("/x/chromium-1194/chrome-linux/chrome");
    expect(findChromium({ PLAYWRIGHT_BROWSERS_PATH: "/nowhere" }, exists, list)).toBeUndefined();
  });
});

describe("the session-start hook", () => {
  const HOOK = join(import.meta.dirname, "..", "..", ".claude", "hooks", "session-start.local.sh");

  const hookInstalls = (home, env) => {
    const work = mkdtempSync(join(tmpdir(), "darkroom-hook-"));
    try {
      mkdirSync(join(work, "frontend"));
      mkdirSync(join(work, "bin"));
      writeFileSync(join(work, "bin", "npx"), `#!/bin/sh\ntouch ${join(work, "installed")}\n`, { mode: 0o755 });
      spawnSync("bash", ["-c", `. ${HOOK}`], { cwd: work, env: { PATH: `${join(work, "bin")}:/usr/bin:/bin`, HOME: home, ...env }, encoding: "utf8" });
      return readdirSync(work).includes("installed");
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  it("and findChromium resolve the same default location when PLAYWRIGHT_BROWSERS_PATH is unset", () => {
    for (const present of [false, true]) {
      const home = mkdtempSync(join(tmpdir(), "darkroom-home-"));
      try {
        const chrome = join(home, ".cache", "ms-playwright", "chromium-1", "chrome-linux64", "chrome");
        if (present) { mkdirSync(dirname(chrome), { recursive: true }); writeFileSync(chrome, ""); }
        expect(hookInstalls(home, {})).toBe(!present);
        expect(findChromium({ HOME: home })).toBe(present ? chrome : undefined);
      } finally { rmSync(home, { recursive: true, force: true }); }
    }
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
    expect(flows.map((f) => f.name)).toEqual(["exports-download", "exports-renewal", "exports", "open-people", "review-item", "sign-out"]);
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

  it("refuses two files with one flow name, naming both", () => {
    write("a.json", JSON.stringify({ name: "same", steps: [{ goto: "/people" }] }));
    write("b.json", JSON.stringify({ name: "same", steps: [{ goto: "/" }] }));
    expect(readFlows(folder).error).toBe("flows a.json and b.json are both named same: a flow's name is its identity, so each file needs its own");
    write("b.json", JSON.stringify({ name: "other", steps: [{ goto: "/" }] }));
    expect(readFlows(folder).error).toBeUndefined();
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
      if (req.url === "/ws") return res.end(`<!doctype html><title>t</title><main><h1>Home</h1></main><script>new WebSocket(${JSON.stringify(`ws://127.0.0.1:${other.address().port}/socket`)});</script>`);
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

  it("is blocked for a WebSocket too: nothing is sent to the other host, and it is reported", async () => {
    let upgrades = 0;
    other.on("upgrade", (_req, socket) => { upgrades++; socket.destroy(); });
    const out = mkdtempSync(join(tmpdir(), "darkroom-visit-"));
    try {
      const result = await visit({ browser, now: FROZEN_NOW, images: out, out, base, cookie: null }, "sockets", "desktop", async (page) => {
        await page.goto(`${base}/ws`, { waitUntil: "load" });
        await page.waitForTimeout(500);
      }, { audit: false, signedIn: false });
      expect(upgrades).toBe(0);
      expect(result.problems.some((p) => p.includes(`127.0.0.1:${other.address().port}`) && p.includes("blocked before it was sent"))).toBe(true);
    } finally { rmSync(out, { recursive: true, force: true }); }
  });

  it("lets the session's own server through, and nothing else", () => {
    expect(allowedRequest(`${base}/api/me`, base)).toBe(true);
    expect(allowedRequest(`${otherBase}/leak`, base)).toBe(false);
    expect(allowedRequest("https://example.com/", base)).toBe(false);
  });
});

describe("a flow that signs out", () => {
  let browser, home, base;
  const listen = (handler) => new Promise((resolve) => { const server = createServer(handler); server.listen(0, "127.0.0.1", () => resolve(server)); });

  beforeAll(async () => {
    home = await listen((req, res) => {
      if (req.url === "/api/me") {
        res.statusCode = (req.headers.cookie ?? "").includes("sid=tok") ? 200 : 401;
        return res.end("{}");
      }
      res.setHeader("Content-Type", "text/html");
      res.end(`<!doctype html><title>t</title><main><h1>Home</h1></main><script>fetch("/api/me");</script>`);
    });
    base = `http://127.0.0.1:${home.address().port}`;
    browser = await chromium.launch(launchOptions());
  });
  afterAll(async () => {
    await browser?.close();
    home?.close();
  });

  const run = async (signedIn, work, allowConsole = []) => {
    const out = mkdtempSync(join(tmpdir(), "darkroom-visit-"));
    try {
      return await visit({ browser, now: FROZEN_NOW, images: out, out, base, cookie: { name: "sid", value: "tok" } }, "signs out", "desktop", work, { audit: false, signedIn, allowConsole });
    } finally { rmSync(out, { recursive: true, force: true }); }
  };

  it("expects the 401 of the signed-out reload without the flow listing it", async () => {
    const result = await run(true, async (page, shot, download, markSignedOut) => {
      await page.goto(`${base}/`, { waitUntil: "load" });
      await runStep(page, { sign_out: true }, null, null, markSignedOut);
      await runStep(page, { reload: true }, null, null);
      await page.waitForTimeout(300);
    });
    expect(result.consoleErrors.some((c) => c.includes("401"))).toBe(true);
    expect(result.problems).toEqual([]);
  });

  it("fails a 401 from before the sign_out step even when the flow ends signed out", async () => {
    const result = await run(true, async (page, shot, download, markSignedOut) => {
      await page.goto(`${base}/`, { waitUntil: "load" });
      await page.context().clearCookies();
      await page.evaluate(() => fetch("/api/me"));
      await page.waitForTimeout(100);
      await runStep(page, { sign_out: true }, null, null, markSignedOut);
      await page.waitForTimeout(300);
    });
    const seen = result.consoleErrors.filter((c) => c.includes("401")).length;
    const failed = result.problems.filter((p) => p.includes("console error") && p.includes("401")).length;
    expect(failed).toBeGreaterThanOrEqual(1);
    expect(failed).toBeLessThan(seen);
  });

  it("fails a lost session in a flow that ends signed out without a sign_out step", async () => {
    const result = await run(true, async (page) => {
      await page.goto(`${base}/`, { waitUntil: "load" });
      await page.context().clearCookies();
      await page.reload({ waitUntil: "load" });
      await page.waitForTimeout(300);
    });
    expect(result.problems.some((p) => p.includes("console error") && p.includes("401"))).toBe(true);
  });

  it("allows the 401 of a visit that starts signed out", async () => {
    const result = await run(false, async (page) => {
      await page.goto(`${base}/`, { waitUntil: "load" });
      await page.waitForTimeout(300);
    });
    expect(result.consoleErrors.some((c) => c.includes("401"))).toBe(true);
    expect(result.problems).toEqual([]);
  });

  it("tells the runner the moment of the sign out before the reload", async () => {
    const seen = [];
    const page = { context: () => ({ clearCookies: async () => seen.push("cleared") }), reload: async () => seen.push("reloaded") };
    await runStep(page, { sign_out: true }, null, null, () => seen.push("marked"));
    expect(seen).toEqual(["cleared", "marked", "reloaded"]);
  });

  it("allows the signed-out message only after the sign out", () => {
    expect(signedOutConsoleAllowed(SIGNED_OUT_CONSOLE, true)).toBe(true);
    expect(signedOutConsoleAllowed(SIGNED_OUT_CONSOLE, false)).toBe(false);
    expect(signedOutConsoleAllowed("boom", true)).toBe(false);
  });

  it("still fails a 401 on a visit that stays signed in", async () => {
    const result = await run(true, async (page) => {
      await page.goto(`${base}/`, { waitUntil: "load" });
      await page.context().clearCookies();
      await page.evaluate(() => fetch("/api/me"));
      await page.context().addCookies([{ name: "sid", value: "tok", url: base }]);
    });
    expect(result.problems.some((p) => p.includes("console error") && p.includes("401"))).toBe(true);
  });
});

describe("signedInNow", () => {
  it("is true while the browser holds a cookie and false once the cookies are cleared", async () => {
    const browser = await chromium.launch(launchOptions());
    try {
      const ctx = await browser.newContext();
      await ctx.addCookies([{ name: "sid", value: "tok", url: "http://127.0.0.1:1" }]);
      expect(await signedInNow(ctx)).toBe(true);
      await ctx.clearCookies();
      expect(await signedInNow(ctx)).toBe(false);
    } finally { await browser.close(); }
  });
});

describe("the committed images", () => {
  const folder = mkdtempSync(join(tmpdir(), "darkroom-images-"));
  afterAll(() => rmSync(folder, { recursive: true, force: true }));
  const put = (dir, name, text) => { mkdirSync(dir, { recursive: true }); writeFileSync(join(dir, name), text); };
  const names = (dir) => readdirSync(dir).sort();

  it("are replaced only after a run: a whole run swaps them all, a partial one only the pages it took", () => {
    const images = join(folder, "images-a"), staging = join(folder, "staging-a");
    put(images, "old-phone.png", "old"); put(images, "status-phone.png", "before");
    put(staging, "status-phone.png", "after");
    commitImages(staging, images, false);
    expect(names(images)).toEqual(["old-phone.png", "status-phone.png"]);
    expect(readFileSync(join(images, "status-phone.png"), "utf8")).toBe("after");
    commitImages(staging, images, true);
    expect(names(images)).toEqual(["status-phone.png"]);
  });

  it("are left byte for byte when copying the new set fails part way", () => {
    const images = join(folder, "images-c"), staging = join(folder, "staging-c");
    put(images, "a-phone.png", "old a"); put(images, "b-phone.png", "old b");
    put(staging, "a-phone.png", "new a"); put(staging, "c-phone.png", "new c");
    let calls = 0;
    const flaky = (from, to) => { if (++calls === 2) throw new Error("disk full"); copyFileSync(from, to); };
    expect(() => commitImages(staging, images, true, flaky)).toThrow("disk full");
    expect(names(images)).toEqual(["a-phone.png", "b-phone.png"]);
    expect(readFileSync(join(images, "a-phone.png"), "utf8")).toBe("old a");
    expect(readFileSync(join(images, "b-phone.png"), "utf8")).toBe("old b");
    expect(readdirSync(folder).filter((n) => n.startsWith("images-c."))).toEqual([]);
  });

  it("are left byte for byte when a run fails", () => {
    const images = join(folder, "images-b"), session = join(folder, "session.json");
    put(images, "status-phone.png", "baseline");
    writeFileSync(session, JSON.stringify({ cookie: "darkroom_session", token: "t" }));
    const run = spawnSync(process.execPath, [join(import.meta.dirname, "verify.mjs"), "--url", "http://127.0.0.1:9", "--session", session, "--images", images, "--out", join(folder, "out"), "status"], { encoding: "utf8", timeout: 120000 });
    expect(run.status).toBe(1);
    expect(run.stderr).toContain("FAIL:");
    expect(names(images)).toEqual(["status-phone.png"]);
    expect(readFileSync(join(images, "status-phone.png"), "utf8")).toBe("baseline");
  }, 150000);
});

describe("a download that fails after it starts", () => {
  let browser, home, base;

  beforeAll(async () => {
    home = await new Promise((resolve) => {
      const server = createServer((req, res) => {
        if (req.url === "/file") {
          res.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=x.bin", "Content-Length": "1000000" });
          res.write("partial");
          setTimeout(() => res.destroy(), 200);
          return;
        }
        res.setHeader("Content-Type", "text/html");
        res.end(`<!doctype html><title>t</title><main><h1>Home</h1><a href="/file" download>get</a></main>`);
      });
      server.listen(0, "127.0.0.1", () => resolve(server));
    });
    base = `http://127.0.0.1:${home.address().port}`;
    browser = await chromium.launch(launchOptions());
  });
  afterAll(async () => {
    await browser?.close();
    home?.close();
  });

  for (const viewport of ["desktop", "laptop"]) {
    it(`fails the visit at ${viewport}, whether or not the download is kept`, async () => {
      const out = mkdtempSync(join(tmpdir(), "darkroom-visit-"));
      try {
        const result = await visit({ browser, now: FROZEN_NOW, images: out, out, base, cookie: null }, "downloads", viewport, async (page, _shot, saveDownload) => {
          await page.goto(`${base}/`, { waitUntil: "load" });
          await runStep(page, { download: { selector: "a", name: "x" } }, null, saveDownload);
        }, { audit: false, signedIn: false });
        expect(result.problems.some((p) => p.includes("downloads @") && p.toLowerCase().includes("download"))).toBe(true);
      } finally { rmSync(out, { recursive: true, force: true }); }
    }, 30000);
  }
});

describe("a download step whose click fails", () => {
  it("ends in the flow's error path with nothing left unhandled", async () => {
    const browser = await chromium.launch(launchOptions());
    const unhandled = [];
    const listener = (e) => unhandled.push(e);
    process.on("unhandledRejection", listener);
    try {
      const page = await browser.newPage();
      await page.setContent("<main><h1>Home</h1></main>");
      await expect(runStep(page, { download: { selector: "button.nope", name: "x" }, timeout: 300 }, null, async () => {})).rejects.toThrow(/Timeout/);
      await new Promise((resolve) => setTimeout(resolve, 600));
      expect(unhandled).toEqual([]);
    } finally {
      process.off("unhandledRejection", listener);
      await browser.close();
    }
  });
});
