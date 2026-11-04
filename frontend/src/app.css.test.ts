import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./app.css", import.meta.url), "utf8");
const theme = css.slice(css.indexOf("@theme {"), css.indexOf("}", css.indexOf("@theme {")));
const declared = Object.fromEntries([...theme.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));

const SIGNED_OFF_COLOURS = {
  "color-canvas": "#16140f",
  "color-rail": "#1b1813",
  "color-surface": "#1f1c17",
  "color-raised": "#2b2721",
  "color-ink": "#f1ece2",
  "color-ink-muted": "#b3ab9d",
  "color-line": "#3a352c",
  "color-line-strong": "#7a7260",
  "color-accent": "#e2693f",
  "color-accent-ink": "#1a0e08",
  "color-ok": "#7cc38f",
  "color-ok-bg": "#1d2b20",
  "color-warn": "#e0b253",
  "color-warn-bg": "#332a12",
  "color-danger": "#f08a84",
  "color-danger-bg": "#3a1a1a",
};

describe("the theme", () => {
  it("holds exactly the dark colour tokens signed off in docs/design/web/00-tokens.dc.html", () => {
    const colours = Object.fromEntries(Object.entries(declared).filter(([name]) => name.startsWith("color-")));
    expect(colours).toEqual(SIGNED_OFF_COLOURS);
  });

  it("is dark only", () => {
    expect(css).toMatch(/:root\s*{\s*color-scheme:\s*dark;\s*}/);
    expect(css).not.toContain("prefers-color-scheme");
  });

  it("keeps no placeholder token or marker", () => {
    expect(css).not.toContain("placeholder");
    expect(css).not.toContain("--design");
  });

  it("names the fonts, the spacing unit and the radii of the design", () => {
    expect(declared["font-sans"]).toContain("system-ui");
    expect(declared["font-mono"]).toContain("ui-monospace");
    expect(declared["spacing"]).toBe("0.25rem");
    expect(["radius-sm", "radius-md", "radius-lg"].map((name) => declared[name])).toEqual(["0.25rem", "0.5rem", "0.75rem"]);
  });
});
