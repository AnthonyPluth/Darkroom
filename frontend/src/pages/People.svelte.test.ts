// @vitest-environment jsdom
import { render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import type { Status } from "$lib/api-types";
import { emptyStatus, familyAccount, joAccount, joPhone, jo, oldPhone, reply, sam, samPhone, serve, shared, solo } from "../test/server";
import People from "./People.svelte";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(1_000_000));
  history.replaceState(null, "", "/people");
  router.sync();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const eligibleStatus: Status = {
  ...emptyStatus,
  people: [
    { id: 1, slug: "jo-example", name: "Jo Example", indexed: 0, with_gps: 0, unknown_time: 0, accounts: [], pixels: [], last_import: null, unmatched: null, alerts: [],
      devices: [{ id: 30, name: "Jo's iPhone", last_checkin: 990, pending_uploads: 0, cleanup_enabled: false, eligible: 212, eligible_bytes: 1_610_612_736, eligible_checked: 990 }] },
  ],
};

const seeded = () => ({
  "GET /api/status": eligibleStatus,
  "GET /api/people": { people: [jo, sam] },
  "GET /api/accounts": { accounts: [joAccount, familyAccount] },
  "GET /api/pixels": { pixels: [solo, shared] },
  "GET /api/people/1/devices": { devices: [joPhone] },
  "GET /api/people/2/devices": { devices: [samPhone, oldPhone] },
});

function show(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(People);
  return server;
}

describe("the People page with no data", () => {
  it("asks for a person first, then an account, and disables what can't be added yet", async () => {
    show({ "GET /api/people": { people: [] }, "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [] }, "GET /api/status": emptyStatus });
    expect(await screen.findByText("Add a person first.")).toBeInTheDocument();
    expect(screen.getByText("Add a Google account first.")).toBeInTheDocument();
    expect(screen.getByText("No iPhone has signed in yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add Pixel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add person" })).toBeEnabled();
  });

  it("says no accounts or Pixels yet once there is a person", async () => {
    show({ "GET /api/people": { people: [jo] }, "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [] }, "GET /api/people/1/devices": { devices: [] }, "GET /api/status": emptyStatus });
    expect(await screen.findByText("No Google accounts yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add account" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Add Pixel" })).toBeDisabled();
  });
});

describe("the People page with data", () => {
  it("lists persons with who serves them, showing who else shares a Pixel", async () => {
    show(seeded());
    const persons = await screen.findByRole("table", { name: "Persons" });
    const rows = within(persons).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Jo Example");
    expect(rows[0]).toHaveTextContent("immich-jo");
    expect(rows[0]).toHaveTextContent("API key saved");
    expect(rows[0]).toHaveTextContent("jo.photos@example.com, family.photos@example.com");
    expect(rows[0]).toHaveTextContent("Pixel 9a, Pixel 8 (also Sam Example)");
    expect(rows[0]).toHaveTextContent("Jo's iPhone");
    expect(rows[1]).toHaveTextContent("Not linked");
    expect(rows[1]).toHaveTextContent("No API key");
    expect(rows[1]).toHaveTextContent("Pixel 8 (also Jo Example)");
    expect(rows[1]).toHaveTextContent("Sam's iPhone");
    expect(rows[1]).not.toHaveTextContent("Old iPhone");
  });

  it("lists accounts with their session and a sign-in link only where one is needed", async () => {
    show(seeded());
    const table = await screen.findByRole("table", { name: "Google accounts" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Session ok");
    expect(within(rows[0]).queryByRole("link")).not.toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("Sign-in needed");
    expect(rows[1]).toHaveTextContent("Jo Example, Sam Example");
    await userEvent.click(within(rows[1]).getByRole("link", { name: "Sign in" }));
    expect(router.path).toBe("/browser");
  });

  it("lists Pixels with the people each serves", async () => {
    show(seeded());
    const table = await screen.findByRole("table", { name: "Pixels" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Pixel 9a");
    expect(rows[0]).toHaveTextContent("jo.photos@example.com");
    expect(rows[0]).toHaveTextContent("Jo Example");
    expect(rows[1]).toHaveTextContent("Jo Example, Sam Example");
  });

  it("shows when each Pixel last synced, and says not synced yet rather than inventing a time", async () => {
    show(seeded());
    const table = await screen.findByRole("table", { name: "Pixels" });
    expect(within(table).getByRole("columnheader", { name: "Last sync" })).toBeInTheDocument();
    const [synced, never] = within(table).getAllByRole("row").slice(1);
    const cell = (row: HTMLElement) => row.querySelector('td[data-label="Last sync"]') as HTMLElement;
    expect(cell(synced)).toHaveTextContent("1 minute ago");
    expect(cell(synced)).not.toHaveClass("text-ink-muted");
    expect(cell(never)).toHaveTextContent("Not synced yet");
    expect(cell(never)).toHaveClass("text-ink-muted");
  });

  it("shows each iPhone with its state and a revoke button only while it works", async () => {
    show(seeded());
    const joCard = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    expect(within(joCard).getByText("Checked in")).toBeInTheDocument();
    expect(within(joCard).getByText("0.1.0")).toBeInTheDocument();
    expect(within(joCard).getByText("just now")).toBeInTheDocument();
    const samCard = screen.getByRole("article", { name: "Sam's iPhone, Sam Example" });
    expect(within(samCard).getByText("Never checked in")).toBeInTheDocument();
    expect(within(samCard).getByText("Never")).toBeInTheDocument();
    expect(within(samCard).getByText("Unknown")).toBeInTheDocument();
    const oldCard = screen.getByRole("article", { name: "Old iPhone, Sam Example" });
    expect(within(oldCard).getByText("Revoked")).toBeInTheDocument();
    expect(within(oldCard).queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows the photos eligible to delete with their space, or that they were not checked", async () => {
    show(seeded());
    const joCard = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    expect(within(joCard).getByText("Eligible to delete")).toBeInTheDocument();
    expect(joCard).toHaveTextContent("212 photos (1.5 GB), confirmed in Google Photos");
    const samCard = screen.getByRole("article", { name: "Sam's iPhone, Sam Example" });
    expect(samCard).toHaveTextContent("Eligible to delete");
    expect(samCard).toHaveTextContent("Not checked yet");
  });

  it("marks an iPhone with uploads waiting", async () => {
    show({ ...seeded(), "GET /api/people/1/devices": { devices: [{ ...joPhone, pending_uploads: 4 }] } });
    const card = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    expect(within(card).getByText("Uploads pending", { selector: "span" })).toHaveAttribute("data-tone", "warn");
  });
});

describe("forms in dialogs", () => {
  it("adds a person and shows the list again", async () => {
    const created = { ...sam, id: 3, name: "Kai", slug: "kai" };
    let listed = [jo];
    const server = show({
      ...seeded(),
      "GET /api/people": () => ({ people: listed }),
      "GET /api/people/1/devices": { devices: [] },
      "POST /api/people": () => {
        listed = [jo, created];
        return reply(201, { person: created });
      },
      "GET /api/people/3/devices": { devices: [] },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    const dialog = await screen.findByRole("dialog", { name: "Add person" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Kai");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add person" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("POST /api/people")[0].body).toEqual({ name: "Kai" });
    expect(await screen.findByText("Kai")).toBeInTheDocument();
  });

  it("keeps the dialog open and says what the server refused", async () => {
    show({ ...seeded(), "POST /api/people": reply(409, { error: "there is already a person with the folder name kai" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "Kai");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add person" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("there is already a person with the folder name kai");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("cancels a dialog without calling the server", async () => {
    const server = show(seeded());
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.calls.filter((c) => c.key.startsWith("POST"))).toEqual([]);
  });

  it("edits a person", async () => {
    const server = show({ ...seeded(), "PATCH /api/people/1": { person: { ...jo, name: "Joanna" } } });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Jo Example" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit person" });
    const name = within(dialog).getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Joanna");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("PATCH /api/people/1")[0].body).toMatchObject({ name: "Joanna", immich_user_id: "immich-jo" });
  });

  it("deletes a person after asking, and reports a refusal", async () => {
    const server = show({ ...seeded(), "DELETE /api/people/2": reply(409, { error: "Sam Example still has devices; remove those first" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Sam Example" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete person" }));
    const ask = await screen.findByRole("dialog", { name: "Delete Sam Example?" });
    expect(ask).toHaveTextContent("Their folders are never touched.");
    await userEvent.click(within(ask).getByRole("button", { name: "Delete person" }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent("still has devices");
    expect(server.sent("DELETE /api/people/2")).toHaveLength(1);
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not show a refused delete's error when another confirmation opens", async () => {
    show({
      ...seeded(),
      "DELETE /api/people/2": reply(409, { error: "Sam Example still has devices; remove those first" }),
      "DELETE /api/devices/30": { device: { ...joPhone, revoked_at: 1 } },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Sam Example" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete person" }));
    const ask = await screen.findByRole("dialog", { name: "Delete Sam Example?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Delete person" }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent("still has devices");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await userEvent.click(await screen.findByRole("button", { name: "Revoke Jo's iPhone, Jo Example" }));
    const revoke = await screen.findByRole("dialog", { name: "Revoke Jo's iPhone?" });
    expect(within(revoke).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("deletes a person that can go and loads the list again", async () => {
    let gone = false;
    const server = show({
      ...seeded(),
      "GET /api/people": () => ({ people: gone ? [jo] : [jo, sam] }),
      "DELETE /api/people/2": () => {
        gone = true;
        return { ok: true };
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Sam Example" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete person" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Delete Sam Example?" })).getByRole("button", { name: "Delete person" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Edit Sam Example" })).not.toBeInTheDocument());
    expect(server.sent("DELETE /api/people/2")).toHaveLength(1);
  });

  it("adds, edits and deletes a Google account", async () => {
    const server = show({
      ...seeded(),
      "POST /api/accounts": reply(201, { account: joAccount }),
      "PATCH /api/accounts/10": { account: joAccount },
      "DELETE /api/accounts/10": { ok: true },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add account" }));
    let dialog = await screen.findByRole("dialog", { name: "Add Google account" });
    await userEvent.type(within(dialog).getByLabelText("Google account email"), "new@example.com");
    await userEvent.selectOptions(within(dialog).getByLabelText("Default person"), "Sam Example");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add account" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("POST /api/accounts")[0].body).toEqual({ email: "new@example.com", default_person_id: 2 });

    await userEvent.click(screen.getByRole("button", { name: "Edit jo.photos@example.com" }));
    dialog = await screen.findByRole("dialog", { name: "Edit Google account" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("PATCH /api/accounts/10")[0].body).toEqual({ email: "jo.photos@example.com", default_person_id: 1 });

    await userEvent.click(screen.getByRole("button", { name: "Edit jo.photos@example.com" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete account" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Delete jo.photos@example.com?" })).getByRole("button", { name: "Delete account" }));
    await waitFor(() => expect(server.sent("DELETE /api/accounts/10")).toHaveLength(1));
  });

  it("adds, edits and deletes a Pixel", async () => {
    const server = show({
      ...seeded(),
      "POST /api/pixels": reply(201, { pixel: solo }),
      "PATCH /api/pixels/20": { pixel: solo },
      "DELETE /api/pixels/20": { ok: true },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add Pixel" }));
    let dialog = await screen.findByRole("dialog", { name: "Add Pixel" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Pixel 7");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add Pixel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("POST /api/pixels")[0].body).toEqual({ name: "Pixel 7", google_account_id: 10, syncthing_device_id: null, people_ids: [1] });

    await userEvent.click(screen.getByRole("button", { name: "Edit Pixel 9a" }));
    dialog = await screen.findByRole("dialog", { name: "Edit Pixel" });
    await userEvent.click(within(dialog).getByLabelText("Sam Example"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("PATCH /api/pixels/20")[0].body).toMatchObject({ people_ids: [1, 2], syncthing_device_id: "AAA-111" });

    await userEvent.click(screen.getByRole("button", { name: "Edit Pixel 9a" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete Pixel" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Delete Pixel 9a?" })).getByRole("button", { name: "Delete Pixel" }));
    await waitFor(() => expect(server.sent("DELETE /api/pixels/20")).toHaveLength(1));
  });

  it("revokes an iPhone after asking", async () => {
    const server = show({ ...seeded(), "DELETE /api/devices/30": { device: { ...joPhone, revoked_at: 1 } } });
    await userEvent.click(await screen.findByRole("button", { name: "Revoke Jo's iPhone, Jo Example" }));
    const ask = await screen.findByRole("dialog", { name: "Revoke Jo's iPhone?" });
    expect(ask).toHaveTextContent("Photos already uploaded stay where they are.");
    await userEvent.click(within(ask).getByRole("button", { name: "Revoke iPhone" }));
    await waitFor(() => expect(server.sent("DELETE /api/devices/30")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("the Cleanup switch", () => {
  const ON = { ...joPhone, cleanup_enabled: true };
  const turnOn = () => screen.findByRole("switch", { name: "Cleanup off for Jo's iPhone, Jo Example" });

  it("is off with its sentence, and absent on a revoked iPhone", async () => {
    show(seeded());
    const joCard = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    expect(await turnOn()).toHaveAttribute("aria-checked", "false");
    expect(joCard).toHaveTextContent("Nothing is deleted from this phone while cleanup is off.");
    const oldCard = screen.getByRole("article", { name: "Old iPhone, Sam Example" });
    expect(within(oldCard).queryByRole("switch")).not.toBeInTheDocument();
  });

  it("asks about iCloud Photos before turning on, then sends the confirmation", async () => {
    let enabled = false;
    const server = show({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [{ ...joPhone, cleanup_enabled: enabled }] }),
      "PATCH /api/devices/30": () => {
        enabled = true;
        return { device: ON };
      },
    });
    await userEvent.click(await turnOn());
    const ask = await screen.findByRole("dialog", { name: "Turn on cleanup for Jo's iPhone, Jo Example?" });
    expect(ask).toHaveTextContent("Confirm that iCloud Photos is off on this iPhone. With it on, deleting a photo from the phone also removes it from iCloud and every other device.");
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(0);
    await userEvent.click(within(ask).getByRole("button", { name: "Turn on cleanup" }));
    await waitFor(() => expect(server.sent("PATCH /api/devices/30")).toHaveLength(1));
    expect(server.sent("PATCH /api/devices/30")[0]?.body).toEqual({ cleanup_enabled: true, icloud_photos_off: true });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" })).toHaveAttribute("aria-checked", "true");
  });

  it("leaves it off and sends nothing when the question is cancelled", async () => {
    const server = show({ ...seeded(), "PATCH /api/devices/30": { device: ON } });
    await userEvent.click(await turnOn());
    const ask = await screen.findByRole("dialog", { name: "Turn on cleanup for Jo's iPhone, Jo Example?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(0);
    expect(await turnOn()).toHaveAttribute("aria-checked", "false");
  });

  it("stays off and says why when turning on fails", async () => {
    show({ ...seeded(), "PATCH /api/devices/30": reply(409, { error: "confirm that iCloud Photos is off on this iPhone before turning cleanup on" }) });
    await userEvent.click(await turnOn());
    const ask = await screen.findByRole("dialog", { name: "Turn on cleanup for Jo's iPhone, Jo Example?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Turn on cleanup" }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent("confirm that iCloud Photos is off");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(await turnOn()).toHaveAttribute("aria-checked", "false");
  });

  it("turns off at once, with no question", async () => {
    let off = false;
    const server = show({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [off ? joPhone : ON] }),
      "PATCH /api/devices/30": () => {
        off = true;
        return { device: joPhone };
      },
    });
    await userEvent.click(await screen.findByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" }));
    await waitFor(() => expect(server.sent("PATCH /api/devices/30")).toHaveLength(1));
    expect(server.sent("PATCH /api/devices/30")[0]?.body).toEqual({ cleanup_enabled: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await turnOn()).toHaveAttribute("aria-checked", "false");
  });

  it("reloads the eligible photos after turning off, so no stale count stays", async () => {
    let off = false;
    const status = (cleared: boolean): Status => ({
      ...emptyStatus,
      people: [{ ...eligibleStatus.people[0]!, devices: [{ ...eligibleStatus.people[0]!.devices[0]!, cleanup_enabled: !cleared, eligible: cleared ? null : 212, eligible_bytes: cleared ? null : 1_610_612_736, eligible_checked: cleared ? null : 990 }] }],
    });
    show({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [{ ...ON, cleanup_enabled: !off }] }),
      "GET /api/status": () => status(off),
      "PATCH /api/devices/30": () => {
        off = true;
        return { device: joPhone };
      },
    });
    const joCard = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    await waitFor(() => expect(joCard).toHaveTextContent("212 photos (1.5 GB), confirmed in Google Photos"));
    await userEvent.click(within(joCard).getByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" }));
    const after = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    await waitFor(() => expect(after).toHaveTextContent("Not checked yet"));
    expect(after).not.toHaveTextContent("212 photos");
  });

  it("keeps the old count when turning off fails", async () => {
    show({ ...seeded(), "GET /api/people/1/devices": { devices: [ON] }, "PATCH /api/devices/30": reply(503, { error: "busy" }) });
    const joCard = await screen.findByRole("article", { name: "Jo's iPhone, Jo Example" });
    await userEvent.click(within(joCard).getByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" }));
    expect(await within(joCard).findByRole("alert")).toHaveTextContent("busy");
    expect(joCard).toHaveTextContent("212 photos (1.5 GB), confirmed in Google Photos");
  });

  it("ignores a second activation while the first request is pending", async () => {
    let off = false;
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const server = serve({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [off ? joPhone : ON] }),
      "PATCH /api/devices/30": () => {
        off = true;
        return { device: joPhone };
      },
    });
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => (init?.method === "PATCH" ? gate.then(() => server.mock(url, init)) : server.mock(url, init)));
    render(People);
    const flip = await screen.findByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" });
    await userEvent.click(flip);
    await waitFor(() => expect(flip).toBeDisabled());
    await userEvent.click(flip);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(0);
    release();
    await waitFor(() => expect(flip).toBeEnabled());
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(1);
    expect(await turnOn()).toHaveAttribute("aria-checked", "false");
  });

  it("cannot be backed out of while turning on is pending", async () => {
    let enabled = false;
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const server = serve({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [{ ...joPhone, cleanup_enabled: enabled }] }),
      "PATCH /api/devices/30": () => {
        enabled = true;
        return { device: ON };
      },
    });
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => (init?.method === "PATCH" ? gate.then(() => server.mock(url, init)) : server.mock(url, init)));
    render(People);
    await userEvent.click(await turnOn());
    const ask = await screen.findByRole("dialog", { name: "Turn on cleanup for Jo's iPhone, Jo Example?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Turn on cleanup" }));
    await waitFor(() => expect(within(ask).getByRole("button", { name: "Cancel" })).toBeDisabled());
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Turn on cleanup for Jo's iPhone, Jo Example?" })).toBeInTheDocument();
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(0);
    release();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(1);
    expect(await screen.findByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" })).toHaveAttribute("aria-checked", "true");
  });

  it("puts the switch back on and shows the error when turning off fails", async () => {
    show({ ...seeded(), "GET /api/people/1/devices": { devices: [ON] }, "PATCH /api/devices/30": reply(503, { error: "Darkroom is busy with another change; try again in a moment" }) });
    await userEvent.click(await screen.findByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" }));
    const joCard = screen.getByRole("article", { name: "Jo's iPhone, Jo Example" });
    expect(await within(joCard).findByRole("alert")).toHaveTextContent("Darkroom is busy");
    expect(screen.getByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" })).toHaveAttribute("aria-checked", "true");
  });
});

function hold(server: ReturnType<typeof serve>, rule: (key: string, nth: number) => Promise<void> | undefined) {
  const seen = new Map<string, number>();
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    const nth = (seen.get(key) ?? 0) + 1;
    seen.set(key, nth);
    const held = rule(key, nth);
    return held ? held.then(() => server.mock(url, init)) : server.mock(url, init);
  });
  render(People);
}

const later = () => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  return { gate, release };
};

const switchOf = (name: string, on: boolean) => screen.findByRole("switch", { name: `Cleanup ${on ? "on" : "off"} for ${name}`, hidden: true });

describe("the Cleanup switch, with other requests in flight", () => {
  const SAM_ON = { ...samPhone, cleanup_enabled: true };
  const JO_ON = { ...joPhone, cleanup_enabled: true };

  it("stays busy when an unrelated reload finishes during a pending turn-off", async () => {
    const patch = later();
    let off = false;
    const server = serve({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [off ? joPhone : JO_ON] }),
      "PATCH /api/people/1": { person: jo },
      "PATCH /api/devices/30": () => {
        off = true;
        return { device: joPhone };
      },
    });
    hold(server, (key) => (key === "PATCH /api/devices/30" ? patch.gate : undefined));
    await userEvent.click(await switchOf("Jo's iPhone, Jo Example", true));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Cleanup off for Jo's iPhone, Jo Example" })).toBeDisabled());
    await userEvent.click(await screen.findByRole("button", { name: "Edit Jo Example" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit person" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(server.sent("GET /api/people")).toHaveLength(2));
    const flip = await switchOf("Jo's iPhone, Jo Example", false);
    expect(flip).toBeDisabled();
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(0);
    patch.release();
    await waitFor(() => expect(flip).toBeEnabled());
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(1);
  });

  it("stays busy through its own reload while another device's request settles and reloads", async () => {
    const samPatch = later();
    const joReload = later();
    let samOff = false;
    let joOn = false;
    const server = serve({
      ...seeded(),
      "GET /api/people/1/devices": () => ({ devices: [{ ...joPhone, cleanup_enabled: joOn }] }),
      "GET /api/people/2/devices": () => ({ devices: [samOff ? samPhone : SAM_ON, oldPhone] }),
      "PATCH /api/devices/30": () => {
        joOn = true;
        return { device: JO_ON };
      },
      "PATCH /api/devices/31": () => {
        samOff = true;
        return { device: samPhone };
      },
    });
    hold(server, (key, nth) => (key === "PATCH /api/devices/31" ? samPatch.gate : key === "GET /api/people" && nth === 2 ? joReload.gate : undefined));
    await userEvent.click(await switchOf("Sam's iPhone, Sam Example", true));
    await userEvent.click(await switchOf("Jo's iPhone, Jo Example", false));
    const ask = await screen.findByRole("dialog", { name: "Turn on cleanup for Jo's iPhone, Jo Example?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Turn on cleanup" }));
    await waitFor(() => expect(server.sent("PATCH /api/devices/30")).toHaveLength(1));
    expect(await switchOf("Sam's iPhone, Sam Example", false)).toBeDisabled();
    samPatch.release();
    await waitFor(() => expect(server.sent("GET /api/people")).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Cleanup off for Sam's iPhone, Sam Example", hidden: true })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example", hidden: true })).toBeDisabled();
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(1);
    joReload.release();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await switchOf("Jo's iPhone, Jo Example", true)).toBeEnabled();
    expect(server.sent("PATCH /api/devices/30")).toHaveLength(1);
    expect(server.sent("PATCH /api/devices/31")).toHaveLength(1);
  });

  it("clears only its own device when a request fails", async () => {
    const samPatch = later();
    const server = serve({
      ...seeded(),
      "GET /api/people/1/devices": { devices: [JO_ON] },
      "GET /api/people/2/devices": { devices: [SAM_ON, oldPhone] },
      "PATCH /api/devices/30": reply(503, { error: "busy" }),
      "PATCH /api/devices/31": { device: samPhone },
    });
    hold(server, (key) => (key === "PATCH /api/devices/31" ? samPatch.gate : undefined));
    await userEvent.click(await switchOf("Sam's iPhone, Sam Example", true));
    await userEvent.click(await switchOf("Jo's iPhone, Jo Example", true));
    const joCard = screen.getByRole("article", { name: "Jo's iPhone, Jo Example" });
    expect(await within(joCard).findByRole("alert")).toHaveTextContent("busy");
    expect(await switchOf("Jo's iPhone, Jo Example", true)).toBeEnabled();
    expect(await switchOf("Sam's iPhone, Sam Example", false)).toBeDisabled();
    samPatch.release();
    await waitFor(() => expect(screen.getByRole("switch", { name: "Cleanup on for Sam's iPhone, Sam Example" })).toBeEnabled());
    expect(screen.getByRole("switch", { name: "Cleanup on for Jo's iPhone, Jo Example" })).toBeEnabled();
  });
});

describe("when loading fails", () => {
  it("says why and tries again, with no stale list", async () => {
    let attempt = 0;
    show({ ...seeded(), "GET /api/accounts": () => (attempt++ === 0 ? reply(503, {}) : { accounts: [joAccount] }) });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the Google accounts (503)");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Persons" })).toBeInTheDocument();
  });

  it("names the person whose iPhones couldn't be loaded", async () => {
    show({ ...seeded(), "GET /api/people/2/devices": reply(500, {}) });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the iPhones of Sam Example (500)");
  });
});
