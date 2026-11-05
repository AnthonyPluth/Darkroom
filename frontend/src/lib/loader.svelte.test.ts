import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Loader } from "./loader.svelte";

const INTERVAL = 10_000;
const SLOW = 15_000;

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function slow<T>(answer: () => T): () => Promise<T> {
  return () => new Promise<T>((resolve, reject) => setTimeout(() => {
    try { resolve(answer()); } catch (err) { reject(err); }
  }, SLOW));
}

function poll(loader: { load(quiet?: boolean): Promise<void> }): ReturnType<typeof setInterval> {
  void loader.load();
  return setInterval(() => void loader.load(true), INTERVAL);
}

describe("polling when every load takes longer than the interval", () => {
  it("shows the first result instead of staying on loading", async () => {
    const loader = new Loader(slow(() => "first"));
    const timer = poll(loader);
    await vi.advanceTimersByTimeAsync(SLOW);
    expect(loader.current).toEqual({ kind: "ready", data: "first" });
    clearInterval(timer);
  });

  it("keeps refreshing a page that already has data", async () => {
    let version = 0;
    const loader = new Loader(slow(() => ++version));
    const timer = poll(loader);
    await vi.advanceTimersByTimeAsync(SLOW);
    expect(loader.current).toEqual({ kind: "ready", data: 1 });
    await vi.advanceTimersByTimeAsync(INTERVAL);
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
    await vi.advanceTimersByTimeAsync(INTERVAL);
    expect(loader.current).toEqual({ kind: "ready", data: 3 });
    clearInterval(timer);
  });

  it("shows repeated failures", async () => {
    let attempt = 0;
    const loader = new Loader(slow(() => { throw new Error(`down ${++attempt}`); }));
    const timer = poll(loader);
    await vi.advanceTimersByTimeAsync(SLOW);
    expect(loader.current).toEqual({ kind: "error", message: "down 1" });
    await vi.advanceTimersByTimeAsync(INTERVAL);
    expect(loader.current).toEqual({ kind: "error", message: "down 2" });
    clearInterval(timer);
  });
});

describe("overlapping loads", () => {

  it("discards an older load that finishes after a newer one", async () => {
    const applied: number[] = [];
    const releases: Array<(value: number) => void> = [];
    const loader = new Loader(() => new Promise<number>((resolve) => releases.push(resolve)), (data) => applied.push(data));
    const older = loader.load();
    const newer = loader.load(true);
    releases[1](2);
    await newer;
    releases[0](1);
    await older;
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
    expect(applied).toEqual([2]);
  });

  it("applies an older load that finishes first, then the newer one", async () => {
    const applied: number[] = [];
    const releases: Array<(value: number) => void> = [];
    const loader = new Loader(() => new Promise<number>((resolve) => releases.push(resolve)), (data) => applied.push(data));
    const older = loader.load();
    const newer = loader.load(true);
    releases[0](1);
    await older;
    expect(loader.current).toEqual({ kind: "ready", data: 1 });
    releases[1](2);
    await newer;
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
    expect(applied).toEqual([1, 2]);
  });

  it("keeps a newer failure when an older load succeeds afterwards", async () => {
    const releases: Array<(value: number) => void> = [];
    const rejects: Array<(err: Error) => void> = [];
    let calls = 0;
    const loader = new Loader(() => new Promise<number>((resolve, reject) => {
      releases[calls] = resolve;
      rejects[calls++] = reject;
    }));
    const older = loader.load(true);
    const newer = loader.load(true);
    rejects[1](new Error("down"));
    await newer;
    releases[0](1);
    await older;
    expect(loader.current).toEqual({ kind: "error", message: "down" });
  });
});
