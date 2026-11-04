// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import { failedSchedule, familyAccount, joAccount, queuedConnect, reply, serve, waitingConnect } from "../test/server";
import Exports from "./Exports.svelte";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date(1_000_000));
  history.replaceState(null, "", "/exports");
  router.sync();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function show(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(Exports);
  return server;
}

describe("the Exports page", () => {
  it("offers to add the first Google account when there is none", async () => {
    show({ "GET /api/accounts": { accounts: [] }, "GET /api/browser/jobs": { jobs: [] } });
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    await userEvent.click(await screen.findByRole("link", { name: "Add a Google account" }));
    expect(router.path).toBe("/people");
  });

  it("shows each account's session, schedule dates and its buttons", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Session ok")).toBeInTheDocument();
    expect(within(jo).getByText("Not created")).toBeInTheDocument();
    expect(within(jo).getByText("No schedule")).toBeInTheDocument();
    expect(within(jo).getByRole("button", { name: "Connect" })).toBeEnabled();
    expect(within(jo).getByRole("button", { name: "Create schedule" })).toBeEnabled();
    expect(within(jo).queryByRole("link", { name: "Open remote browser" })).not.toBeInTheDocument();
    const family = screen.getByRole("region", { name: "family.photos@example.com" });
    expect(within(family).getByText("Sign-in needed")).toBeInTheDocument();
    expect(within(family).getByText("16 Feb 1970")).toBeInTheDocument();
    expect(within(family).getByText("27 Feb 1970")).toBeInTheDocument();
    expect(within(family).getByRole("button", { name: "Create schedule" })).toBeDisabled();
    await userEvent.click(within(family).getByRole("link", { name: "Open remote browser" }));
    expect(router.path).toBe("/browser");
  });

  it("queues a connect job and reloads, then disables the button while it is open", async () => {
    let jobs: unknown[] = [];
    const server = show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": () => ({ jobs }),
      "POST /api/accounts/10/connect": () => {
        jobs = [queuedConnect];
        return reply(201, { job: queuedConnect });
      },
    });
    const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
    await userEvent.click(within(region).getByRole("button", { name: "Connect" }));
    expect(await within(region).findByText("Connect: waiting for the browser worker to pick it up.")).toBeInTheDocument();
    expect(server.sent("POST /api/accounts/10/connect")).toHaveLength(1);
    expect(within(region).getByRole("button", { name: "Connect" })).toBeDisabled();
    expect(within(region).getByRole("button", { name: "Create schedule" })).toBeEnabled();
  });

  it("queues a schedule job", async () => {
    const server = show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": { jobs: [] },
      "POST /api/accounts/10/schedule": reply(201, { job: queuedConnect }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Create schedule" }));
    expect(server.sent("POST /api/accounts/10/schedule")).toHaveLength(1);
  });

  it("says why a job could not be queued and keeps the buttons usable", async () => {
    show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": { jobs: [] },
      "POST /api/accounts/10/connect": reply(409, { error: "a connect job for this account is already waiting or running" }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already waiting or running");
    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();
  });

  it("links to the remote browser while a job waits for a sign-in, and reports a failed job", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [waitingConnect, failedSchedule] } });
    const family = await screen.findByRole("region", { name: "family.photos@example.com" });
    expect(within(family).getByText("Connect: waiting for you to sign in.")).toBeInTheDocument();
    expect(within(family).getByRole("link", { name: "Open remote browser" })).toBeInTheDocument();
    const jo = screen.getByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Create schedule: failed. Open Status for details.")).toBeInTheDocument();
    expect(within(jo).getByRole("button", { name: "Create schedule" })).toBeEnabled();
  });

  it("refreshes on a timer", async () => {
    const server = show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] } });
    await screen.findByRole("region", { name: "jo.photos@example.com" });
    vi.advanceTimersByTime(10_000);
    await vi.waitFor(() => expect(server.sent("GET /api/accounts")).toHaveLength(2));
  });

  it("offers a retry when loading fails", async () => {
    show({ "GET /api/accounts": reply(500, { error: "database is locked" }), "GET /api/browser/jobs": { jobs: [] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("database is locked");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
