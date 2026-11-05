// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onDay, onDayAt } from "$lib/format";
import { router } from "$lib/nav.svelte";
import { downloadingExport, expiredExport, failedExport, failedSchedule, familyAccount, importedExport, joAccount, noExports, preparingExport, queuedConnect, reply, serve, waitingConnect } from "../test/server";
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
  const server = serve({ "GET /api/exports": noExports, "GET /api/imports": { imports: [] }, ...routes });
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
    const message = "ScheduleNotConfirmed: Takeout did not confirm a recurring export; check Takeout before trying again";
    let needsCheck = true;
    const server = show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": () => ({ jobs: [{ ...failedSchedule, message, needs_check: needsCheck }] }),
      "POST /api/accounts/10/schedule/checked": () => {
        needsCheck = false;
        return { job: { ...failedSchedule, message, needs_check: false } };
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

  it("says when nothing has been exported yet and shows no export sections", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("None yet")).toBeInTheDocument();
    expect(within(jo).getByText("Nothing exported yet")).toBeInTheDocument();
    expect(within(jo).getByText("None planned")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Current export parts/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Earlier exports/ })).not.toBeInTheDocument();
  });

  it("shows when the schedule ends, the last export and the next export", async () => {
    show({
      "GET /api/accounts": { accounts: [familyAccount] },
      "GET /api/browser/jobs": { jobs: [] },
      "GET /api/exports": { exports: [{ ...importedExport, account_id: 11 }], schedules: [{ account_id: 11, last_export_at: 300_000, next_export_at: 2_000_000 }] },
    });
    const family = await screen.findByRole("region", { name: "family.photos@example.com" });
    expect(within(family).getByText("In 8 weeks")).toBeInTheDocument();
    expect(within(family).getByText(onDayAt(300_000))).toBeInTheDocument();
    expect(within(family).getByText("2 parts, all imported")).toBeInTheDocument();
    expect(within(family).getByText(onDay(2_000_000))).toBeInTheDocument();
    expect(within(family).getByText("Darkroom checks daily")).toBeInTheDocument();
  });

  it("shows an export that Google is still preparing without a parts table", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [preparingExport], schedules: [{ account_id: 10, last_export_at: 990_000, next_export_at: null }] } });
    const section = await screen.findByRole("region", { name: "Current export parts for jo.photos@example.com" });
    expect(within(section).getByRole("heading", { name: `Export started ${onDay(990_000)}` })).toBeInTheDocument();
    expect(within(section).getByText("Google is preparing it")).toBeInTheDocument();
    expect(within(section).getByText(/Google is still preparing this export/)).toBeInTheDocument();
    expect(within(section).queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("Google is still preparing it")).toBeInTheDocument();
  });

  it("lists the parts of the current export with progress, downloads used and state", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [downloadingExport], schedules: [] } });
    const section = await screen.findByRole("region", { name: "Current export parts for jo.photos@example.com" });
    expect(within(section).getByRole("heading", { name: `Export started ${onDay(900_000)}, 3 parts` })).toBeInTheDocument();
    expect(within(section).getAllByText("Downloading")).toHaveLength(2);
    const table = within(section).getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Part", "File", "Progress", "Downloads used", "State"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows.map((row) => Array.from(row.querySelectorAll("td")).map((cell) => cell.dataset.label))).toEqual(Array(3).fill(["Part", "File", "Progress", "Downloads used", "State"]));
    expect(within(rows[0]).getByText("takeout-20261102-001.zip")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("progressbar", { name: "Part 1 download" })).toHaveAttribute("aria-valuenow", "100");
    expect(within(rows[0]).getByText("1 of 5")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Downloaded")).toBeInTheDocument();
    expect(within(rows[1]).getByRole("progressbar", { name: "Part 2 download" })).toHaveAttribute("aria-valuenow", "0");
    expect(within(rows[1]).getByText("Downloading")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Not downloaded yet")).toBeInTheDocument();
    expect(within(rows[2]).getByText("0 of 5")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Waiting")).toBeInTheDocument();
    expect(within(section).getByText(/Google allows each part to be downloaded 5 times/)).toBeInTheDocument();
  });

  it("marks the parts of an export that is waiting, importing, imported or failed", async () => {
    const parts = (status: string, state: string) => ({ ...importedExport, id: 50, status, parts: importedExport.parts.map((part) => ({ ...part, status: state })) });
    for (const [status, state, chip, partChip] of [["announced", "pending", "Waiting to download", "Waiting"], ["importing", "done", "Importing", "Downloaded"], ["downloaded", "done", "Downloaded", "Downloaded"], ["failed", "failed", "Failed", "Failed"]] as const) {
      const server = serve({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [parts(status, state)], schedules: [] }, "GET /api/imports": { imports: [] } });
      vi.stubGlobal("fetch", server.mock);
      const view = render(Exports);
      const section = status === "failed" ? await screen.findByRole("region", { name: /Earlier exports/ }) : await screen.findByRole("region", { name: /Current export parts/ });
      expect(within(section).getAllByText(chip).length).toBeGreaterThan(0);
      if (status !== "failed") expect(within(within(section).getByRole("table")).getAllByText(partChip)).toHaveLength(2);
      view.unmount();
    }
  });

  it("lists earlier exports with their result, what went to review and what expired unread", async () => {
    show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": { jobs: [] },
      "GET /api/exports": { exports: [downloadingExport, importedExport, failedExport, expiredExport], schedules: [] },
      "GET /api/imports": { imports: [{ id: 1, export_id: 38, account_id: 10, started: 1, finished: 2, stage: "done", status: "done", added: 5, skipped_t1: 0, skipped_t2: 0, skipped_t3: 0, review: 2, missing_gps: 0, failed: 0, unmatched_by_person: {}, error: null }] },
    });
    const section = await screen.findByRole("region", { name: "Earlier exports for jo.photos@example.com" });
    const table = within(section).getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Exported", "Parts", "Result"]);
    const [imported, failed, expired] = within(table).getAllByRole("row").slice(1);
    expect(within(imported).getByText(onDay(300_000))).toBeInTheDocument();
    expect(within(imported).getByText("Imported")).toBeInTheDocument();
    expect(within(imported).getByText("2 sent to review")).toBeInTheDocument();
    expect(within(failed).getByText("Failed")).toBeInTheDocument();
    expect(within(failed).queryByText(/sent to review/)).not.toBeInTheDocument();
    expect(within(expired).getByText("Expired")).toBeInTheDocument();
    expect(within(expired).getByText("Never downloaded")).toBeInTheDocument();
    expect(within(expired).getByText("4")).toBeInTheDocument();
  });

  it("shows each account's own exports", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [downloadingExport, { ...importedExport, account_id: 11 }], schedules: [] } });
    await screen.findByRole("region", { name: "family.photos@example.com" });
    expect(screen.getAllByRole("region", { name: /Current export parts/ })).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Earlier exports for family.photos@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Current export parts for jo.photos@example.com" })).toBeInTheDocument();
  });

  it("checks for new exports now and says what was queued", async () => {
    let queued = false;
    const server = show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": () => ({ jobs: queued ? [{ ...queuedConnect, id: 9, kind: "status" }] : [] }),
      "POST /api/exports/check-now": () => {
        queued = true;
        return reply(201, { jobs: [{ ...queuedConnect, id: 9, kind: "status" }], skipped: 0 });
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Check exports now" }));
    expect(server.sent("POST /api/exports/check-now")).toHaveLength(1);
    expect(await screen.findByText("Checking one Google account for new exports.")).toBeInTheDocument();
    expect(await screen.findByText("Check exports: waiting for the browser worker to pick it up.")).toBeInTheDocument();
  });

  it("says when a check is already waiting or nothing is connected", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "POST /api/exports/check-now": reply(200, { jobs: [], skipped: 1 }) });
    await userEvent.click(await screen.findByRole("button", { name: "Check exports now" }));
    expect(await screen.findByRole("status")).toHaveTextContent("A check is already waiting or running.");
  });

  it("says when no account is connected and reports a check that could not be queued", async () => {
    let answer: unknown = reply(200, { jobs: [], skipped: 0 });
    show({ "GET /api/accounts": { accounts: [familyAccount] }, "GET /api/browser/jobs": { jobs: [] }, "POST /api/exports/check-now": () => answer });
    await userEvent.click(await screen.findByRole("button", { name: "Check exports now" }));
    expect(await screen.findByText("No Google account is connected, so there is nothing to check.")).toBeInTheDocument();
    answer = reply(500, { error: "database is locked" });
    await userEvent.click(screen.getByRole("button", { name: "Check exports now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("database is locked");
    expect(screen.queryByText("No Google account is connected, so there is nothing to check.")).not.toBeInTheDocument();
  });

  it("links to the remote browser and the capture when a check waits for a sign-in or fails", async () => {
    const waiting = { ...waitingConnect, id: 8, account_id: 10, kind: "status" };
    const view = show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [waiting] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Check exports: waiting for you to sign in.")).toBeInTheDocument();
    expect(within(jo).getByRole("link", { name: "Open remote browser" })).toBeInTheDocument();
    expect(view.sent("GET /api/exports")).toHaveLength(1);
  });

  it("shows why a check failed, with its capture", async () => {
    const failed = { ...failedSchedule, id: 12, kind: "status", message: "ExportsUnreadable: Takeout's list of exports did not load" };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [failed] } });
    expect(await screen.findByText("Check exports: failed, Takeout's list of exports did not load.")).toBeInTheDocument();
    expect(screen.getByText(/Capture of the failed Check exports/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "screenshot" })).toHaveAttribute("href", "/api/browser/failures/12/screenshot.png");
  });

  it("offers a retry when the exports cannot be loaded", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": reply(500, { error: "exports unavailable" }) });
    expect(await screen.findByRole("alert")).toHaveTextContent("exports unavailable");
    expect(screen.getByRole("button", { name: "Check exports now" })).toBeDisabled();
  });

  it("shows the expired export of a chain on its own as an earlier export", async () => {
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [expiredExport], schedules: [] } });
    const section = await screen.findByRole("region", { name: /Earlier exports/ });
    expect(within(section).getByText("Expired")).toBeInTheDocument();
    expect(screen.getByText("Expired before it was downloaded")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Current export parts/ })).not.toBeInTheDocument();
  });
});
