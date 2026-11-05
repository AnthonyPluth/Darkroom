import { describe, expect, it } from "vitest";
import { ReplaceFailed, replaceServer } from "./control-reset.mjs";

const session = () => {
  const state = { server: "old", base: "http://127.0.0.1:1", log: [] };
  return {
    state,
    deps: (extra = {}) => ({
      launch: async () => ({ server: "new", base: "http://127.0.0.1:2" }),
      open: async (fresh) => { state.log.push(`open ${fresh.base}`); },
      rollback: async () => { state.log.push("rollback"); },
      commit: async (fresh) => { state.server = fresh.server; state.base = fresh.base; state.log.push("commit"); },
      ...extra,
    }),
  };
};

describe("replaceServer", () => {
  it("opens the new browser before the new server replaces the old one", async () => {
    const { state, deps } = session();
    await replaceServer(deps());
    expect(state.log).toEqual(["open http://127.0.0.1:2", "commit"]);
    expect(state.server).toBe("new");
  });

  it("changes nothing when the new server doesn't start", async () => {
    const { state, deps } = session();
    const failure = replaceServer(deps({ launch: async () => { throw new Error("no port\nmore"); } }));
    await expect(failure).rejects.toMatchObject({ stage: "launch", message: "no port" });
    expect(state).toEqual({ server: "old", base: "http://127.0.0.1:1", log: [] });
  });

  it("rolls back and leaves the old server and data when the new browser can't open the page", async () => {
    const { state, deps } = session();
    const failure = replaceServer(deps({ open: async () => { throw new Error("navigation failed"); } }));
    await expect(failure).rejects.toBeInstanceOf(ReplaceFailed);
    await expect(failure).rejects.toMatchObject({ stage: "open" });
    expect(state).toEqual({ server: "old", base: "http://127.0.0.1:1", log: ["rollback"] });
  });

  it("still reports the open failure when the rollback itself fails", async () => {
    const { deps } = session();
    const failure = replaceServer(deps({ open: async () => { throw new Error("navigation failed"); }, rollback: async () => { throw new Error("stuck"); } }));
    await expect(failure).rejects.toMatchObject({ stage: "open", message: "navigation failed" });
  });
});
