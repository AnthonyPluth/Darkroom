import { chromium } from "@playwright/test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { auditPage, axeProblems, focusProblems, MIN_TAP, tapProblems } from "./a11y.mjs";
import { launchOptions } from "./verify.mjs";

describe("axeProblems", () => {
  it("names each failing element with its rule", () => {
    const violations = [{ id: "color-contrast", help: "Elements must meet contrast", nodes: [{ target: ["p.muted"] }, { target: ["iframe", "span"] }] }];
    expect(axeProblems(violations)).toEqual(["color-contrast: p.muted (Elements must meet contrast)", "color-contrast: iframe span (Elements must meet contrast)"]);
    expect(axeProblems([])).toEqual([]);
  });
});

describe("tapProblems", () => {
  const boxes = [{ target: "button A", width: MIN_TAP, height: MIN_TAP }, { target: "button B", width: 32, height: 44 }, { target: "a C", width: 100, height: 20.4 }];
  it("flags targets under 44 px on the phone only", () => {
    expect(tapProblems(boxes, "phone")).toEqual([`tap-target: button B is 32×44 px, under ${MIN_TAP}`, `tap-target: a C is 100×20 px, under ${MIN_TAP}`]);
    expect(tapProblems(boxes, "laptop")).toEqual([]);
    expect(tapProblems(boxes, "desktop")).toEqual([]);
  });
});

describe("focusProblems", () => {
  it("flags a stop with no ring", () => {
    expect(focusProblems([{ target: "a X", ringed: true }, { target: "g Y", ringed: false }])).toEqual(["focus: g Y shows no focus ring"]);
  });
});

describe("auditPage", () => {
  let browser;
  beforeAll(async () => { browser = await chromium.launch(launchOptions()); });
  afterAll(async () => { await browser?.close(); });

  const audit = async (html, viewport, tapTargetsFail) => {
    const page = await browser.newPage();
    try {
      await page.setContent(html);
      return await auditPage(page, viewport, tapTargetsFail);
    } finally { await page.close(); }
  };
  const clean = "<!doctype html><html lang=en><title>t</title><main><h1>Home</h1><a href='/x' style='display:block;min-height:48px;min-width:48px'>Go</a></main>";
  const broken = "<!doctype html><html lang=en><title>t</title><main><h1>Home</h1><img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='><button style='height:20px;width:30px'>x</button></main>";

  it("passes a clean page", async () => {
    expect(await audit(clean, "phone", true)).toEqual({ problems: [], notes: [] });
  });

  it("fails a page with an image with no text alternative", async () => {
    const found = await audit(broken, "desktop", true);
    expect(found.problems.some((p) => p.startsWith("image-alt:"))).toBe(true);
  });

  it("fails a small tap target at phone width when tap targets fail, and lists it as a note when they don't", async () => {
    const strict = await audit(broken, "phone", true);
    expect(strict.problems.some((p) => p.startsWith("tap-target:"))).toBe(true);
    const lenient = await audit(broken, "phone", false);
    expect(lenient.problems.some((p) => p.startsWith("tap-target:"))).toBe(false);
    expect(lenient.notes.some((n) => n.startsWith("tap-target:"))).toBe(true);
    expect((await audit(broken, "desktop", true)).problems.some((p) => p.startsWith("tap-target:"))).toBe(false);
  });
});
