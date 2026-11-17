// @vitest-environment jsdom
import { render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import { emptyStatus, familyAccount, joAccount, jo, reply, sam, serve, shared, solo, transport } from "../test/server";
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

const seeded = () => ({
  "GET /api/people": { people: [jo, sam] },
  "GET /api/accounts": { accounts: [joAccount, familyAccount] },
  "GET /api/pixels": { pixels: [solo, shared], syncthing: null },
});

function show(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(People);
  return server;
}

describe("the People page with no data", () => {
  it("asks for a person first, then an account, and disables what can't be added yet", async () => {
    show({ "GET /api/people": { people: [] }, "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [], syncthing: null }, "GET /api/status": emptyStatus });
    expect(await screen.findByText("Add a person first.")).toBeInTheDocument();
    expect(screen.getByText("Add a Google account first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add Pixel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add person" })).toBeEnabled();
  });

  it("says no accounts or Pixels yet once there is a person", async () => {
    show({ "GET /api/people": { people: [jo] }, "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [], syncthing: null }, "GET /api/status": emptyStatus });
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
    expect(rows[1]).toHaveTextContent("Not linked");
    expect(rows[1]).toHaveTextContent("No API key");
    expect(rows[1]).toHaveTextContent("Pixel 8 (also Jo Example)");
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
});

describe("forms in dialogs", () => {
  it("adds a person and shows the list again", async () => {
    const created = { ...sam, id: 3, name: "Kai", slug: "kai" };
    let listed = [jo];
    const server = show({
      ...seeded(),
      "GET /api/people": () => ({ people: listed }),
      "POST /api/people": () => {
        listed = [jo, created];
        return reply(201, { person: created });
      },
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
    const server = show({ ...seeded(), "DELETE /api/people/2": reply(409, { error: "Sam Example still has Pixels that serve them; remove those first" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Sam Example" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete person" }));
    const ask = await screen.findByRole("dialog", { name: "Delete Sam Example?" });
    expect(ask).toHaveTextContent("Their folders are never touched.");
    await userEvent.click(within(ask).getByRole("button", { name: "Delete person" }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent("still has Pixels that serve them");
    expect(server.sent("DELETE /api/people/2")).toHaveLength(1);
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not show a refused delete's error when another confirmation opens", async () => {
    show({
      ...seeded(),
      "DELETE /api/people/2": reply(409, { error: "Sam Example still has Pixels that serve them; remove those first" }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Sam Example" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete person" }));
    const ask = await screen.findByRole("dialog", { name: "Delete Sam Example?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Delete person" }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent("still has Pixels that serve them");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await userEvent.click(await screen.findByRole("button", { name: "Edit Pixel 9a" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete Pixel" }));
    const other = await screen.findByRole("dialog", { name: "Delete Pixel 9a?" });
    expect(within(other).queryByRole("alert")).not.toBeInTheDocument();
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
});

describe("the People page with Darkroom's Syncthing", () => {
  const ON = { "GET /api/pairing/device": { device_id: "DRKROOM-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG" }, "GET /api/pairing/waiting": { devices: [] } };
  const withSyncthing = () => ({
    ...seeded(),
    ...ON,
    "GET /api/pixels": { pixels: [solo, shared], syncthing: [transport(20, "Pixel 9a", "connected", { completion: 100 }), transport(21, "Pixel 8", "not_paired")] },
  });

  it("has no Syncthing column or Pair button when Syncthing is off", async () => {
    show(seeded());
    const table = await screen.findByRole("table", { name: "Pixels" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Device", "Google account", "People served", "Last sync", ""]);
    expect(within(table).queryByRole("button", { name: "Pair Pixel 8" })).not.toBeInTheDocument();
  });

  it("asks whether Darkroom's Syncthing pairs, which the setup wizard's Pixels step never does", async () => {
    const server = show(withSyncthing());
    await screen.findByRole("table", { name: "Pixels" });
    expect(server.sent("GET /api/pairing/device")).toHaveLength(1);
  });

  it("puts a Syncthing column between People served and Last sync, with each Pixel's chip and line", async () => {
    show(withSyncthing());
    const table = await screen.findByRole("table", { name: "Pixels" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Device", "Google account", "People served", "Syncthing", "Last sync", ""]);
    const [paired, unpaired] = within(table).getAllByRole("row").slice(1);
    const cell = (row: HTMLElement) => row.querySelector('td[data-label="Syncthing"]') as HTMLElement;
    expect(cell(paired)).toHaveTextContent(/Connected\s*Up to date/);
    expect(cell(unpaired)).toHaveTextContent(/Not paired\s*Pair it to start sending photos\./);
    expect(within(paired).queryByRole("button", { name: "Pair Pixel 9a" })).not.toBeInTheDocument();
  });

  it("gives an unpaired Pixel a Pair button before Edit, which opens the pairing step", async () => {
    show(withSyncthing());
    const table = await screen.findByRole("table", { name: "Pixels" });
    const unpaired = within(table).getAllByRole("row")[2];
    expect(within(unpaired).getAllByRole("button").map((button) => button.textContent)).toEqual(["Pair", "Edit"]);
    await userEvent.click(within(unpaired).getByRole("button", { name: "Pair Pixel 8" }));
    expect(await screen.findByRole("dialog", { name: "Pair Pixel 8" })).toBeInTheDocument();
  });

  it("gives a Pixel whose device ID is not valid or shared a Pair again button before Edit, which opens the pairing step", async () => {
    show({
      ...seeded(),
      ...ON,
      "GET /api/pixels": { pixels: [solo, { ...shared, syncthing_device_id: "OLD-ID" }], syncthing: [
        transport(20, "Pixel 9a", "device_id_invalid"), transport(21, "Pixel 8", "device_id_shared", { others: ["Pixel 7"] })] },
    });
    const table = await screen.findByRole("table", { name: "Pixels" });
    const [invalid, sharing] = within(table).getAllByRole("row").slice(1);
    const cell = (row: HTMLElement) => row.querySelector('td[data-label="Syncthing"]') as HTMLElement;
    expect(cell(invalid)).toHaveTextContent(/Device ID not valid\s*Its saved device ID is not a Syncthing device ID, so nothing is shared with it\. Pair it again\./);
    expect(cell(sharing)).toHaveTextContent(/Same device ID\s*Pixel 7 has the same device ID, so Darkroom shares neither folder\. Pair again whichever one is wrong\./);
    for (const row of [invalid, sharing]) expect(within(row).getAllByRole("button").map((button) => button.textContent)).toEqual(["Pair again", "Edit"]);
    await userEvent.click(within(sharing).getByRole("button", { name: "Pair again: Pixel 8" }));
    const dialog = await screen.findByRole("dialog", { name: "Pair Pixel 8" });
    expect(within(dialog).getByRole("img", { name: "QR code of Darkroom's device ID" })).toBeInTheDocument();
  });

  it("gives a refused folder its chip and no button but Edit", async () => {
    show({ ...seeded(), "GET /api/pixels": { pixels: [solo], syncthing: [transport(20, "Pixel 9a", "folder_path_refused")] } });
    const row = within(await screen.findByRole("table", { name: "Pixels" })).getAllByRole("row")[1];
    expect(row).toHaveTextContent(/Folder refused\s*Its folder leads outside the photo share, so Darkroom does not share it\./);
    expect(within(row).getAllByRole("button").map((button) => button.textContent)).toEqual(["Edit"]);
  });

  it("shares a removed folder again only after the confirmation, says why a press was refused, and shows Checking once done", async () => {
    let released = false;
    let answer: (value: Response) => void = () => {};
    const server = serve({
      ...seeded(),
      "GET /api/pixels": () => ({ pixels: [solo], syncthing: [transport(20, "Pixel 9a", released ? "checking" : "folder_removed")] }),
    });
    vi.stubGlobal("fetch", (url: string, init: RequestInit = {}) => {
      if (url !== "/api/pixels/20/release-folder") return server.mock(url, init);
      server.calls.push({ key: `POST ${url}`, body: JSON.parse(String(init.body)) });
      return new Promise<Response>((resolve) => (answer = resolve));
    });
    render(People);
    const row = within(await screen.findByRole("table", { name: "Pixels" })).getAllByRole("row")[1];
    expect(row).toHaveTextContent(/Folder removed\s*Darkroom removed its folder because it was changed outside Darkroom\. It stays unshared until you share it again\./);
    expect(within(row).getAllByRole("button").map((button) => button.textContent)).toEqual(["Share again", "Edit"]);
    await userEvent.click(within(row).getByRole("button", { name: "Share again: Pixel 9a's folder" }));
    const ask = await screen.findByRole("dialog", { name: "Share Pixel 9a's folder again?" });
    for (const paragraph of [
      "Darkroom removed Pixel 9a's folder from its Syncthing because the folder was changed outside Darkroom: it was no longer Send Only, or no longer at Pixel 9a's folder on the photo share.",
      "If you share it again, Darkroom adds the folder afresh, Send Only, at Pixel 9a's folder, as soon as the photo share passes its check and the folder is not held. If the Syncthing app on Pixel 9a asks, accept the folder there.",
      "If the folder is changed outside Darkroom again, Darkroom removes it again.",
    ]) expect(within(ask).getByText(paragraph)).toBeInTheDocument();
    expect(within(ask).getAllByRole("button").map((button) => button.textContent)).toEqual(["Cancel", "Share folder again"]);
    expect(within(ask).getByRole("button", { name: "Share folder again" })).toHaveClass("btn-primary");
    expect(server.sent("POST /api/pixels/20/release-folder")).toHaveLength(0);
    await userEvent.click(within(ask).getByRole("button", { name: "Share folder again" }));
    await waitFor(() => expect(within(ask).getByRole("button", { name: "Cancel" })).toBeDisabled());
    expect(within(ask).getByRole("button", { name: "Share folder again" })).toBeDisabled();
    expect(server.sent("POST /api/pixels/20/release-folder")[0].body).toEqual({ confirmed: true });
    answer(new Response(JSON.stringify({ error: "Darkroom has not removed this Pixel's Syncthing folder, so there is nothing to share again" }), { status: 409 }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent(
      "Couldn't share Pixel 9a's folder again: Darkroom has not removed this Pixel's Syncthing folder, so there is nothing to share again.");
    expect(within(ask).getByRole("button", { name: "Share folder again" })).toBeEnabled();
    await userEvent.click(within(ask).getByRole("button", { name: "Share folder again" }));
    await waitFor(() => expect(within(ask).queryByRole("alert")).not.toBeInTheDocument());
    released = true;
    answer(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const after = within(await screen.findByRole("table", { name: "Pixels" })).getAllByRole("row")[1];
    await waitFor(() => expect(after).toHaveTextContent(/Checking\s*Darkroom is checking its folder before sharing it\./));
    expect(within(after).getAllByRole("button").map((button) => button.textContent)).toEqual(["Edit"]);
  });

  it("adds a full stop after a refusal that has none and keeps one that has", async () => {
    const server = serve({ ...seeded(), "GET /api/pixels": { pixels: [solo], syncthing: [transport(20, "Pixel 9a", "folder_removed")] } });
    vi.stubGlobal("fetch", (url: string, init: RequestInit = {}) => url === "/api/pixels/20/release-folder" ? Promise.reject(new TypeError("offline")) : server.mock(url, init));
    render(People);
    await userEvent.click(await screen.findByRole("button", { name: "Share again: Pixel 9a's folder" }));
    const ask = await screen.findByRole("dialog", { name: "Share Pixel 9a's folder again?" });
    await userEvent.click(within(ask).getByRole("button", { name: "Share folder again" }));
    expect(await within(ask).findByRole("alert")).toHaveTextContent(/^Couldn't share Pixel 9a's folder again: Can't reach Darkroom\. Check your connection and try again\.$/);
  });
});

describe("pairing a Pixel with Darkroom's Syncthing on", () => {
  const OWN = "DRKROOM-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG";
  const PHONE = { device_id: "PIXELNA-MADEUP2-EXAMPLE-ONLYAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE", name: "Pixel 7" };
  const added = { ...shared, id: 22, name: "Pixel 7" };
  const on = () => ({ ...seeded(), "GET /api/pairing/device": { device_id: OWN }, "GET /api/pairing/waiting": { devices: [PHONE] } });

  it("offers Pair only on an unpaired Pixel, and opens the modal straight at the pairing step", async () => {
    show(on());
    expect(await screen.findByRole("button", { name: "Pair Pixel 8" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pair Pixel 9a" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Pair Pixel 8" }));
    const dialog = await screen.findByRole("dialog", { name: "Pair Pixel 8" });
    expect(dialog).toHaveAccessibleDescription("Connect the Syncthing app on Pixel 8 to Darkroom. Nothing is shared until you approve the Pixel here.");
    expect(within(dialog).getByRole("img", { name: "QR code of Darkroom's device ID" })).toBeInTheDocument();
  });

  it("adds a Pixel, moves the same modal on to pairing it, and closes once it is paired", async () => {
    const server = show({ ...on(), "POST /api/pixels": reply(201, { pixel: added }), "POST /api/pixels/22/pair": { pixel: { ...added, syncthing_device_id: PHONE.device_id } } });
    await userEvent.click(await screen.findByRole("button", { name: "Add Pixel" }));
    let dialog = await screen.findByRole("dialog", { name: "Add Pixel" });
    expect(within(dialog).queryByLabelText(/Syncthing device ID/)).not.toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText("Name"), "Pixel 7");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add Pixel" }));
    dialog = await screen.findByRole("dialog", { name: "Pair Pixel 7" });
    await waitFor(() => expect(server.sent("GET /api/pixels")).toHaveLength(2));
    await userEvent.click(await within(dialog).findByRole("button", { name: "This is Pixel 7" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await within(dialog).findByText("Darkroom is sharing Pixel 7's folder with it.")).toBeInTheDocument();
    expect(within(dialog).getByText("Paired")).toBeInTheDocument();
    expect(within(dialog).getByText("In Google Photos on the Pixel, turn on backup for that folder.")).toBeInTheDocument();
    await waitFor(() => expect(server.sent("GET /api/pixels")).toHaveLength(3));
    await userEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("leaves an unpaired Pixel behind when pairing is put off", async () => {
    show(on());
    await userEvent.click(await screen.findByRole("button", { name: "Edit Pixel 8" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Pixel" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Pair" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Pair Pixel 8" })).getByRole("button", { name: "Pair later" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps today's form and no Pair button with Darkroom's Syncthing off", async () => {
    show({ ...seeded(), "GET /api/pairing/device": reply(409, { error: "Darkroom's Syncthing is off" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Pixel 8" }));
    expect(within(await screen.findByRole("dialog", { name: "Edit Pixel" })).getByLabelText("Syncthing device ID (optional)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pair Pixel 8" })).not.toBeInTheDocument();
  });

  it("still offers pairing when Darkroom's Syncthing is on but not answering", async () => {
    show({ ...seeded(), "GET /api/pairing/device": reply(503, { error: "Darkroom's Syncthing could not be reached" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Pair Pixel 8" }));
    expect(await screen.findByLabelText("Syncthing device ID")).toBeInTheDocument();
  });
});
