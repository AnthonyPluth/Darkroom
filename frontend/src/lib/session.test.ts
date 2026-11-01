// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./contract", () => ({ apiCall: vi.fn() }));

import { ApiError, SIGNED_OUT } from "./api";
import { apiCall } from "./contract";
import { displayName, session, watchSignOut } from "./session.svelte";

const me = { sub: "u-1", email: "sam@example.com", name: "Sam", groups: [] };

beforeEach(() => { vi.mocked(apiCall).mockReset(); });

describe("the session", () => {
  it("is signed in with who GET /api/me says", async () => {
    vi.mocked(apiCall).mockResolvedValue(me);
    await session.load();
    expect(apiCall).toHaveBeenCalledWith("/api/me");
    expect(session.current).toEqual({ kind: "signed-in", me });
  });

  it("is signed out on a 401", async () => {
    vi.mocked(apiCall).mockRejectedValue(new ApiError("signed out", 401));
    await session.load();
    expect(session.current).toEqual({ kind: "signed-out" });
  });

  it("says Darkroom is unreachable on any other failure, with the reason", async () => {
    vi.mocked(apiCall).mockRejectedValue(new ApiError("Darkroom is restarting", 503));
    await session.load();
    expect(session.current).toEqual({ kind: "unreachable", message: "Darkroom is restarting" });
  });

  it("is loading while it asks", () => {
    vi.mocked(apiCall).mockReturnValue(new Promise(() => {}));
    void session.load();
    expect(session.current).toEqual({ kind: "loading" });
  });

  it("turns signed out when any call is refused for want of sign-in, until it stops watching", async () => {
    vi.mocked(apiCall).mockResolvedValue(me);
    await session.load();
    const stop = watchSignOut();
    window.dispatchEvent(new Event(SIGNED_OUT));
    expect(session.current).toEqual({ kind: "signed-out" });
    await session.load();
    stop();
    window.dispatchEvent(new Event(SIGNED_OUT));
    expect(session.current.kind).toBe("signed-in");
  });
});

describe("displayName", () => {
  it("is the name, else the email, else a plain label", () => {
    expect(displayName(me)).toBe("Sam");
    expect(displayName({ ...me, name: "" })).toBe("sam@example.com");
    expect(displayName({ ...me, name: "", email: "" })).toBe("Signed in");
  });
});
