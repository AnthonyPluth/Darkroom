// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, OFFLINE, SESSION_ENDED, SIGNED_OUT, signInUrl, UNREACHABLE } from "./api";

const reply = (body: unknown, status = 200) => Promise.resolve(new Response(body === undefined ? null : JSON.stringify(body), { status }));
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => { fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("GETs JSON with the same-origin cookies and no body", async () => {
    fetchMock.mockReturnValue(reply({ a: 1 }));
    expect(await api("/api/x")).toEqual({ a: 1 });
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/x");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBeUndefined();
    expect(init.headers).not.toHaveProperty("Content-Type");
  });

  it("sends a body as JSON with its method", async () => {
    fetchMock.mockReturnValue(reply({ ok: true }));
    await api("/api/x", { method: "POST", body: { kind: "takeout" } });
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe('{"kind":"takeout"}');
  });

  it("answers an empty object for a 204", async () => {
    fetchMock.mockReturnValue(Promise.resolve(new Response(null, { status: 204 })));
    expect(await api("/api/x", { method: "DELETE" })).toEqual({});
  });

  it("throws the server's own error message, with the status and the reply", async () => {
    fetchMock.mockReturnValue(reply({ error: "kind must be one of takeout", field: "kind" }, 400));
    const err = await api("/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ message: "kind must be one of takeout", status: 400, data: { error: "kind must be one of takeout", field: "kind" } });
  });

  it("says what failed when the server gives no message", async () => {
    fetchMock.mockReturnValue(reply("not an object", 500));
    await expect(api("/api/x")).rejects.toMatchObject({ message: "Request failed (500)", status: 500, data: {} });
    fetchMock.mockReturnValue(reply(undefined, 418));
    await expect(api("/api/x", { failed: "Couldn't start the export" })).rejects.toMatchObject({ message: "Couldn't start the export (418)" });
  });

  it("says Darkroom is restarting on a gateway error", async () => {
    for (const status of [502, 503, 504]) {
      fetchMock.mockReturnValue(Promise.resolve(new Response("<html>bad gateway</html>", { status })));
      await expect(api("/api/x")).rejects.toMatchObject({ message: UNREACHABLE, status });
    }
  });

  it("turns a network failure into an offline message", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(api("/api/x")).rejects.toMatchObject({ message: OFFLINE, status: 0 });
  });

  it("passes on anything else thrown", async () => {
    const odd = new RangeError("odd");
    fetchMock.mockRejectedValue(odd);
    await expect(api("/api/x")).rejects.toBe(odd);
  });

  it("announces a 401 so the shell shows the signed-out view, and throws", async () => {
    const heard = vi.fn();
    window.addEventListener(SIGNED_OUT, heard);
    fetchMock.mockReturnValue(reply({ error: "sign in required" }, 401));
    await expect(api("/api/x")).rejects.toMatchObject({ message: SESSION_ENDED, status: 401 });
    expect(heard).toHaveBeenCalledOnce();
    window.removeEventListener(SIGNED_OUT, heard);
  });
});

describe("signInUrl", () => {
  it("comes back to the page the person was on", () => {
    history.replaceState(null, "", "/review?account=2");
    expect(signInUrl()).toBe("/auth/login?next=%2Freview%3Faccount%3D2");
    history.replaceState(null, "", "/");
    expect(signInUrl()).toBe("/auth/login?next=%2F");
  });
});
