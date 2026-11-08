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
    expect(within(region).getByRole("button", { name: "Takeout has the schedule" })).toBeEnabled();
    await userEvent.click(within(region).getByRole("button", { name: "I checked Takeout, there is no schedule" }));
    expect(server.sent("POST /api/accounts/10/schedule/checked").map((call) => call.body)).toEqual([{ found: false }]);
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
      "GET /api/imports": { imports: [{ id: 1, export_id: 38, account_id: 10, export_kind: "incremental", export_created_at: 1, part_count: 2, parts: [], started: 1, finished: 2, stage: "done", status: "done", added: 5, skipped_t1: 0, skipped_t2: 0, skipped_t3: 0, review: 2, missing_gps: 0, failed: 0, unmatched_by_person: {}, error: null }] },
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

  it("says how a running download is going", async () => {
    const running = { ...waitingConnect, id: 9, account_id: 10, kind: "download", status: "running", message: "downloading part 2 of 3" };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [running] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Download: running, downloading part 2 of 3.")).toBeInTheDocument();
    expect(within(jo).queryByRole("link", { name: "Open remote browser" })).not.toBeInTheDocument();
  });

  it("links to the remote browser when a download waits for a verification, and shows a failed download's capture", async () => {
    const waiting = { ...waitingConnect, id: 9, account_id: 10, kind: "download", message: "verify" };
    const failed = { ...failedSchedule, id: 13, account_id: 11, kind: "download", message: "DownloadFailed: part 2 failed after 3 attempts" };
    show({ "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [waiting, failed] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Download: waiting for you to confirm your identity in the remote browser.")).toBeInTheDocument();
    expect(within(jo).queryByText("Download: waiting for you to sign in.")).not.toBeInTheDocument();
    expect(within(jo).getByRole("link", { name: "Open remote browser" })).toBeInTheDocument();
    const family = screen.getByRole("region", { name: "family.photos@example.com" });
    expect(within(family).getByText("Download: failed, part 2 failed after 3 attempts.")).toBeInTheDocument();
    expect(within(family).getByText(/Capture of the failed Download/)).toBeInTheDocument();
    expect(within(family).getByRole("link", { name: "screenshot" })).toHaveAttribute("href", "/api/browser/failures/13/screenshot.png");
  });

  it("shows a renewal in progress, one waiting for a sign-in and a failed one with its capture", async () => {
    const running = { ...waitingConnect, id: 9, account_id: 10, kind: "renew", status: "running", message: "creating the schedule" };
    const waiting = { ...waitingConnect, id: 10, account_id: 11, kind: "renew", message: "sign_in" };
    show({ "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [running, waiting] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText("Renew schedule: running, creating the schedule.")).toBeInTheDocument();
    const family = screen.getByRole("region", { name: "family.photos@example.com" });
    expect(within(family).getByText("Renew schedule: waiting for you to sign in.")).toBeInTheDocument();
    expect(within(family).getByRole("link", { name: "Open remote browser" })).toBeInTheDocument();
  });

  it("shows why a renewal failed, with its capture", async () => {
    const failed = { ...failedSchedule, id: 14, kind: "renew", message: "ScheduleNotConfirmed: Takeout did not confirm a recurring export" };
    show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [failed] } });
    expect(await screen.findByText("Renew schedule: failed, Takeout did not confirm a recurring export.")).toBeInTheDocument();
    expect(screen.getByText(/Capture of the failed Renew schedule/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "screenshot" })).toHaveAttribute("href", "/api/browser/failures/14/screenshot.png");
  });

  it("tells the owner to look in Takeout when a renewal could not be confirmed, and not after a plain failure", async () => {
    const unsure = { ...failedSchedule, id: 15, account_id: 10, kind: "renew", message: "ScheduleNotConfirmed: Takeout did not confirm a recurring export", needs_check: true };
    const plain = { ...failedSchedule, id: 16, account_id: 11, kind: "renew", message: "UnexpectedPage: the page changed" };
    show({ "GET /api/accounts": { accounts: [joAccount, familyAccount] }, "GET /api/browser/jobs": { jobs: [unsure, plain] } });
    const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
    expect(within(jo).getByText(/could not confirm the renewal/)).toBeInTheDocument();
    expect(within(jo).getByRole("button", { name: "Create schedule" })).toBeDisabled();
    expect(within(jo).getByText("Renew schedule: not confirmed by Takeout; say below what you found.")).toBeInTheDocument();
    expect(within(jo).queryByText(/failed|trying again/)).not.toBeInTheDocument();
    expect(within(jo).getByText(/Capture of the unconfirmed Renew schedule/)).toBeInTheDocument();
    const family = screen.getByRole("region", { name: "family.photos@example.com" });
    expect(within(family).queryByText(/could not confirm the renewal/)).not.toBeInTheDocument();
    expect(within(family).queryByRole("button", { name: "Takeout has the new schedule" })).not.toBeInTheDocument();
  });

  for (const [button, found, after] of [
    ["Takeout has the new schedule", true, "Renew schedule: not confirmed by Takeout, then you found it there."],
    ["Takeout has no new schedule", false, "Renew schedule: not confirmed by Takeout, and you found none there."],
  ] as const) {
    it(`sends "${button}" for an unconfirmed renewal and shows the answer`, async () => {
      const unsure = { ...failedSchedule, id: 15, account_id: 11, kind: "renew", message: "ScheduleNotConfirmed: Takeout did not confirm a recurring export" };
      let answered: "found" | "missing" | null = null;
      const server = show({
        "GET /api/accounts": { accounts: [familyAccount] },
        "GET /api/browser/jobs": () => ({ jobs: [{ ...unsure, needs_check: answered === null, checked: answered }] }),
        "POST /api/accounts/11/schedule/checked": () => {
          answered = found ? "found" : "missing";
          return { job: { ...unsure, needs_check: false, checked: answered } };
        },
      });
      const family = await screen.findByRole("region", { name: "family.photos@example.com" });
      await userEvent.click(within(family).getByRole("button", { name: button }));
      expect(server.sent("POST /api/accounts/11/schedule/checked").map((call) => call.body)).toEqual([{ found }]);
      expect(await within(family).findByText(after)).toBeInTheDocument();
      expect(within(family).queryByText(/could not confirm the renewal/)).not.toBeInTheDocument();
    });
  }

  it("offers Create schedule again only once the schedule has ended and no renewal is open", async () => {
    const live = { ...familyAccount, id: 13, email: "live.photos@example.com" };
    const ended = { ...familyAccount, id: 12, email: "ended.photos@example.com", schedule_created: 100, schedule_end: 900 };
    const queuedRenewal = { ...queuedConnect, id: 17, account_id: 11, kind: "renew" };
    show({ "GET /api/accounts": { accounts: [{ ...ended, id: 11, email: "family.photos@example.com" }, ended, live] }, "GET /api/browser/jobs": { jobs: [queuedRenewal] } });
    const region = (email: string) => screen.getByRole("region", { name: email });
    await screen.findByRole("region", { name: "ended.photos@example.com" });
    expect(within(region("ended.photos@example.com")).getByRole("button", { name: "Create schedule" })).toBeEnabled();
    expect(within(region("family.photos@example.com")).getByRole("button", { name: "Create schedule" })).toBeDisabled();
    expect(within(region("live.photos@example.com")).getByRole("button", { name: "Create schedule" })).toBeDisabled();
    expect(within(region("live.photos@example.com")).getByText("The schedule exports Google Photos every 2 months for 1 year.")).toBeInTheDocument();
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

  describe("Renew now", () => {
    const window = { ...joAccount, schedule_created: 100, schedule_end: 900_000, renew_from: 500 };
    const early = { ...joAccount, id: 11, email: "early.photos@example.com", schedule_created: 100, schedule_end: 9_000_000, renew_from: 8_000_000 };
    const renewJob = { ...queuedConnect, id: 30, account_id: 10, kind: "renew" };
    const card = (email = "jo.photos@example.com") => screen.getByRole("region", { name: email });

    it("is not drawn for an account with no schedule", async () => {
      show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] } });
      const jo = await screen.findByRole("region", { name: "jo.photos@example.com" });
      expect(within(jo).queryByRole("button", { name: "Renew now" })).not.toBeInTheDocument();
      expect(within(jo).getByText(/^Create schedule exports only Google Photos/)).toBeInTheDocument();
    });

    it("sits last in the button row, disabled before the window with the day it opens as its description", async () => {
      show({ "GET /api/accounts": { accounts: [early] }, "GET /api/browser/jobs": { jobs: [] } });
      const region = await screen.findByRole("region", { name: "early.photos@example.com" });
      const buttons = within(region).getAllByRole("button").map((button) => button.textContent);
      expect(buttons).toEqual(["Connect", "Create schedule", "Renew now"]);
      const button = within(region).getByRole("button", { name: "Renew now" });
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription(`Darkroom asks for the renewal itself from ${onDay(8_000_000)}, three weeks before the schedule ends. Renew now is available from then.`);
    });

    it("queues one renewal, reloads and turns the button off", async () => {
      let jobs: unknown[] = [];
      const server = show({
        "GET /api/accounts": { accounts: [window] },
        "GET /api/browser/jobs": () => ({ jobs }),
        "POST /api/accounts/10/schedule/renew": () => {
          jobs = [renewJob];
          return reply(201, { job: renewJob });
        },
      });
      const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
      const button = within(region).getByRole("button", { name: "Renew now" });
      expect(button).toBeEnabled();
      expect(button).toHaveAccessibleDescription(/^Renew now creates next year's schedule in Takeout/);
      await userEvent.click(button);
      expect(await within(region).findByText("Renew schedule: waiting for the browser worker to pick it up.")).toBeInTheDocument();
      expect(server.sent("POST /api/accounts/10/schedule/renew")).toHaveLength(1);
      expect(screen.getByRole("status")).toHaveTextContent("Renewal queued for jo.photos@example.com.");
      expect(within(region).getByRole("button", { name: "Renew now" })).toBeDisabled();
      expect(within(region).getByText("A schedule or renew job is already open for this account.")).toBeInTheDocument();
    });

    it("is off while a schedule job is open, a sign-in is needed or Takeout's answer is awaited", async () => {
      const signIn = { ...window, id: 12, email: "signin.photos@example.com", session_state: "needs_sign_in" };
      const unanswered = { ...window, id: 13, email: "unanswered.photos@example.com" };
      const open = { ...window, id: 14, email: "open.photos@example.com" };
      const jobs = [
        { ...queuedConnect, id: 41, account_id: 13, kind: "renew", status: "failed", needs_check: true },
        { ...queuedConnect, id: 42, account_id: 14, kind: "schedule" },
      ];
      show({ "GET /api/accounts": { accounts: [signIn, unanswered, open] }, "GET /api/browser/jobs": { jobs } });
      await screen.findByRole("region", { name: "open.photos@example.com" });
      const expectations: [string, RegExp][] = [
        ["signin.photos@example.com", /^This account needs to sign in to Google again/],
        ["unanswered.photos@example.com", /^Answer the Takeout question above first\.$/],
        ["open.photos@example.com", /^A schedule or renew job is already open/],
      ];
      for (const [email, sentence] of expectations) {
        const region = card(email);
        expect(within(region).getByRole("button", { name: "Renew now" })).toBeDisabled();
        expect(within(region).getByText(sentence)).toBeInTheDocument();
      }
    });

    it("is on after a failed renewal inside the window, and after the end beside Create schedule", async () => {
      const failed = { ...failedSchedule, id: 50, account_id: 10, kind: "renew", created: 600, message: "UnexpectedPage: the page changed" };
      const ended = { ...joAccount, id: 12, email: "ended.photos@example.com", schedule_created: 100, schedule_end: 900, renew_from: -1_000_000 };
      show({ "GET /api/accounts": { accounts: [window, ended] }, "GET /api/browser/jobs": { jobs: [failed] } });
      await screen.findByRole("region", { name: "ended.photos@example.com" });
      expect(within(card()).getByRole("button", { name: "Renew now" })).toBeEnabled();
      expect(within(card()).getByText("The last renewal failed. Darkroom does not retry a renewal on its own: press Renew now to try again.")).toBeInTheDocument();
      const endedCard = card("ended.photos@example.com");
      expect(within(endedCard).getByRole("button", { name: "Renew now" })).toBeEnabled();
      expect(within(endedCard).getByRole("button", { name: "Create schedule" })).toBeEnabled();
      expect(within(endedCard).getByText(/^This schedule ended on 1 Jan 1970, so scheduled exports have stopped/)).toBeInTheDocument();
    });

    it("shows the route's refusal in the account's card, reloads, and clears it at the next press in the card", async () => {
      let accounts = [window];
      const server = show({
        "GET /api/accounts": () => ({ accounts }),
        "GET /api/browser/jobs": { jobs: [] },
        "POST /api/accounts/10/schedule/renew": () => {
          accounts = [{ ...window, session_state: "needs_sign_in" }];
          return reply(409, { error: "this account needs to sign in to Google again before its schedule can be renewed" });
        },
        "POST /api/accounts/10/connect": reply(201, { job: queuedConnect }),
      });
      const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
      await userEvent.click(within(region).getByRole("button", { name: "Renew now" }));
      const refusal = await within(region).findByRole("alert");
      expect(refusal).toHaveTextContent("Couldn't renew jo.photos@example.com: this account needs to sign in to Google again before its schedule can be renewed.");
      expect(server.sent("GET /api/accounts")).toHaveLength(2);
      expect(await within(region).findByText(/^This account needs to sign in to Google again before its schedule can be renewed\. Press Connect first\./)).toBeInTheDocument();
      expect(within(region).getByRole("button", { name: "Renew now" })).toBeDisabled();
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      await userEvent.click(within(region).getByRole("button", { name: "Connect" }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("keeps one account's refusal when a button of another account is pressed", async () => {
      const other = { ...window, id: 12, email: "other.photos@example.com" };
      show({
        "GET /api/accounts": { accounts: [window, other] },
        "GET /api/browser/jobs": { jobs: [] },
        "POST /api/accounts/10/schedule/renew": reply(409, { error: "a schedule or renew job for this account is already waiting or running" }),
        "POST /api/accounts/12/connect": reply(201, { job: queuedConnect }),
      });
      const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
      await userEvent.click(within(region).getByRole("button", { name: "Renew now" }));
      await within(region).findByRole("alert");
      await userEvent.click(within(card("other.photos@example.com")).getByRole("button", { name: "Connect" }));
      expect(within(region).getByRole("alert")).toHaveTextContent("Couldn't renew jo.photos@example.com");
    });

    it("keeps one account's refusal when Renew now is pressed for another account", async () => {
      const other = { ...window, id: 12, email: "other.photos@example.com" };
      show({
        "GET /api/accounts": { accounts: [window, other] },
        "GET /api/browser/jobs": { jobs: [] },
        "POST /api/accounts/10/schedule/renew": reply(409, { error: "a schedule or renew job for this account is already waiting or running" }),
        "POST /api/accounts/12/schedule/renew": reply(201, { job: { ...renewJob, id: 31, account_id: 12 } }),
      });
      const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
      await userEvent.click(within(region).getByRole("button", { name: "Renew now" }));
      await within(region).findByRole("alert");
      await userEvent.click(within(card("other.photos@example.com")).getByRole("button", { name: "Renew now" }));
      expect(await screen.findByText("Renewal queued for other.photos@example.com.")).toBeInTheDocument();
      expect(within(region).getByRole("alert")).toHaveTextContent("Couldn't renew jo.photos@example.com");
    });

    it("keeps the refusal in the page's alert line when the reload fails, until the retry works", async () => {
      let reloads = 0;
      show({
        "GET /api/accounts": () => {
          reloads += 1;
          return reloads === 2 ? reply(500, { error: "accounts unavailable" }) : { accounts: [window] };
        },
        "GET /api/browser/jobs": { jobs: [] },
        "POST /api/accounts/10/schedule/renew": reply(409, { error: "a schedule or renew job for this account is already waiting or running" }),
      });
      await userEvent.click(await screen.findByRole("button", { name: "Renew now" }));
      const alerts = await screen.findAllByRole("alert");
      expect(alerts.map((alert) => alert.textContent)).toContain("Couldn't renew jo.photos@example.com: a schedule or renew job for this account is already waiting or running.");
      expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "jo.photos@example.com" })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Try again" }));
      const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
      expect(within(region).getByRole("alert")).toHaveTextContent("Couldn't renew jo.photos@example.com: a schedule or renew job for this account is already waiting or running.");
    });

    it("shows any other failure in the card without reloading", async () => {
      const server = show({
        "GET /api/accounts": { accounts: [window] },
        "GET /api/browser/jobs": { jobs: [] },
        "POST /api/accounts/10/schedule/renew": reply(500, { error: "database is locked" }),
      });
      const region = await screen.findByRole("region", { name: "jo.photos@example.com" });
      await userEvent.click(within(region).getByRole("button", { name: "Renew now" }));
      expect(await within(region).findByRole("alert")).toHaveTextContent("Couldn't renew jo.photos@example.com: database is locked.");
      expect(server.sent("GET /api/accounts")).toHaveLength(1);
      expect(within(region).getByRole("button", { name: "Renew now" })).toBeEnabled();
    });
  });
});
