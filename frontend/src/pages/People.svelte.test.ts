// @vitest-environment jsdom
import { render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import { emptyStatus, familyAccount, joAccount, jo, reply, sam, serve, shared, solo } from "../test/server";
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
  "GET /api/pixels": { pixels: [solo, shared] },
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
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add Pixel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add person" })).toBeEnabled();
  });

  it("says no accounts or Pixels yet once there is a person", async () => {
    show({ "GET /api/people": { people: [jo] }, "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [] }, "GET /api/status": emptyStatus });
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
