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

describe("a poll that races an explicit load", () => {
  const deferred = () => {
    const releases: Array<(value: number) => void> = [];
    const loader = new Loader(() => new Promise<number>((resolve) => releases.push(resolve)));
    return { loader, releases };
  };

  it("drops a poll that started before an explicit load that started after it, even when it finishes before that load settles", async () => {
    const { loader, releases } = deferred();
    const polled = loader.poll();
    const reloaded = loader.load(true);
    releases[0](1);
    await polled;
    expect(loader.current).toEqual({ kind: "loading" });
    releases[1](2);
    await reloaded;
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
  });

  it("drops the stale poll without losing the data already on screen", async () => {
    const { loader, releases } = deferred();
    const first = loader.load();
    releases[0](1);
    await first;
    const polled = loader.poll();
    const reloaded = loader.load(true);
    releases[1](99);
    await polled;
    expect(loader.current).toEqual({ kind: "ready", data: 1 });
    releases[2](2);
    await reloaded;
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
  });

  it("keeps the failure of an explicit load when a stale poll succeeds first", async () => {
    const releases: Array<(value: number) => void> = [];
    const rejects: Array<(err: Error) => void> = [];
    let calls = 0;
    const loader = new Loader(() => new Promise<number>((resolve, reject) => {
      releases[calls] = resolve;
      rejects[calls++] = reject;
    }));
    const polled = loader.poll();
    const reloaded = loader.load(true);
    releases[0](1);
    await polled;
    rejects[1](new Error("down"));
    await reloaded;
    expect(loader.current).toEqual({ kind: "error", message: "down" });
  });

  it("applies a poll that started after the explicit load", async () => {
    const { loader, releases } = deferred();
    const reloaded = loader.load(true);
    const polled = loader.poll();
    releases[1](2);
    await polled;
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
    releases[0](1);
    await reloaded;
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
  });

  it("still applies polls on their own, as the polling does for a page that never reloads", async () => {
    let version = 0;
    const loader = new Loader(slow(() => ++version));
    void loader.load();
    const timer = setInterval(() => void loader.poll(), INTERVAL);
    await vi.advanceTimersByTimeAsync(SLOW);
    expect(loader.current).toEqual({ kind: "ready", data: 1 });
    await vi.advanceTimersByTimeAsync(INTERVAL);
    expect(loader.current).toEqual({ kind: "ready", data: 2 });
    clearInterval(timer);
  });
});
