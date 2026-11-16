import { auditPage } from "./a11y.mjs";

export const SETUP_PAGE = "setup";
export const isSetupPage = (name) => name === SETUP_PAGE || name.startsWith(`${SETUP_PAGE}-`);
export const SIGN_IN_CLIENT = { id: "darkroom-web", secret: "verify-client-secret", groups: "parents" };

export const wantsSetup = (pages, filtered, flowsOnly) => !flowsOnly && (!filtered || pages.some(isSetupPage));

const heading = (page, name) => page.getByRole("heading", { level: 1, name, exact: true });
const button = (scope, name) => scope.getByRole("button", { name, exact: true });

async function dialogDone(page, title, submit, fill) {
  const dialog = page.getByRole("dialog", { name: title });
  await dialog.waitFor();
  await fill(dialog);
  await button(dialog, submit).click();
  await dialog.waitFor({ state: "hidden" });
}

export const UNANSWERED_IMMICH = "http://127.0.0.1:9";
export const REFUSED_IMMICH_CONSOLE = "the server responded with a status of 422";

export async function walkSetup(page, { base, signIn, immich, shot, audit }) {
  const step = async (name, title) => {
    await heading(page, title).waitFor();
    await shot(name);
    await audit(name);
  };
  await page.goto(`${base}/setup`, { waitUntil: "load" });
  await step("setup-welcome", "Set up Darkroom");

  await button(page, "Start setup").click();
  await step("setup-address", "Darkroom's address");

  await button(page, "Confirm address").click();
  await heading(page, "Sign-in").waitFor();
  await page.getByRole("textbox", { name: "Issuer address" }).fill(signIn);
  await page.getByRole("textbox", { name: "Client ID" }).fill(SIGN_IN_CLIENT.id);
  await page.getByLabel("Client secret").fill(SIGN_IN_CLIENT.secret);
  await page.getByRole("textbox", { name: "Allowed groups" }).fill(SIGN_IN_CLIENT.groups);
  await button(page, "Save details").click();
  await page.getByText("Saved. Test them below.").waitFor();
  await step("setup-sign-in", "Sign-in");

  await button(page, "Test sign-in").click();
  await page.waitForURL(`${base}/setup`);
  await page.getByText("Sign-in works.", { exact: false }).waitFor();
  await step("setup-immich", "Immich");

  const immichField = page.getByRole("textbox", { name: "Immich address" });
  await immichField.fill(UNANSWERED_IMMICH);
  await button(page, "Save and continue").click();
  await page.getByRole("alert").filter({ hasText: "Immich didn't answer at" }).waitFor();
  await step("setup-immich-refused", "Immich");

  await immichField.fill(immich);
  await button(page, "Save and continue").click();
  await heading(page, "People").waitFor();
  await button(page, "Add person").click();
  await dialogDone(page, "Add person", "Add person", (d) => d.getByRole("textbox", { name: "Name", exact: true }).fill("Sam Example"));
  await page.getByRole("table", { name: "Persons" }).waitFor();
  await step("setup-people", "People");

  await button(page, "Continue").click();
  await heading(page, "Google accounts").waitFor();
  await button(page, "Add account").click();
  await dialogDone(page, "Add Google account", "Add account", (d) => d.getByRole("textbox", { name: "Google account email" }).fill("family@example.com"));
  await page.getByRole("table", { name: "Google accounts" }).waitFor();
  await step("setup-accounts", "Google accounts");

  await button(page, "Continue").click();
  await heading(page, "Pixels").waitFor();
  await button(page, "Add Pixel").click();
  await dialogDone(page, "Add Pixel", "Add Pixel", async (d) => {
    await d.getByRole("textbox", { name: "Name", exact: true }).fill("Pixel 9a");
    await d.getByRole("checkbox").first().check();
  });
  await page.getByRole("table", { name: "Pixels" }).waitFor();
  await step("setup-pixels", "Pixels");

  await button(page, "Continue").click();
  await step("setup-alerts", "Alert emails");

  await button(page, "Skip").click();
  await step("setup-done", "Darkroom is set up");

  await button(page, "Finish setup").click();
  await page.waitForURL(`${base}/`);
  await page.getByRole("navigation", { name: "Main" }).first().waitFor();
  await page.goto(`${base}/setup`, { waitUntil: "load" });
  await button(page, "Start setup").click();
  await step("setup-closed", "Setup isn't open");
}

export function setupVisit(visit, env, { label, viewport, signIn, immich }) {
  return visit(env, label, viewport, async (page, shot) => {
    const problems = [];
    await walkSetup(page, {
      base: env.base,
      signIn,
      immich,
      shot: (name) => shot(name, env.images),
      audit: async (name) => {
        const found = await auditPage(page, viewport);
        problems.push(...found.problems.map((p) => `${name}: ${p}`));
      },
    });
    if (problems.length) throw new Error(`accessibility: ${problems.join("; ")}`);
  }, { signedIn: false, audit: false, also: [signIn], allowConsole: [REFUSED_IMMICH_CONSOLE] });
}
