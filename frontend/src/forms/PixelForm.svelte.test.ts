// @vitest-environment jsdom
import { render, screen } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PersonView, PixelView } from "$lib/api-types";
import { familyAccount, jo, joAccount, reply, sam, serve, shared, solo } from "../test/server";
import PixelForm from "./PixelForm.svelte";

afterEach(() => vi.unstubAllGlobals());

const kai: PersonView = { ...sam, id: 3, name: "Kai Example", slug: "kai-example" };
const everyone = [jo, sam, kai];

function mount(pixel: PixelView | null, routes: Parameters<typeof serve>[0] = {}) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  const onsaved = vi.fn();
  render(PixelForm, { pixel, accounts: [joAccount, familyAccount], people: everyone, onsaved, oncancel: vi.fn(), ondelete: vi.fn() });
  return { server, onsaved };
}

const box = (name: string) => screen.getByRole("checkbox", { name });

describe("adding a Pixel", () => {
  it("serves the account's default person unless told otherwise", async () => {
    const { server, onsaved } = mount(null, { "POST /api/pixels": reply(201, { pixel: solo }) });
    expect(box("Jo Example")).toBeChecked();
    expect(box("Sam Example")).not.toBeChecked();
    await userEvent.type(screen.getByLabelText("Name"), "Pixel 7");
    await userEvent.click(screen.getByRole("button", { name: "Add Pixel" }));
    expect(server.sent("POST /api/pixels")[0].body).toEqual({ name: "Pixel 7", google_account_id: 10, syncthing_device_id: null, people_ids: [1] });
    expect(onsaved).toHaveBeenCalledOnce();
  });

  it("follows the chosen account's default person until a person is ticked by hand", async () => {
    mount(null, {});
    await userEvent.selectOptions(screen.getByLabelText("Google account"), "family.photos@example.com");
    expect(box("Jo Example")).not.toBeChecked();
    expect(box("Sam Example")).toBeChecked();
    await userEvent.click(box("Kai Example"));
    await userEvent.selectOptions(screen.getByLabelText("Google account"), "jo.photos@example.com");
    expect(box("Sam Example")).toBeChecked();
    expect(box("Jo Example")).not.toBeChecked();
  });

  it("sends three people for a shared Pixel, in order", async () => {
    const { server } = mount(null, { "POST /api/pixels": reply(201, { pixel: shared }) });
    await userEvent.type(screen.getByLabelText("Name"), "Shared");
    await userEvent.type(screen.getByLabelText("Syncthing device ID (optional)"), " ABC-123 ");
    await userEvent.click(box("Kai Example"));
    await userEvent.click(box("Sam Example"));
    await userEvent.click(screen.getByRole("button", { name: "Add Pixel" }));
    expect(server.sent("POST /api/pixels")[0].body).toEqual({ name: "Shared", google_account_id: 10, syncthing_device_id: "ABC-123", people_ids: [1, 2, 3] });
  });

  it("will not send a Pixel that serves no one", async () => {
    const { server, onsaved } = mount(null);
    await userEvent.type(screen.getByLabelText("Name"), "Empty");
    await userEvent.click(box("Jo Example"));
    await userEvent.click(screen.getByRole("button", { name: "Add Pixel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose at least one person this Pixel serves.");
    expect(server.calls).toEqual([]);
    expect(onsaved).not.toHaveBeenCalled();
  });

  it("shows what the server refused", async () => {
    mount(null, { "POST /api/pixels": reply(404, { error: "no such Google account" }) });
    await userEvent.type(screen.getByLabelText("Name"), "Pixel 7");
    await userEvent.click(screen.getByRole("button", { name: "Add Pixel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no such Google account");
  });
});

describe("editing a Pixel", () => {
  it("shows a Pixel with one person and one with several as they are saved", async () => {
    const { unmount } = render(PixelForm, { pixel: solo, accounts: [joAccount, familyAccount], people: everyone, onsaved: vi.fn(), oncancel: vi.fn() });
    expect(box("Jo Example")).toBeChecked();
    expect(box("Sam Example")).not.toBeChecked();
    expect(screen.getByLabelText("Syncthing device ID (optional)")).toHaveValue("AAA-111");
    expect(screen.getByLabelText("Google account")).toHaveValue("10");
    unmount();
    render(PixelForm, { pixel: shared, accounts: [joAccount, familyAccount], people: everyone, onsaved: vi.fn(), oncancel: vi.fn() });
    expect(box("Jo Example")).toBeChecked();
    expect(box("Sam Example")).toBeChecked();
    expect(box("Kai Example")).not.toBeChecked();
    expect(screen.getByLabelText("Google account")).toHaveValue("11");
    expect(screen.queryByRole("button", { name: "Delete Pixel" })).not.toBeInTheDocument();
  });

  it("keeps the saved people when the account changes, and saves the edit", async () => {
    const { server } = mount(shared, { "PATCH /api/pixels/21": { pixel: shared } });
    await userEvent.selectOptions(screen.getByLabelText("Google account"), "jo.photos@example.com");
    expect(box("Sam Example")).toBeChecked();
    await userEvent.click(box("Jo Example"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(server.sent("PATCH /api/pixels/21")[0].body).toEqual({ name: "Pixel 8", google_account_id: 10, syncthing_device_id: null, people_ids: [2] });
    expect(screen.getByRole("button", { name: "Delete Pixel" })).toBeInTheDocument();
  });
});
