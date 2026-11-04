// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { router } from "$lib/nav.svelte";
import { session } from "$lib/session.svelte";
import { emptyStatus, joAccount, me, reply, serve } from "./test/server";
import App from "./App.svelte";

const LABELS = ["Status", "People", "Exports", "Imports", "Review", "Browser"];
const quiet = { "GET /api/me": me, "GET /api/status": emptyStatus, "GET /api/events": { events: [] }, "GET /api/people": { people: [] }, "GET /api/accounts": { accounts: [joAccount] }, "GET /api/pixels": { pixels: [] } };

function start(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(App);
  return server;
}

beforeEach(() => {
  session.current = { kind: "loading" };
  history.replaceState(null, "", "/");
  router.sync();
});
afterEach(() => vi.unstubAllGlobals());

describe("signed out", () => {
  it("shows the sign-in view with a link back to this page, and no nav", async () => {
    history.replaceState(null, "", "/people");
    router.sync();
    const server = start({ "GET /api/me": reply(401, { error: "sign in required" }) });
    const link = await screen.findByRole("link", { name: "Continue with Pocket ID" });
    expect(link).toHaveAttribute("href", "/auth/login?next=%2Fpeople");
    expect(screen.getByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("You are signed out. Darkroom is for the family's parents only.")).toBeInTheDocument();
    expect(screen.getByText("Photos leave the phone only after Google Photos has them.")).toBeInTheDocument();
    expect(screen.getByText(/Reaching Darkroom away from home needs Tailscale/)).toBeInTheDocument();
    expect(screen.queryByText(/placeholder/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(server.mock).toHaveBeenCalledWith("/api/me", expect.objectContaining({ method: "GET" }));
  });

  it("says so while it asks who is signed in", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(App);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });
});

describe("signed in", () => {
  it("shows the rail and the tab bar with every page, who is signed in and a sign-out form that POSTs to /auth/logout", async () => {
    start(quiet);
    const rail = await screen.findByRole("navigation", { name: "Main" });
    const tabs = screen.getByRole("navigation", { name: "Main tabs" });
    for (const nav of [rail, tabs]) {
      expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(LABELS);
      expect(within(nav).getByRole("link", { name: "Status" })).toHaveAttribute("aria-current", "page");
    }
    expect(screen.getByRole("heading", { level: 1, name: "Status" })).toBeInTheDocument();
    expect(screen.getByTestId("signed-in-as")).toHaveTextContent("Sam Example");
    const signOut = screen.getByRole("button", { name: "Sign out" });
    const form = signOut.closest("form");
    expect(form).toHaveAttribute("method", "post");
    expect(form).toHaveAttribute("action", "/auth/logout");
    expect(signOut).toHaveAttribute("type", "submit");
  });

  it("opens the account menu on the phone bar, with the same sign-out form", async () => {
    start(quiet);
    await userEvent.click(await screen.findByRole("button", { name: "Account: Sam Example" }));
    const menu = await screen.findByText("Signed in as", { selector: "[data-popover-content] p" });
    const form = menu.closest("[data-popover-content]")?.querySelector("form");
    expect(form).toHaveAttribute("action", "/auth/logout");
    expect(form).toHaveAttribute("method", "post");
    expect(form?.querySelector("button")).toHaveTextContent("Sign out");
  });

  it("moves between the pages without reloading", async () => {
    const server = start(quiet);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await screen.findByText("No one has been added yet.");
    for (const label of ["People", "Exports", "Imports", "Review", "Browser", "Status"]) {
      await userEvent.click(within(nav).getByRole("link", { name: label }));
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(label);
      expect(within(nav).getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    }
    await userEvent.click(within(nav).getByRole("link", { name: "Review" }));
    expect(screen.getByText("This page arrives in a later issue.")).toBeInTheDocument();
    await userEvent.click(within(nav).getByRole("link", { name: "Browser" }));
    await userEvent.click(screen.getAllByRole("link", { name: "Darkroom" })[0]);
    expect(location.pathname).toBe("/");
    expect(server.sent("GET /api/me")).toHaveLength(1);
  });

  it("goes home from the brand on the phone bar too", async () => {
    start(quiet);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await userEvent.click(within(nav).getByRole("link", { name: "People" }));
    await userEvent.click(screen.getAllByRole("link", { name: "Darkroom" })[1]);
    expect(location.pathname).toBe("/");
  });

  it("shows the People page at /people", async () => {
    history.replaceState(null, "", "/people");
    router.sync();
    start(quiet);
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Add person" })).toBeInTheDocument();
  });

  it("says when there is no page at an address", async () => {
    history.replaceState(null, "", "/nowhere");
    router.sync();
    start(quiet);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });
});

describe("when Darkroom can't be reached", () => {
  it("says why and tries again on request", async () => {
    const server = serve(quiet);
    let first = true;
    vi.stubGlobal("fetch", (url: string, init: RequestInit) => {
      if (!first) return server.mock(url, init);
      first = false;
      return Promise.reject(new TypeError("Failed to fetch"));
    });
    render(App);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Can't reach Darkroom");
    expect(alert).toHaveTextContent("Check your connection and try again.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });
});
