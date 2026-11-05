import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiCall } from "./contract";
import { pending, refreshPending, reservePending, showPending } from "./pending.svelte";

vi.mock("./contract", () => ({ apiCall: vi.fn() }));

type Settle = { resolve: (count: number) => void; reject: (error: Error) => void };

function deferredCalls(): Settle[] {
  const calls: Settle[] = [];
  vi.mocked(apiCall).mockImplementation(
    () =>
      new Promise((resolve, reject) => {
        calls.push({ resolve: (count) => resolve({ pending: count } as never), reject });
      }) as never,
  );
  return calls;
}

describe("refreshPending", () => {
  beforeEach(() => {
    vi.mocked(apiCall).mockReset();
    showPending(reservePending(), 0);
  });

  it("shows the count the server answers", async () => {
    const calls = deferredCalls();
    const done = refreshPending();
    calls[0].resolve(4);
    await done;
    expect(pending.count).toBe(4);
  });

  it("keeps the newer count when an earlier refresh answers last", async () => {
    const calls = deferredCalls();
    const earlier = refreshPending();
    const later = refreshPending();
    calls[1].resolve(2);
    await later;
    calls[0].resolve(9);
    await earlier;
    expect(pending.count).toBe(2);
  });

  it("does not let an earlier refresh's failure reset a newer count", async () => {
    const calls = deferredCalls();
    const earlier = refreshPending();
    const later = refreshPending();
    calls[1].resolve(3);
    await later;
    calls[0].reject(new Error("offline"));
    await earlier;
    expect(pending.count).toBe(3);
  });

  it("keeps the count the Review page shows over a refresh started before it", async () => {
    const calls = deferredCalls();
    const earlier = refreshPending();
    showPending(reservePending(), 5);
    calls[0].resolve(8);
    await earlier;
    expect(pending.count).toBe(5);
  });

  it("drops a Review count whose load started before a newer refresh", async () => {
    const review = reservePending();
    const calls = deferredCalls();
    const later = refreshPending();
    calls[0].resolve(2);
    await later;
    showPending(review, 7);
    expect(pending.count).toBe(2);
  });

  it("drops a Review count whose load started before a newer Review load", () => {
    const earlier = reservePending();
    const later = reservePending();
    showPending(later, 4);
    showPending(earlier, 9);
    expect(pending.count).toBe(4);
  });

  it("shows no count when the latest refresh fails", async () => {
    showPending(reservePending(), 6);
    const calls = deferredCalls();
    const done = refreshPending();
    calls[0].reject(new Error("offline"));
    await done;
    expect(pending.count).toBe(0);
  });
});
