// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onDay, onDayAt } from "$lib/format";
import { router } from "$lib/nav.svelte";
import { downloadingExport, keptPartsExport, perPartExport, expiredExport, failedDownload, failedExport, olderFailedDownload, SPENT, spentDownload, failedSchedule, familyAccount, importedExport, joAccount, noExports, preparingExport, queuedConnect, reply, serve, waitingConnect } from "../test/server";
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

  describe("an export imported part by part", () => {
    const OLD_WAY = "Google allows each part to be downloaded 5 times. Darkroom downloads each part once and keeps the file until it is imported.";
    const PER_PART = "Google allows each part to be downloaded 5 times. Darkroom downloads each part once and imports it as soon as it arrives. Once everything in a part is safely in the library, Darkroom deletes the part's file to free space. A part with an item that failed is kept.";

    async function current(item: object) {
      show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [item], schedules: [] } });
      return screen.findByRole("region", { name: "Current export parts for jo.photos@example.com" });
    }

    function states(section: HTMLElement) {
      const cells = within(section).getByRole("table").querySelectorAll("td[data-label='State']");
      return Array.from(cells, (cell) => Array.from(cell.querySelectorAll("[data-tone], [data-tone] + span"), (each) => (each.textContent ?? "").trim()).join(" "));
    }

    it("shows the totals and where each part is, with one detail line under its chip", async () => {
      const section = await current(perPartExport);
      const totals = section.querySelector<HTMLElement>("dl[aria-label='Import so far']");
      if (totals === null) throw new Error("no totals");
      expect(within(totals).getAllByRole("term").map((term) => term.textContent)).toEqual(["Imported", "Held"]);
      expect(within(totals).getByText("2 of 6 parts")).toBeInTheDocument();
      expect(within(totals).getByText("23 items")).toBeInTheDocument();
      expect(within(totals).getByText("Until the whole export is in. 21 of them are waiting for a sidecar.")).toBeInTheDocument();
      expect(within(section).queryByText(/Disk freed/)).not.toBeInTheDocument();
      expect(states(section)).toEqual([
        "Imported 1,517 items",
        "Holding 23 items 1,402 items. 21 wait for a sidecar, 2 for the whole export.",
        "Importing 812 of 1,488 items",
        "Downloaded Waiting to import",
        "Downloading",
        "Waiting",
      ]);
      expect(within(section).getByText("Holding 23 items").closest("[data-tone]")).toHaveAttribute("data-tone", "neutral");
      expect(within(within(section).getByRole("table")).getByText("Imported").closest("[data-tone]")).toHaveAttribute("data-tone", "ok");
      expect(within(section).getByText(PER_PART)).toBeInTheDocument();
      expect(within(section).queryByText(OLD_WAY)).not.toBeInTheDocument();
    });

    it("shows a freed part and the disk freed once Darkroom has deleted a part's file", async () => {
      const freedParts = perPartExport.parts.map((part, index) => (index < 2 ? { ...part, freed: true, bytes_freed: 50_000_000_000, held: 0, held_waiting_sidecar: 0, held_waiting_export: 0 } : part));
      const section = await current({ ...perPartExport, bytes_freed: 100_000_000_000, parts: freedParts });
      const totals = section.querySelector<HTMLElement>("dl[aria-label='Import so far']");
      if (totals === null) throw new Error("no totals");
      expect(within(totals).getAllByRole("term").map((term) => term.textContent)).toEqual(["Imported", "Held", "Disk freed"]);
      expect(within(totals).getByText("100 GB")).toBeInTheDocument();
      expect(states(section).slice(0, 2)).toEqual(["Freed 1,517 items imported, 50 GB freed", "Freed 1,402 items imported, 50 GB freed"]);
      expect(within(section).getAllByText("Freed")[0].closest("[data-tone]")).toHaveAttribute("data-tone", "ok");
      expect(within(section).getByText(PER_PART)).toBeInTheDocument();
    });

    it("says a part is being read before its listing is known", async () => {
      const section = await current({ ...perPartExport, part_count: 1, parts_ingested: 0, held_waiting_sidecar: 0, held_waiting_export: 0, parts: [{ ...perPartExport.parts[2], entries: 0, entries_done: 0 }] });
      expect(states(section)).toEqual(["Importing Reading the file"]);
    });

    it("shows the parts Darkroom kept, with one fixed sentence each under the table in part order", async () => {
      const section = await current(keptPartsExport);
      expect(within(section).getByText("2 of 4 parts")).toBeInTheDocument();
      expect(within(section).getByText("None")).toBeInTheDocument();
      expect(within(section).queryByText(/Until the whole export is in/)).not.toBeInTheDocument();
      expect(states(section)).toEqual(["Imported 1,517 items", "Kept 1,402 items, 2 failed", "Couldn't read", "Import failed File error (OSError)"]);
      expect(within(section).getByText("Kept").closest("[data-tone]")).toHaveAttribute("data-tone", "warn");
      expect(within(section).getByText("Couldn't read").closest("[data-tone]")).toHaveAttribute("data-tone", "bad");
      expect(within(section).getByText("Import failed").closest("[data-tone]")).toHaveAttribute("data-tone", "bad");
      const sentences = Array.from(section.querySelectorAll("p")).map((p) => p.textContent);
      expect(sentences.slice(sentences.indexOf(PER_PART))).toEqual([
        PER_PART,
        "Part 2: 2 items couldn't be imported, so Darkroom keeps the part's file.",
        "Part 3: Darkroom couldn't safely read this part, so it imported nothing from it and keeps the file. It doesn't try again on its own.",
        "Part 4: importing this part stopped with an error, so Darkroom keeps the file. It doesn't try again on its own.",
        "Retry import imports the parts whose import failed again and keeps the parts already imported.",
      ]);
    });

    it("shows the totals at 0 and None before anything is imported", async () => {
      const section = await current({ ...downloadingExport, parts: [{ ...downloadingExport.parts[0], ingest_state: "queued" }, ...downloadingExport.parts.slice(1)] });
      expect(within(section).getByText("0 of 3 parts")).toBeInTheDocument();
      expect(within(section).getByText("None")).toBeInTheDocument();
      expect(states(section)).toEqual(["Downloaded Waiting to import", "Downloading", "Waiting"]);
      expect(within(section).getByText(PER_PART)).toBeInTheDocument();
    });

    it("keeps today's display for an export that never went per-part", async () => {
      const section = await current(downloadingExport);
      expect(within(section).queryByRole("term")).not.toBeInTheDocument();
      expect(within(section).queryByText(/of 3 parts/)).not.toBeInTheDocument();
      expect(states(section)).toEqual(["Downloaded", "Downloading", "Waiting"]);
      expect(within(section).getByText("Downloaded").closest("[data-tone]")).toHaveAttribute("data-tone", "ok");
      expect(within(section).getByText(OLD_WAY)).toBeInTheDocument();
      expect(within(section).queryByText(PER_PART)).not.toBeInTheDocument();
    });

    it("shows no item name, path or sidecar text, only counts and the fixed sentences", async () => {
      const named = { ...keptPartsExport, parts: keptPartsExport.parts.map((each) => ({ ...each, name: "Takeout/Google Photos/IMG_SECRET.jpg", path: "/photos/secret", sidecar: "IMG_SECRET.jpg.json" })) };
      const section = await current(named);
      expect(section.textContent).not.toMatch(/SECRET|secret|Takeout\//);
    });
  });

  it("lists earlier exports with their result, what went to review and what expired unread", async () => {
    show({
      "GET /api/accounts": { accounts: [joAccount] },
      "GET /api/browser/jobs": { jobs: [] },
      "GET /api/exports": { exports: [downloadingExport, importedExport, failedExport, expiredExport], schedules: [] },
      "GET /api/imports": { imports: [{ id: 1, export_id: 38, account_id: 10, export_kind: "incremental", export_created_at: 1, part_count: 2, parts: [], started: 1, finished: 2, stage: "done", status: "done", added: 5, skipped_t1: 0, skipped_t2: 0, skipped_t3: 0, review: 2, missing_gps: 0, failed: 0, sidecars_unreadable: 0, sidecars_repaired: 0, unmatched_by_person: {}, error: null }] },
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
    expect(within(failed).getByRole("button", { name: "Retry import" })).toHaveAccessibleDescription("This export was downloaded and its import failed; Retry import on this page tries the import again.");
    expect(within(imported).queryByRole("button", { name: "Retry import" })).not.toBeInTheDocument();
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
    expect(within(region("live.photos@example.com")).getByText("The schedule exports Google Photos every month for 1 year, with only new and updated items after the first export.")).toBeInTheDocument();
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
  describe("Retry import", () => {
    const PER_PART_NOTE = "Retry import imports the parts whose import failed again and keeps the parts already imported.";
    const requeued = { ...keptPartsExport, import_retry_allowed: false, parts: keptPartsExport.parts.map((part) => (part.ingest_state === "failed" ? { ...part, ingest_state: "queued", import_failure: null } : part)) };

    it("shows why a part failed and queues its import again on request", async () => {
      let current = keptPartsExport;
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": () => ({ exports: [current], schedules: [] }),
        "POST /api/exports/46/import": () => {
          current = requeued;
          return reply(202, { export_id: 46, status: "downloading", stage: null, per_part: true, parts_requeued: 1, run_requeued: false });
        },
      });
      const section = await screen.findByRole("region", { name: /Current export parts/ });
      expect(within(section).getByText("File error (OSError)")).toBeInTheDocument();
      const button = within(section).getByRole("button", { name: "Retry import" });
      expect(button).toHaveAccessibleDescription(PER_PART_NOTE);
      expect(within(section).queryByRole("button", { name: "Retry download" })).not.toBeInTheDocument();
      await userEvent.click(button);
      expect(server.sent("POST /api/exports/46/import")).toHaveLength(1);
      expect(await screen.findByRole("status")).toHaveTextContent("1 part of jo.photos@example.com's export queued to import again.");
      const after = await screen.findByRole("region", { name: /Current export parts/ });
      expect(within(after).queryByRole("button", { name: "Retry import" })).not.toBeInTheDocument();
      expect(within(after).queryByText("File error (OSError)")).not.toBeInTheDocument();
    });

    it("shows the server's refusal and reloads", async () => {
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": { exports: [keptPartsExport], schedules: [] },
        "POST /api/exports/46/import": () => reply(409, { error: "this export has not been downloaded yet" }),
      });
      await userEvent.click(await screen.findByRole("button", { name: "Retry import" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("this export has not been downloaded yet");
      expect(server.sent("GET /api/exports").length).toBeGreaterThan(1);
    });

    it("queues the import of an earlier export whose whole import failed", async () => {
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": { exports: [failedExport], schedules: [] },
        "POST /api/exports/37/import": () => reply(202, { export_id: 37, status: "downloaded", stage: "queued", per_part: false, parts_requeued: 0, run_requeued: false }),
      });
      const earlier = await screen.findByRole("region", { name: /Earlier exports/ });
      await userEvent.click(within(earlier).getByRole("button", { name: "Retry import" }));
      expect(server.sent("POST /api/exports/37/import")).toHaveLength(1);
      expect(await screen.findByRole("status")).toHaveTextContent("Import queued again for jo.photos@example.com.");
    });

    it("says when the retry queued the export's finishing again rather than a part", async () => {
      const finishing = { ...keptPartsExport, status: "failed", parts: keptPartsExport.parts.map((part) => ({ ...part, ingest_state: "ingested", import_failure: null, kept_reason: null, failed_items: 0 })) };
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": { exports: [finishing], schedules: [] },
        "POST /api/exports/46/import": () => reply(202, { export_id: 46, status: "failed", stage: null, per_part: true, parts_requeued: 0, run_requeued: true }),
      });
      const earlier = await screen.findByRole("region", { name: /Earlier exports/ });
      await userEvent.click(within(earlier).getByRole("button", { name: "Retry import" }));
      expect(server.sent("POST /api/exports/46/import")).toHaveLength(1);
      expect(await screen.findByRole("status")).toHaveTextContent("Finishing the import of jo.photos@example.com's export queued again.");
    });

    it("says so when there was nothing to retry", async () => {
      show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": { exports: [keptPartsExport], schedules: [] },
        "POST /api/exports/46/import": () => reply(202, { export_id: 46, status: "downloading", stage: null, per_part: true, parts_requeued: 0, run_requeued: false }),
      });
      await userEvent.click(await screen.findByRole("button", { name: "Retry import" }));
      expect(await screen.findByRole("status")).toHaveTextContent("Nothing in this export was waiting for a retry.");
    });

    it("is not offered when no import can be retried", async () => {
      show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [perPartExport, spentDownload, importedExport], schedules: [] } });
      await screen.findByRole("region", { name: /Current export parts/ });
      expect(screen.queryByRole("button", { name: "Retry import" })).not.toBeInTheDocument();
      expect(screen.queryByText(PER_PART_NOTE)).not.toBeInTheDocument();
    });
  });
  describe("Retry download", () => {
    const retried = { ...failedDownload, status: "downloading", retry_allowed: false, parts: failedDownload.parts.map((part) => (part.status === "failed" ? { ...part, status: "pending", attempts: 0 } : part)) };

    it("says why a download stopped and puts it back on request", async () => {
      let current = failedDownload;
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": () => ({ exports: [current], schedules: [] }),
        "POST /api/exports/42/retry-download": () => {
          current = retried;
          return reply(200, { export: retried, retried: true });
        },
      });
      const section = await screen.findByRole("region", { name: /Current export parts/ });
      expect(within(section).getAllByText("Failed").length).toBeGreaterThan(0);
      const button = within(section).getByRole("button", { name: "Retry download" });
      expect(button).toHaveAccessibleDescription(/^Download stopped: part 2 failed after 3 attempts\./);
      await userEvent.click(button);
      expect(server.sent("POST /api/exports/42/retry-download")).toHaveLength(1);
      expect(await screen.findByRole("status")).toHaveTextContent("Download queued again for jo.photos@example.com.");
      const after = await screen.findByRole("region", { name: /Current export parts/ });
      expect(within(after).queryByRole("button", { name: "Retry download" })).not.toBeInTheDocument();
      expect(within(after).getAllByText("Downloading").length).toBeGreaterThan(0);
    });

    it("shows the server's refusal and reloads", async () => {
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": { exports: [failedDownload], schedules: [] },
        "POST /api/exports/42/retry-download": () => reply(409, { error: "This export has expired in Takeout, so it can't be downloaded again." }),
      });
      await userEvent.click(await screen.findByRole("button", { name: "Retry download" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("This export has expired in Takeout, so it can't be downloaded again.");
      expect(server.sent("GET /api/exports").length).toBeGreaterThan(1);
    });

    it("is offered for an older failed download listed under a newer export that is still downloading", async () => {
      const server = show({
        "GET /api/accounts": { accounts: [joAccount] },
        "GET /api/browser/jobs": { jobs: [] },
        "GET /api/exports": { exports: [downloadingExport, olderFailedDownload], schedules: [] },
        "POST /api/exports/44/retry-download": () => reply(200, { export: { ...olderFailedDownload, status: "downloading", retry_allowed: false }, retried: true }),
      });
      const current = await screen.findByRole("region", { name: /Current export parts/ });
      expect(within(current).queryByRole("button", { name: "Retry download" })).not.toBeInTheDocument();
      const earlier = screen.getByRole("region", { name: /Earlier exports/ });
      const button = within(earlier).getByRole("button", { name: "Retry download" });
      expect(button).toHaveAccessibleDescription(/^Download stopped: part 2 failed after 3 attempts\./);
      await userEvent.click(button);
      expect(server.sent("POST /api/exports/44/retry-download")).toHaveLength(1);
      expect(await screen.findByRole("status")).toHaveTextContent("Download queued again for jo.photos@example.com.");
    });

    it("is not offered for an export that cannot be retried, which says what to do instead", async () => {
      show({ "GET /api/accounts": { accounts: [joAccount] }, "GET /api/browser/jobs": { jobs: [] }, "GET /api/exports": { exports: [spentDownload], schedules: [] } });
      const section = await screen.findByRole("region", { name: /Earlier exports/ });
      expect(within(section).getByText(SPENT)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Retry download" })).not.toBeInTheDocument();
    });
  });
});
