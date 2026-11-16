// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import { emptyStatus, reply, runningCard, seededStatus, serve, someEvents, syncthingStatus, transport } from "../test/server";
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

  it("shows a card per person with their health, accounts, Pixels and counts", async () => {
    show(routes);
    const jo = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(jo).getByText("Healthy")).toBeInTheDocument();
    expect(within(jo).getByText("120 photos, 100 with GPS, 3 without a time")).toBeInTheDocument();
    expect(within(jo).getByText("jo.photos@example.com")).toBeInTheDocument();
    expect(within(jo).getByText("Session ok")).toBeInTheDocument();
    expect(within(jo).getByText("Pixel 9a")).toBeInTheDocument();
    expect(within(jo).getAllByText("Not checked yet")).toHaveLength(1);
    for (const unknown of within(jo).getAllByText("Not checked yet")) expect(unknown).toHaveClass("text-ink-muted");
    expect(within(jo).queryByText(/^0 (photos|originals)$/)).not.toBeInTheDocument();

    const sam = screen.getByRole("article", { name: "Sam Example" });
    expect(within(sam).getByText("Needs attention")).toBeInTheDocument();
    expect(within(sam).getByText("Sign-in needed")).toBeInTheDocument();
    expect(within(sam).getByText(/Schedule ends/)).toBeInTheDocument();
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
    expect(items[0]).toHaveTextContent("Indexed the originals of jo-example (new=4 failed=0).");
    expect(items[1]).toHaveTextContent("Mystery event: sam-example.");
    expect(items[2]).toHaveTextContent("Added jo-example.");
  });

  it("notes when a person has no account or Pixel", async () => {
    const lonely = { ...seededStatus, alerts: [], people: [{ ...seededStatus.people[0], accounts: [], pixels: [] }] };
    show({ ...routes, "GET /api/status": lonely });
    const card = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(card).getAllByText("None")).toHaveLength(2);
  });

  it("shows the counts once they have been checked, and a count of 0 as checked", async () => {
    const jo = seededStatus.people[0];
    const checked = { ...seededStatus, people: [{ ...jo, unmatched: 0 }] };
    show({ ...routes, "GET /api/status": checked });
    const card = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(card).getByText("0 originals")).not.toHaveClass("text-ink-muted");
    expect(within(card).queryByText("Not checked yet")).not.toBeInTheDocument();
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

describe("the Status page with Darkroom's Syncthing", () => {
  const routes = (status: typeof syncthingStatus) => ({ "GET /api/status": status, "GET /api/events": { events: [] } });

  it("draws no Syncthing card and lists the Pixels by name when Syncthing is off", async () => {
    show(routes(seededStatus));
    const jo = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(jo).getByText("Pixel 9a")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Syncthing" })).not.toBeInTheDocument();
  });

  it("puts the card after People and before Recent events, with each Pixel's chip and line", async () => {
    show(routes(syncthingStatus));
    const card = await screen.findByRole("region", { name: "Syncthing" });
    const order = [...document.querySelectorAll("section")].map((section) => section.getAttribute("aria-label"));
    expect(order.indexOf("Syncthing")).toBe(order.indexOf("Recent events") - 1);
    expect(order.indexOf("People")).toBeLessThan(order.indexOf("Syncthing"));
    expect(within(card).getByRole("heading", { name: "Syncthing" })).toBeInTheDocument();
    expect(within(card).getByText("Running")).toBeInTheDocument();
    expect(within(card).getByText("Syncthing is running with the settings Darkroom set.")).toBeInTheDocument();
    for (const text of ["Connected", "Up to date", "Sending", "82% sent", "Not seen for 4 days", "Folder held", "Not connected", "Last seen 5 hours ago."]) {
      expect(within(card).getByText(text)).toBeInTheDocument();
    }
    expect(within(card).getByText("Darkroom reaches the Pixel only on the home network. Check it is on, on the Wi-Fi, with Syncthing running.")).toBeInTheDocument();
    expect(within(card).getByText("1 photo could not be written again, so its folder is not shared.")).toBeInTheDocument();
    expect(within(card).getByText("Its folder has 3 files Darkroom did not put there, so it is not shared.")).toBeInTheDocument();
  });

  it("writes the person card's Pixel row with its sync and a chip only when it is not ok", async () => {
    show(routes(syncthingStatus));
    const jo = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(jo).getByText("Pixel 9a, synced 6 minutes ago")).toBeInTheDocument();
    expect(within(jo).queryByText("Connected")).not.toBeInTheDocument();
    expect(within(jo).getByText("Pixel 5, synced 2 days ago")).toBeInTheDocument();
    expect(within(jo).getByText("Folder held")).toBeInTheDocument();
    expect(within(jo).getByText("Pixel 4a, not synced yet")).toBeInTheDocument();
    expect(within(jo).getByText("Waiting for the Pixel")).toBeInTheDocument();
  });

  it("shows the states with no usable folder on the card and the person card, with no button", async () => {
    const pixels = [
      transport(30, "Pixel 3", "device_id_invalid"), transport(31, "Pixel 2", "device_id_shared", { others: ["Pixel 1", "Pixel 0"] }),
      transport(32, "Pixel 1", "folder_removed"), transport(33, "Pixel 0", "folder_path_refused"),
    ];
    const status = {
      ...syncthingStatus, syncthing: { ...runningCard, pixels },
      people: syncthingStatus.people.map((person) => person.id === 1 ? { ...person, pixels: [{ id: 32, name: "Pixel 1", google_account_id: 10, last_sync: null, syncthing: pixels[2] }] } : person),
    };
    show(routes(status));
    const card = await screen.findByRole("region", { name: "Syncthing" });
    for (const text of ["Device ID not valid", "Same device ID", "Folder removed", "Folder refused",
      "Pixel 1 and Pixel 0 have the same device ID, so Darkroom shares neither folder. Pair again whichever one is wrong."]) {
      expect(within(card).getByText(text)).toBeInTheDocument();
    }
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
    const jo = await screen.findByRole("article", { name: "Jo Example" });
    expect(within(jo).getByText("Pixel 1, not synced yet")).toBeInTheDocument();
    expect(within(jo).getByText("Folder removed")).toBeInTheDocument();
  });

  it("puts one person card per row", async () => {
    show(routes(syncthingStatus));
    const jo = await screen.findByRole("article", { name: "Jo Example" });
    expect(jo.parentElement).toHaveClass("grid");
    expect(jo.parentElement).not.toHaveClass("md:grid-cols-2");
  });

  it("says no Pixel is paired yet, with the way to People", async () => {
    show(routes({ ...syncthingStatus, syncthing: { ...runningCard, pixels: [] } }));
    const card = await screen.findByRole("region", { name: "Syncthing" });
    expect(within(card).getByText("No Pixel is paired yet.")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("link", { name: "Open People" }));
    expect(router.path).toBe("/people");
  });

  it.each([
    ["starting", null, null, "Starting", "Darkroom is checking Syncthing's settings before anything syncs."],
    ["down", null, 1000 - 2 * 3600, "Down", "Syncthing has not answered for 2 hours. Darkroom keeps restarting it. Placing and pruning carry on, and the Pixels catch up once it is back."],
    ["settings_failed", "Syncthing did not keep the configuration as written", 900, "Paused", "Darkroom could not apply its settings to Syncthing (Syncthing did not keep the configuration as written), so nothing is syncing. It tries again before the next hourly run."],
    ["version", "Syncthing reported v2.2.0, outside the tested range v2.1.7", 900, "Not started", "Syncthing reported v2.2.0, outside the tested range v2.1.7, so Darkroom has not started Syncthing."],
    ["key", "Syncthing refused the API key the init handed over", 900, "Not started", "Syncthing refused the API key the init handed over, so Darkroom has not started Syncthing. Restart the container."],
    ["share_check_failed", "the sentinel is missing from the share", 900, "Paused", "The photo share failed Darkroom's check (the sentinel is missing from the share), so placing, pruning and syncing are paused, and nothing on the share is changed."],
  ] as const)("shows the %s card with its chip and sentence, and every Pixel as not syncing", async (state, reason, since, chip, sentence) => {
    const pixels = runningCard.pixels.map((pixel) => ({ ...pixel, state: "not_syncing" as const }));
    show(routes({ ...syncthingStatus, syncthing: { state, reason, since, pixels } }));
    const card = await screen.findByRole("region", { name: "Syncthing" });
    expect(within(card).getByText(chip)).toBeInTheDocument();
    expect(within(card).getByText(sentence)).toBeInTheDocument();
    expect(within(card).getAllByText("Not syncing")).toHaveLength(pixels.length);
    expect(within(card).getAllByText("Nothing syncs until Syncthing is running again.")).toHaveLength(pixels.length);
  });

  it("labels the new alerts with their chips and no button", async () => {
    const opened = 900;
    const kinds = [["syncthing_down", "Syncthing down"], ["syncthing_settings_failed", "Settings not applied"], ["syncthing_version", "Untested Syncthing"],
      ["share_check_failed", "Share check failed"], ["pixel_held", "Folder held"]] as const;
    const listed = kinds.map(([kind], index) => ({ id: 50 + index, kind, subject: "syncthing", message: `Message ${index}.`, link: null, opened }));
    show(routes({ ...syncthingStatus, alerts: listed }));
    const alerts = await screen.findByRole("region", { name: "Open alerts" });
    for (const [, label] of kinds) expect(within(alerts).getByText(label)).toBeInTheDocument();
    expect(within(alerts).queryAllByRole("link")).toHaveLength(0);
    expect(within(alerts).queryAllByRole("button")).toHaveLength(0);
  });
});
