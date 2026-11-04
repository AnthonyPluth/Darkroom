// @vitest-environment jsdom
import { render, screen } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { familyAccount, jo, reply, sam, serve } from "../test/server";
import AccountForm from "./AccountForm.svelte";

afterEach(() => vi.unstubAllGlobals());

describe("AccountForm", () => {
  it("adds an account for the first person unless another is chosen", async () => {
    const server = serve({ "POST /api/accounts": reply(201, { account: familyAccount }) });
    vi.stubGlobal("fetch", server.mock);
    const onsaved = vi.fn();
    render(AccountForm, { account: null, people: [jo, sam], onsaved, oncancel: vi.fn() });
    expect(screen.queryByRole("button", { name: "Delete account" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Google account email"), "new@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Add account" }));
    expect(server.sent("POST /api/accounts")[0].body).toEqual({ email: "new@example.com", default_person_id: 1 });
    expect(onsaved).toHaveBeenCalledOnce();
  });

  it("edits an account, shows a refusal, and offers delete", async () => {
    const server = serve({ "PATCH /api/accounts/11": reply(409, { error: "there is already a Google account with that email" }) });
    vi.stubGlobal("fetch", server.mock);
    const ondelete = vi.fn();
    const onsaved = vi.fn();
    render(AccountForm, { account: familyAccount, people: [jo, sam], onsaved, oncancel: vi.fn(), ondelete });
    expect(screen.getByLabelText("Default person")).toHaveValue("2");
    await userEvent.selectOptions(screen.getByLabelText("Default person"), "Jo Example");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already a Google account");
    expect(server.sent("PATCH /api/accounts/11")[0].body).toEqual({ email: "family.photos@example.com", default_person_id: 1 });
    expect(onsaved).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
    expect(ondelete).toHaveBeenCalledOnce();
  });

  it("copes with no people at all", () => {
    render(AccountForm, { account: null, people: [], onsaved: vi.fn(), oncancel: vi.fn() });
    expect(screen.getByLabelText("Default person")).toBeInTheDocument();
  });
});
