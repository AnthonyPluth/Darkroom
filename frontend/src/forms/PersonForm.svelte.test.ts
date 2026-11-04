// @vitest-environment jsdom
import { render, screen } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jo, reply, sam, serve } from "../test/server";
import PersonForm from "./PersonForm.svelte";

afterEach(() => vi.unstubAllGlobals());

function mount(person: typeof jo | null, routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  const onsaved = vi.fn();
  const oncancel = vi.fn();
  const ondelete = vi.fn();
  render(PersonForm, { person, onsaved, oncancel, ondelete });
  return { server, onsaved, oncancel, ondelete };
}

describe("adding a person", () => {
  it("sends only what was filled in, and the key as typed", async () => {
    const { server, onsaved } = mount(null, { "POST /api/people": reply(201, { person: jo }) });
    await userEvent.type(screen.getByLabelText("Name"), "Kai Example");
    await userEvent.type(screen.getByLabelText("Folder name"), "kai");
    await userEvent.type(screen.getByLabelText("Email (optional)"), "kai@example.com");
    await userEvent.type(screen.getByLabelText("Immich user ID (optional)"), "u-9");
    await userEvent.type(screen.getByLabelText("Immich library ID (optional)"), "l-9");
    await userEvent.type(screen.getByLabelText("Immich API key"), "  secret-key-1  ");
    expect(screen.getByLabelText("Immich API key")).toHaveAttribute("type", "password");
    expect(screen.queryByText("Remove the saved key")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete person" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    expect(server.sent("POST /api/people")[0].body).toEqual({
      name: "Kai Example", slug: "kai", email: "kai@example.com", immich_user_id: "u-9", immich_library_id: "l-9", immich_api_key: "secret-key-1",
    });
    expect(onsaved).toHaveBeenCalledOnce();
  });

  it("sends just the name when that is all there is", async () => {
    const { server } = mount(null, { "POST /api/people": reply(201, { person: jo }) });
    await userEvent.type(screen.getByLabelText("Name"), "Kai");
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    expect(server.sent("POST /api/people")[0].body).toEqual({ name: "Kai" });
  });

  it("shows the refusal in place and does not report saved", async () => {
    const { onsaved } = mount(null, { "POST /api/people": reply(400, { error: "slug must be lower-case letters" }) });
    await userEvent.type(screen.getByLabelText("Name"), "Kai");
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("slug must be lower-case letters");
    expect(onsaved).not.toHaveBeenCalled();
  });

  it("cancels", async () => {
    const { oncancel } = mount(null, {});
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(oncancel).toHaveBeenCalledOnce();
  });
});

describe("editing a person", () => {
  it("fills the form, hides the folder name and never sends the saved key unless asked", async () => {
    const { server, onsaved } = mount(jo, { "PATCH /api/people/1": { person: jo } });
    expect(screen.getByLabelText("Name")).toHaveValue("Jo Example");
    expect(screen.getByLabelText("Immich user ID (optional)")).toHaveValue("immich-jo");
    expect(screen.getByLabelText("Immich API key")).toHaveValue("");
    expect(screen.getByText(/A key is saved and is never shown again/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Folder name")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(server.sent("PATCH /api/people/1")[0].body).toEqual({ name: "Jo Example", email: "jo@example.com", immich_user_id: "immich-jo", immich_library_id: null });
    expect(onsaved).toHaveBeenCalledOnce();
  });

  it("replaces the key when a new one is typed", async () => {
    const { server } = mount(jo, { "PATCH /api/people/1": { person: jo } });
    await userEvent.type(screen.getByLabelText("Immich API key"), "new-key-123");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(server.sent("PATCH /api/people/1")[0].body).toMatchObject({ immich_api_key: "new-key-123" });
  });

  it("removes the key on request, and clears fields that were emptied", async () => {
    const { server } = mount(jo, { "PATCH /api/people/1": { person: jo } });
    await userEvent.click(screen.getByLabelText("Remove the saved key"));
    await userEvent.clear(screen.getByLabelText("Email (optional)"));
    await userEvent.clear(screen.getByLabelText("Immich user ID (optional)"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(server.sent("PATCH /api/people/1")[0].body).toEqual({ name: "Jo Example", email: null, immich_user_id: null, immich_library_id: null, immich_api_key: null });
  });

  it("explains when no key is saved yet, and offers delete", async () => {
    const { ondelete } = mount(sam, {});
    expect(screen.getByText("Stored encrypted. Darkroom never shows it again.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete person" }));
    expect(ondelete).toHaveBeenCalledOnce();
  });
});
