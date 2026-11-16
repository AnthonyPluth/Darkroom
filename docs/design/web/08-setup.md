# First-run setup wizard (#303, proposed)

Status: **draft for the owner's sign-off on #303. Nothing here is signed off, and nothing is built from it until it is (AGENTS.md, promise 6).** The UI is #305; the API it calls is on `main` (#304, `darkroom/server/api/wizard.py` and `darkroom/server/wizard.py`, behind `DARKROOM_WIZARD=1`). The steps come from `docs/design/single-container.md`, section 8, and the owner's answer 4 there. This document changes no API: where the list in section 8 asks for something the API as built does not do, it says so and the decision is in "Open points" at the end, each with a recommendation.

Every name, address and key below is made up.

## What the owner reviews

There are no images yet, and none are drawn here: an image of a screen that does not exist would be a guess. The review is in two stages, and both are recorded on #303.

1. **This document**: the steps and their order, every screen and state, the exact copy, every error and empty state, what the wizard may and may not show, and how it is turned on and off. Copy in quotation marks or in the copy tables is word for word; `{placeholders}` are filled at run time.
2. **The boards**, once the owner has answered the open points: drawn on the Design canvas (https://claude.ai/artifact/BvpF7AujGCxiVA9bKvWiUR) with the tokens of `00-tokens.dc.html` only, no new colour or token, and exported into this folder like the others. Desktop boards 1280 wide, phone boards 390 wide, as for #16. Each board stacks the states named for it:

| Board (desktop and phone) | States drawn |
| --- | --- |
| `08-setup-welcome-desktop.dc.html`, `08-setup-welcome-phone.dc.html` | Welcome; Welcome with "Starting..." in flight; Welcome with the start error line |
| `08-setup-closed-desktop.dc.html`, `08-setup-closed-phone.dc.html` | Setup isn't open; Setup is open in another browser |
| `08-setup-address-desktop.dc.html`, `08-setup-address-phone.dc.html` | Prefilled with the address in use; edited to another address (the warning line); a refused save; set in the environment |
| `08-setup-immich-desktop.dc.html`, `08-setup-immich-phone.dc.html` | Empty; stored; a refused save; set in the environment |
| `08-setup-alerts-desktop.dc.html`, `08-setup-alerts-phone.dc.html` | Empty; a key stored; a refused save; set in the environment |
| `08-setup-sign-in-desktop.dc.html`, `08-setup-sign-in-phone.dc.html` | No details saved yet (test disabled); details saved with a secret stored (test enabled); unsaved edits (test disabled); a refused save; the test could not start; set in the environment |
| `08-setup-result-desktop.dc.html`, `08-setup-result-phone.dc.html` | The server's result pages after the test: Sign-in works; Not allowed; The test sign-in failed; The settings changed; Nothing to test |

After the build, #305's pull request puts `make verify` images of each step at phone (440 x 956) and desktop (1280 x 800) beside these boards and says where they differ, as `fleet-design-signoff` asks. The phone boards are 390 wide and `make verify`'s phone is 440, as for every other page.

## Where it is and who can open it

- **Address.** The wizard is the web app's `/setup` page, outside the app shell: no rail, no tab bar, no navigation, because nobody is signed in. `/` is unchanged: a visitor there sees the signed-out page of boards `01-signin-*`. The API already names `/setup` as the test sign-in's path (`TEST_PATH` in `darkroom/server/wizard.py`), though the test itself ends on the server's own result page (below).
- **How a person finds it.** The one-container guide (`docs/deploy/one-container.md`) and the runbook say to open `http://<server's address>:8000/setup` the first time. Nothing on the signed-out page changes (open point 4).
- **Who.** Exactly the rule the API enforces (SECURITY.md, the runbook's "The first-run setup API"): with `DARKROOM_WIZARD=1`, while no sign-in is set up (no `OIDC_*` in the environment, not `DARKROOM_ALLOW_NO_AUTH`, not `DARKROOM_DEMO`) and before anyone has signed in, the first browser that presses Start setup holds the wizard. It must connect directly from a private address (10/8, 172.16/12, 192.168/16, loopback, fc00::/7), with no forwarding header and with an allowed `Host`. Everyone else, signed-in members included, gets the "Setup isn't open" screen. The page cannot tell which condition failed, because the API answers 401 for all of them, on purpose; the screen lists them all instead.

## Layout

- **Phone (390).** One column, 16 px side padding: the brand row ("Darkroom" with the accent dot, as on the signed-out page), then a muted line "Step {n} of 4: {step name}" (not on Welcome or the closed screens), then one card (`card`) holding the step. Buttons are full width, 48 px high, stacked: the primary first, then Back. Fields are full width with their label above and their hint below.
- **Desktop (1280).** The signed-out page's two columns. The left panel (`bg-rail`) keeps the brand row and, in place of the tagline, the list of steps: Address, Immich, Alert emails, Sign-in. The current step is marked with `aria-current="step"` and bold ink; a finished step says "Done" in text after its name, never by colour alone; a step not reached is muted. Finished steps are links back to themselves. The right column holds the same card as on a phone, at most 30 rem wide, with the buttons in one row (primary first, then Back).
- **Tokens and parts.** Only the signed-off tokens and the parts already in `frontend/src/app.css` (`card`, `btn`, `btn-primary`, the muted text, the danger alert line used on Exports). No new colour, icon or token.
- **Focus and announcements.** Each screen has one `h1`. On a change of step, focus moves to that `h1`. Error lines are `role="alert"` and named by the field's `aria-describedby`; "Copied." and "Saved." are `role="status"`.

## The steps

Welcome, then four numbered steps, then the server's result page. The order is fixed by the API: a passing test sign-in stores the sign-in settings and closes the wizard for good, so every other setting has to be saved before it and the test is last (open point 2). People, Google accounts and Pixels are not in the wizard; the last step says where they are done.

When the page loads, it asks `GET /api/setup`. A 200 means this browser already holds the wizard: it opens at the first step whose setting is still unset (address, then Immich, then sign-in; Alert emails is skipped over because it is optional), or at Sign-in when all are set. A 401 shows Welcome. Nothing is claimed on load (open point 5).

### How a setting is shown

Each setting the API lists (`settings[]` in its reply) is drawn by its `source`:

| Source | Plain setting (address, Immich, emails, issuer, client ID, groups) | Secret (`OIDC_CLIENT_SECRET`, `RESEND_API_KEY`) |
| --- | --- | --- |
| `default` (not set) | Empty field, or prefilled where the step says | Empty field |
| `stored` | Field prefilled with the stored value; hint "Saved. Darkroom uses it from its next start." | Empty field; hint "A key is saved. Leave this empty to keep it." (for the client secret: "A secret is saved. Leave this empty to keep it.") |
| `environment` | No field. A read-only row: the label, then "Set in Darkroom's environment.", then the muted hint "The environment always wins over setup. To change it, change the container's settings." No value: the API returns none | The same row; no value |

When every setting of a step is set in the environment, its primary button reads "Continue" and saves nothing. The alert step is one unit in the API (any of its three in the environment refuses all three), so if any of the three is in the environment the whole step shows rows and "Continue".

### Welcome

| Part | Copy |
| --- | --- |
| Heading | Set up Darkroom |
| Text | This sets Darkroom up on this server, from this browser. You'll need: |
| List | The address you open Darkroom at (this one, most likely). |
| | Immich's address. |
| | A client for Darkroom in Pocket ID: its issuer address, client ID and client secret. |
| | Optionally, a Resend API key for alert emails. |
| Text | You won't be asked to invent a password or a key: Darkroom makes and keeps its own. |
| Muted text | Once you start, only this browser can continue. Setup closes for good when the test sign-in at the end works. |
| Primary button | Start setup |

Pressing it posts `POST /api/setup/claim`. While it is in flight the button is disabled and reads "Starting...".

| Reply | What shows |
| --- | --- |
| 200 | Step 1 (or the first unset step, as above) |
| 401 | Setup isn't open |
| 409 | Setup is open in another browser |
| Anything else (no network, a 5xx) | Under the button, a danger `role="alert"` line: "Couldn't start setup: {message}." The button is enabled again |

### Setup isn't open

Shown for a 401 from the claim, and for a 401 from any later call (the wizard closed, the claim was cleared, or `DARKROOM_WIZARD` was turned off).

| Part | Copy |
| --- | --- |
| Heading | Setup isn't open |
| Text | Darkroom's first-run setup opens only when all of these hold: |
| List | `DARKROOM_WIZARD=1` is set in the container's settings. |
| | Nobody has signed in to Darkroom yet, and sign-in isn't set up in the environment. |
| | This device is on your home network. |
| | You opened `http://<server's address>:8000/setup` directly, not through Traefik or another proxy. |
| Muted text | After the first sign-in, settings are changed in Darkroom's environment. |
| Secondary button | Go to sign-in (a link to `/`) |

### Setup is open in another browser

Shown for a 409 from the claim.

| Part | Copy |
| --- | --- |
| Heading | Setup is open in another browser |
| Text | Another browser started Darkroom's setup first, and only that browser can continue it. Finish it there. |
| Muted text | If that browser is lost, run `python run.py reopen-setup` in the Darkroom container's console, then press Try again. |
| Primary button | Try again (posts the claim again) |

### Step 1 of 4: Address

| Part | Copy |
| --- | --- |
| Heading | Darkroom's address |
| Text | The address people open Darkroom at. Sign-in comes back to it, and alert emails link to it. |
| Field label | Address |
| Field value | The stored value, else the address this browser is using (`location.origin`, such as `http://192.168.1.20:8000`) |
| Hint | This is the address you're using now. The test sign-in at the end can only come back here, so keep it for setup. |
| Primary button | Confirm address |
| Secondary button | Back (to Welcome) |

When the field no longer equals `location.origin`, a warning line (warn tone, not an alert) appears under the hint: "This isn't the address you're using now, so the test sign-in at the end can't finish here." The button stays enabled: the API accepts the address, and the test then answers 409.

Posts `POST /api/setup/address` with `public_url`. 200: on to step 2.

### Step 2 of 4: Immich

| Part | Copy |
| --- | --- |
| Heading | Immich |
| Text | The address Darkroom reaches Immich at from this server, such as `http://192.168.1.20:2283`. |
| Field label | Immich address |
| Primary button | Save and continue |
| Secondary button | Back |

Posts `POST /api/setup/immich` with `immich_url`. The API stores the address without contacting Immich, so the screen makes no claim that it was checked (open point 8). Each person's Immich API key is entered later on People, as today.

### Step 3 of 4: Alert emails (optional)

| Part | Copy |
| --- | --- |
| Heading | Alert emails |
| Text | Optional. Darkroom can email one address when something needs you, through Resend. You can skip this and set it in the environment later. |
| Field | Resend API key (a password field; hint "Darkroom stores it sealed with its secret key and never shows it again.") |
| Field | From address (hint "An address on a domain you've verified in Resend.") |
| Field | Send alerts to |
| Primary button | Save and continue |
| Secondary buttons | Skip, Back |

Posts `POST /api/setup/alerts` with `alert_email_from`, `alert_email_to` and, only when the key field is not empty, `resend_api_key`. With no key stored and the key field empty, the API answers 400 "resend_api_key is required", shown as below. Skip saves nothing and goes to step 4. There is no test email (open point 9).

### Step 4 of 4: Sign-in

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
| Text | Sign in with an account these details allow. If it works, Darkroom keeps these details and setup closes for good. |
| Sub-heading | After the test |
| Numbered list | Restart the Darkroom container. Everything you set here takes effect then, sign-in included. |
| | Remove `DARKROOM_WIZARD=1` from the container's settings. |
| | Open {public_url} and sign in. |
| | On People, add each person with their Immich API key, and each Google account; sign the accounts in on Remote browser. |
| | Back up the appdata folder. It holds Darkroom's database and its secret key, which are only useful together, so keep the backup as safe as a password. |
| Muted text | Pixels are paired later, once Darkroom's Pixel sync is turned on. |
| Primary button | Test sign-in |
| Secondary button | Back |

Test sign-in is enabled only when details are saved (`proposal` is not null) and no field has changed since they were saved; otherwise a muted line under it says "Save the details above first." Pressing it posts `POST /api/setup/sign-in/test`, and on 200 the browser goes to the returned `location` in the same tab (Pocket ID's sign-in).

### After Pocket ID: the result pages

Pocket ID sends the browser back to `/auth/callback`, which the server answers with its own small page (the `page` helper in `darkroom/server/common.py`, its own style and CSP), not the web app. Their text is the API's as built, quoted here so that it is part of the sign-off:

| Result | Title | Text | Link |
| --- | --- | --- | --- |
| It worked | Sign-in works | Darkroom stored these sign-in settings. Restart Darkroom to turn sign-in on, then sign in. | Sign in |
| The account is not allowed | Not allowed | The account you signed in with is not allowed by the proposed settings, so nothing was saved. Check the allowed groups and email addresses, then test again. | none (open point 13) |
| Another browser, or the claim was cleared | Not allowed | This test sign-in did not start here, so nothing was saved. | none |
| Pocket ID refused or the reply was wrong | The test sign-in failed | Something was wrong with this sign-in: {error}. Nothing was saved. | none |
| The details changed during the test | The settings changed | The proposed sign-in settings changed during the test, so nothing was saved. Test again. | none |
| No details to test | Nothing to test | There are no proposed sign-in settings to test, so nothing was saved. | none |

After "Sign-in works" the wizard is closed: `/setup` shows Welcome, and Start setup leads to "Setup isn't open".

## Errors on every step

A refused save or test shows one danger `role="alert"` line under the step's primary button, with the API's message unchanged (open point 10), and keeps what the person typed except secrets (see below). It goes at the next press of any button in the card.

| Reply | Line |
| --- | --- |
| 400 from a save | Couldn't save {what}: {message}. (`{what}`: "the address", "Immich's address", "the alert settings", "the sign-in details") |
| 409, set in the environment (only when the page is out of date) | Couldn't save {what}: {message}. The page then reloads its state, so the field turns into the environment row |
| 409 from Save details, no address | Couldn't save the sign-in details: {message}. |
| 503, no secret key | Couldn't save {what}: {message}. |
| 409 from Test sign-in (another address, or nothing saved) | Couldn't start the test sign-in: {message}. |
| 502 from Test sign-in | Couldn't start the test sign-in: {message}. |
| 401 from any call | The "Setup isn't open" screen |
| No network, or any other 5xx | Couldn't save {what}: {the error's text}. (or "Couldn't start the test sign-in: ...") |
| `GET /api/setup` fails other than 401 | In the card, "Couldn't load setup: {message}." with a "Try again" button that loads it again |

The messages the API gives today, so the boards can show real ones: "public_url must be an http:// or https:// address without a user name, query or fragment", "public_url must be just the address, without a path", "public_url must use https:// for an address reachable beyond the LAN", the same three for `immich_url` (without the path one), "alert_email_from must be one email address, like alerts@example.com", "resend_api_key is required", "resend_api_key must be printable ASCII without spaces", "oidc_client_id must be printable ASCII without spaces", "oidc_allowed_emails must hold email addresses, like jo@example.com", "OIDC_ISSUER must use https:// unless the provider is on the LAN", "set OIDC_ALLOWED_GROUPS or OIDC_ALLOWED_EMAILS so someone can sign in", "confirm the address Darkroom is opened at first, so the sign-in can come back to it", "Darkroom has no secret key to seal settings with; check the secret-key file in DARKROOM_DATA_DIR", "{VARIABLE} is set in the environment, which always wins over the wizard; change it there", "there are no proposed sign-in settings to test; send them first", "the test sign-in comes back to DARKROOM_PUBLIC_URL, which is not the address this browser is using, so the wizard could not see it finish; open the wizard at the address you confirmed, or confirm the address you are using now" and "the test sign-in could not start: {reason}".

## What the wizard may and may not show

- **No secret is ever echoed.** The client secret and the Resend key are password fields (`type="password"`, `autocomplete="off"`), never prefilled, without a show button, emptied after every save (successful or not), never put in a URL, the page's history or browser storage, and never in an error line. The API gives back only whether one is set (`set`, `has_client_secret`), and the page shows only that, in the hints above. The claim cookie is HttpOnly and never shown.
- **Values from the environment are never shown.** The API does not return them; the page says only that the setting is set there.
- **Nothing on the page comes from outside the LAN.** The page loads only Darkroom's own files, under the app's CSP (`'self'`): no web font, image, script or analytics from anywhere else. Copy uses the browser's own clipboard. What the person types goes only to Darkroom.
- **What reaches beyond Darkroom, and when.** Only the test sign-in: the browser is sent to the issuer address the person typed, and Darkroom fetches that issuer's configuration and exchanges the code with it. Saving the address, Immich's address or the alert settings contacts nothing. The wizard sends no email, because Resend is on the internet (open point 9).
- **Not in the wizard:** a person's Immich API key, Google accounts and their sign-in, Pixels and their pairing, the schedule, or anything about photos. No photo, file name, person or account is shown, because none exists yet and the setup routes return none.

## How it is turned on and off

From SECURITY.md and the runbook's "The first-run setup API"; the screens add nothing to these rules and remove nothing.

- **On:** `DARKROOM_WIZARD=1` in the container's settings, with no sign-in configured in the environment, and only while Darkroom's port cannot be reached from outside the LAN, IPv6 included. A connection relayed by Docker's userland proxy or a port forwarder arrives from a private address without forwarding headers and would count as a visitor from the LAN; the page cannot see that, so the one-container guide says it next to the `/setup` address.
- **Who holds it:** the first browser to press Start setup, connecting directly from a private address with an allowed `Host` and no forwarding header; its claim cookie lasts 30 days.
- **Closed for good:** at the first successful test sign-in or the first sign-in. After that every setup route answers 401, to signed-in members too, and settings change only in the environment.
- **Off:** unsetting `DARKROOM_WIZARD`; the routes answer 401 and settings come from the environment as before. The wizard's last step asks the person to do this after the restart.
- **A lost browser:** `python run.py reopen-setup` clears the claim; it reopens nothing once anyone has signed in or sign-in is set up.
- **When values apply:** at Darkroom's next start; until then the running service keeps what it started with, which is why the stored hint and the last step say so.

## Open points for the owner

Each has a recommendation; the owner's answers are recorded on #303, and this section then becomes "approved as ...".

1. **What is signed off.** Recommended: this document first (flow, copy, states, rules), then the boards listed above, drawn from it on the canvas with the signed-off tokens and committed to this folder before #303 closes. Alternative: sign off this document alone and let #305 compare its `make verify` images with the text.
2. **The steps and their order.** Recommended: Welcome, Address, Immich, Alert emails (optional), Sign-in with the test last; People, Google accounts and Pixels leave the wizard. A passing test closes the wizard, and the People, Accounts and Pixel routes need a signed-in session, so section 8's order (sign-in third, Immich and alerts after it, then People, Google accounts and Pixels) cannot run on the API as built. Alternative: keep section 8's order by extending the API (a new issue, and a security review of the pre-sign-in window).
3. **Section 8's "Done" screen.** Recommended: its content (restart, turn the wizard off, sign in, People and Google accounts, back up appdata) sits in step 4 before the test, because after a passing test the wizard is closed and the server's "Sign-in works" page is what shows. Alternative: change the server's success page to carry it (a small backend change, in its own issue).
4. **How a person finds `/setup`.** Recommended: the one-container guide and the runbook name `http://<server's address>:8000/setup`; the signed-out page (boards 01) is unchanged. Alternative: a link on the signed-out page, which changes a signed-off board and would show to every signed-out visitor.
5. **When the wizard is claimed.** Recommended: only when Start setup is pressed, never on loading the page, so a visitor who only looks does not lock everyone else out for 30 days. Alternative: claim on load.
6. **One closed screen for every reason.** Recommended: "Setup isn't open" with all the conditions listed, because the API deliberately gives one 401 whatever the reason. Alternative: none without an API change, which would tell a stranger more.
7. **The address step.** Recommended: prefilled with the address in use, editable, with the warning line when changed. Setup then works at the LAN address only; a household that uses an https address through Traefik sets `DARKROOM_PUBLIC_URL` and the `OIDC_*` settings in the environment afterwards, as the runbook already says. Alternative: a read-only address with only "Confirm".
8. **"Immich: its address, checked."** Recommended: the screen claims no check, because the API does not contact Immich; a check is a follow-up issue, since it would be an outbound request from a route open before sign-in. Alternative: hold #305 until the API checks it.
9. **A test email.** Recommended: none in the wizard; the API has no route for one and it would reach Resend on the internet before anyone has signed in. Alternative: a follow-up API issue and a "Send a test email" button on step 3.
10. **Error wording.** Recommended: the API's message unchanged inside "Couldn't save {what}: {message}.", as on Exports (#164). Alternative: reword the API's messages, several of which name fields such as `public_url`, in a follow-up issue before #305.
11. **Secret fields.** Recommended: password fields with no show button, emptied after every save. Alternative: a show button for what the person has just typed (it never shows a stored secret either way).
12. **Settings from the environment.** Recommended: the read-only row with no value, and the whole alert step as rows when any of its three is in the environment. Alternative: hide those settings altogether.
13. **The server's failure pages.** Recommended: add a "Back to setup" link (to `/setup`) on every failure page, since the claim still holds after a failed test; it is a small change in `darkroom/server/wizard.py`, in its own issue or in #305 with the owner's say-so. Alternative: keep them as built and rely on the browser's back button.
14. **The desktop layout.** Recommended: the signed-out page's two columns, with the step list in the left panel. Alternative: one centred card at every width, with the "Step {n} of 4" line as on a phone.
15. **Images for #305.** `make verify` runs on demo data, and demo mode keeps the wizard closed, so the wizard cannot be shown as `make verify` runs today. Recommended: #305 adds a wizard mode to `tools/verify.py` (an empty data folder, `DARKROOM_WIZARD=1` on loopback, no demo session, a fake sign-in provider for the result pages) and `setup-*` pages and a flow. Alternative: a separate issue for that before #305.
