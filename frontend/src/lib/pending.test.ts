import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiCall } from "./contract";
import { pending, refreshPending, showPending } from "./pending.svelte";

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
    showPending(0);
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
    showPending(5);
    calls[0].resolve(8);
    await earlier;
    expect(pending.count).toBe(5);
  });

  it("shows no count when the latest refresh fails", async () => {
    showPending(6);
    const calls = deferredCalls();
    const done = refreshPending();
    calls[0].reject(new Error("offline"));
    await done;
    expect(pending.count).toBe(0);
  });
});
