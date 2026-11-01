import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./app.css", import.meta.url), "utf8");
const theme = css.slice(css.indexOf("@theme {"), css.indexOf("}", css.indexOf("@theme {")));
const tokens = [...theme.matchAll(/--([\w-]+):/g)].map((m) => m[1]);

describe("the theme", () => {
  it("is marked as a placeholder, to be replaced from the design signed off in #16", () => {
    expect(css).toContain('--design: "placeholder";');
    expect(css).toMatch(/--design-replaced-by: "[^"]*#16[^"]*#17[^"]*";/);
  });

  it("names every token a placeholder, so none outlives the real design by accident", () => {
    expect(tokens.length).toBeGreaterThan(0);
    for (const token of tokens) expect(token).toMatch(/^(color|font)-placeholder-/);
  });
});
