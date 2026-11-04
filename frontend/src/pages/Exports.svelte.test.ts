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
    expect(within(family).queryByRole("link", { name: "Open remote browser" })).not.toBeInTheDocument();
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
    await userEvent.click(within(family).getByRole("link", { name: "Open remote browser" }));
    expect(router.path).toBe("/browser");
    const jo = screen.getByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Create schedule: failed (UnexpectedPage).")).toBeInTheDocument();
    expect(within(jo).getByRole("button", { name: "Create schedule" })).toBeEnabled();
  });

  it("shows why a job failed, with links to its capture", async () => {
    const failed = { ...failedSchedule, message: "WrongAccount: Takeout is signed in to a different Google account than the one this job is for." };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [failed] } });
    expect(await screen.findByText("Create schedule: failed, Takeout is signed in to a different Google account than the one this job is for.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "screenshot" })).toHaveAttribute("href", "/api/browser/failures/4/screenshot.png");
    expect(screen.getByRole("link", { name: "page snapshot" })).toHaveAttribute("href", "/api/browser/failures/4/page.html");
  });

  it("links only to the capture files that were saved", async () => {
    const failed = { ...failedSchedule, capture_files: ["page.html"] };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [failed] } });
    expect(await screen.findByRole("link", { name: "page snapshot" })).toHaveAttribute("href", "/api/browser/failures/4/page.html");
    expect(screen.queryByRole("link", { name: "screenshot" })).not.toBeInTheDocument();
  });

  it("links only to the screenshot when the page snapshot failed", async () => {
    const failed = { ...failedSchedule, capture_files: ["screenshot.png"] };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [failed] } });
    expect(await screen.findByRole("link", { name: "screenshot" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "page snapshot" })).not.toBeInTheDocument();
  });

  it("shows no capture links for a failure with no capture", async () => {
    const failed = { ...failedSchedule, has_capture: false, capture_files: [] };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [failed] } });
    await screen.findByText("Create schedule: failed (UnexpectedPage).");
    expect(screen.queryByText(/Capture of the failed/)).not.toBeInTheDocument();
  });

  it("holds back Create schedule after an unconfirmed attempt until the person says they checked Takeout", async () => {
    let message = "ScheduleNotConfirmed: Takeout did not confirm a recurring export; check Takeout before trying again";
    const server = show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": () => ({ jobs: [{ ...failedSchedule, message }] }),
      "POST /api/accounts/10/schedule/checked": () => {
        message += " [checked]";
        return { job: { ...failedSchedule, message } };
      },
    });
    const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(region).getByText(/Takeout may already have this schedule/)).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Create schedule" })).toBeDisabled();
    await userEvent.click(within(region).getByRole("button", { name: "I checked Takeout, there is no schedule" }));
    expect(server.sent("POST /api/accounts/10/schedule/checked")).toHaveLength(1);
    await vi.waitFor(() => expect(within(region).getByRole("button", { name: "Create schedule" })).toBeEnabled());
    expect(within(region).queryByText(/Takeout may already have this schedule/)).not.toBeInTheDocument();
  });

  it("says when a job waits for a switch of account", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [{ ...waitingConnect, account_id: 10, message: "wrong_account" }] } });
    expect(await screen.findByText("Connect: waiting for you to switch to the right Google account.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open remote browser" })).toBeInTheDocument();
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
