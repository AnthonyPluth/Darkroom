// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupSetting, SetupState } from "$lib/api-types";
import { familyAccount, jo, reply, serve, solo } from "../test/server";
import Setup from "./Setup.svelte";
import { browser } from "./wizard";

const ORIGIN = location.origin;
const CALLBACK = `${ORIGIN}/auth/callback`;
const unauthorized = reply(401, { error: "sign in required" });
const setting = (variable: string, source: SetupSetting["source"], value: string | null = null): SetupSetting => ({ variable, source, set: source !== "default", value });

const before = (...settings: SetupSetting[]): SetupState => ({ settings, redirect_uri: null, proposal: null, phase: "before_test", tested_email: null });
const addressed = (...settings: SetupSetting[]): SetupState => ({ ...before(setting("DARKROOM_PUBLIC_URL", "stored", ORIGIN), ...settings), redirect_uri: CALLBACK });
const proposed: SetupState = {
  ...addressed(),
  proposal: { oidc_issuer: "https://id.example.com", oidc_client_id: "darkroom-7c41e2", has_client_secret: true, oidc_allowed_groups: ["parents"], oidc_allowed_emails: [] },
};
const after = (...settings: SetupSetting[]): SetupState => ({ ...addressed(...settings), phase: "after_test", tested_email: "jo@example.com" });
const immich = setting("IMMICH_URL", "stored", "http://192.168.1.20:2283");
const family = (people = [jo], accounts: unknown[] = [], pixels: unknown[] = []) => ({
  "GET /api/people": { people }, "GET /api/accounts": { accounts }, "GET /api/pixels": { pixels },
});

function stepItem(name: string): HTMLElement {
  const found = within(screen.getByRole("list", { name: "Setup steps" })).getAllByRole("listitem").find((li) => li.textContent?.trim().startsWith(name));
  if (!found) throw new Error(`no step ${name}`);
  return found;
}

function show(routes: Parameters<typeof serve>[0]) {
  const server = serve(routes);
  vi.stubGlobal("fetch", server.mock);
  render(Setup);
  return server;
}

let left: string[];
beforeEach(() => {
  sessionStorage.clear();
  left = [];
  vi.spyOn(browser, "leave").mockImplementation((url) => void left.push(url));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("before setup is claimed", () => {
  it("shows Welcome without claiming, and Start setup claims and opens Address", async () => {
    let release = () => {};
    const gate = new Promise<void>((done) => { release = done; });
    const server = serve({ "GET /api/setup": unauthorized, "POST /api/setup/claim": before() });
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => (url === "/api/setup/claim" ? gate.then(() => server.mock(url, init)) : server.mock(url, init)));
    render(Setup);
    expect(await screen.findByRole("heading", { level: 1, name: "Set up Darkroom" })).toBeInTheDocument();
    expect(server.sent("POST /api/setup/claim")).toHaveLength(0);
    expect(server.calls.map((c) => c.key)).toEqual(["GET /api/setup"]);
    expect(within(screen.getByRole("list", { name: "Setup steps" })).getAllByRole("listitem")).toHaveLength(8);
    expect(screen.queryByText(/^Step \d of 8/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start setup" }));
    expect(screen.getByRole("button", { name: "Starting..." })).toBeDisabled();
    release();
    const heading = await screen.findByRole("heading", { level: 1, name: "Darkroom's address" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByText("Step 1 of 8: Address")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Address" })).toHaveValue(ORIGIN);
    expect(stepItem("Address")).toHaveAttribute("aria-current", "step");
  });

  it("says setup isn't open when the claim is refused", async () => {
    show({ "GET /api/setup": unauthorized, "POST /api/setup/claim": unauthorized });
    await userEvent.click(await screen.findByRole("button", { name: "Start setup" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Setup isn't open" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to sign-in" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("list", { name: "Setup steps" })).not.toBeInTheDocument();
  });

  it("says another browser holds setup, and Try again claims again", async () => {
    let tries = 0;
    show({ "GET /api/setup": unauthorized, "POST /api/setup/claim": () => (++tries === 1 ? reply(409, { error: "already open" }) : before()) });
    await userEvent.click(await screen.findByRole("button", { name: "Start setup" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Setup is open in another browser" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Darkroom's address" })).toBeInTheDocument();
  });

  it("shows a line under Start setup when the claim fails otherwise", async () => {
    show({ "GET /api/setup": unauthorized, "POST /api/setup/claim": reply(500, { error: "database is busy" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Start setup" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't start setup: database is busy.");
    expect(screen.getByRole("button", { name: "Start setup" })).toBeEnabled();
  });

  it("offers Try again when setup can't be loaded", async () => {
    let tries = 0;
    show({ "GET /api/setup": () => (++tries === 1 ? reply(500, { error: "database is busy" }) : unauthorized) });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load setup: database is busy.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Set up Darkroom" })).toBeInTheDocument();
  });
});

describe("the Address step", () => {
  it("warns when the address isn't this one, shows a refusal and goes on to Sign-in", async () => {
    let tries = 0;
    const server = show({
      "GET /api/setup": before(),
      "POST /api/setup/address": () => (++tries === 1 ? reply(400, { error: "public_url must be just the address, without a path" }) : addressed()),
    });
    const field = await screen.findByRole("textbox", { name: "Address" });
    expect(screen.queryByText(/This isn't the address you're using now/)).not.toBeInTheDocument();
    await userEvent.type(field, "/darkroom");
    expect(screen.getByText(/This isn't the address you're using now/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm address" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save the address: public_url must be just the address, without a path.");
    expect(field).toHaveValue(`${ORIGIN}/darkroom`);
    await userEvent.clear(field);
    await userEvent.type(field, ORIGIN);
    await userEvent.click(screen.getByRole("button", { name: "Confirm address" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sign-in" })).toBeInTheDocument();
    expect(server.sent("POST /api/setup/address").map((c) => c.body)).toEqual([{ public_url: `${ORIGIN}/darkroom` }, { public_url: ORIGIN }]);
    expect(screen.getByRole("link", { name: "Address" })).toBeInTheDocument();
  });

  it("shows an address from the environment as a row and saves nothing", async () => {
    const server = show({ "GET /api/setup": before(setting("DARKROOM_PUBLIC_URL", "environment")) });
    await screen.findByRole("heading", { level: 1, name: "Sign-in" });
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Set in Darkroom's environment.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Address" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sign-in" })).toBeInTheDocument();
    expect(server.sent("POST /api/setup/address")).toHaveLength(0);
  });

  it("goes back to Welcome", async () => {
    show({ "GET /api/setup": before() });
    await userEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Set up Darkroom" })).toBeInTheDocument();
  });
});

describe("the Sign-in step", () => {
  it("copies the callback URL and asks to save before testing", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    show({ "GET /api/setup": addressed() });
    expect(await screen.findByLabelText("Callback URL")).toHaveTextContent(CALLBACK);
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(CALLBACK);
    expect(await screen.findByRole("status")).toHaveTextContent("Copied.");
    expect(screen.getByRole("button", { name: "Test sign-in" })).toBeDisabled();
    expect(screen.getByText("Save the details above first.")).toBeInTheDocument();
  });

  it("copies through a selection when the page isn't a secure context and has no clipboard", async () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
    show({ "GET /api/setup": addressed() });
    const copy = await screen.findByRole("button", { name: "Copy" });
    await userEvent.click(copy);
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(await screen.findByRole("status")).toHaveTextContent("Copied.");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(document.querySelector("textarea")).toBeNull();
    expect(copy).toHaveFocus();
    Reflect.deleteProperty(document, "execCommand");
  });

  it("says copying failed and selects the callback URL when the browser won't copy", async () => {
    const writeText = vi.fn(() => Promise.reject(new Error("not allowed")));
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => false) });
    show({ "GET /api/setup": addressed() });
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't copy the callback URL. It is selected above: copy it with your keyboard or the browser's menu.");
    expect(screen.queryByText("Copied.")).not.toBeInTheDocument();
    expect(window.getSelection()?.toString()).toBe(CALLBACK);
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => { throw new Error("unsupported"); }) });
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    expect(screen.queryByText(/Couldn't copy/)).not.toBeInTheDocument();
    Reflect.deleteProperty(document, "execCommand");
  });

  it("asks for the address first when there is no callback URL", async () => {
    show({ "GET /api/setup": { ...before(), settings: [setting("DARKROOM_PUBLIC_URL", "environment")] } });
    await userEvent.click(await screen.findByRole("link", { name: "Go to Address" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Darkroom's address" })).toBeInTheDocument();
  });

  it("saves split lists and the secret once, then starts the test sign-in in this tab", async () => {
    const server = show({
      "GET /api/setup": addressed(),
      "POST /api/setup/sign-in": proposed,
      "POST /api/setup/sign-in/test": { location: "https://id.example.com/authorize?state=x" },
    });
    await userEvent.type(await screen.findByRole("textbox", { name: "Issuer address" }), "https://id.example.com");
    await userEvent.type(screen.getByRole("textbox", { name: "Client ID" }), "darkroom-7c41e2");
    const secret = screen.getByLabelText("Client secret");
    expect(secret).toHaveAttribute("type", "password");
    await userEvent.type(secret, "client-secret-canary");
    await userEvent.type(screen.getByRole("textbox", { name: "Allowed groups" }), " parents , ,");
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved. Test them below.");
    expect(server.sent("POST /api/setup/sign-in")[0].body).toEqual({
      oidc_issuer: "https://id.example.com", oidc_client_id: "darkroom-7c41e2", oidc_client_secret: "client-secret-canary", oidc_allowed_groups: ["parents"], oidc_allowed_emails: [],
    });
    expect(secret).toHaveValue("");
    expect(screen.getByText("A secret is saved. Leave this empty to keep it.")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("client-secret-canary");
    await userEvent.click(screen.getByRole("button", { name: "Test sign-in" }));
    await waitFor(() => expect(left).toEqual(["https://id.example.com/authorize?state=x"]));
    expect(sessionStorage.getItem("darkroom.setup.testing")).toBe("1");
  });

  it("disables the test again after an unsaved edit, and shows why the test couldn't start", async () => {
    show({ "GET /api/setup": proposed, "POST /api/setup/sign-in/test": reply(502, { error: "the test sign-in could not start: the provider could not be reached" }) });
    const groups = await screen.findByRole("textbox", { name: "Allowed groups" });
    expect(groups).toHaveValue("parents");
    await userEvent.click(screen.getByRole("button", { name: "Test sign-in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't start the test sign-in: the test sign-in could not start: the provider could not be reached.");
    await userEvent.type(groups, ", family");
    expect(screen.getByRole("button", { name: "Test sign-in" })).toBeDisabled();
    expect(left).toEqual([]);
  });

  it("shows a refused save under Save details, empties the secret and reloads when a setting is in the environment", async () => {
    let loads = 0;
    const server = show({
      "GET /api/setup": () => (++loads === 1 ? addressed() : addressed(setting("OIDC_ISSUER", "environment"))),
      "POST /api/setup/sign-in": reply(409, { error: "OIDC_ISSUER is set in the environment, which always wins over the wizard; change it there" }),
    });
    await userEvent.type(await screen.findByLabelText("Client secret"), "client-secret-canary");
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save the sign-in details: OIDC_ISSUER is set in the environment, which always wins over the wizard; change it there.");
    expect(screen.getByLabelText("Client secret")).toHaveValue("");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Issuer address" })).not.toBeInTheDocument());
    expect(screen.getByText("Issuer address")).toBeInTheDocument();
    expect(server.sent("GET /api/setup")).toHaveLength(2);
  });
});

describe("after the test sign-in", () => {
  it("opens Immich with the status line, with Address and Sign-in settled", async () => {
    sessionStorage.setItem("darkroom.setup.testing", "1");
    const server = show({ "GET /api/setup": after(), ...family([]), "POST /api/setup/immich": after(immich) });
    expect((await screen.findByText(/^Sign-in works/)).closest("[role=status]")).toHaveTextContent("Sign-in works. You're signed in as jo@example.com.");
    expect(screen.getByText("Step 3 of 8: Immich")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Address" })).not.toBeInTheDocument();
    expect(stepItem("Sign-in")).toHaveTextContent("Done");
    expect(sessionStorage.getItem("darkroom.setup.testing")).toBeNull();
    await userEvent.type(screen.getByRole("textbox", { name: "Immich address" }), "http://192.168.1.20:2283");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeInTheDocument();
    expect(server.sent("POST /api/setup/immich")[0].body).toEqual({ immich_url: "http://192.168.1.20:2283" });
    expect(screen.queryByText(/Sign-in works/)).not.toBeInTheDocument();
  });

  it("checks Immich, and a refusal keeps the field and the step without reloading", async () => {
    let release = () => {};
    let tries = 0;
    const gate = new Promise<void>((done) => { release = done; });
    const refused = "Immich didn't answer at http://192.168.1.21:2283: nothing answered there";
    const server = serve({ "GET /api/setup": after(), ...family([]), "POST /api/setup/immich": () => (++tries === 1 ? reply(422, { error: refused }) : after(immich)) });
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => (url === "/api/setup/immich" && tries === 0 ? gate.then(() => server.mock(url, init)) : server.mock(url, init)));
    render(Setup);
    const field = await screen.findByRole("textbox", { name: "Immich address" });
    await userEvent.type(field, "http://192.168.1.21:2283");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(screen.getByRole("button", { name: "Checking Immich..." })).toBeDisabled();
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent(`Couldn't save Immich's address: ${refused}.`);
    expect(screen.getByRole("button", { name: "Save and continue" })).toBeEnabled();
    expect(screen.getByRole("heading", { level: 1, name: "Immich" })).toBeInTheDocument();
    expect(field).toHaveValue("http://192.168.1.21:2283");
    expect(server.sent("GET /api/setup")).toHaveLength(1);
    await userEvent.clear(field);
    await userEvent.type(field, "http://192.168.1.20:2283");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(server.sent("POST /api/setup/immich").map((c) => c.body)).toEqual([{ immich_url: "http://192.168.1.21:2283" }, { immich_url: "http://192.168.1.20:2283" }]);
  });

  it("reloads only for the environment's 409, not for the check's 422", async () => {
    const server = show({
      "GET /api/setup": after(),
      ...family([]),
      "POST /api/setup/immich": reply(409, { error: "IMMICH_URL is set in the environment, which always wins over the wizard; change it there" }),
    });
    await userEvent.type(await screen.findByRole("textbox", { name: "Immich address" }), "http://192.168.1.20:2283");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    await screen.findByRole("alert");
    await waitFor(() => expect(server.sent("GET /api/setup")).toHaveLength(2));
  });

  it("needs a person before People can continue", async () => {
    let people = [] as (typeof jo)[];
    const server = show({
      "GET /api/setup": after(immich),
      "GET /api/people": () => ({ people }),
      "GET /api/accounts": { accounts: [] },
      "GET /api/pixels": { pixels: [] },
      "POST /api/people": () => { people = [jo]; return jo; },
    });
    expect(await screen.findByText("No people yet. Add at least one person to continue.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    const dialog = await screen.findByRole("dialog", { name: "Add person" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "Jo Example");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add person" }));
    expect(await screen.findByRole("table", { name: "Persons" })).toHaveTextContent("Jo Example");
    expect(server.sent("POST /api/people")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Google accounts" })).toBeInTheDocument();
  });

  it("lets Google accounts and Pixels be skipped, and alert emails saved, on the way to Done", async () => {
    sessionStorage.setItem("darkroom.setup.step", "accounts");
    const server = show({
      "GET /api/setup": after(immich),
      ...family(),
      "POST /api/setup/alerts": after(immich, setting("RESEND_API_KEY", "stored"), setting("ALERT_EMAIL_FROM", "stored", "alerts@example.com"), setting("ALERT_EMAIL_TO", "stored", "jo@example.com")),
      "POST /api/setup/finish": { ok: true },
    });
    expect(await screen.findByText("No Google accounts yet. You can add them now or later on the People page.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(await screen.findByText("No Pixels yet. You can add them now or later on the People page.")).toBeInTheDocument();
    expect(stepItem("Google accounts")).toHaveTextContent("Skipped");
    await userEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    const key = await screen.findByLabelText("Resend API key");
    await userEvent.type(key, "re_canary_key");
    await userEvent.type(screen.getByRole("textbox", { name: "From address" }), "alerts@example.com");
    await userEvent.type(screen.getByRole("textbox", { name: "Send alerts to" }), "jo@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Darkroom is set up" })).toBeInTheDocument();
    expect(server.sent("POST /api/setup/alerts")[0].body).toEqual({ alert_email_from: "alerts@example.com", alert_email_to: "jo@example.com", resend_api_key: "re_canary_key" });
    expect(screen.getByText("1 people, 0 Google accounts and 0 Pixels. Alert emails: on.")).toBeInTheDocument();
    expect(stepItem("Alert emails")).toHaveTextContent("Done");
    await userEvent.click(screen.getByRole("button", { name: "Finish setup" }));
    await waitFor(() => expect(left).toEqual(["/"]));
    expect(server.sent("POST /api/setup/finish")).toHaveLength(1);
  });

  it("sends a Google account's sign-in to the Remote browser page with the way back", async () => {
    sessionStorage.setItem("darkroom.setup.step", "accounts");
    show({ "GET /api/setup": after(immich), ...family([jo], [familyAccount]) });
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/browser?from=setup");
    expect(screen.queryByRole("button", { name: "Skip for now" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeInTheDocument();
  });

  it("keeps alert emails off when skipped, and shows a refused finish", async () => {
    sessionStorage.setItem("darkroom.setup.step", "alerts");
    show({ "GET /api/setup": after(immich), ...family([jo], [familyAccount], [solo]), "POST /api/setup/finish": reply(500, { error: "database is busy" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Skip" }));
    expect(await screen.findByText("1 people, 1 Google accounts and 1 Pixels. Alert emails: off.")).toBeInTheDocument();
    expect(stepItem("Alert emails")).toHaveTextContent("Skipped");
    await userEvent.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't finish setup: database is busy.");
    expect(left).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Alert emails" })).toBeInTheDocument();
  });

  it("shows alert settings from the environment as rows", async () => {
    sessionStorage.setItem("darkroom.setup.step", "alerts");
    const server = show({ "GET /api/setup": after(immich, setting("ALERT_EMAIL_TO", "environment")), ...family() });
    expect(await screen.findAllByText("Set in Darkroom's environment.")).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Darkroom is set up" })).toBeInTheDocument();
    expect(server.sent("POST /api/setup/alerts")).toHaveLength(0);
  });

  it("says so when the people can't be reloaded after an add, and keeps Continue off until a retry works", async () => {
    let people = [] as (typeof jo)[];
    let reloads = 0;
    const server = show({
      "GET /api/setup": after(immich),
      "GET /api/people": () => {
        reloads += 1;
        if (reloads === 2 || reloads === 3) return reply(503, { error: "database is busy" });
        return { people };
      },
      "GET /api/accounts": { accounts: [] },
      "GET /api/pixels": { pixels: [] },
      "POST /api/people": () => { people = [jo]; return jo; },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    const dialog = await screen.findByRole("dialog", { name: "Add person" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "Jo Example");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add person" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("database is busy");
    expect(screen.queryByText("No people yet. Add at least one person to continue.")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Add person" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    expect(stepItem("People")).not.toHaveTextContent("Done");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(reloads).toBe(3));
    expect(await screen.findByRole("alert")).toHaveTextContent("database is busy");
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Persons" })).toHaveTextContent("Jo Example");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
    expect(server.sent("POST /api/people")).toHaveLength(1);
  });

  it("keeps the newest reload when an older one answers after it", async () => {
    sessionStorage.setItem("darkroom.setup.step", "people");
    let calls = 0;
    let releaseStale = () => {};
    const stale = new Promise<void>((done) => { releaseStale = done; });
    const base = serve({ "GET /api/setup": after(immich), "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [] }, "GET /api/people": { people: [jo] } });
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (url !== "/api/people") return base.mock(url, init);
      calls += 1;
      if (calls === 1) return base.mock(url, init);
      if (calls === 2) return Promise.resolve(new Response(JSON.stringify({ error: "database is busy" }), { status: 503 }));
      if (calls === 3) return stale.then(() => new Response(JSON.stringify({ error: "an old failure" }), { status: 503 }));
      return base.mock(url, init);
    });
    render(Setup);
    await screen.findByRole("table", { name: "Persons" });
    await userEvent.click(screen.getByRole("button", { name: "Edit Jo Example" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit person" });
    vi.stubGlobal("fetch", ((previous) => (url: string, init?: RequestInit) => (url.startsWith("/api/people/") ? Promise.resolve(new Response(JSON.stringify(jo), { status: 200 })) : previous(url, init)))(globalThis.fetch));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("database is busy");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const retry = screen.getByRole("button", { name: "Try again" });
    await fireEvent.click(retry);
    await fireEvent.click(retry);
    expect(await screen.findByRole("table", { name: "Persons" })).toHaveTextContent("Jo Example");
    releaseStale();
    await waitFor(() => expect(calls).toBe(4));
    await new Promise((done) => setTimeout(done, 20));
    expect(screen.queryByText("an old failure")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("keeps Skip for now away while the accounts are unknown", async () => {
    sessionStorage.setItem("darkroom.setup.step", "accounts");
    let reloads = 0;
    show({
      "GET /api/setup": after(immich),
      "GET /api/people": { people: [jo] },
      "GET /api/accounts": () => (++reloads === 2 ? reply(500, { error: "database is busy" }) : { accounts: [] }),
      "GET /api/pixels": { pixels: [] },
      "POST /api/accounts": familyAccount,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add account" }));
    const dialog = await screen.findByRole("dialog", { name: "Add Google account" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Google account email" }), "family@example.com");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("database is busy");
    expect(screen.queryByRole("button", { name: "Skip for now" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  });

  it("closes when the tested session ends", async () => {
    let ended = false;
    show({
      "GET /api/setup": after(immich),
      "GET /api/people": () => (ended ? unauthorized : { people: [] }),
      "GET /api/accounts": { accounts: [] },
      "GET /api/pixels": { pixels: [] },
      "POST /api/people": () => { ended = true; return jo; },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add person" }));
    const dialog = await screen.findByRole("dialog", { name: "Add person" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "Jo Example");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add person" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Setup isn't open" })).toBeInTheDocument();
  });

  it("closes when the wizard was closed before the page loaded its people", async () => {
    show({ "GET /api/setup": after(immich), "GET /api/people": unauthorized, "GET /api/accounts": { accounts: [] }, "GET /api/pixels": { pixels: [] } });
    expect(await screen.findByRole("heading", { level: 1, name: "Setup isn't open" })).toBeInTheDocument();
  });
});
