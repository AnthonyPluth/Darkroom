// @vitest-environment jsdom
import { fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { familyAccount, joAccount, queuedConnect, reply, serve, waitingConnect } from "../test/server";
import Browser from "./Browser.svelte";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date(1_000_000));
});
afterEach(() => {
  for (const name of ["fullscreenEnabled", "fullscreenElement", "exitFullscreen"]) Reflect.deleteProperty(document, name);
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
    expect(frame).toHaveAttribute("allowfullscreen");
    const separate = screen.getByRole("link", { name: "Open in new tab" });
    expect(separate).toHaveAttribute("href", VNC);
    expect(separate).toHaveAttribute("target", "_blank");
    expect(separate).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("button", { name: "Full screen" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Remote browser view" })).getByText("browser.example:6080")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Sign-in task" })).not.toBeInTheDocument();
    expect(screen.getByText(/Tailscale/)).toBeInTheDocument();
  });

  it("expands the existing viewer and returns without reloading the browser session", async () => {
    Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: true });
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    const button = await screen.findByRole("button", { name: "Full screen" });
    const viewer = screen.getByRole("region", { name: "Remote browser view" });
    const frame = screen.getByTitle("Remote browser");
    const change = (element: HTMLElement | null) => {
      Object.defineProperty(document, "fullscreenElement", { configurable: true, value: element });
      document.dispatchEvent(new Event("fullscreenchange"));
    };
    const request = vi.fn(async () => change(viewer));
    const exit = vi.fn(async () => change(null));
    Object.defineProperty(viewer, "requestFullscreen", { configurable: true, value: request });
    Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exit });
    await fireEvent.click(button);
    expect(request).toHaveBeenCalledOnce();
    const leave = await screen.findByRole("button", { name: "Exit full screen" });
    expect(leave).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTitle("Remote browser")).toBe(frame);
    await fireEvent.click(leave);
    expect(exit).toHaveBeenCalledOnce();
    expect(await screen.findByRole("button", { name: "Full screen" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTitle("Remote browser")).toBe(frame);
  });

  it("handles browser fullscreen rejection with a usable new-tab fallback", async () => {
    Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: true });
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    const button = await screen.findByRole("button", { name: "Full screen" });
    const viewer = screen.getByRole("region", { name: "Remote browser view" });
    Object.defineProperty(viewer, "requestFullscreen", { configurable: true, value: vi.fn().mockRejectedValue(new Error("denied")) });
    await fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("Use Open in new tab");
    expect(screen.getByRole("link", { name: "Open in new tab" })).toHaveAttribute("href", VNC);
    expect(screen.getByTitle("Remote browser")).toHaveAttribute("src", VNC);
  });

  it("shows no sign-in task for a signed-out account while no job is waiting for it", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [] } });
    await screen.findByText("Connected");
    expect(screen.queryByRole("region", { name: "Sign-in task" })).not.toBeInTheDocument();
  });

  it("shows the sign-in task for an account with a job waiting for a sign-in", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [waitingConnect] } });
    const task = await screen.findByRole("region", { name: "Sign-in task" });
    expect(within(task).getByText("Sign-in needed")).toBeInTheDocument();
    expect(within(task).getByText("Sign in to family.photos@example.com")).toBeInTheDocument();
    expect(within(task).getByText(/This account needs a Google sign-in\./)).toBeInTheDocument();
    expect(within(task).getByText(/After Connect finishes, use Create schedule on the Exports page/)).toBeInTheDocument();
    expect(within(task).queryByRole("button")).not.toBeInTheDocument();
  });

  it("says an interrupted export job will resume after sign-in", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [familyAccount] }, "GET /api/browser/jobs": { jobs: [{ ...waitingConnect, kind: "download" }] } });
    const task = await screen.findByRole("region", { name: "Sign-in task" });
    expect(within(task).getByText(/The waiting browser job then carries on/)).toBeInTheDocument();
    expect(within(task).queryByText(/After Connect finishes/)).not.toBeInTheDocument();
  });

  it("marks keyboard focus inside the embedded browser and clears it on leaving", async () => {
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    await screen.findByText("Connected");
    const frame = screen.getByTitle("Remote browser");
    frame.focus();
    await fireEvent(window, new Event("blur"));
    await vi.waitFor(() => expect(frame).toHaveClass("viewer-focused"));
    screen.getByRole("link", { name: "Open in new tab" }).focus();
    await fireEvent(window, new Event("focus"));
    await vi.waitFor(() => expect(frame).not.toHaveClass("viewer-focused"));
  });

  it("says Google asked to verify when the waiting job is a verification", async () => {
    const verify = { ...waitingConnect, account_id: 10, message: "verify" };
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [verify] } });
    const task = await screen.findByRole("region", { name: "Sign-in task" });
    expect(within(task).getByText(/Google asked to verify this account\./)).toBeInTheDocument();
  });

  it("asks for a switch of account when the browser is signed in to the wrong one", async () => {
    const wrong = { ...waitingConnect, account_id: 10, message: "wrong_account" };
    show({ "GET /api/browser/view": { vnc_url: VNC, worker_last_seen: 995 }, "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [wrong] } });
    const task = await screen.findByRole("region", { name: "Sign-in task" });
    expect(within(task).getByText("Wrong account")).toBeInTheDocument();
    expect(within(task).getByText("Switch to jo.photos@example.com")).toBeInTheDocument();
    expect(within(task).getByText(/signed in to a different Google account/)).toBeInTheDocument();
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

  it("offers a retry when loading fails", async () => {
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
