// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import { emptyStatus, reply, seededStatus, serve, someEvents } from "../test/server";
import Status from "./Status.svelte";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(1_000_000));
  history.replaceState(null, "", "/");
  router.sync();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function show(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(Status);
  return server;
}

describe("the Status page with no data", () => {
  it("says so and offers to add the first person", async () => {
    show({ "GET /api/status": emptyStatus, "GET /api/events": { events: [] } });
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(await screen.findByText("No one has been added yet.")).toBeInTheDocument();
    expect(screen.getByText(/Nothing needs you right now\. The browser worker has not reported yet\./)).toBeInTheDocument();
    expect(screen.getByText("Nothing has happened yet.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Open alerts")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Add the first person" }));
    expect(router.path).toBe("/people");
  });
});

describe("the Status page with the seeded data", () => {
  const routes = { "GET /api/status": seededStatus, "GET /api/events": { events: someEvents } };

  it("lists the open alerts, with a way to the remote browser for a sign-in", async () => {
    show(routes);
    const alerts = await screen.findByRole("region", { name: "Open alerts" });
    expect(within(alerts).getByRole("heading", { name: "Needs you, 2" })).toBeInTheDocument();
    expect(within(alerts).getByText("Sign-in needed")).toBeInTheDocument();
    expect(within(alerts).getByText("Check backup")).toBeInTheDocument();
    expect(within(alerts).getByText(/14 of Sam's originals/)).toBeInTheDocument();
    expect(within(alerts).getAllByRole("link")).toHaveLength(1);
    expect(screen.getByText(/2 things need you\. The browser worker was last seen 1 minute ago\./)).toBeInTheDocument();
    await userEvent.click(within(alerts).getByRole("link", { name: "Open remote browser" }));
    expect(router.path).toBe("/browser");
  });

  it("shows a card per person with their health, accounts, Pixels, iPhones and counts", async () => {
    show(routes);
    const jo = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(jo).getByText("Healthy")).toBeInTheDocument();
    expect(within(jo).getByText("120 photos, 100 with GPS, 3 without a time")).toBeInTheDocument();
    expect(within(jo).getByText("jo.photos@example.com")).toBeInTheDocument();
    expect(within(jo).getByText("Session ok")).toBeInTheDocument();
    expect(within(jo).getByText("Pixel 9a")).toBeInTheDocument();
    expect(within(jo).getByText("Jo's iPhone: checked in just now, 0 pending")).toBeInTheDocument();
    expect(within(jo).getByText("0 photos")).toBeInTheDocument();
    expect(within(jo).getByText("0 originals")).toBeInTheDocument();

    const sam = screen.getByRole("article", { name: "Sam Example" });
    expect(within(sam).getByText("Needs attention")).toBeInTheDocument();
    expect(within(sam).getByText("Sign-in needed")).toBeInTheDocument();
    expect(within(sam).getByText(/Schedule ends/)).toBeInTheDocument();
    expect(within(sam).getByText("Sam's iPhone: never checked in, 18 pending")).toBeInTheDocument();
    expect(within(sam).getByText("None yet")).toBeInTheDocument();
    expect(within(sam).getAllByText("None")).toHaveLength(1);
    await userEvent.click(within(sam).getByRole("link", { name: "Open Sam Example" }));
    expect(router.path).toBe("/people");
  });

  it("lists the recent events in words", async () => {
    show(routes);
    const events = await screen.findByRole("region", { name: "Recent events" });
    const items = within(events).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Jo's iPhone checked in for jo-example.");
    expect(items[1]).toHaveTextContent("Mystery event: sam-example.");
    expect(items[2]).toHaveTextContent("Added jo-example.");
  });

  it("notes when a person has no account, Pixel or iPhone", async () => {
    const lonely = { ...seededStatus, alerts: [], people: [{ ...seededStatus.people[0], accounts: [], pixels: [], devices: [] }] };
    show({ ...routes, "GET /api/status": lonely });
    const card = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(card).getAllByText("None")).toHaveLength(3);
  });
});

describe("when loading fails", () => {
  it("says why, shows no stale numbers, and tries again on request", async () => {
    let attempt = 0;
    show({
      "GET /api/status": () => (attempt++ === 0 ? reply(500, { error: "the database is busy" }) : seededStatus),
      "GET /api/events": { events: [] },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("the database is busy");
    expect(screen.queryByText(/things need you/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("article", { name: "Jo Example" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
