import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({ api: vi.fn() }));

import { api } from "./api";
import { apiCall } from "./contract";

beforeEach(() => { vi.mocked(api).mockReset(); vi.mocked(api).mockResolvedValue({}); });

describe("apiCall", () => {
  it("GETs a route through api() with no options", async () => {
    vi.mocked(api).mockResolvedValue({ sub: "s", email: "e", name: "n", groups: [] });
    const me = await apiCall<"GET /api/me">("/api/me");
    expect(me.groups).toEqual([]);
    expect(api).toHaveBeenCalledWith("/api/me");
  });

  it("sends the typed body with the route's method", async () => {
    await apiCall<"POST /api/browser/jobs">("/api/browser/jobs", { method: "POST", body: { account_id: 1, kind: "takeout" } });
    expect(api).toHaveBeenCalledWith("/api/browser/jobs", { method: "POST", body: { account_id: 1, kind: "takeout" } });
  });

  it("fills a route's {id}", async () => {
    await apiCall<"POST /internal/browser/jobs/{id}/progress">("/internal/browser/jobs/12/progress", { method: "POST", body: { message: "Downloading" } });
    expect(api).toHaveBeenCalledWith("/internal/browser/jobs/12/progress", { method: "POST", body: { message: "Downloading" } });
  });

  it("takes a query on the address", async () => {
    await apiCall<"GET /api/browser/jobs">("/api/browser/jobs?account=2", { failed: "Couldn't load the jobs" });
    expect(api).toHaveBeenCalledWith("/api/browser/jobs?account=2", { failed: "Couldn't load the jobs" });
  });

  it("refuses at compile time what the contract doesn't allow", () => {
    const wrong = () => {
      // @ts-expect-error: the route needs its body
      void apiCall<"POST /api/browser/jobs">("/api/browser/jobs", { method: "POST" });
      // @ts-expect-error: a GET takes no body
      void apiCall<"GET /api/me">("/api/me", { body: { x: 1 } });
      // @ts-expect-error: the address must be the route's
      void apiCall<"GET /api/me">("/api/status");
      // @ts-expect-error: not a route in the contract
      void apiCall<"GET /api/nothing">("/api/nothing");
    };
    expect(wrong).toBeTypeOf("function");
  });
});
