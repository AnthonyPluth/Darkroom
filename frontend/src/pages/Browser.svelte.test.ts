// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { familyAccount, joAccount, queuedConnect, reply, serve, waitingConnect } from "../test/server";
import Browser from "./Browser.svelte";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date(1_000_000));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const VNC = "https://browser.example:6080/vnc.html?autoconnect=1";

function show(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(Browser);
  return server;
}

describe("the Remote browser page", () => {
  it("embeds the viewer and shows the worker as connected when nothing needs a sign-in", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [queuedConnect] } });
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    const frame = screen.getByTitle("Remote browser");
    expect(frame).toHaveAttribute("src", VNC);
    expect(within(screen.getByRole("region", { name: "Remote browser view" })).getByText("browser.example:6080")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Sign-in task" })).not.toBeInTheDocument();
    expect(screen.getByText(/Tailscale/)).toBeInTheDocument();
  });

  it("shows the sign-in task for an account that needs it", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [] } });
    const task = await screen.findByRole("region", { name: "Sign-in task" });
    expect(within(task).getByText("Sign-in needed")).toBeInTheDocument();
    expect(within(task).getByText("Sign in to family.photos@example.com")).toBeInTheDocument();
    expect(within(task).getByText(/This account is signed out of Google\./)).toBeInTheDocument();
    expect(within(task).queryByRole("button")).not.toBeInTheDocument();
  });

  it("says Google asked to verify when the waiting job is a verification", async () => {
    const verify = { ...waitingConnect, account_id: 10, message: "verify" };
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [verify] } });
    const task = await screen.findByRole("region", { name: "Sign-in task" });
    expect(within(task).getByText(/Google asked to verify this account\./)).toBeInTheDocument();
  });

  it("says so when no viewer address is configured", async () => {
    show({ "GET /api/browser/view": { vnc_url: null, worker_last_seen: null }, "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    expect(await screen.findByText("The remote browser is not set up")).toBeInTheDocument();
    expect(screen.getByText("Worker not seen yet")).toBeInTheDocument();
    expect(screen.queryByTitle("Remote browser")).not.toBeInTheDocument();
  });

  it("flags a worker that stopped responding", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 800 }, "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    expect(await screen.findByText("Worker not responding")).toBeInTheDocument();
  });

  it("offers a retry when loading fails and refreshes on a timer", async () => {
    show({ "GET /api/browser/view": reply(500, { error: "database is locked" }), "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("database is locked");
    expect(screen.getByRole("heading", { name: "Remote browser" })).toBeInTheDocument();
  });

  it("reloads every ten seconds", async () => {
    const server = show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    await screen.findByText("Connected");
    vi.advanceTimersByTime(10_000);
    await vi.waitFor(() => expect(server.sent("GET /api/browser/view")).toHaveLength(2));
  });
});
