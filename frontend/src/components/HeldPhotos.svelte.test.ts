// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { HoldResolution, HoldView } from "$lib/api-types";
import { jo, reply, serve } from "../test/server";
import HeldPhotos from "./HeldPhotos.svelte";

const base: HoldView = {
  id: 7, person_id: jo.id, name: "IMG_1082.HEIC", source: "import", export_id: 42,
  hold: "unknown_offset", reason: "The photo has no UTC offset.", actions: ["offset", "skip"],
  facts: { wall_clock: "2026-09-12T14:03:22", google_utc: 1789214602, place_known: false },
  bytes_verified_at: null, updated_at: 1, resolution: null,
};

function resolution(extra: Partial<HoldResolution> = {}): HoldResolution {
  return {
    id: 10, receipt_id: base.id, hold: base.hold, action: "offset", offset_minutes: -30, taken_local: null,
    state: "recorded", result: null, decided_by_person_id: jo.id, decided_at: 1, applied_at: null, undone_at: null, can_undo: true,
    ...extra,
  };
}

function show(routes: Record<string, unknown> = {}) {
  const server = serve({ "GET /api/holds": { holds: [base], total: 1 }, ...routes });
  vi.stubGlobal("fetch", server.mock);
  const onCount = vi.fn();
  render(HeldPhotos, { people: [jo], onCount });
  return { ...server, onCount };
}

async function openOffset() {
  await userEvent.click(await screen.findByRole("button", { name: "Supply the offset" }));
  await userEvent.selectOptions(screen.getByLabelText("Sign"), "-");
  await userEvent.type(screen.getByLabelText("Hours"), "0");
  await userEvent.selectOptions(screen.getByLabelText("Minutes"), "30");
}

afterEach(() => vi.unstubAllGlobals());

it("shows the source and known facts with the count from the server", async () => {
  const { onCount } = show();
  const card = await screen.findByRole("article", { name: base.name! });
  expect(within(card).getByText("Jo Example · Takeout export #42")).toBeInTheDocument();
  expect(within(card).getByText("12 Sep 2026, 14:03:22")).toBeInTheDocument();
  expect(within(card).getByText("No", { exact: true })).toBeInTheDocument();
  expect(onCount).toHaveBeenLastCalledWith(1);
});

it("validates a blank offset, sends the explicit signed value and reloads it", async () => {
  let current = base;
  const server = show({
    "GET /api/holds": () => ({ holds: [current], total: 1 }),
    "POST /api/holds/7/resolve": () => {
      const saved = resolution();
      current = { ...base, resolution: saved };
      return { resolution: saved };
    },
  });
  await userEvent.click(await screen.findByRole("button", { name: "Supply the offset" }));
  expect(screen.getByLabelText("Hours")).toHaveValue(null);
  await userEvent.click(screen.getByRole("button", { name: "Save offset" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Enter a UTC offset");
  expect(server.sent("POST /api/holds/7/resolve")).toHaveLength(0);
  await userEvent.selectOptions(screen.getByLabelText("Sign"), "-");
  await userEvent.type(screen.getByLabelText("Hours"), "0");
  await userEvent.selectOptions(screen.getByLabelText("Minutes"), "30");
  await userEvent.click(screen.getByRole("button", { name: "Save offset" }));
  expect(await screen.findByText("Saved; it goes to Immich on the next import tick.")).toBeInTheDocument();
  expect(server.sent("POST /api/holds/7/resolve")[0].body).toEqual({ action: "offset", offset_minutes: -30 });
  expect(screen.getByRole("button", { name: "Undo" })).toBeEnabled();
});

it("submits capture time as a local date and clock with a separate offset", async () => {
  const row: HoldView = { ...base, hold: "no_instant", actions: ["capture_time", "skip"] };
  const server = show({
    "GET /api/holds": { holds: [row], total: 1 },
    "POST /api/holds/7/resolve": { resolution: resolution({ action: "capture_time" }) },
  });
  await userEvent.click(await screen.findByRole("button", { name: "Supply the capture time" }));
  await fireEvent.input(screen.getByLabelText("Date"), { target: { value: "2001-07-01" } });
  await fireEvent.input(screen.getByLabelText("Local time"), { target: { value: "08:23:45" } });
  await userEvent.selectOptions(screen.getByLabelText("Sign"), "+");
  await userEvent.type(screen.getByLabelText("Hours"), "5");
  await userEvent.selectOptions(screen.getByLabelText("Minutes"), "45");
  await userEvent.click(screen.getByRole("button", { name: "Save capture time" }));
  expect(server.sent("POST /api/holds/7/resolve")[0].body).toEqual({
    action: "capture_time", taken_local: "2001-07-01T08:23:45", offset_minutes: 345,
  });
});

it("keeps form values after the server refuses the time zone", async () => {
  const server = show({ "POST /api/holds/7/resolve": reply(422, { error: "This place was in a different time zone." }) });
  await openOffset();
  await userEvent.click(screen.getByRole("button", { name: "Save offset" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This place was in a different time zone.");
  expect(screen.getByLabelText("Hours")).toHaveValue(0);
  expect(screen.getByLabelText("Minutes")).toHaveValue("30");
  expect(server.sent("GET /api/holds")).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("button", { name: "Supply the offset" })).toHaveFocus();
});

it("replaces stale rows on a failed conflict refresh and retries without a cursor", async () => {
  let reads = 0;
  const server = show({
    "GET /api/holds": () => ++reads === 2 ? reply(503, {}) : { holds: reads === 1 ? [base] : [], total: reads === 1 ? 1 : 0 },
    "POST /api/holds/7/resolve": reply(409, { error: "This photo is already decided." }),
  });
  await userEvent.click(await screen.findByRole("button", { name: "Skip" }));
  expect(await screen.findByText("Couldn’t load held photos.")).toBeInTheDocument();
  expect(screen.queryByText(base.name!)).not.toBeInTheDocument();
  expect(server.onCount).toHaveBeenLastCalledWith(null);
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("No held photos.")).toBeInTheDocument();
  expect(server.onCount).toHaveBeenLastCalledWith(0);
});

it("loads the next page by receipt ID without duplicating a row", async () => {
  const second = { ...base, id: 3, name: "More.jpg" };
  const server = show({
    "GET /api/holds": { holds: [base], total: 2 },
    "GET /api/holds?after=7": { holds: [base, second], total: 2 },
  });
  await userEvent.click(await screen.findByRole("button", { name: "Load more held photos" }));
  expect(await screen.findByText("More.jpg")).toBeInTheDocument();
  expect(screen.getAllByText(base.name!)).toHaveLength(1);
  expect(server.sent("GET /api/holds?after=7")).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Load more held photos" })).not.toBeInTheDocument();
});

it("can undo a skip and displays a completed send without an Undo action", async () => {
  let current: HoldView = { ...base, resolution: resolution({ action: "skip", offset_minutes: null }) };
  const server = show({
    "GET /api/holds": () => ({ holds: [current], total: 1 }),
    "POST /api/holds/7/undo": () => {
      const undone = resolution({ state: "undone", can_undo: false });
      current = { ...base, resolution: resolution({ state: "applied", can_undo: false }) };
      return { resolution: undone };
    },
  });
  expect(await screen.findByText("Skipped. Darkroom is keeping the copy.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() => expect(server.sent("POST /api/holds/7/undo")).toHaveLength(1));
  expect(await screen.findByText("Already sent to Immich, so it can’t be undone.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
});

it("shows the outcome of a failed recheck and offers the current actions", async () => {
  const row: HoldView = {
    ...base, hold: "unusable", source: "backfill", export_id: null,
    facts: { kind: "trashed" }, actions: ["recheck", "skip"],
    resolution: resolution({ state: "superseded", action: "recheck", result: "still_unusable", can_undo: false }),
  };
  show({ "GET /api/holds": { holds: [row], total: 1 } });
  expect(await screen.findByText("Checked again. Immich still can’t provide a usable copy.")).toBeInTheDocument();
  expect(screen.getByText("In Immich’s trash")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check again" })).toBeEnabled();
});
