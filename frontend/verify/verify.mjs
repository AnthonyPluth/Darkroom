import { chromium } from "@playwright/test";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { animationsRunning, auditPage } from "./a11y.mjs";
import { planVisits, runPool, summarize, urlsOf } from "./jobs.mjs";
import { LOOPBACK_HOSTS, ROUTES, SIGNED_OUT_PAGES, allowedRequest, routeOf, sameServer } from "./routes.mjs";

export { LOOPBACK_HOSTS, ROUTES, SIGNED_OUT_PAGES, allowedRequest, routeOf, sameServer };

export const VIEWPORTS = { phone: { width: 440, height: 956 }, desktop: { width: 1280, height: 800 }, laptop: { width: 1024, height: 768 }, wide: { width: 1440, height: 900 } };
export const DEFAULT_VIEWPORTS = ["phone", "desktop"];
export const IMAGE_VIEWPORTS = ["phone", "desktop"];
export const CHROMIUM_ARGS = ["--force-color-profile=srgb", "--disable-lcd-text", "--num-raster-threads=1", "--disable-partial-raster"];
export const launchOptions = () => ({ executablePath: findChromium(), args: CHROMIUM_ARGS });
export const contextOptions = (viewport, { reducedMotion = "no-preference" } = {}) => ({ viewport: VIEWPORTS[viewport], colorScheme: "dark", timezoneId: "UTC", reducedMotion });
export const SIGNED_OUT_CONSOLE = "the server responded with a status of 401";

export async function prepareContext(ctx, now) {
  await ctx.clock.setFixedTime(now);
  await ctx.route("**/favicon.ico", (route) => route.fulfill({ status: 204 }));
}

export const loadedWhen = (signedIn) => (signedIn ? "networkidle" : "load");
export const SETTLE_MS = 300;
export const FROZEN_NOW = "2026-06-15T12:00:00Z";
export const PAGES = ["sign-in", "status", "people"];
export const SESSION_FILE = "demo-session.json";
export const LOADING = "[role=status]:has-text('Loading')";

export function findChromium(env = process.env, exists = existsSync, list = readdirSync) {
  const root = env.PLAYWRIGHT_BROWSERS_PATH || "/opt/pw-browsers";
  if (!exists(root)) return undefined;
  for (const dir of list(root).filter((d) => d.startsWith("chromium-")).sort().reverse()) {
    for (const sub of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
      const p = join(root, dir, sub);
      if (exists(p)) return p;
    }
  }
  return undefined;
}

export const screenshotFile = (name, viewport) => `${name}-${viewport}.png`;
export const writesImage = (viewport) => IMAGE_VIEWPORTS.includes(viewport);

export function sessionCookie(file, read = readFileSync) {
  const { cookie, token } = JSON.parse(read(file, "utf8"));
  if (typeof cookie !== "string" || typeof token !== "string") throw new Error(`${file} has no cookie and token`);
  return { name: cookie, value: token };
}

export const signIn = (ctx, base, cookie) => ctx.addCookies([{ ...cookie, url: base, httpOnly: true, sameSite: "Lax" }]);

export const hostOf = (url) => { try { return new URL(url).host || url; } catch { return "an address that is not a URL"; } };

export async function blockOtherHosts(ctx, base) {
  const blocked = [];
  await ctx.route((url) => !allowedRequest(url.href, base), (route) => {
    blocked.push(hostOf(route.request().url()));
    return route.abort("blockedbyclient");
  });
  return blocked;
}

const ACTIONS = { goto: "string", click: "string", click_if_present: "string", hover: "string", select: "object", fill: "object", press: "object", upload: "object", download: "object", scroll_to: "string", sign_out: "boolean", reload: "boolean", wait_for: "string", expect_text: "object", screenshot: "string" };

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const unknownPages = (names) => names.filter((n) => !PAGES.includes(n));

export function flowProblems(flow) {
  const out = [];
  if (!flow || typeof flow.name !== "string" || !/^[\w-]+$/.test(flow.name)) out.push("needs a name of letters, digits, - or _");
  if (!Array.isArray(flow?.steps) || !flow.steps.length) out.push("needs a list of steps");
  for (const [i, s] of (flow?.steps ?? []).entries()) {
    const keys = Object.keys(s).filter((k) => k !== "timeout");
    if (keys.length !== 1 || !(keys[0] in ACTIONS)) out.push(`step ${i + 1} must have exactly one of ${Object.keys(ACTIONS).join(", ")}`);
    else if (typeof s[keys[0]] !== ACTIONS[keys[0]]) out.push(`step ${i + 1}: ${keys[0]} takes a ${ACTIONS[keys[0]]}`);
    else if (keys[0] === "download" && (typeof s.download?.selector !== "string" || !/^[\w-]+$/.test(s.download?.name ?? ""))) out.push(`step ${i + 1}: download needs a selector and a name of letters, digits, - or _`);
  }
  if (flow?.page !== undefined && !PAGES.includes(flow.page)) out.push(`unknown page ${flow.page}`);
  for (const v of flow?.viewports ?? []) if (!(v in VIEWPORTS)) out.push(`unknown viewport ${v}`);
  if (flow?.allow_console !== undefined && !(Array.isArray(flow.allow_console) && flow.allow_console.every((t) => typeof t === "string"))) out.push("allow_console is a list of texts");
  if (flow?.scheme !== undefined) out.push("scheme is not an option: the app is dark only");
  if (flow?.host !== undefined) out.push("host is not an option: the session's cookie belongs to 127.0.0.1");
  return out;
}

export async function runStep(page, step, shot, saveDownload) {
  const timeout = step.timeout ?? 10000;
  if ("sign_out" in step) {
    await page.context().clearCookies();
    await page.reload({ waitUntil: "load", timeout });
  } else if ("reload" in step) {
    await page.reload({ waitUntil: "domcontentloaded", timeout });
  } else if ("goto" in step) {
    await page.goto(step.goto.startsWith("/") ? new URL(step.goto, page.url()).href : step.goto);
  } else if ("click" in step) {
    await page.locator(step.click).first().click({ timeout });
  } else if ("click_if_present" in step) {
    const target = page.locator(step.click_if_present).first();
    const present = await target.waitFor({ state: "visible", timeout: step.timeout ?? 1000 }).then(() => true, () => false);
    if (present) await target.click({ timeout });
  } else if ("hover" in step) {
    await page.locator(step.hover).first().hover({ timeout });
  } else if ("select" in step) {
    await page.locator(step.select.selector).first().selectOption({ index: step.select.index }, { timeout });
  } else if ("fill" in step) {
    await page.locator(step.fill.selector).first().fill(step.fill.text, { timeout });
  } else if ("press" in step) {
    await page.locator(step.press.selector).first().press(step.press.key, { timeout });
  } else if ("upload" in step) {
    await page.locator(step.upload.selector).first().setInputFiles(join(REPO, step.upload.file), { timeout });
  } else if ("download" in step) {
    const pending = page.waitForEvent("download", { timeout });
    await page.locator(step.download.selector).first().click({ timeout });
    await saveDownload(step.download.name, await pending);
  } else if ("scroll_to" in step) {
    await page.locator(step.scroll_to).first().evaluate((el) => el.scrollIntoView({ block: "start" }), undefined, { timeout });
  } else if ("wait_for" in step) {
    await page.locator(step.wait_for).first().waitFor({ timeout });
  } else if ("expect_text" in step) {
    const el = page.locator(step.expect_text.selector).first();
    await el.waitFor({ timeout });
    const until = Date.now() + timeout;
    let got = await el.innerText();
    while (!got.includes(step.expect_text.text) && Date.now() < until) {
      await page.waitForTimeout(100);
      got = await el.innerText();
    }
    if (!got.includes(step.expect_text.text)) throw new Error(`expected "${step.expect_text.text}" in ${step.expect_text.selector}, found "${got.slice(0, 80)}"`);
  } else if ("screenshot" in step) {
    await shot(step.screenshot);
  }
}

const NEUTRAL_STEPS = ["wait_for", "screenshot", "scroll_to"];

export function checksAfter(checks, step) {
  const action = Object.keys(step).find((k) => k !== "timeout");
  if (action === "expect_text") return [...checks, step];
  return NEUTRAL_STEPS.includes(action) ? checks : [];
}

export async function recheck(page, checks, timeout = 2000) {
  for (const step of checks) {
    try { await runStep(page, { ...step, timeout }, null, null); }
    catch (e) { return `${JSON.stringify(step).slice(0, 80)} no longer holds after the screenshot: ${String(e.message).split("\n")[0]}`; }
  }
  return null;
}

export async function settlePage(page, name, where, notes, scrollToTop = true) {
  if (scrollToTop) await page.evaluate(`window.scrollTo({ top: 0, left: 0, behavior: "instant" })`);
  const signedIn = (await page.context().cookies()).length > 0;
  if (signedIn) {
    try { await page.waitForLoadState("networkidle", { timeout: 3000 }); } catch { notes.push(`${where}: still loading after 3 s when ${name} was captured`); }
  } else {
    await page.waitForLoadState("load", { timeout: 3000 }).catch(() => notes.push(`${where}: still loading after 3 s when ${name} was captured`));
  }
  await page.evaluate("document.fonts.ready");
  try { await page.waitForFunction(() => globalThis.document.getAnimations().every((a) => a.playState !== "running" || a.effect?.getComputedTiming().iterations === Infinity), undefined, { timeout: 3000 }); }
  catch { notes.push(`${where}: an animation was still running after 3 s when ${name} was captured`); }
  await page.locator(LOADING).first().waitFor({ state: "hidden", timeout: 3000 }).catch(() => notes.push(`${where}: a Loading indicator was still showing after 3 s when ${name} was captured`));
  await page.waitForTimeout(SETTLE_MS);
}

export async function visit(env, label, viewport, work, { audit = true, reducedMotion = "no-preference", allowConsole = [], signedIn = true } = {}) {
  const { browser, now, images, out, base, cookie } = env;
  const ctx = await browser.newContext(contextOptions(viewport, { reducedMotion }));
  try {
    await prepareContext(ctx, now);
    const blocked = await blockOtherHosts(ctx, base);
    if (signedIn && cookie) await signIn(ctx, base, cookie);
    const page = await ctx.newPage();
    const where = `${label} @ ${viewport}`;
    const problems = [];
    const notes = [];
    const consoleErrors = [];
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
    page.on("pageerror", (e) => consoleErrors.push(`uncaught: ${e.message}`));
    page.on("response", (r) => { if (r.status() >= 500) problems.push(`${where}: ${r.status()} from ${r.request().method()} ${new URL(r.url()).pathname}`); });
    page.on("requestfailed", (r) => { if (r.failure()?.errorText !== "net::ERR_ABORTED" && allowedRequest(r.url(), base)) notes.push(`${where}: request failed (${r.failure()?.errorText}) ${new URL(r.url()).pathname}`); });
    const files = [];
    const shot = async (name, dir = images) => {
      if (!writesImage(viewport)) return;
      const file = screenshotFile(name, viewport);
      await settlePage(page, name, where, notes);
      await page.screenshot({ path: join(dir, file), fullPage: true, animations: "disabled" });
      files.push(file);
    };
    const saveDownload = async (name, download, dir = images) => {
      if (!writesImage(viewport)) return;
      const file = screenshotFile(name, viewport);
      await download.saveAs(join(dir, file));
      files.push(file);
    };
    try {
      await work(page, shot, saveDownload);
      if (audit) {
        const found = await auditPage(page, viewport);
        for (const p of found.problems) problems.push(`${where}: a11y ${p}`);
        for (const n of found.notes) notes.push(`${where}: a11y ${n}`);
      }
    } catch (e) {
      problems.push(`${where}: ${String(e.message).split("\n")[0]}`);
      await shot(`${label.replace(/\W+/g, "-")}-FAILED`, out).catch(() => {});
    }
    const allowed = signedIn ? allowConsole : [...allowConsole, SIGNED_OUT_CONSOLE];
    for (const c of consoleErrors.filter((c) => !allowed.some((t) => c.includes(t)))) problems.push(`${where}: console error: ${c.slice(0, 300)}`);
    for (const host of new Set(blocked)) problems.push(`${where}: the page asked for ${host}, which isn't the session's server: the request was blocked before it was sent`);
    return { screenshots: files, consoleErrors, problems, notes };
  } finally { await ctx.close(); }
}

export function runVisit(env, v) {
  const { base } = env;
  if (v.kind === "flow") {
    const { flow } = v;
    return visit(env, `flow ${flow.name}`, v.viewport, async (page, shot, saveDownload) => {
      await page.goto(`${base}${routeOf(flow.page ?? "status")}`, { waitUntil: loadedWhen(!SIGNED_OUT_PAGES.includes(flow.page)) });
      let checks = [];
      const shotChecked = async (file) => {
        await shot(file, env.out);
        const lost = await recheck(page, checks);
        if (lost) throw new Error(lost);
      };
      for (const [i, step] of flow.steps.entries()) {
        try { await runStep(page, step, (n) => shotChecked(`flow-${flow.name}-${n}`), (n, d) => saveDownload(`flow-${flow.name}-${n}`, d, env.out)); }
        catch (e) { throw new Error(`step ${i + 1} (${JSON.stringify(step).slice(0, 80)}): ${String(e.message).split("\n")[0]}`, { cause: e }); }
        checks = checksAfter(checks, step);
      }
      await shotChecked(`flow-${flow.name}`);
    }, { allowConsole: flow.allow_console, signedIn: !SIGNED_OUT_PAGES.includes(flow.page) });
  }
  if (v.kind === "reduced") {
    return visit(env, `reduced motion ${v.name}`, v.viewport, async (page) => {
      await page.goto(`${base}${routeOf(v.name)}`, { waitUntil: loadedWhen(!SIGNED_OUT_PAGES.includes(v.name)) });
      const moving = await animationsRunning(page);
      if (moving.length) throw new Error(`animations still run under prefers-reduced-motion: ${moving.slice(0, 5).join(", ")}`);
    }, { audit: false, reducedMotion: "reduce", signedIn: !SIGNED_OUT_PAGES.includes(v.name) });
  }
  return visit(env, v.name, v.viewport, async (page, shot) => {
    await page.goto(`${base}${routeOf(v.name)}`, { waitUntil: loadedWhen(!SIGNED_OUT_PAGES.includes(v.name)) });
    await shot(v.name, env.images);
  }, { signedIn: !SIGNED_OUT_PAGES.includes(v.name) });
}

export function readFlows(flowsDir) {
  if (!existsSync(flowsDir)) return { flows: [] };
  const flows = [];
  for (const f of readdirSync(flowsDir).filter((x) => x.endsWith(".json")).sort()) {
    let flow;
    try { flow = JSON.parse(readFileSync(join(flowsDir, f), "utf8")); } catch (e) { return { error: `flow ${f} isn't valid JSON (${e.message})` }; }
    const bad = flowProblems(flow);
    if (bad.length) return { error: `flow ${f}: ${bad.join("; ")}` };
    flows.push(flow);
  }
  return { flows };
}

async function main() {
  const args = process.argv.slice(2);
  const opt = (name, fallback) => { const i = args.indexOf(name); return i < 0 ? fallback : args.splice(i, 2)[1]; };
  const optAll = (name) => { const found = []; for (let i = args.indexOf(name); i >= 0; i = args.indexOf(name)) found.push(args.splice(i, 2)[1]); return found; };
  const given = optAll("--url").flatMap(urlsOf);
  const urls = [...(given.length ? given : urlsOf(process.env.DARKROOM_VERIFY_URL))].filter((u, i, all) => all.indexOf(u) === i);
  const here = dirname(fileURLToPath(import.meta.url));
  const out = opt("--out", join(here, "../../artifacts/verify"));
  const images = opt("--images", join(here, "../../docs/screenshots"));
  const now = new Date(opt("--now", process.env.DARKROOM_VERIFY_NOW ?? FROZEN_NOW));
  const flowsDir = opt("--flows", join(here, "flows"));
  const sessionFile = opt("--session", process.env.DARKROOM_VERIFY_SESSION);
  const only = optAll("--flow").flatMap(urlsOf);
  if (!urls.length) { console.error("verify: no server address (--url or DARKROOM_VERIFY_URL)"); process.exit(2); }
  if (!sessionFile) { console.error("verify: no demo session file (--session or DARKROOM_VERIFY_SESSION)"); process.exit(2); }
  const unknown = unknownPages(args);
  if (unknown.length) { console.error(`verify: no such page: ${unknown.join(", ")} (pages: ${PAGES.join(", ")})`); process.exit(2); }
  const { flows: everyFlow, error: flowError } = readFlows(flowsDir);
  if (flowError) { console.error(`verify: ${flowError}`); process.exit(2); }
  const missing = only.filter((n) => !everyFlow.some((f) => f.name === n));
  if (missing.length) { console.error(`verify: no such flow: ${missing.join(", ")}`); process.exit(2); }
  let cookie;
  try { cookie = sessionCookie(sessionFile); } catch (e) { console.error(`verify: couldn't read the demo session (${e.message})`); process.exit(2); }
  const pages = args.length ? args : PAGES;
  const flows = only.length ? everyFlow.filter((f) => only.includes(f.name)) : everyFlow;
  const visits = planVisits({ pages, flows, viewports: DEFAULT_VIEWPORTS, ownPages: [], filtered: args.length > 0 || only.length > 0, flowsOnly: only.length > 0 });

  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  if (!args.length && !only.length) rmSync(images, { recursive: true, force: true });
  mkdirSync(images, { recursive: true });
  const started = Date.now();
  const { outcomes, failures } = await runPool({
    visits,
    workers: urls.length,
    prepare: async (worker) => ({ browser: await chromium.launch(launchOptions()), base: urls[worker], cookie, now, images, out }),
    work: (v, env) => runVisit(env, v),
    release: (env) => env.browser.close(),
  });
  const { problems, notes, results } = summarize({ outcomes, failures, urls });
  const seconds = Math.round((Date.now() - started) / 1000);

  writeFileSync(join(out, "report.json"), JSON.stringify({ base: urls[0], instances: urls, seconds, results, problems, notes }, null, 2));
  const shots = results.reduce((n, r) => n + r.screenshots.length, 0);
  console.log(`verify: ${results.length} visits, ${shots} screenshots in ${images}, ${seconds} s on ${urls.length} server${urls.length === 1 ? "" : "s"}`);
  for (const n of notes) console.log(`  note: ${n}`);
  for (const p of problems) console.error(`  FAIL: ${p}`);
  console.log(problems.length ? `verify: FAILED (${problems.length} problem${problems.length === 1 ? "" : "s"})` : "verify: OK, no console errors, 5xx responses or accessibility problems");
  process.exit(problems.length ? 1 : 0);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main().catch((e) => { console.error(e); process.exit(2); });
