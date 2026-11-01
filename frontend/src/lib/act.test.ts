import { describe, expect, it, vi } from "vitest";
import { act, actGet, errMsg, ignoreFailure } from "./act";

describe("errMsg", () => {
  it("is the message of an error, or of anything shaped like one", () => {
    expect(errMsg(new Error("Nope"))).toBe("Nope");
    expect(errMsg({ message: "Refused" })).toBe("Refused");
  });
  it("copes with what isn't an error", () => {
    expect(errMsg("plain")).toBe("plain");
    expect(errMsg({ message: 3 })).toBe("[object Object]");
    expect(errMsg(null)).toBe("null");
    expect(errMsg(undefined)).toBe("undefined");
  });
});

describe("ignoreFailure", () => {
  it("swallows a rejection, by name", async () => {
    await expect(Promise.reject(new Error("x")).catch(ignoreFailure)).resolves.toBeUndefined();
  });
});

describe("act", () => {
  it("resolves to true when it worked, and reports nothing", async () => {
    const onError = vi.fn();
    expect(await act(async () => undefined, { onError })).toBe(true);
    expect(onError).not.toHaveBeenCalled();
  });

  it("reports the message and resolves to false when it fails, without rejecting", async () => {
    const onError = vi.fn();
    const err = new Error("The server said no");
    expect(await act(async () => { throw err; }, { onError })).toBe(false);
    expect(onError).toHaveBeenCalledExactlyOnceWith("The server said no", err);
  });

  it("catches a throw from a function that isn't async", async () => {
    const onError = vi.fn();
    expect(await act(() => { throw new Error("Sync trouble"); }, { onError })).toBe(false);
    expect(onError).toHaveBeenCalledWith("Sync trouble", expect.any(Error));
  });

  it("holds busy for the length of the action, success or failure", async () => {
    const busy = vi.fn();
    await act(async () => undefined, { busy, onError: vi.fn() });
    await act(async () => { throw new Error("x"); }, { busy, onError: vi.fn() });
    expect(busy.mock.calls).toEqual([[true], [false], [true], [false]]);
  });
});

describe("actGet", () => {
  it("is the value when it worked and null when it failed", async () => {
    expect(await actGet(async () => 7, { onError: vi.fn() })).toBe(7);
    const onError = vi.fn();
    expect(await actGet(async () => { throw new Error("gone"); }, { onError })).toBeNull();
    expect(onError).toHaveBeenCalledWith("gone", expect.any(Error));
  });
});
