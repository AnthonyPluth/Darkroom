# First-run setup wizard (#303, document signed off)

Status: **document signed off by the owner on 2026-11-16 (#344, recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926281385); the boards signed off by the owner on 2026-11-16 (visual sign-off recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926640234).** The boards are signed off, so #305 builds to them (AGENTS.md, promise 6; decision 1). The owner's answers to the open points on #344, including the three raised by the new order, are recorded under "The owner's decisions" below and are written into this design. The UI is #305. It calls the setup API that is on `main` (#304, `darkroom/server/api/wizard.py` and `darkroom/server/wizard.py`, behind `DARKROOM_WIZARD=1`), as extended by #346, which #305 depends on. The steps are the full list in `docs/design/single-container.md`, section 8; who may open the wizard is the owner's answer 4 there.

Every name, address and key below is made up.

## What the owner reviews

The review has two stages (decision 1), and both are recorded on #303. Both are done: the owner signed off this document on 2026-11-16, and the boards on 2026-11-16 (visual sign-off recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926640234).

1. **This document** (signed off on 2026-11-16): the steps and their order, every screen and state, the exact copy, every error and empty state, what the wizard may and may not show, how it is turned on and off, and what the API must change (#346). Copy in quotation marks or in the copy tables is word for word; `{placeholders}` are filled at run time.
2. **The boards** (reviewed and signed off by the owner on 2026-11-16), drawn from this document in the format of the canvas boards (https://claude.ai/artifact/BvpF7AujGCxiVA9bKvWiUR) with the tokens of `00-tokens.dc.html` only, no new colour or token, and kept in this folder like the others; they were signed off before #303 closes. Desktop boards 1280 wide, phone boards 390 wide, as for #16. Each board stacks the states named for it:

| Board (desktop and phone) | States drawn |
| --- | --- |
| `08-setup-welcome-desktop.dc.html`, `08-setup-welcome-phone.dc.html` | Welcome; Welcome with "Starting..." in flight; Welcome with the start error line |
| `08-setup-closed-desktop.dc.html`, `08-setup-closed-phone.dc.html` | Setup isn't open; Setup is open in another browser |
| `08-setup-address-desktop.dc.html`, `08-setup-address-phone.dc.html` | Prefilled with the address in use; edited to another address (the warning line); a refused save; set in the environment |
| `08-setup-sign-in-desktop.dc.html`, `08-setup-sign-in-phone.dc.html` | No details saved yet (test disabled); details saved with a secret stored (test enabled); unsaved edits (test disabled); a refused save; the test could not start; set in the environment |
| `08-setup-test-failed-desktop.dc.html`, `08-setup-test-failed-phone.dc.html` | The server's failure pages after the test: Not allowed (account); Not allowed (not started here); The test sign-in failed; The settings changed; Nothing to test |
| `08-setup-immich-desktop.dc.html`, `08-setup-immich-phone.dc.html` | Arrived from a passing test (the "Sign-in works" status line); empty; stored; a refused save; set in the environment |
| `08-setup-immich-check-desktop.dc.html`, `08-setup-immich-check-phone.dc.html` (#347, signed off) | Checking Immich (in flight); refused, no answer; refused, not Immich; refused, TLS failed |
| `08-setup-people-desktop.dc.html`, `08-setup-people-phone.dc.html` | No people yet (Continue disabled); the add-person form open; two people added |
| `08-setup-accounts-desktop.dc.html`, `08-setup-accounts-phone.dc.html` | No accounts yet; one account waiting for its sign-in; one account signed in |
| `08-setup-pixels-desktop.dc.html`, `08-setup-pixels-phone.dc.html` | No Pixels yet; the add-Pixel form open; one Pixel added |
| `08-setup-alerts-desktop.dc.html`, `08-setup-alerts-phone.dc.html` | Empty; a key stored; a refused save; set in the environment |
| `08-setup-done-desktop.dc.html`, `08-setup-done-phone.dc.html` | Done, before Finish setup; Finish setup refused |

After the build, #305's pull request puts `make verify` images of each step at phone (440 x 956) and desktop (1280 x 800) beside these boards and says where they differ, as `fleet-design-signoff` asks. The phone boards are 390 wide and `make verify`'s phone is 440, as for every other page. Demo mode keeps the wizard closed, so #305 itself adds a wizard mode to `tools/verify.py` (decision 15): an empty data folder, `DARKROOM_WIZARD=1` on loopback, no demo session, and a fake sign-in provider so the flow can pass the test and reach the later steps; its pages are `setup-*`, with a flow that walks every step.

## Where it is and who can open it

- **Address.** The wizard is the web app's `/setup` page, outside the app shell: no rail, no tab bar, no navigation. `/` is unchanged: a visitor there sees the signed-out page of boards `01-signin-*`. The API already names `/setup` as the test sign-in's path (`TEST_PATH` in `darkroom/server/wizard.py`).
- **How a person finds it** (decision 4). The one-container guide (`docs/deploy/one-container.md`) and the runbook say to open `http://<server's address>:8000/setup` the first time. Nothing on the signed-out page changes.
- **Who, before the test sign-in.** Exactly the rule the API enforces today (SECURITY.md, the runbook's "The first-run setup API"): with `DARKROOM_WIZARD=1`, while no sign-in is set up in the environment (no `OIDC_*`, not `DARKROOM_ALLOW_NO_AUTH`, not `DARKROOM_DEMO`) and before anyone has signed in, the first browser that presses Start setup holds the wizard (decision 5: never on loading the page). It must connect directly from a private address (10/8, 172.16/12, 192.168/16, loopback, fc00::/7), with no forwarding header and with an allowed `Host` and `Origin`.
- **Who, after the test sign-in** (#346). The passing test signs that account in. From then on the wizard answers only a request that carries both that browser's claim and a session of the account that passed the test, and that still meets every address, header, `Host` and `Origin` condition above. The claim alone, the session alone, any other member and any other peer get the "Setup isn't open" screen. The wizard is never reachable by an unauthenticated peer after the test, and before it only by the one claimant on the LAN.
- **Everyone else** gets "Setup isn't open". The page cannot tell which condition failed, because the API answers 401 for all of them on purpose; the screen lists them all instead (decision 6).

## Layout

- **Phone (390).** One column, 16 px side padding: the brand row ("Darkroom" with the accent dot, as on the signed-out page), then a muted line "Step {n} of 8: {step name}" (not on Welcome or the closed screens), then one card (`card`) holding the step. Buttons are full width, 48 px high, stacked: the primary first, then the others. Fields are full width with their label above and their hint below.
- **Desktop (1280)** (decision 14). The signed-out page's two columns. The left panel (`bg-rail`) keeps the brand row and, in place of the tagline, the list of steps: Address, Sign-in, Immich, People, Google accounts, Pixels, Alert emails, Done. The current step is marked with `aria-current="step"` and bold ink; a finished step says "Done" in text after its name, never by colour alone; a skipped step says "Skipped"; a step not reached is muted. Finished and skipped steps are links back to themselves, except that Address and Sign-in are no longer links once the test has passed (their settings are stored and the test is not repeated). The right column holds the same card as on a phone, ~~at most 30 rem wide (the People, Google accounts and Pixels steps at most 48 rem, for their lists)~~ (superseded by the signed-off revision below), with the buttons in one row. **Revision (#347, signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469): the 304 px left pane with 40 px padding on all 11 desktop wizard boards, the card filling the column as built in #363, the Immich check states, refusal status 422, an Immich save before the passing test refused with 409 and contacting nothing, choices B to H as recommended; phone boards, the test-failed desktop board and the add-person dialog unchanged), which supersedes the 30 rem and 48 rem caps and the half-page and 420 px left panels above and in decision 14:** the left panel is 304 px wide on every step (was half the page, or 420 px on People, Google accounts and Pixels), with 40 px side padding, and the card fills the column right of it, inset by that column's padding, with no 30 rem or 48 rem cap, as the wizard is built in #363; see the README's #347 section.
- **Tokens and parts.** Only the signed-off tokens and the parts already in `frontend/src/app.css` (`card`, `btn`, `btn-primary`, the muted text, the danger alert line used on Exports), and on the People, Google accounts and Pixels steps the signed-off parts of the People page (boards `03-people-*`). No new colour, icon or token.
- **Focus and announcements.** Each screen has one `h1`. On a change of step, focus moves to that `h1`. Error lines are `role="alert"` and named by the field's `aria-describedby`; "Copied.", "Saved." and "Sign-in works." are `role="status"`.

## The steps

Welcome, then eight numbered steps, in the order of `single-container.md` section 8 (decision 2):

| # | Step | Before or after the test | Calls |
| --- | --- | --- | --- |
| | Welcome | before | `POST /api/setup/claim` |
| 1 | Address | before | `POST /api/setup/address` |
| 2 | Sign-in, with the test | before; the test ends this phase | `POST /api/setup/sign-in`, `POST /api/setup/sign-in/test` |
| 3 | Immich | after | `POST /api/setup/immich` |
| 4 | People | after | the People page's routes, with the tested session |
| 5 | Google accounts | after | the People page's account routes; the Remote browser page for the sign-in |
| 6 | Pixels | after | the People page's Pixel routes |
| 7 | Alert emails (optional) | after | `POST /api/setup/alerts` |
| 8 | Done | after | `POST /api/setup/finish` (#346) |

When the page loads, it asks `GET /api/setup`. A 200 means this browser holds the wizard: it opens at the first unfinished step. Before the test that is Address, then Sign-in; after it (`phase` is `after_test`) it is the first of Immich (unset), People (none yet), then Google accounts, and otherwise the last step the person was on in this browser (kept in `sessionStorage`, a convenience only), else Done. A 401 shows Welcome. Nothing is claimed on load.

### How a setting is shown

Each setting the API lists (`settings[]` in its reply) is drawn by its `source` (decision 12):

| Source | Plain setting (address, Immich, emails, issuer, client ID, groups) | Secret (`OIDC_CLIENT_SECRET`, `RESEND_API_KEY`) |
| --- | --- | --- |
| `default` (not set) | Empty field, or prefilled where the step says | Empty field |
| `stored` | Field prefilled with the stored value; hint "Saved." | Empty field; hint "A key is saved. Leave this empty to keep it." (for the client secret: "A secret is saved. Leave this empty to keep it.") |
| `environment` | No field. A read-only row: the label, then "Set in Darkroom's environment.", then the muted hint "The environment always wins over setup. To change it, change the container's settings." No value: the API returns none | The same row; no value |

Stored values take effect at once (#346), so no hint mentions a restart. When every setting of a step is set in the environment, its primary button reads "Continue" and saves nothing. The alert step is one unit in the API (any of its three in the environment refuses all three), so if any of the three is in the environment the whole step shows rows and "Continue". When all five sign-in settings are in the environment, the wizard does not open at all (sign-in is set up), so the Sign-in step never shows only rows; when some are, it shows those as rows and the API refuses to save the rest, so the step shows the "Couldn't save" line with the API's message and the person sets the rest in the environment.

### Welcome

| Part | Copy |
| --- | --- |
| Heading | Set up Darkroom |
| Text | This sets Darkroom up on this server, from this browser, from the first sign-in to the people, Google accounts and Pixels. You'll need: |
| List | The address you open Darkroom at (this one, most likely). |
| | A client for Darkroom in Pocket ID: its issuer address, client ID and client secret. |
| | Immich's address, and an Immich API key for each person. |
| | Each person's Google account and its password, to sign in once in Darkroom's remote browser. |
| | Each Pixel, with its Syncthing app's device ID. |
| | Optionally, a Resend API key for alert emails. |
| Text | You won't be asked to invent a password or a key: Darkroom makes and keeps its own. |
| Muted text | Once you start, only this browser can continue. After the test sign-in, only you, signed in on this browser, can. |
| Primary button | Start setup |

Pressing it posts `POST /api/setup/claim`. While it is in flight the button is disabled and reads "Starting...".

| Reply | What shows |
| --- | --- |
| 200 | Step 1 (or the first unfinished step, as above) |
| 401 | Setup isn't open |
| 409 | Setup is open in another browser |
| Anything else (no network, a 5xx) | Under the button, a danger `role="alert"` line: "Couldn't start setup: {message}." The button is enabled again |

### Setup isn't open

Shown for a 401 from the claim, and for a 401 from any later setup call (the wizard finished or closed, the claim was cleared, the tested session ended, or `DARKROOM_WIZARD` was turned off).

| Part | Copy |
| --- | --- |
| Heading | Setup isn't open |
| Text | Darkroom's first-run setup opens only when all of these hold: |
| List | `DARKROOM_WIZARD=1` is set in the container's settings. |
| | Setup hasn't been finished, and sign-in isn't set up in the environment. |
| | This device is on your home network. |
| | You opened `http://<server's address>:8000/setup` directly, not through Traefik or another proxy. |
| | After the test sign-in: you're still signed in, on the browser that started setup. |
| Muted text | If setup was closed before you finished, sign in and carry on from the People page. Settings are changed in Darkroom's environment. |
| Secondary button | Go to sign-in (a link to `/`) |

### Setup is open in another browser

Shown for a 409 from the claim.

| Part | Copy |
| --- | --- |
| Heading | Setup is open in another browser |
| Text | Another browser started Darkroom's setup first, and only that browser can continue it. Finish it there. |
| Muted text | If that browser is lost, run `python run.py reopen-setup` in the Darkroom container's console, then press Try again. After the test sign-in, that closes setup instead; sign in and carry on from the People page. |
| Primary button | Try again (posts the claim again) |

### Step 1 of 8: Address

| Part | Copy |
| --- | --- |
| Heading | Darkroom's address |
| Text | The address people open Darkroom at. Sign-in comes back to it, and alert emails link to it. |
| Field label | Address |
| Field value | The stored value, else the address this browser is using (`location.origin`, such as `http://192.168.1.20:8000`) |
| Hint | This is the address you're using now. The test sign-in in the next step can only come back here, so keep it for setup. To use an https:// address through Traefik later, set DARKROOM_PUBLIC_URL and the OIDC_ settings in the environment then. |
| Primary button | Confirm address |
| Secondary button | Back (to Welcome) |

When the field no longer equals `location.origin`, a warning line (warn tone, not an alert) appears under the hint: "This isn't the address you're using now, so the test sign-in in the next step can't finish here." The button stays enabled: the API accepts the address, and the test then answers 409 (decision 7).

Posts `POST /api/setup/address` with `public_url`. 200: on to step 2.

### Step 2 of 8: Sign-in

One card in three numbered parts.

**1. Add Darkroom to Pocket ID**

| Part | Copy |
| --- | --- |
| Heading (the step's `h1`) | Sign-in |
| Sub-heading | 1. Add Darkroom to Pocket ID |
| Text | In Pocket ID, add an OIDC client for Darkroom with this callback URL: |
| Box | `{redirect_uri}` from the API, such as `http://192.168.1.20:8000/auth/callback`, in a monospace read-only box |
| Secondary button | Copy (to the clipboard, in the browser; then a status line "Copied.") |
| When `redirect_uri` is null | In place of the box: "Confirm Darkroom's address first." with a link "Go to Address" |

**2. Enter the client's details**

| Field | Label and hint |
| --- | --- |
| `oidc_issuer` | Issuer address. Hint: "Pocket ID's address, such as https://id.example.com. Use https:// unless Pocket ID is on your home network." |
| `oidc_client_id` | Client ID |
| `oidc_client_secret` | Client secret (a password field). When the saved details hold one: "A secret is saved. Leave this empty to keep it." |
| `oidc_allowed_groups` | Allowed groups. Hint: "People in any of these Pocket ID groups may sign in. Separate them with commas." |
| `oidc_allowed_emails` | Allowed email addresses. Hint: "Or these people, by email. Separate them with commas. Set at least one group or address." |
| Secondary button | Save details |

Saved details (`proposal` in the reply) prefill the plain fields. Posts `POST /api/setup/sign-in`; the lists are split on commas and trimmed before sending. 200 shows a status line "Saved. Test them below." under the button.

**3. Test the sign-in**

| Part | Copy |
| --- | --- |
| Sub-heading | 3. Test the sign-in |
| Text | Sign in with your own account, one these details allow. If it works, Darkroom keeps these details, you stay signed in, and setup carries on with Immich. Only you, on this browser, can continue it from then on. |
| Primary button | Test sign-in |
| Secondary button | Back |

Test sign-in is enabled only when details are saved (`proposal` is not null) and no field has changed since they were saved; otherwise a muted line under it says "Save the details above first." Pressing it posts `POST /api/setup/sign-in/test`, and on 200 the browser goes to the returned `location` in the same tab (Pocket ID's sign-in).

### After Pocket ID

- **It worked** (#346). The server signs the account in, stores the sign-in settings, and sends the browser back to `/setup`, which opens step 3 with a status line at the top of the card: "Sign-in works. You're signed in as {email}." The server's "Sign-in works" page of #304 is no longer shown.
- **It failed.** The server answers with its own small page (the `page` helper in `darkroom/server/common.py`, its own style and CSP), as built; the claim still holds and nothing was stored. Their text is the API's, quoted here so that it is part of the sign-off. A "Back to setup" link on them is #349; #305 ships without it, so the person uses the browser's back button or opens `/setup` again.

| Result | Title | Text |
| --- | --- | --- |
| The account is not allowed | Not allowed | The account you signed in with is not allowed by the proposed settings, so nothing was saved. Check the allowed groups and email addresses, then test again. |
| Another browser, or the claim was cleared | Not allowed | This test sign-in did not start here, so nothing was saved. |
| Pocket ID refused or the reply was wrong | The test sign-in failed | Something was wrong with this sign-in: {error}. Nothing was saved. |
| The details changed during the test | The settings changed | The proposed sign-in settings changed during the test, so nothing was saved. Test again. |
| No details to test | Nothing to test | There are no proposed sign-in settings to test, so nothing was saved. |

### Step 3 of 8: Immich

| Part | Copy |
| --- | --- |
| Heading | Immich |
| Text | The address Darkroom reaches Immich at from this server, such as `http://192.168.1.20:2283`. |
| Field label | Immich address |
| Primary button | Save and continue |

There is no Back button from here on to Address or Sign-in: those are settled by the passing test. Posts `POST /api/setup/immich` with `immich_url`.

**Checking the address (#347, signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469): the 304 px left pane with 40 px padding on all 11 desktop wizard boards, the card filling the column as built in #363, the Immich check states, refusal status 422, an Immich save before the passing test refused with 409 and contacting nothing, choices B to H as recommended; phone boards, the test-failed desktop board and the add-person dialog unchanged; boards `08-setup-immich-check-*`).** Before storing the address, Darkroom asks Immich's unauthenticated ping (`GET /api/server/ping`) at that address, through `darkroom/tls.py`, with a short time limit and without following a redirect, and stores it only if Immich answers as Immich. Every press of Save and continue checks again, a stored value too; Continue, when the address is set in the environment, checks nothing.

| Part | Copy |
| --- | --- |
| Primary button while the check runs (disabled) | Checking Immich... |
| Refused (the API's message, inside the usual line) | Couldn't save Immich's address: Immich didn't answer at {address}: {reason}. |
| `{reason}`, nothing answered (refused, timed out, name not found) | nothing answered there |
| `{reason}`, something answered but not as Immich (any other reply, or a redirect) | the server there isn't Immich |
| `{reason}`, the TLS connection failed (untrusted certificate, failed handshake) | its TLS connection failed |

`{address}` is the address as it would be stored, as the person typed it; the line never names a redirect's target, a reply's body or headers, or a certificate or error detail. A refusal is a 422 (the owner's answer A in the README's #347 section, so it cannot be mistaken for the environment's 409): nothing is stored, the page state is not reloaded, the field keeps what was typed, and the line stays until the next press, which clears it. When the check passes, the address is stored and the wizard goes on to People as before, with no new status line; coming back to the step shows the stored state with "Saved.". There is no way to store an address Immich doesn't answer at from the wizard; the environment's `IMMICH_URL` remains for that. Before the passing test the route refuses a save with a 409, "Immich's address can be saved only after the test sign-in has passed", and contacts nothing (the owner's answer J); the wizard never shows the step then.

### Step 4 of 8: People

| Part | Copy |
| --- | --- |
| Heading | People |
| Text | Add each person whose photos Darkroom looks after, with an Immich API key from their own Immich account. |
| Content | The People page's list of people and its add-person form, exactly as on boards `03-people-*`, with the same fields, buttons, copy, errors and key handling (the key is sealed and never shown back) |
| Empty state | No people yet. Add at least one person to continue. |
| Primary button | Continue (disabled until at least one person exists) |
| Secondary button | Back (to Immich) |

The calls are the People page's own, made with the tested session; their errors show as they do on People.

### Step 5 of 8: Google accounts

| Part | Copy |
| --- | --- |
| Heading | Google accounts |
| Text | Add each Google account whose Google Photos Darkroom exports, and sign it in once in the remote browser. Darkroom never sees the password; you type it into Google yourself. |
| Content | The People page's Google account parts, exactly as on boards `03-people-*`, including its "Sign in" button, which opens the Remote browser page (boards `07-browser-*`) |
| Empty state | No Google accounts yet. You can add them now or later on the People page. |
| Primary button | Continue |
| Secondary buttons | Skip for now (when no account exists), Back |

The Remote browser page is the app's own, unchanged; it is opened from the wizard with `?from=setup`, and then shows one extra link at its top, "Back to setup" (to `/setup`), the only change to board 07 this design asks for. An account still waiting for its sign-in shows its usual "needs sign-in" state; Continue does not wait for it.

### Step 6 of 8: Pixels

| Part | Copy |
| --- | --- |
| Heading | Pixels |
| Text | Add each Pixel that uploads to Google Photos, the Google account it uploads to and the people whose photos it carries. |
| Content | The People page's Pixel parts, exactly as on boards `03-people-*`, as built today (the device ID typed from the Pixel's Syncthing app) |
| Empty state | No Pixels yet. You can add them now or later on the People page. |
| Primary button | Continue |
| Secondary buttons | Skip for now (when no Pixel exists), Back |

When Darkroom's own Pixel pairing exists (C5, with its design signed off in C6), this step shows that pairing instead; until then it is the form as built.

### Step 7 of 8: Alert emails (optional)

| Part | Copy |
| --- | --- |
| Heading | Alert emails |
| Text | Optional. Darkroom can email one address when something needs you, through Resend. You can skip this and set it in the environment later. |
| Field | Resend API key (a password field; hint "Darkroom stores it sealed with its secret key and never shows it again.") |
| Field | From address (hint "An address on a domain you've verified in Resend.") |
| Field | Send alerts to |
| Primary button | Save and continue |
| Secondary buttons | Skip, Back |

Posts `POST /api/setup/alerts` with `alert_email_from`, `alert_email_to` and, only when the key field is not empty, `resend_api_key`. With no key stored and the key field empty, the API answers 400 "resend_api_key is required", shown as below. Skip saves nothing and goes to Done. There is no test email; that is #348, and #305 ships without it (decision 9).

### Step 8 of 8: Done

| Part | Copy |
| --- | --- |
| Heading | Darkroom is set up |
| Text | {n} people, {n} Google accounts and {n} Pixels. Alert emails: on, or "off". |
| Sub-heading | Before you go |
| Numbered list | Remove `DARKROOM_WIZARD=1` from the container's settings. |
| | Back up the appdata folder. It holds Darkroom's database and its secret key, which are only useful together, so keep the backup as safe as a password. |
| | Google accounts still waiting for their sign-in are listed on the People page; sign them in from there. |
| Muted text | Finishing closes setup for good. After that, settings are changed in Darkroom's environment. |
| Primary button | Finish setup |
| Secondary button | Back |

Finish setup posts `POST /api/setup/finish` (#346) and then goes to `/`, the Status page, already signed in. A refusal shows "Couldn't finish setup: {message}." under the button. Decision 3 put this content before the test, because the wizard closed there; with the order of decision 2 the wizard stays open, so the content is here, at the end, where section 8 lists it.

## Errors on every step

A refused save or test shows one danger `role="alert"` line under the step's primary button, with the API's message unchanged (decision 10), and keeps what the person typed except secrets (below). It goes at the next press of any button in the card. On People, Google accounts and Pixels the People page's own error handling applies unchanged.

| Reply | Line |
| --- | --- |
| 400 from a save | Couldn't save {what}: {message}. (`{what}`: "the address", "the sign-in details", "Immich's address", "the alert settings") |
| 409, set in the environment (only when the page is out of date) | Couldn't save {what}: {message}. The page then reloads its state, so the field turns into the environment row |
| 409 from Save details, no address | Couldn't save the sign-in details: {message}. |
| 503, no secret key | Couldn't save {what}: {message}. |
| 422 from Save and continue on Immich: the check failed (#347, signed off) | Couldn't save Immich's address: {message}. The page state is not reloaded |
| 409 from Test sign-in (another address, or nothing saved) | Couldn't start the test sign-in: {message}. |
| 502 from Test sign-in | Couldn't start the test sign-in: {message}. |
| 401 from any setup call | The "Setup isn't open" screen |
| 401 from a People, account or Pixel call after the test | The "Setup isn't open" screen (the tested session ended, which also closes setup) |
| No network, or any other 5xx | Couldn't save {what}: {the error's text}. (or "Couldn't start the test sign-in: ...", "Couldn't finish setup: ...") |
| `GET /api/setup` fails other than 401 | In the card, "Couldn't load setup: {message}." with a "Try again" button that loads it again |

The messages the API gives today, so the boards can show real ones: "public_url must be an http:// or https:// address without a user name, query or fragment", "public_url must be just the address, without a path", "public_url must use https:// for an address reachable beyond the LAN", the same for `immich_url` (without the path one), "alert_email_from must be one email address, like alerts@example.com", "resend_api_key is required", "resend_api_key must be printable ASCII without spaces", "oidc_client_id must be printable ASCII without spaces", "oidc_allowed_emails must hold email addresses, like jo@example.com", "OIDC_ISSUER must use https:// unless the provider is on the LAN", "set OIDC_ALLOWED_GROUPS or OIDC_ALLOWED_EMAILS so someone can sign in", "confirm the address Darkroom is opened at first, so the sign-in can come back to it", "Darkroom has no secret key to seal settings with; check the secret-key file in DARKROOM_DATA_DIR", "{VARIABLE} is set in the environment, which always wins over the wizard; change it there", "there are no proposed sign-in settings to test; send them first", "the test sign-in comes back to DARKROOM_PUBLIC_URL, which is not the address this browser is using, so the wizard could not see it finish; open the wizard at the address you confirmed, or confirm the address you are using now", "the test sign-in could not start: {reason}" and, with #347 (signed off), "Immich didn't answer at {address}: nothing answered there", "Immich didn't answer at {address}: the server there isn't Immich" and "Immich didn't answer at {address}: its TLS connection failed", and before the test "Immich's address can be saved only after the test sign-in has passed".

## What the wizard may and may not show

- **No secret is ever echoed** (decision 11). The client secret, the Resend key and each person's Immich API key are password fields (`type="password"`, `autocomplete="off"`), never prefilled, without a show button, emptied after every save (successful or not), never put in a URL, the page's history or browser storage, and never in an error line. The API gives back only whether one is set (`set`, `has_client_secret`, and the People page's own flag), and the page shows only that. The claim cookie and the session cookie are HttpOnly and never shown.
- **Values from the environment are never shown.** The API does not return them; the page says only that the setting is set there.
- **Nothing on the page comes from outside the LAN.** The page loads only Darkroom's own files, under the app's CSP (`'self'`): no web font, image, script or analytics from anywhere else. Copy uses the browser's own clipboard. What the person types goes only to Darkroom.
- **What reaches beyond Darkroom, and when.** The test sign-in: the browser is sent to the issuer address the person typed, and Darkroom fetches that issuer's configuration and exchanges the code with it. After the test, the steps that already reach out on the People page do the same as there (a person's Immich key against the Immich address; a Google account's sign-in in the remote browser, which reaches Google). Saving the address or the alert settings contacts nothing, and the wizard sends no email. Saving Immich's address (#347, signed off) asks Immich's unauthenticated ping at the address typed, and nothing else: no key, cookie or header from Darkroom goes with it, and it is made only after the passing test, never by a route open before it.
- **Before the test**, the wizard shows nothing about people, accounts, Pixels or photos, and the setup routes return none. After it, the People, Google accounts and Pixels steps show what the People page shows to that signed-in member, nothing more.

## How it is turned on and off

From SECURITY.md and the runbook's "The first-run setup API", with the changes #346 makes; #346 also updates both documents.

- **On:** `DARKROOM_WIZARD=1` in the container's settings, with no sign-in configured in the environment, and only while Darkroom's port cannot be reached from outside the LAN, IPv6 included. A connection relayed by Docker's userland proxy or a port forwarder arrives from a private address without forwarding headers and would count as a visitor from the LAN; the page cannot see that, so the one-container guide says it next to the `/setup` address.
- **Who holds it before the test:** the first browser to press Start setup, connecting directly from a private address with an allowed `Host` and `Origin` and no forwarding header; its claim cookie lasts 30 days.
- **Who holds it after the test:** only a request with both that claim and a session of the tested account, still meeting every address and header condition. No second claim is accepted.
- **Closed for good:** at the first of Finish setup, the tested session ending (sign-out, expiry, a failed re-check), a sign-in by any other account, or 7 days after the passing test. After that every setup route answers 401 to everyone, signed-in members included, and settings change only in the environment. Steps not finished are done on the ordinary pages.
- **Off:** unsetting `DARKROOM_WIZARD`; the routes answer 401 and settings come from the environment as before. Setting it again after the test reopens only the same claim-and-session pair, never the first phase. Done asks the person to unset it.
- **A lost browser:** before the test, `python run.py reopen-setup` clears the claim. After it, the same command closes setup; it reopens nothing once anyone has signed in.
- **When values apply:** at once (#346). Precedence stays environment, then stored, then default.

## What the API must change (#346)

What the API on `main` (#304) does today, and what this design needs instead. #346 holds the scope, the negative tests and the acceptance criteria.

| Today (#304) | Needed |
| --- | --- |
| A passing test stores the sign-in settings and closes the wizard for good (`SETUP_LOCKED`); `wizard.locked` also counts any user as closing it | A passing test stores the settings and ends the first phase only; the tested account's user row does not close the wizard |
| The test signs nobody in and renders the "Sign-in works... Restart Darkroom" page | The test signs the account in with a normal session (the same cookie, lifetime and re-checks as `/auth/callback`) and redirects to `/setup` |
| Setup routes accept the claim cookie alone | Before the test, the claim cookie alone (as today); after it, the claim cookie and the tested account's session together, with the same address, header, `Host` and `Origin` checks; anything else 401 |
| Stored values apply from the next start | Stored values apply at once, so sign-in, Immich's address and the alert settings work without a restart |
| No way to finish | `POST /api/setup/finish` closes the wizard for good; so do the tested session ending, another account's sign-in and 7 days after the test |
| `GET /api/setup` has no phase | It adds `phase` (`before_test`, `after_test`) and, after the test, the tested account's email; nothing secret |
| `reopen-setup` clears the claim; reopens nothing after a sign-in | The same before the test; after it, it closes setup |

The People, account and Pixel routes are not changed: the wizard calls them with the tested session like the People page does.

## The owner's decisions

The owner answered the first draft's open points on #344, and the three that decision 2 raised afterwards (16 to 18). They are recorded here as the owner's decisions. The owner then signed off the document as a whole on 2026-11-16 (#344, recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926281385); the owner then signed off the boards on 2026-11-16 (visual sign-off recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926640234), with the drawing choices below.

1. **What is signed off:** accepted as recommended. This document first, then the boards listed above, before #303 closes.
2. **The steps and their order:** the owner chose to extend the API rather than shorten the wizard: "we should never provide a worse experience just so the tests pass". The wizard keeps section 8's full order, and a passing test does not close it before People, Google accounts and Pixels (#346, which #305 depends on).
3. **Section 8's Done screen:** accepted as recommended (its content in the wizard, not on the server's success page). With decision 2 the wizard stays open after the test, so the content (restart, turn the wizard off, sign in, back up appdata) sits on the final Done step at the end, not before the test, and the server's success page is replaced by the redirect to `/setup`. The owner confirmed this reading.
4. **How a person finds `/setup`:** accepted as recommended. Named in the one-container guide and the runbook; the signed-out page is unchanged.
5. **When the wizard is claimed:** accepted as recommended. Only when Start setup is pressed, never on load.
6. **One closed screen for every reason:** accepted as recommended.
7. **The address step:** accepted as recommended. Prefilled with the address in use, editable, with the warning line; an https address through Traefik is set in the environment afterwards.
8. **Immich "checked":** a follow-up issue, #347; #305 is not held for it and ships without it, as this design says. Addendum for #347 (signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469): the 304 px left pane with 40 px padding on all 11 desktop wizard boards, the card filling the column as built in #363, the Immich check states, refusal status 422, an Immich save before the passing test refused with 409 and contacting nothing, choices B to H as recommended; phone boards, the test-failed desktop board and the add-person dialog unchanged): the check, its in-flight state and its three refusals are in "Step 3 of 8: Immich" and on boards `08-setup-immich-check-*`; the choices are in the README's #347 section.
9. **A test email:** a follow-up issue, #348; #305 ships without it.
10. **Error wording:** accepted as recommended. The API's message unchanged inside "Couldn't save {what}: {message}.".
11. **Secret fields:** accepted as recommended. Password fields with no show button, emptied after every save.
12. **Settings from the environment:** accepted as recommended. The read-only row with no value; the whole alert step as rows when any of its three is in the environment.
13. **The server's failure pages:** a follow-up issue, #349 (a "Back to setup" link); #305 ships without it.
14. **The desktop layout:** accepted as recommended. The signed-out page's two columns with the step list on the left. The widths in the recommendation (half-page and 420 px left panels, the card at most 30 rem or 48 rem) are superseded by the signed-off revision in "Desktop (1280)" above: a 304 px left pane and the card filling the column.
15. **Images for #305:** the wizard mode of `tools/verify.py` is part of #305.
16. **People required:** accepted as recommended. Continue on People needs at least one person; Google accounts and Pixels stay skippable. Not chosen: letting People be skipped too.
17. **The one change to the Remote browser page:** accepted as recommended. A "Back to setup" link at its top when the page is opened from the wizard (`?from=setup`); this is the only change to board 07. Not chosen: no change, leaving the person to the browser's back button.
18. **The post-test time limit:** accepted as recommended. After the passing test the wizard stays reachable for 7 days, then closes by itself; it also closes on Finish setup, when the tested session ends, or when another account signs in. Not chosen: no limit beyond the session's own life.

### The boards' drawing choices (decided)

Where this document was silent, the boards made these choices. The owner signed off the boards with all of them on 2026-11-16 (recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926640234):

1. The People, Google accounts and Pixels steps say "exactly as on boards `03-people-*`", but those boards have no add forms; the forms are drawn as the built People page has them (its modal, fields and copy).
2. The Pixels step is drawn as this document says, "as built today" (the typed device ID); once the signed-off pairing (#322) is built (#323) the step shows that pairing instead.
3. The step's own `h1` names the Google accounts and Pixels steps, so their People-page card title is not repeated; People keeps "Persons" above its table.
4. Desktop button order in one row: primary first, then the secondary ones, the same order as the phone's stack.
5. Sign-in: a refused Save details shows its line under Save details (where "Saved. Test them below." goes), not under the step's primary button (Test sign-in).
6. Welcome on desktop shows the step list with no step current; the closed screens show only the brand row, because the browser does not hold the wizard.
7. Stored settings show the hint "Saved." in place of the field's own hint (Alert emails' From address).
8. Done's sentence has no singular forms in this document ("{n} Google accounts and {n} Pixels"), so the board uses 2 of each.
9. Alert emails set in the environment keeps Skip and Back beside Continue, since this document only renames the primary.
10. The Remote browser's "Back to setup" link (decision 17) is described in a note on the accounts board, not drawn; the board list names no browser board.
11. "No network" messages use the app's existing "Couldn't reach Darkroom." text, as the pairing boards do.
