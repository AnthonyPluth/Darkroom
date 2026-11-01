// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import App from "./App.svelte";

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
const me = { sub: "u-1", email: "sam@example.com", name: "Sam Example", groups: ["family"] };
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  history.replaceState(null, "", "/");
  router.sync();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("signed out", () => {
  it("shows the placeholder sign-in view with a sign-in link back to this page, and no nav", async () => {
    history.replaceState(null, "", "/people");
    router.sync();
    fetchMock.mockReturnValue(json({ error: "sign in required" }, 401));
    render(App);
    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link).toHaveAttribute("href", "/auth/login?next=%2Fpeople");
    expect(screen.getByText(/placeholder/i)).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("data-placeholder");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/me", expect.objectContaining({ method: "GET" }));
  });

  it("says so while it asks who is signed in", () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    render(App);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });
});

describe("signed in", () => {
  it("shows the nav with every page, who is signed in and a sign-out form that POSTs to /auth/logout", async () => {
    fetchMock.mockReturnValue(json(me));
    render(App);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["Status", "People", "Exports", "Imports", "Review", "Browser"]);
    expect(within(nav).getByRole("link", { name: "Status" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "Status" })).toBeInTheDocument();
    expect(screen.getByTestId("signed-in-as")).toHaveTextContent("Sam Example");
    const signOut = screen.getByRole("button", { name: "Sign out" });
    const form = signOut.closest("form");
    expect(form).toHaveAttribute("method", "post");
    expect(form).toHaveAttribute("action", "/auth/logout");
    expect(signOut).toHaveAttribute("type", "submit");
  });

  it("moves between the placeholder pages without reloading", async () => {
    fetchMock.mockReturnValue(json(me));
    render(App);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    for (const label of ["People", "Exports", "Imports", "Review", "Browser", "Status"]) {
      await userEvent.click(within(nav).getByRole("link", { name: label }));
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(label);
      expect(within(nav).getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    }
    await userEvent.click(within(nav).getByRole("link", { name: "Review" }));
    await userEvent.click(screen.getByRole("link", { name: "Darkroom" }));
    expect(location.pathname).toBe("/");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("says when there is no page at an address", async () => {
    history.replaceState(null, "", "/nowhere");
    router.sync();
    fetchMock.mockReturnValue(json(me));
    render(App);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });
});

describe("when Darkroom can't be reached", () => {
  it("says why and tries again on request", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch")).mockReturnValueOnce(json(me));
    render(App);
    expect(await screen.findByRole("alert")).toHaveTextContent("Can't reach Darkroom");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });
});
