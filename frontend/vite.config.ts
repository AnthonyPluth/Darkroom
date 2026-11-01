/// <reference types="vitest/config" />
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { svelteTesting } from "@testing-library/svelte/vite";
import path from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  plugins: [tailwindcss(), svelte(), svelteTesting()],
  resolve: { alias: { $lib: path.resolve("./src/lib") } },
  build: { outDir: "../darkroom/static/app", emptyOutDir: true, sourcemap: false, modulePreload: { polyfill: false } },
  test: {
    setupFiles: ["src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,svelte}"],
      exclude: ["src/**/*.test.ts", "src/**/*.d.ts", "src/main.ts", "src/test/**", "src/lib/api-types.ts"],
      reporter: ["text-summary"],
      thresholds: { statements: 90, lines: 90, functions: 85, branches: 80 },
    },
  },
  server: {
    proxy: Object.fromEntries(["/api", "/auth", "/healthz"].map((p) => [p, "http://127.0.0.1:8000"])),
  },
});
