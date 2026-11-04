// @vitest-environment jsdom
import { render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportView, MediaView } from "$lib/api-types";
import { router } from "$lib/nav.svelte";
import { jo, reply, sam, serve } from "../test/server";
import Imports from "./Imports.svelte";

const NOW = 100_000_000;
const YEAR_AGO = NOW - 365 * 86_400;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(NOW * 1000));
  history.replaceState(null, "", "/imports");
  router.sync();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const done: ImportView = {
  id: 3, export_id: 2, account_id: 11, export_kind: "initial", export_created_at: NOW - 2 * 86_400, part_count: 5,
  parts: [1, 2, 3, 4, 5].map((number) => ({ number, status: number <= 3 ? "done" : "pending" })),
  started: NOW - 3600, finished: NOW - 3500, stage: "done", status: "done", added: 1204, skipped_t1: 300, skipped_t2: 10, skipped_t3: 1, review: 4,
  missing_gps: 37, failed: 0, unmatched_by_person: { "jo-example": 2, "sam-example": 0 }, error: null,
};
const failed: ImportView = {
  ...done, id: 2, export_id: 1, export_created_at: null, part_count: null, parts: [], stage: "placed", status: "failed", review: 0,
  unmatched_by_person: {}, error: "the export could not be unpacked",
};
const photo: MediaView = {
  id: 9, item_id: 9, person_id: 1, source: "phone", name: "IMG_2418.HEIC", path: "originals/IMG_2418.HEIC", bytes: 1, captured_utc: NOW - 86_400,
  captured_local: "1973-03-02T17:42:00", offset_minutes: 60, make: "Apple", model: "iPhone 15", width: 1, height: 1, duration_ms: null, is_video: false,
  export_id: null, lat: null, lon: null,
};
const undated: MediaView = { ...photo, id: 10, item_id: 10, person_id: 2, name: "PXL_1.jpg", captured_local: null, captured_utc: null, model: null, make: null };

function show(extra: Record<string, unknown> = {}) {
  const server = serve({
    "GET /api/imports": { imports: [done, failed] },
    "GET /api/people": { people: [jo, sam] },
    [`GET /api/media?gps=none&page=0&from=${YEAR_AGO}`]: { media: [photo, undated], total: 3, page: 0, page_size: 50 },
    ...extra,
  });
  vi.stubGlobal("fetch", server.mock);
  render(Imports);
  return server;
}

describe("import runs", () => {
  it("lists each run with its counts, parts and unmatched originals", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Import runs" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Export of");
    expect(rows[0]).toHaveTextContent("3 of 5 parts downloaded");
    expect(rows[0]).toHaveTextContent("1,204");
    expect(rows[0]).toHaveTextContent("311");
    expect(rows[0]).toHaveTextContent("37");
    expect(rows[0]).toHaveTextContent("Jo Example 2");
    await userEvent.click(within(rows[0]).getByRole("link", { name: "4" }));
    expect(router.path).toBe("/review");
  });

  it("says a run that stopped early is not counted yet, never 0", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Import runs" });
    const row = within(table).getAllByRole("row")[2];
    expect(row).toHaveTextContent("Export 1");
    expect(row).toHaveTextContent("Failed");
    expect(row).toHaveTextContent("the export could not be unpacked");
    expect(within(row).getAllByText("Not counted yet")).toHaveLength(5);
  });

  it("shows phone cards with a link to the review queue", async () => {
    show();
    const cards = await screen.findAllByRole("article");
    expect(within(cards[0]).getByText("Sent to review")).toBeInTheDocument();
    expect(within(cards[0]).getByRole("link", { name: "Review the 4" })).toBeInTheDocument();
    expect(within(cards[1]).queryByRole("link")).not.toBeInTheDocument();
  });

  it("says when nothing has been imported, and offers a retry on failure", async () => {
    show({ "GET /api/imports": { imports: [] } });
    expect(await screen.findByText("No export has been imported yet.")).toBeInTheDocument();
  });

  it("offers a retry when the runs can't load", async () => {
    show({ "GET /api/imports": reply(500, {}) });
    expect(await screen.findByText("Couldn't load the imports (500)")).toBeInTheDocument();
  });

  it("says not checked yet for unmatched originals a counted run has none for", async () => {
    show({ "GET /api/imports": { imports: [{ ...done, unmatched_by_person: {} }] } });
    const table = await screen.findByRole("table", { name: "Import runs" });
    expect(within(table).getByText("Not checked yet")).toBeInTheDocument();
  });
});

describe("missing GPS", () => {
  it("lists photos without a location with wall-clock times", async () => {
    show();
    const list = await screen.findByRole("list", { name: "Photos without a location" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("IMG_2418.HEIC");
    expect(items[0]).toHaveTextContent("2 Mar 1973, 17:42");
    expect(items[0]).toHaveTextContent("iPhone 15");
    expect(items[1]).toHaveTextContent("Unknown time");
    expect(screen.getByText("3 photos have no location. Newest first.")).toBeInTheDocument();
    expect(screen.getByText("Showing 2 of 3")).toBeInTheDocument();
  });

  it("filters by person and range, and loads more", async () => {
    const server = show({
      [`GET /api/media?gps=none&page=1&from=${YEAR_AGO}`]: { media: [{ ...photo, id: 11, name: "IMG_3.HEIC" }], total: 3, page: 1, page_size: 2 },
      [`GET /api/media?gps=none&page=0&person=2&from=${YEAR_AGO}`]: { media: [undated], total: 1, page: 0, page_size: 50 },
      "GET /api/media?gps=none&page=0&person=2": { media: [], total: 0, page: 0, page_size: 50 },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByText("IMG_3.HEIC")).toBeInTheDocument();
    const group = screen.getByRole("group", { name: "Person" });
    await userEvent.click(within(group).getByRole("button", { name: "Sam Example" }));
    expect(within(group).getByRole("button", { name: "Sam Example" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(screen.getByText("1 photo has no location. Newest first.")).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText("Taken"), "all");
    expect(await screen.findByText("Every photo in this range has a location.")).toBeInTheDocument();
    expect(server.sent("GET /api/media?gps=none&page=0&person=2")).toHaveLength(1);
  });

  it("never lets an older, slower answer replace a newer filter's list", async () => {
    const first = `/api/media?gps=none&page=0&from=${YEAR_AGO}`;
    const server = serve({
      "GET /api/imports": { imports: [done] },
      "GET /api/people": { people: [jo, sam] },
      [`GET ${first}`]: { media: [photo], total: 1, page: 0, page_size: 50 },
      [`GET /api/media?gps=none&page=0&person=2&from=${YEAR_AGO}`]: { media: [undated], total: 1, page: 0, page_size: 50 },
    });
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => (url === first ? held.then(() => server.mock(url, init)) : server.mock(url, init)));
    render(Imports);
    const group = await screen.findByRole("group", { name: "Person" });
    await userEvent.click(within(group).getByRole("button", { name: "Sam Example" }));
    expect(await screen.findByText("PXL_1.jpg")).toBeInTheDocument();
    release();
    await held;
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("PXL_1.jpg")).toBeInTheDocument();
    expect(screen.queryByText("IMG_2418.HEIC")).not.toBeInTheDocument();
  });

  it("offers a retry when the list can't load", async () => {
    show({ [`GET /api/media?gps=none&page=0&from=${YEAR_AGO}`]: reply(500, {}) });
    expect(await screen.findByText("Couldn't load the photos without a location (500)")).toBeInTheDocument();
  });
});
