// @vitest-environment jsdom
import { render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DecisionView, ReviewDetail, ReviewSummary } from "$lib/api-types";
import { jo, reply, sam, serve } from "../test/server";
import Review from "./Review.svelte";

const NOW = 1_000_000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(NOW * 1000));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const pending: ReviewSummary = {
  id: 7, status: "pending", decision_id: 70, export_id: 1, account_id: 11, tier: 4, reason: "perceptual distance 2, 1 s apart", decided_at: NOW - 100,
  reversed: false, resolved_at: null, undo_until: null, name: "IMG_2431.jpg", library_name: "IMG_2431.HEIC", person_id: 1, kept_item_id: null, kept_person_id: null,
};
const kept: ReviewSummary = { ...pending, id: 5, status: "kept_both", name: "IMG_2102.jpg", library_name: "IMG_2102.HEIC", resolved_at: NOW - 60, undo_until: NOW + 1000, kept_item_id: 50, kept_person_id: 1 };
const old: ReviewSummary = { ...pending, id: 4, status: "skipped", name: "PXL_1.jpg", library_name: "IMG_1.HEIC", resolved_at: NOW - 20 * 86_400, undo_until: NOW - 6 * 86_400 };
const detail: ReviewDetail = {
  review: pending,
  library: { item_id: 3, person_id: 1, source: "phone", name: "IMG_2431.HEIC", path: "originals/IMG_2431.HEIC", bytes: 3_355_443, captured_utc: 1, captured_local: "2026-09-12T14:03:22", offset_minutes: 120, make: "Apple", model: "iPhone 15", width: 4032, height: 3024, duration_ms: null, is_video: false },
  export: { name: "IMG_2431.jpg", path: "Takeout/IMG_2431.jpg", held: true, bytes: 1_153_434, captured_utc: 1, captured_local: "2026-09-12T14:03:22", offset_minutes: 120, make: "Apple", model: "iPhone 15", width: 4032, height: 3024, duration_ms: null, is_video: false },
  phash_distance: 2, served_people: [{ id: 1, name: "Jo Example" }, { id: 2, name: "Sam Example" }], default_person_id: 1,
};
const tierOne: DecisionView = {
  id: 80, export_id: 1, account_id: 11, name: "IMG_1001.JPG", path: "Takeout/IMG_1001.JPG", person_id: 1, tier: 1, reason: "same SHA-1", decided_by: "auto",
  decided_at: NOW - 100, reversed_at: null, matched_item_id: 1, takeout_item_id: null, review_status: null,
};
const queued: DecisionView = { ...tierOne, id: 70, name: "IMG_2431.jpg", tier: 4, review_status: "pending" };
const gone: DecisionView = { ...tierOne, id: 81, name: "IMG_1002.JPG", reversed_at: NOW - 10 };

function routes(extra: Record<string, unknown> = {}) {
  return {
    "GET /api/review?status=pending": { reviews: [pending], pending: 1 },
    "GET /api/review?status=kept_both": { reviews: [kept], pending: 1 },
    "GET /api/review?status=skipped": { reviews: [old], pending: 1 },
    "GET /api/people": { people: [jo, sam] },
    "GET /api/review/7": detail,
    "GET /api/decisions?page=0": { decisions: [tierOne, queued, gone], total: 3, page: 0, page_size: 50 },
    ...extra,
  };
}

function show(extra: Record<string, unknown> = {}) {
  const server = serve(routes(extra));
  vi.stubGlobal("fetch", server.mock);
  render(Review);
  return server;
}

describe("the current pair", () => {
  it("shows both sides with their fields and thumbnails", async () => {
    show();
    const card = await screen.findByRole("region", { name: "Current pair" });
    expect(within(card).getByRole("heading", { name: "Pair 1 of 1" })).toBeInTheDocument();
    expect(within(card).getByText("Likely re-encode")).toBeInTheDocument();
    expect(within(card).getByText("IMG_2431.HEIC")).toBeInTheDocument();
    expect(within(card).getByText("3.2 MB")).toBeInTheDocument();
    expect(within(card).getByText("1.1 MB, smaller")).toBeInTheDocument();
    expect(within(card).getAllByText("12 Sep 2026, 14:03:22")).toHaveLength(2);
    expect(within(card).getAllByText("4032 × 3024")).toHaveLength(2);
    expect(within(card).getByText("Unmatched")).toBeInTheDocument();
    expect(within(card).getByRole("img", { name: "The photo already in the library" })).toHaveAttribute("src", "/api/review/7/thumb/library");
    expect(within(card).getByRole("img", { name: "The photo from the export" })).toHaveAttribute("src", "/api/review/7/thumb/export");
    expect(screen.getByText("1 pair looks like the same photo saved twice. Nothing is removed until you decide.")).toBeInTheDocument();
  });

  it("keeps both, says so and offers undo", async () => {
    const server = show({ "POST /api/review/7/resolve": { review: { ...pending, status: "kept_both" } }, "POST /api/review/7/undo": { review: pending } });
    await userEvent.click(await screen.findByRole("button", { name: "Keep both" }));
    expect(server.sent("POST /api/review/7/resolve")[0].body).toEqual({ action: "keep_both" });
    const saved = await screen.findByRole("status");
    expect(saved).toHaveTextContent("Decision saved: kept both for IMG_2431.jpg.");
    await userEvent.click(within(saved).getByRole("button", { name: "Undo" }));
    expect(server.sent("POST /api/review/7/undo")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByText(/Decision saved/)).not.toBeInTheDocument());
  });

  it("skips", async () => {
    const server = show({ "POST /api/review/7/resolve": { review: { ...pending, status: "skipped" } } });
    await userEvent.click(await screen.findByRole("button", { name: "Skip" }));
    expect(server.sent("POST /api/review/7/resolve")[0].body).toEqual({ action: "skip" });
    expect(await screen.findByText("Decision saved: skipped IMG_2431.jpg.")).toBeInTheDocument();
  });

  it("shows why an action was refused", async () => {
    show({ "POST /api/review/7/resolve": reply(409, { error: "this pair is already decided; undo that decision first" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Skip" }));
    expect(await screen.findByText("this pair is already decided; undo that decision first")).toBeInTheDocument();
    expect(screen.queryByText(/Decision saved/)).not.toBeInTheDocument();
  });

  it("reassigns by keeping both and moving the kept copy", async () => {
    const server = show({
      "POST /api/review/7/resolve": { review: { ...pending, status: "kept_both", kept_item_id: 90, kept_person_id: 1 } },
      "POST /api/media/90/reassign": { media: {} },
    });
    await userEvent.selectOptions(await screen.findByLabelText("Reassign to"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Reassign" }));
    await waitFor(() => expect(server.sent("POST /api/media/90/reassign")).toHaveLength(1));
    expect(server.sent("POST /api/media/90/reassign")[0].body).toEqual({ person_id: 2 });
  });

  it("does not move the copy when the default person is chosen", async () => {
    const server = show({ "POST /api/review/7/resolve": { review: { ...pending, status: "kept_both", kept_item_id: 90, kept_person_id: 1 } } });
    await userEvent.click(await screen.findByRole("button", { name: "Reassign" }));
    await screen.findByText("Decision saved: kept both for IMG_2431.jpg.");
    expect(server.sent("POST /api/media/90/reassign")).toHaveLength(0);
  });

  it("says when the kept copy could not be moved", async () => {
    show({
      "POST /api/review/7/resolve": { review: { ...pending, status: "kept_both", kept_item_id: 90, kept_person_id: 1 } },
      "POST /api/media/90/reassign": reply(409, { error: "Sam already has this photo" }),
    });
    await userEvent.selectOptions(await screen.findByLabelText("Reassign to"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Reassign" }));
    expect(await screen.findByText("Sam already has this photo")).toBeInTheDocument();
    expect(screen.getByText("Decision saved: kept both for IMG_2431.jpg.")).toBeInTheDocument();
  });

  it("hides reassign for an account that serves one person", async () => {
    show({ "GET /api/review/7": { ...detail, served_people: [{ id: 1, name: "Jo Example" }] } });
    await screen.findByRole("button", { name: "Keep both" });
    expect(screen.queryByLabelText("Reassign to")).not.toBeInTheDocument();
  });

  it("allows only skip once the held copy is gone", async () => {
    show({ "GET /api/review/7": { ...detail, export: { ...detail.export, held: false, bytes: null } } });
    expect(await screen.findByRole("button", { name: "Keep both" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skip" })).toBeEnabled();
    expect(screen.getByText("Not checked yet")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "The photo from the export" })).not.toBeInTheDocument();
  });

  it("says when there is nothing to review", async () => {
    show({ "GET /api/review?status=pending": { reviews: [], pending: 0 } });
    expect(await screen.findByText("Nothing to review. Pairs that look like the same photo saved twice show up here.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Current pair" })).not.toBeInTheDocument();
  });

  it("offers a retry when the queue can't load", async () => {
    show({ "GET /api/review?status=pending": reply(500, {}) });
    expect(await screen.findByText("Couldn't load the review queue (500)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows a video's length and a library photo that is gone", async () => {
    show({ "GET /api/review/7": { ...detail, library: null, export: { ...detail.export, is_video: true, duration_ms: 12_000, captured_local: null, make: null, model: null } } });
    expect(await screen.findByText("The library photo is no longer there.")).toBeInTheDocument();
    expect(screen.getByText("0:12")).toBeInTheDocument();
    expect(screen.getByText("Length")).toBeInTheDocument();
  });
});

describe("past decisions", () => {
  it("lists them newest first with undo only inside the window", async () => {
    const server = show({ "POST /api/review/5/undo": { review: pending } });
    const table = await screen.findByRole("table", { name: "Past decisions" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("IMG_2102.HEIC / .jpg");
    expect(rows[0]).toHaveTextContent("Kept both");
    expect(rows[1]).toHaveTextContent("Skipped");
    expect(rows[1]).toHaveTextContent("Too late to undo");
    await userEvent.click(within(rows[0]).getByRole("button", { name: "Undo IMG_2102.HEIC / .jpg" }));
    expect(server.sent("POST /api/review/5/undo")).toHaveLength(1);
  });

  it("shows why an undo was refused", async () => {
    show({ "POST /api/review/5/undo": reply(409, { error: "this decision is more than 14 days old" }) });
    const table = await screen.findByRole("table", { name: "Past decisions" });
    await userEvent.click(within(table).getByRole("button", { name: "Undo IMG_2102.HEIC / .jpg" }));
    expect(await screen.findByText("this decision is more than 14 days old")).toBeInTheDocument();
  });
});

describe("dedupe decisions", () => {
  it("reverses a decision after asking", async () => {
    const server = show({ "POST /api/decisions/80/reverse": { decision: { ...tierOne, reversed_at: NOW } } });
    const table = await screen.findByRole("table", { name: "Dedupe decisions" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("1 · Exact");
    expect(rows[1]).toHaveTextContent("In the review queue");
    expect(within(rows[1]).queryByRole("button")).not.toBeInTheDocument();
    expect(rows[2]).toHaveTextContent("Reversed");
    await userEvent.click(within(rows[0]).getByRole("button", { name: "Reverse IMG_1001.JPG" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("stops being safe to delete");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reverse decision" }));
    await waitFor(() => expect(server.sent("POST /api/decisions/80/reverse")).toHaveLength(1));
  });

  it("filters by tier and loads more", async () => {
    const server = show({
      "GET /api/decisions?page=0": { decisions: [tierOne], total: 2, page: 0, page_size: 1 },
      "GET /api/decisions?page=1": { decisions: [gone], total: 2, page: 1, page_size: 1 },
      "GET /api/decisions?page=0&tier=4": { decisions: [queued], total: 1, page: 0, page_size: 50 },
    });
    expect(await screen.findByText("Showing 1 of 2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show more" }));
    const table = screen.getByRole("table", { name: "Dedupe decisions" });
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(3));
    await userEvent.selectOptions(screen.getByLabelText("Tier"), "4");
    await waitFor(() => expect(server.sent("GET /api/decisions?page=0&tier=4")).toHaveLength(1));
    await waitFor(() => expect(within(screen.getByRole("table", { name: "Dedupe decisions" })).getAllByRole("row")).toHaveLength(2));
  });

  it("explains a reversal that only marks the record", async () => {
    show({ "GET /api/decisions?page=0": { decisions: [{ ...tierOne, tier: 5, name: null }], total: 1, page: 0, page_size: 50 } });
    const table = await screen.findByRole("table", { name: "Dedupe decisions" });
    await userEvent.click(within(table).getByRole("button", { name: "Reverse decision" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("The record for this photo is marked reversed.");
  });

  it("offers a retry when the decisions can't load", async () => {
    show({ "GET /api/decisions?page=0": reply(500, {}) });
    expect(await screen.findByText("Couldn't load the decisions (500)")).toBeInTheDocument();
  });
});
