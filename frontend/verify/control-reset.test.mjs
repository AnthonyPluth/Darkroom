import { chromium } from "@playwright/test";
import { describe, expect, it } from "vitest";
import { ReplaceFailed, commitSwap, replaceServer, rollbackSwap, setUpContext } from "./control-reset.mjs";
import { launchOptions } from "./verify.mjs";

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

const fake = (name, calls) => ({ name, close: async () => { calls.push(`close ${name}`); }, stop: async () => { calls.push(`stop ${name}`); } });

describe("rollbackSwap and commitSwap", () => {
  it("rollback closes the new browser, saves the old session and stops the new server", async () => {
    const calls = [];
    const prior = { ctx: fake("old-ctx", calls), server: fake("old-server", calls), base: "b1" };
    const saved = [];
    const back = await rollbackSwap({ prior, current: { ctx: fake("new-ctx", calls), fresh: { server: fake("new-server", calls) } }, save: (p) => saved.push(p.base) });
    expect(back).toBe(prior);
    expect(calls).toEqual(["close new-ctx", "stop new-server"]);
    expect(saved).toEqual(["b1"]);
  });

  it("rollback doesn't close the old browser when the new one was never opened", async () => {
    const calls = [];
    const prior = { ctx: fake("old-ctx", calls), server: fake("old-server", calls), base: "b1" };
    await rollbackSwap({ prior, current: { ctx: prior.ctx, fresh: { server: fake("new-server", calls) } }, save: () => {} });
    expect(calls).toEqual(["stop new-server"]);
  });

  it("commit saves the new session, then retires the old browser and server, and logs a server that won't stop", async () => {
    const calls = [], logged = [];
    const prior = { ctx: fake("old-ctx", calls), server: { stop: async () => { throw new Error("stuck\nmore"); } } };
    await commitSwap({ prior, fresh: { base: "b2" }, save: (f) => calls.push(`save ${f.base}`), log: (m) => logged.push(m) });
    expect(calls).toEqual(["save b2", "close old-ctx"]);
    expect(logged).toEqual(["stuck"]);
  });
});

describe("setUpContext", () => {
  it("closes the new browser context when setting it up fails, and keeps it when it works", async () => {
    const browser = await chromium.launch(launchOptions());
    try {
      await expect(setUpContext(browser, {}, async () => { throw new Error("sign-in failed"); })).rejects.toThrow("sign-in failed");
      expect(browser.contexts()).toHaveLength(0);
      await expect(setUpContext(browser, {}, async (made) => { made.newPage = async () => { throw new Error("no page"); }; return made.newPage(); })).rejects.toThrow("no page");
      expect(browser.contexts()).toHaveLength(0);
      const kept = await setUpContext(browser, {}, async (made) => made.newPage());
      expect(browser.contexts()).toEqual([kept.context]);
      expect(kept.value.context()).toBe(kept.context);
    } finally { await browser.close(); }
  });
});
