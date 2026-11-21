# Web UI design (signed off)

The owner signed off this design on 2026-11-04 in issue #16. It is **dark mode only**: the app has one dark theme and no light one, and `frontend/src/app.css` holds exactly the token set in `00-tokens.dc.html` (the `@theme` names given there).

The boards are exported as read from the Design canvas: https://claude.ai/artifact/BvpF7AujGCxiVA9bKvWiUR. Each `.dc.html` file is one board; they reference the canvas runtime (`./support.js`), which is not kept here, so they read as source rather than open as pages. No screenshots are committed; a pull request that changes the UI describes what `make verify` and the demo server showed.

| File | Board |
| --- | --- |
| `Main.dc.html` | Cover and screen map |
| `00-tokens.dc.html` | Theme tokens: colour, type, spacing, radius |
| `01-signin-desktop.dc.html`, `01-signin-phone.dc.html` | Sign-in, signed out |
| `01-signin-error-desktop.dc.html`, `01-signin-error-phone.dc.html` | Sign-in, not allowed |
| `02-status-desktop.dc.html`, `02-status-phone.dc.html` | Status (home); the About card with its Version row, last on the page (approved 2026-11-18, #421, recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6986445014)) |
| `03-people-desktop.dc.html`, `03-people-phone.dc.html` | People |
| `04-exports-desktop.dc.html`, `04-exports-phone.dc.html` | Exports |
| `04-exports-renew-desktop.dc.html`, `04-exports-renew-phone.dc.html` | Exports, Renew now states (signed off, #164, recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6720414904)) |
| `04-exports-desktop.dc.html`, `04-exports-phone.dc.html` (last two blocks) | Exports, a failed download: the parts, why it stopped and Retry download, and in Earlier exports an older failed download that can still be retried (its sentence and a Retry download button in the row, shown when a newer export is current) and one that can't be retried with what to do instead (signed off by the owner, AnthonyPluth, on 2026-11-20, #427) |
| `05-imports-desktop.dc.html`, `05-imports-phone.dc.html` | Imports and Missing GPS |
| `04-exports-ingest-desktop.dc.html`, `04-exports-ingest-phone.dc.html` | Exports, per-part import (PP-13): each part's import state, the export's totals, parts Darkroom kept, an export that never went per-part; below a dashed line, PP-10's freed state, which PP-13 does not build (signed off by the owner, AnthonyPluth, on 2026-11-20, #490, recorded on #424 and #16; see "Per-part import on Exports" below) |
| `06-review-desktop.dc.html`, `06-review-phone.dc.html` | Review queue |
| `07-browser-desktop.dc.html`, `07-browser-phone.dc.html` | Remote browser |
| `07-browser-several-waiting.md` | Remote browser: the copy when several accounts wait for a sign-in, naming the account whose window is on screen (text spec, signed off by the owner, AnthonyPluth, on 2026-11-20, #496) |
| `03-people-pairing-desktop.dc.html`, `03-people-pairing-phone.dc.html` | People: pairing a Pixel and its Syncthing column (signed off, #322) |
| `02-status-syncthing-desktop.dc.html`, `02-status-syncthing-phone.dc.html` | Status: the Syncthing card, the Pixel row and the new alerts (signed off, #322); its last block, the About card's states (approved 2026-11-18, #421) |
| `03-people-pairing-states-desktop.dc.html`, `03-people-pairing-states-phone.dc.html` | People and Status: three per-Pixel states #322 did not draw, and sharing a removed folder again (addendum, signed off), with row 2d drawn in the full-width redraw |
| `03-people-unpair-desktop.dc.html`, `03-people-unpair-phone.dc.html` | People: unpairing a Pixel from Edit Pixel (addendum to #322, signed off 2026-11-16) |
| `08-setup.md` | First-run setup wizard: **document signed off by the owner on 2026-11-16 (#344, recorded on #303); the boards signed off by the owner on 2026-11-16 (#303)**. #305 builds it, after #346 extends the API |
| `08-setup-welcome-desktop.dc.html`, `08-setup-welcome-phone.dc.html` | Setup wizard: Welcome (signed off, #303) |
| `08-setup-closed-desktop.dc.html`, `08-setup-closed-phone.dc.html` | Setup wizard: Setup isn't open, and open in another browser (signed off, #303) |
| `08-setup-address-desktop.dc.html`, `08-setup-address-phone.dc.html` | Setup wizard: Step 1, Address (signed off, #303) |
| `08-setup-sign-in-desktop.dc.html`, `08-setup-sign-in-phone.dc.html` | Setup wizard: Step 2, Sign-in (signed off, #303) |
| `08-setup-test-failed-desktop.dc.html`, `08-setup-test-failed-phone.dc.html` | Setup wizard: After Pocket ID: the server's failure pages (signed off, #303) |
| `08-setup-immich-desktop.dc.html`, `08-setup-immich-phone.dc.html` | Setup wizard: Step 3, Immich (signed off, #303) |
| `08-setup-immich-check-desktop.dc.html`, `08-setup-immich-check-phone.dc.html` | Setup wizard: Step 3, Immich, checking the address (#347, signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469): the 304 px left pane with 40 px padding on all 11 desktop wizard boards, the card filling the column as built in #363, the Immich check states, refusal status 422, an Immich save before the passing test refused with 409 and contacting nothing, choices B to H as recommended; phone boards, the test-failed desktop board and the add-person dialog unchanged) |
| `08-setup-people-desktop.dc.html`, `08-setup-people-phone.dc.html` | Setup wizard: Step 4, People (signed off, #303) |
| `08-setup-accounts-desktop.dc.html`, `08-setup-accounts-phone.dc.html` | Setup wizard: Step 5, Google accounts (signed off, #303) |
| `08-setup-pixels-desktop.dc.html`, `08-setup-pixels-phone.dc.html` | Setup wizard: Step 6, Pixels (signed off, #303) |
| `08-setup-alerts-desktop.dc.html`, `08-setup-alerts-phone.dc.html` | Setup wizard: Step 7, Alert emails (signed off, #303) |
| `08-setup-done-desktop.dc.html`, `08-setup-done-phone.dc.html` | Setup wizard: Step 8, Done (signed off, #303) |

Desktop boards are 1280 wide (a left rail) and phone boards 390 wide (a top bar and a bottom tab bar). A page is built only against its board; a web page without a signed-off board here is not built (AGENTS.md, promise 6).

Any later change to layout, flow or visual style goes back to issue #16 for the owner's sign-off, and the boards here are re-exported from the canvas in the same pull request that changes the UI.

Built so far: Sign-in (signed out and not allowed), the app shell, Status and People (#17), the account part of Exports and the Remote browser (#29), Imports and Review (#27), the schedule, current export parts, earlier exports and "Check exports now" on Exports (#30), and per-part import in the current export parts card on Exports (#490, PP-13: states 1 to 6 of `04-exports-ingest-*`, and Imports is unchanged), and its freed state (#487, PP-10: state 7, the Freed chip, the Disk freed total and the sentence under the table).

Approved departures (#29): the Exports and Remote browser pages are built with these departures from boards 04 and 07, all approved by the owner and recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6636481608):

1. No "Done" button on the Remote browser sign-in banner (the worker notices the sign-in itself).
2. One Exports card per Google account instead of a single schedule card.
3. No export parts, earlier exports or "Check exports now" yet (they arrive with #30 and #31).
4. Exports: a warning box ("Takeout may already have this schedule...") with an "I checked Takeout, there is no schedule" button.
5. Exports: links to the failed job's screenshot and page snapshot.
6. Exports: "Connect: waiting for you to switch to the right Google account".
7. Remote browser: a "Wrong account" chip and "Switch to <email>" instructions.
8. Status: an "Open remote browser" link on the wrong-account alert.

The boards are re-exported from the canvas when the design session next touches them.

Approved departures (#27), approved by the owner and recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6644225186 and https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6645876749):

1. Imports shows one row per export run ("3 of 5 parts downloaded") instead of one row per export part ("Part 3 of 5"), because the counts exist only per export.
2. Review, Past decisions: a reversed decision shows "Reversed, so it can't be undone" in place of Undo.
3. Review: Reassign on the pending pair is one action that keeps both files and then moves the kept copy to the chosen person (hidden when the account serves one person); undoing it takes the copy back.
4. Imports, Missing GPS: the thumbnail is a plain placeholder tile where no thumbnail can be made.

Approved departures (#30), approved by the owner and recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6646447403). The Exports page builds the board's three parts (schedule, current export parts, earlier exports) and "Check exports now" on top of the per-account cards from #29:

1. Schedule: the account card keeps its Session checked, Schedule created and Schedule ends rows and adds Last export and Next export, five rows in the card instead of the board's three stats. Under Schedule ends it says only "In N weeks" (the board adds "A reminder goes out about 3 weeks before", which waits for renewal, #40), and under Next export "Darkroom checks daily" without the board's "from 1 Dec".
2. Current export parts and Earlier exports are one pair of cards per Google account, titled with the account's email for screen readers, below that account's card. "Check exports now" sits at the top of the page, as on the board, and checks every connected account.
3. Parts and earlier exports use the shared table that turns into cards on a phone (label and value on each line), not the board's phone card (part number and state on one line, then file, bar and downloads used).
4. A part's bar is full once the part is downloaded and empty before that; bytes so far arrive with #31. A part that has not been downloaded shows "Not downloaded yet" where the board shows a file name.
5. States the board does not show: "Google is preparing it" (an export Google has not finished, with a sentence instead of the table), "Waiting to download", "Downloaded", "Importing" and "Expired" (with "Never downloaded" in the earlier exports table).
6. Earlier exports show the result chip and "N sent to review"; the board's free-text reason for a failed export is not shown.

Approved departures (#40), approved by the owner on #16 via the session owner on 2026-11-05 and recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6670221021). The Exports account card gains the renewal's states:

1. Exports: after a renewal Darkroom could not confirm, a warning box ("Darkroom could not confirm the renewal...") with two buttons, "Takeout has the new schedule" and "Takeout has no new schedule". Until it is answered, the job's line reads "Renew schedule: not confirmed by Takeout; say below what you found." and the capture is "of the unconfirmed Renew schedule", never "failed". Once answered, the box goes and the job's line says what was found ("Renew schedule: not confirmed by Takeout, then you found it there." or "...and you found none there.").
2. Exports: the warning box for an unconfirmed first schedule (departure 4 of #29) gains a "Takeout has the schedule" button next to "I checked Takeout, there is no schedule".
3. Exports: Create schedule is enabled again once an account's schedule has ended unrenewed (with no schedule or renew job open and no attempt left unanswered), and the line under it then describes a new schedule.

## Renew now on Exports (#164, signed off)

Status: **signed off by the owner on 2026-11-08: all six open points approved as recommended.** Recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6720414904). The server route is on `main` (`POST /api/accounts/{id}/schedule/renew`, #173); the button is built to this design in the pull request for #164. Boards: `04-exports-renew-desktop.dc.html` and `04-exports-renew-phone.dc.html` show one account card in each state below (ten states, each with the existing Create schedule sentence kept); everything else on the page is as in the signed-off boards. The boards use the signed-off tokens only. They were previewed in a browser at 390 and 1280 wide; no images are committed, as above.

### The button

- **Label and place.** "Renew now", a secondary button (`btn`, never primary) last in the account card's button row, after Create schedule: Connect, Create schedule, Renew now. On a phone the row stacks, full width, 44 px high, in that order; from `md` up it is one wrapping row.
- **Shown** only when the account has both a schedule created and an end date. With no schedule it is not drawn (there is nothing to renew; Create schedule is the action) and the card is exactly as today.
- **Under the buttons**, in `text-ink-muted`, one sentence says why the button is as it is. The disabled button is `disabled` like the others on the page, so the sentence is ordinary text in reading order, and the button names it with `aria-describedby`. The existing Create schedule sentence stays below it ("The schedule exports Google Photos every month for 1 year, with only new and updated items after the first export." while the schedule is live, the longer one after it ended, which also says that Darkroom records a schedule Takeout already has), as in every board state that has a schedule. The owner changed this wording on Nov 9, 2026 when the schedule moved from every 2 months to monthly, with only new and updated items after the first export.
- **Enabled** when all hold: now is on or after three weeks before the schedule end (including after the end); the account does not need to sign in; no schedule or renew job is queued, running or waiting for you; no schedule or renew attempt is waiting for the Takeout answer; and no request from this page is in flight (the page's `busy`). These are the cases the route accepts, read from fields the page already loads.
- **Pressing it** posts to the route, then reloads the page data as Create schedule does. The job line "Renew schedule: waiting for the browser worker to pick it up." appears and the button turns disabled. A page-level status line says "Renewal queued for family@example.com." (`role="status"`). No confirmation dialog, as for Create schedule.

### States and copy

| State | Renew now | Sentence under the buttons |
| --- | --- | --- |
| Before the window | disabled | Darkroom asks for the renewal itself from 22 Dec 2026, three weeks before the schedule ends. Renew now is available from then. |
| Window open, nothing wrong | enabled | Renew now creates next year's schedule in Takeout without waiting for the daily check. Darkroom does not retry a renewal on its own. |
| Renew or schedule job open | disabled | A schedule or renew job is already open for this account. |
| Last renewal failed, inside the window | enabled | The last renewal failed. Darkroom does not retry a renewal on its own: press Renew now to try again. |
| Renewal not confirmed, unanswered | disabled | Answer the Takeout question above first. |
| Account needs to sign in | disabled | This account needs to sign in to Google again before its schedule can be renewed. Press Connect first. |
| Schedule ended | enabled, with Create schedule enabled too | This schedule ended on 24 Nov 2026, so scheduled exports have stopped. Renew now or Create schedule sets up a new yearly schedule in Takeout; both run the same steps. |
| No schedule yet, including a first Create schedule attempt still unanswered | not drawn | none; the card is as today, and its existing box asks the Takeout question |

Only an unanswered attempt on a schedule that exists (a renewal) draws the button disabled, in the "Renewal not confirmed" row; an unanswered first Create schedule has no schedule recorded, so the button is not drawn (board states 9 and 10).

The date in the first row is the end minus three weeks, written like every date on the page.

### Which sentence wins

Several rows can apply at once (before the window and needs sign-in; an open job and needs sign-in). The state function follows the route's own order, so the page never shows a reason the route would not give first: no schedule or end date (button not drawn), then before the window, then needs sign-in, then an unanswered attempt, then an open job; only when none applies is the button enabled, and then the sentence is "last renewal failed" if the latest renew job failed, "ended" if the end has passed, otherwise "window open". Checks for the build: before the window with sign-in needed shows the before-the-window sentence; sign-in needed with an open job shows the sign-in sentence; an unanswered attempt with an open job shows the Takeout-answer sentence; ended and failed together shows the failed sentence.

### When Darkroom refuses (409)

The route's own message is shown in the account card, under the sentence, as a danger-toned `role="alert"` line "Couldn't renew {email}: {message}." The message is Darkroom's, unchanged: "this schedule is not due for renewal until 27 Nov 2026, three weeks before it ends", "this account needs to sign in to Google again before its schedule can be renewed", "a schedule or renew job for this account is already waiting or running", "the last schedule attempt may have created an export; check Takeout and say so before renewing", "this account has no export schedule to renew", and "Darkroom is busy with another change; try again in a moment". They appear only when the page is out of date, so after one the page reloads in the background and the button and sentence update to match, while the refusal line stays on screen. It goes at the next press of any button in the card, or when the page is left or reloaded by the person. The line sits last in the card, below both sentences, as in board state 8. Any other failure (no network, a 5xx) shows the same line with the error's text. If the background reload itself fails, the page shows its existing "Couldn't load" state with its retry, which replaces the cards as it does today, and the refusal is not lost: the same text appears in the page's existing alert line at the top, "Couldn't renew {email}: {message}.", until the retry succeeds. The build's checks run this sequence (409, then a failed reload, then a retry).

### Alert wording

The two alerts that name the button say where it is. "Takeout schedule not renewed", before the end: "The export schedule for {account} was not renewed, and it ends on {end}. Darkroom does not retry a renewal on its own. Press Renew now on the Exports page to ask for another." After the end: "The export schedule for {account} was not renewed, and it ended on {end}, so scheduled exports have stopped. Darkroom does not retry a renewal on its own. Press Renew now on the Exports page to create a new schedule." They no longer mention Create schedule, so the alert gives one way out. "Takeout schedule ending" and "renewal not confirmed" keep their text. The change is in `darkroom/domain/alerts.py` and its tests, in the build pull request.

### Open points (approved as recommended, 2026-11-08)

1. **Where the button sits.** Recommended: last in the button row, secondary, as drawn. Alternative: primary while the renewal has failed, which draws the eye but gives the card two primary actions at times.
2. **Disabled with a reason, or hidden.** Recommended: disabled with the sentence above (the owner sees that it exists and when it opens); hidden only when there is no schedule. Alternative: hide it outside the window.
3. **After the end.** Recommended: both Create schedule and Renew now enabled, with the sentence above, because the worker runs the same steps for both (`renew` calls `create_schedule`). Alternative: Renew now only inside the window and Create schedule after it, as the runbook says today.
4. **Where a refusal shows.** Recommended: in the account card, naming the account, because the page can hold several. The page's existing banner at the top (used by Create schedule) would not say which account.
5. **No confirmation dialog.** Recommended, as for Create schedule: the server refuses a second attempt, and an unconfirmed outcome has its own question. Alternative: a dialog "Create next year's schedule in Takeout for {email}?".
6. **Where the window date comes from.** Recommended: the account reply gains `renew_from` (the end minus three weeks), so the page and the server share one rule and the date matches the server's message. Alternative: a constant of 21 days in the page, which can drift from `renewal.NOTICE`.
7. **Alert wording.** Recommended: the two texts above, naming only Renew now. Alternative: keep the mention of Create schedule after the end.
8. **Status line on success.** Recommended: "Renewal queued for {email}." as a page-level status. Alternative: none; the new job line is the only sign.

### Checks the build pull request carries

`frontend/src/pages/Exports.svelte.test.ts` for each row of the state table, the 409 line and the status line; `frontend/src/lib/view.test.ts` for the state function; `tests/test_alerts` text for the two alerts; `make verify` images at phone and desktop width beside these boards.

## Pairing, Syncthing status and the new alerts (#322, signed off)

Status: **signed off by the owner on 2026-11-16: all 13 open points approved as recommended.** Recorded on #322 (https://github.com/AnthonyPluth/Darkroom/issues/322#issuecomment-6924183929). For #322 (single container, step C6 of `docs/design/single-container.md`). It covers section 7.4 (pairing), section 7.5 (Syncthing down, settings not applied, version out of range, share check failed, a Pixel held by the placed-copy check, a Pixel not seen for long) and the wording of each new alert kind. The pairing screens are built in #323 on the routes of #321; the Status and People parts and the alerts in #324.

Boards: `03-people-pairing-desktop.dc.html` and `03-people-pairing-phone.dc.html` (the People page's Pixels card with its new column, then the Pixel form in each pairing state), `02-status-syncthing-desktop.dc.html` and `02-status-syncthing-phone.dc.html` (the new alerts in "Needs you", the person card's Pixel row, and the Syncthing card in each state). They use the signed-off tokens only (no new colour, size or font) and the app shell, chips, buttons, table and modal as built; everything not drawn is as in boards 02 and 03 and the built pages. Device IDs on the boards are made up.

Everything here applies to Darkroom's own Syncthing, which is always on in the one container. It was drawn while a `DARKROOM_SYNCTHING` switch could turn it off for a Syncthing configured by hand; #430 removed that switch and that layout, and with them the share check alert's wording for a Syncthing set up by hand. The web app never draws that layout: when the People page cannot read the pairing route, it shows the load failure card instead ("When Pixel pairing can't be read" below, the follow-up to decision 6 signed off 2026-11-18).

The app calls it **Syncthing** wherever a person reads it (the form already says "Syncthing device ID"); "transport" stays a word for the code and the design documents.

### Pairing, in the People page's Pixel form

The form stays in the existing modal (`Modal`, full width less 1 rem each side on a phone; at desktop width as wide as the content column since the full-width redraw below, where it was at most 32 rem). The device ID field leaves the details step and pairing becomes a second step of the same modal.

**Add Pixel.** The details step is the form as today without the "Syncthing device ID (optional)" field: Name, Google account, People served, then Cancel and Add Pixel. Add Pixel saves the Pixel (`POST /api/pixels`, no device ID), reloads the page data behind the modal, and the modal moves to the pairing step for that Pixel; its title becomes "Pair {name}". The Pixel exists from then on, so closing the modal at any point leaves an unpaired Pixel that can be paired later.

**Edit Pixel.** The details step as today, with the device ID field replaced by a read-only row: label "Syncthing", then the paired ID in mono (two lines of four groups) with a secondary button "Pair again", or, when the Pixel has no ID, the text "Not paired" and a secondary button "Pair". Either button moves to the pairing step. Pair again keeps the old ID until a new one is approved or saved (a reset phone, section 7.4).

**The pairing step**, top to bottom:

1. Modal description (in `text-ink-muted`, under the title): "Connect the Syncthing app on {name} to Darkroom. Nothing is shared until you approve the Pixel here."
2. A numbered heading "1. Scan Darkroom's code" and the sentence "On the Pixel, open the Syncthing app, add a device and scan this code."
3. The QR code of Darkroom's own device ID, nothing else encoded: dark modules in `canvas` on a tile of `ink`, with a quiet zone of four modules, 192 px square on desktop and 224 px on a phone, centred; `role="img"` with the label "QR code of Darkroom's device ID". Under it the label "Darkroom's device ID" and the ID as text, mono, selectable, split after the fourth group onto two lines. No copy button.
4. A numbered heading "2. Approve the Pixel", then the waiting list (below), which is a `role="status"` region.
5. At the end of the step, a text button "Type the device ID instead" (it opens the typed field below, under the waiting list; the rest of the step stays), and the footer: "Pair later" (secondary) on the left, nothing primary until a device is chosen.

**The waiting list** is read when the step opens and then every 5 seconds while the step is open and the tab is visible, with a secondary "Check now" button beside the heading. Polling stops once the step is closed or a device is approved. Each waiting device is a row inside a bordered box (`line`): the name the device presented, bold; under it its device ID, mono, on two lines like Darkroom's; under that the sentence "Check that this matches the device ID the Syncthing app shows for {name} itself."; and a primary button "This is {name}". The rows are in the order Syncthing lists them; nothing is preselected.

**The approval** is its own state, never a single press: "This is {name}" replaces the waiting list (polling paused) with a bordered box: heading "Approve this device for {name}?", the device's presented name and full ID again, and "Darkroom will share {name}'s folder with this device and with nothing else. Approve it only if the ID matches the one on the Pixel." Buttons: "Back" (secondary, returns to the list and resumes polling) and "Approve" (primary). Approve posts the approval with the explicit confirmation #321 requires; while it is in flight both buttons are disabled.

**The typed ID** (opened by "Type the device ID instead", and the only way when the waiting list can't be read): a `Field` labelled "Syncthing device ID", mono, with the hint "Eight groups of seven letters and digits, as the Syncthing app on the Pixel shows it.", and a primary "Save device ID". It saves through the same validation as today (#317's validator). On success it goes to the approved state below.

### Pairing states and copy

| State | What the step shows |
| --- | --- |
| Waiting, nobody yet | The list says "Waiting for {name} to connect. Darkroom checks again every few seconds." Check now is enabled |
| Waiting, one device | One row, as above |
| Waiting, several devices | The rows, with the sentence "More than one device is waiting. Approve only the one whose ID matches the Pixel." above them |
| Approving | The approval box, as above |
| Approved (or a typed ID saved) | The whole step is replaced by: an `ok` chip "Paired", then "Darkroom is sharing {name}'s folder with it." and a numbered list: "1. On the Pixel, accept the folder Darkroom shares and choose where it goes." "2. In Google Photos on the Pixel, turn on backup for that folder." Then "Darkroom shows the Pixel as connected once it has synced." One primary button, "Done", which closes the modal |
| Refused by Darkroom (any 4xx on approve or save) | The step stays, and a danger-toned `role="alert"` line under the approval box or the typed field reads "Couldn't pair {name}: {message}." with Darkroom's own message unchanged; the approval box returns to the list and the list is read again at once. The line stays until the next press in the step |
| The list can't be read (no network, 5xx) | Under the heading: "Couldn't check for waiting devices: {message}." Polling carries on; the line goes at the next successful read |
| Darkroom's Syncthing not answering, or not started | The QR code and the waiting list are not drawn. In their place: "Darkroom's Syncthing is not running, so the Pixel can't be paired by scanning now. Type the Pixel's device ID instead; it connects once Syncthing is running again." and the typed field, open |

The messages #321's routes are asked to give, so the line above reads as a sentence: "that device is no longer waiting; ask the Pixel to connect again", "that device is waiting for another Pixel's approval" (if #321 keeps one waiting device to one Pixel), "the approval was not confirmed", "not a Syncthing device ID", "a check character of the Syncthing device ID is wrong" (the validator's two texts, unchanged), "Darkroom's Syncthing is off" and "Darkroom is busy with another change; try again in a moment".

### The Pixels card on the People page

The Pixels table gains a column "Syncthing" between People served and Last sync: Device, Google account, People served, Syncthing, Last sync, and the actions. The cell holds a chip and, under it in `text-ink-muted`, one short line. On a phone the table turns into cards as today, with "Syncthing" as one more labelled line. An unpaired Pixel has a secondary "Pair" button before Edit in its actions cell; it opens the modal straight at the pairing step. Last sync keeps its wording ("6 minutes ago", "Not synced yet").

### Per-Pixel states

The same chip and line appear in the People page's Syncthing column, in the Syncthing card on Status, and (chip only, and only when it is not `ok`) after the Pixel's name in the person card's Pixel row. The first row that applies wins, in this order.

| # | State | Chip | Line |
| --- | --- | --- | --- |
| 1 | Darkroom's Syncthing is not running (any card state but Running) | neutral "Not syncing" | "Nothing syncs until Syncthing is running again." |
| 2 | Not paired (no device ID) | neutral "Not paired" | "Pair it to start sending photos." |
| 3 | Held, the check has not run since the start | neutral "Checking" | "Darkroom is checking its folder before sharing it." |
| 4 | Held, copies can't be written again | warn "Folder held" | "{n} photos could not be written again, so its folder is not shared." |
| 5 | Held, unaccounted files | warn "Folder held" | "Its folder has {n} files Darkroom did not put there, so it is not shared." |
| 6 | Paired, never seen | neutral "Waiting for the Pixel" | "Paired; the Pixel has not connected yet." |
| 7 | Not seen for 3 days or more | warn "Not seen for {d} days" | "Darkroom reaches the Pixel only on the home network. Check it is on, on the Wi-Fi, with Syncthing running." |
| 8 | Connected, sending | ok "Sending" | "{p}% sent" |
| 9 | Connected, up to date | ok "Connected" | "Up to date" |
| 10 | Seen within 3 days, not connected now | neutral "Not connected" | "Last seen {ago}." |

Rows 4 and 5 together keep the chip and show both lines. Counts are numbers only; no row ever names a file, a path or a device ID. Row 7 is shown and never alerted (section 7.5): the "originals unmatched" alert still covers photos not reaching Google. The percentage is Syncthing's reported completion, display only, never an input to pruning.

### The Syncthing card on Status

A card titled "Syncthing", after the People section and before Recent events. Its first line is the title and a chip for Darkroom's Syncthing as a whole, then one sentence; under that, one row per Pixel (name, its chip and line from the table above), or "No Pixel is paired yet." with a link "Open People". The person card's Pixel row becomes "{Pixel}, synced {ago}" (as board 02 already shows) followed by the per-Pixel chip when it is not `ok`, and "{Pixel}, not synced yet" before the first sync. At desktop width the Syncthing card, the alerts and each person card span the full content column, one person card per row ("Full-width sections" below; signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/322#issuecomment-6926954882): every card, section and dialog spans the full content column at desktop 1280, phone unchanged; person cards one per row).

| Card state | Chip | Sentence |
| --- | --- | --- |
| Starting (the first reconcile after a start has not finished) | neutral "Starting" | "Darkroom is checking Syncthing's settings before anything syncs." |
| Running | ok "Running" | "Syncthing is running with the settings Darkroom set." |
| Not answering | bad "Down" | "Syncthing has not answered for {duration} (for example "2 hours"). Darkroom keeps restarting it. Placing and pruning carry on, and the Pixels catch up once it is back." |
| Settings not applied | bad "Paused" | "Darkroom could not apply its settings to Syncthing ({reason}), so nothing is syncing. It tries again before the next hourly run." |
| Version out of range | bad "Not started" | "{reason}, so Darkroom has not started Syncthing." with the existing text, for example "Syncthing reported v2.2.0, outside the tested range v2.1.7, so Darkroom has not started Syncthing." |
| Key not handed over or refused at start | bad "Not started" | "{reason}, so Darkroom has not started Syncthing. Restart the container." with the existing texts of `syncthing_version.py` |
| Share check failed | bad "Paused" | "The photo share failed Darkroom's check ({reason}), so placing, pruning and syncing are paused, and nothing on the share is changed." with the existing reasons of `share_check.py` |

Share check failed wins over every other state, then version out of range, key refused, settings not applied and not answering, in that order. Each `{reason}` is one of Darkroom's fixed texts (C3 and C7 add the reconciler's, such as "the folder settings did not read back as written"), never a path, a device ID or a file name.

### Alert wording

Each new alert carries only its kind and an id (the Pixel's id for `pixel_held`, none for the others); the text below is built from fixed words, counts and the Pixel's Darkroom name. No alert names a device ID, a path or a file name. The email sends only the title and the link, as every alert does today. Each opens once and closes when its state has recovered, without closing a newer warning. None adds a button on Status; the chip is shown with the existing alert card. Links go to `/` except `pixel_held`, which goes to `/people`.

| Kind | Title (email and `TITLES`) | Chip | Message |
| --- | --- | --- | --- |
| `syncthing_down` (not answering for 1 hour, or the key not handed over or refused at start) | Syncthing not answering | bad "Syncthing down" | "Darkroom's Syncthing has not answered for over an hour, so nothing is reaching the Pixels. Darkroom keeps restarting it, and placing and pruning carry on; the Pixels catch up once it is back. If it stays down, restart the container." |
| `syncthing_settings_failed` | Syncthing settings not applied | bad "Settings not applied" | "Darkroom could not apply its settings to Syncthing or read them back, so it has resumed nothing and the Pixels are not syncing. It tries again before the next hourly run. The Status page gives the reason." |
| `syncthing_version` | Syncthing version not tested | bad "Untested Syncthing" | "The Syncthing in this container is not a version Darkroom was tested with, so Darkroom has not started it and nothing reaches the Pixels. Run the published Darkroom image, which carries a tested Syncthing." |
| `share_check_failed` | Photo share check failed | bad "Share check failed" | "The photo share did not pass Darkroom's check, so placing, pruning and syncing to the Pixels are paused, and nothing on the share is changed. If the share is not mounted, fix the mapping and restart the container. If it really was replaced, bind the new one as the runbook says. The Status page gives the reason." |
| `pixel_held`, copies can't be written again | Pixel folder held | warn "Folder held" | "{n} photos could not be written to {pixel}'s folder again, so Darkroom is not sharing the folder with it. Nothing is deleted from the Pixel. Something else is in their place, or Darkroom can't read it; the runbook says what to look for." |
| `pixel_held`, unaccounted files | Pixel folder held | warn "Folder held" | "{pixel}'s folder has {n} files Darkroom did not put there, so Darkroom is not sharing the folder with it. Darkroom never removes or moves them: remove them, or move them out of the folder, by hand." |

A Pixel held both ways has one alert whose message is the first text followed by "It also has {n} files Darkroom did not put there." Counts take the singular at one ("1 photo", "1 file"). The share check alert opens on the first failed check; `syncthing_down` opens after the set time below, so a restart that recovers within it never alerts.

### The owner's decisions (answered on PR #345, all as recommended)

The owner answered the thirteen open points on pull request #345 and accepted the recommended option for each. These settle the choices below; the owner then signed off the design as a whole on #322 on 2026-11-16.

1. **Pairing is a second step of the Pixel form.** Add Pixel saves the details, then the same modal moves to "Pair {name}", because an approval is for a given Pixel (#321). Not chosen: one long form saved at the end.
2. **The approval is its own step.** "This is {name}", then a separate "Approve this device for {name}?" box with Approve, so nothing is paired on one press. Not chosen: a "The device IDs match" checkbox beside one Approve button.
3. **The waiting list refreshes every 5 seconds** while the step is open, plus Check now. Not chosen: Check now only.
4. **The QR code is `canvas` modules on an `ink` tile**, existing tokens only. Not chosen: light modules on the dark surface.
5. **People read "Syncthing"** everywhere in the app. Not chosen: "Pixel transport".
6. **With `DARKROOM_SYNCTHING` off the pages are exactly as today** (the typed field in the form, no Syncthing column or card); only the share check alert is new. Not chosen: the column and card shown as "Set up by hand". Since #430 the switch is gone and Darkroom's Syncthing is always on, so the server no longer answers in that layout. **Superseded by the follow-up signed off by the owner on 2026-11-18:** the web app no longer has that layout either; a pairing route it cannot read is a load failure ("When Pixel pairing can't be read" below).
7. **Syncthing has its own card on Status**, after People and before Recent events, plus the chip after a Pixel's name in the person card when it is not `ok`. Not chosen: a line in the header sentence.
8. **People has a "Syncthing" column** between People served and Last sync, with a Pair button for an unpaired Pixel. Not chosen: the state only inside the Edit form.
9. **"Not seen for long" is 3 days**, shown in warn, never alerted. Not chosen: 7 days.
10. **`syncthing_down` alerts after 1 hour** without an answer. Not chosen: 15 minutes.
11. **`pixel_held` names the Pixel by its Darkroom name**; the email carries only the title. Not chosen: "One of your Pixels".
12. **No button on the new alerts.** Not chosen: "Open People" on `pixel_held`.
13. **The kinds are `syncthing_down`, `syncthing_settings_failed`, `syncthing_version`, `share_check_failed` and `pixel_held`**, with a key refused at start folded into `syncthing_down`. Not chosen: a separate `syncthing_key_refused`.

### What the owner reviews, and what the build pull requests carry

The review artefact for this sign-off is the four boards above, read in a browser at 390 and 1280 wide (as for the Renew now boards), and the tables in this section; the README is what is signed off when a board and a table disagree. No screenshot can exist yet: `make verify` drives the built app on demo data, and none of these screens is built. No images are committed here.

The build pull requests then carry, beside these boards, the `make verify` images at phone (440x956) and desktop (1280x800): #323 the People page (`docs/screenshots/people-phone.png` and `people-desktop.png`, with an unpaired Pixel and the new column in the demo seed) and a new flow `frontend/verify/flows/pixel-pairing.json` that opens Pair on the demo's unpaired Pixel and screenshots the waiting (nobody, then one device from a fake waiting list), approving, approved and refused states; #324 the Status page (`status-phone.png` and `status-desktop.png`, with the demo seed holding one held Pixel, one not seen for 4 days and one connected) and a flow `frontend/verify/flows/status-syncthing.json` for the Down and Share check failed card states with their alerts. The demo seed and the fake Syncthing those flows need are part of #323 and #324.

### When Pixel pairing can't be read (follow-up to decision 6, signed off)

Status: **signed off by the owner on 2026-11-18, as a follow-up to #322 decision 6** (given by the owner in a Claude Code session on 2026-11-18), after #430 (PR #455) removed the `DARKROOM_SYNCTHING` switch. No board changes, and none is re-exported: the state reuses the People page's existing load failure card as built (`frontend/src/components/LoadFailed.svelte`), with no new token, colour or layout.

- **When.** The People page reads `GET /api/pairing/device` with the people, Google accounts and Pixels. Success, or 503 (Darkroom's Syncthing not running), means pairing is on, and the page is drawn as signed off above: the Syncthing column, Pair, Pair again, Unpair and the pairing step. Any other answer (a 500, a 401, a 409, no answer at all) means the page cannot be drawn correctly.
- **What shows.** In place of the page's content, under the page heading, the load failure card the page already shows when the people, accounts or Pixels can't be loaded: `role="alert"`, the danger border, the message, and a "Try again" button that loads the whole page again. The message is "Couldn't load Pixel pairing" with the status, as the page's other load failures carry theirs ("Couldn't load Pixel pairing (500)"); as for every other load, Darkroom's own message is shown when the answer carries one, and the app's own texts when Darkroom can't be reached or the session has ended.
- **What never shows.** The old off layout: the typed "Syncthing device ID (optional)" field in Add or Edit Pixel, a Pixels table with no Syncthing column, and a Pixel with no Pair or Unpair. Nothing of the page's tables or forms is drawn beside the card.
- **Status.** `GET /api/status` always carries the Syncthing card since #430, so the Status page always draws it and the person card's Pixel row always reads "{Pixel}, synced {ago}" (or "not synced yet") with the chip; the plain list of Pixel names it drew with the switch off is gone.
- **The setup wizard** is unchanged: its Pixels step never asks the pairing route and keeps the typed device ID field and four plain columns, as signed off in `08-setup.md` (Step 6 and its board note 2).

## Three per-Pixel states #322 did not draw (addendum, signed off)

Status: **signed off by the owner on 2026-11-16: all 9 decisions approved as recommended.** Recorded on #322 (https://github.com/AnthonyPluth/Darkroom/issues/322#issuecomment-6926460427). It adds to the section above and changes none of its signed-off rows, cards or alerts. The build of #324 (PR #356) found three states the reconciler records for a Pixel that have no row in "Per-Pixel states", so today they fall through to "Waiting for the Pixel":

- **(a) The device ID is not valid.** The Pixel has a device ID, but not one Syncthing accepts; the reconciler records `device_id_invalid` and gives it no device and no folder. Since the pairing API (#350) a new or changed ID must pass #317's validator, so this is a value saved before that (typed by hand, any 1 to 80 letters, digits and dashes), kept as it was.
- **(b) The device ID is shared with another Pixel.** Two or more Pixels have the same ID (compared in canonical form); the reconciler records `device_id_shared` and gives none of them a folder. #350 refuses an ID another Pixel already has, so this too comes from values saved before it.
- **(c) The folder was removed.** The reconciler found the Pixel's folder in Darkroom's Syncthing not Send Only, or not at the Pixel's folder, removed it (event `syncthing_folder_removed`) and records `folder_removed`. It never shares the folder again by itself. #350 adds the release: `POST /api/pixels/{id}/release-folder` with `{"confirmed": true}` records `syncthing_folder_released`, and the reconciler's next run (within 60 s, as the plan's fingerprint changes) adds the folder afresh, Send Only at the Pixel's folder, once the share check passes and the Pixel is not held. It answers 409 when nothing was removed or the transport is off, and it needs a signed-in session from Darkroom's origin like every other route (Darkroom has no separate owner role).

Board: `03-people-pairing-states-desktop.dc.html` and `03-people-pairing-states-phone.dc.html` (the Pixels card with the three states, the confirmation to share a folder again in each of its states, the row just after, the person card's Pixel row and the Syncthing card; row 2d is drawn there too since the full-width redraw below). Existing tokens, chips, buttons, table and modal only. Names are made up.

### Where they sit

Four rows go between row 2 (Not paired) and row 3 (Checking): 2a to 2c, and row 2d for a fourth code (decision 9). The first row that applies still wins; rows 1 to 10 are as signed off.

| # | State | Chip | Line |
| --- | --- | --- | --- |
| 1 | Darkroom's Syncthing is not running | (as signed off) | |
| 2 | Not paired | (as signed off) | |
| 2a | (a) The device ID is not valid | warn "Device ID not valid" | "Its saved device ID is not a Syncthing device ID, so nothing is shared with it. Pair it again." |
| 2b | (b) The device ID is shared | warn "Same device ID" | "{others} {has/have} the same device ID, so Darkroom shares neither folder. Pair again whichever one is wrong." |
| 2c | (c) The folder was removed | warn "Folder removed" | "Darkroom removed its folder because it was changed outside Darkroom. It stays unshared until you share it again." |
| 2d | (d) The folder was refused (`folder_path_refused`) | warn "Folder refused" | "Its folder leads outside the photo share, so Darkroom does not share it." |
| 3 to 10 | | (as signed off) | |

- **Why there.** Each of the four means the Pixel has no folder at all, so the rows below (held, waiting, connected) say nothing true about it; and Not paired and Not syncing still win, because neither can be helped from the Pixel's row. (a) and (b) come before (c) because a folder can't be shared again until the ID is usable: sharing it again for a Pixel whose ID is wrong would only lead back to (a) or (b).
- **`{others}`** is the other Pixels' Darkroom names, in the People page's order, joined "A", "A and B", "A, B and C", with "has" for one and "have" for more: "Casey's Pixel has the same device ID, so Darkroom shares neither folder." Each Pixel that shares the ID shows the row, naming the others. No row names the ID itself.
- **Warn, not bad**, like Folder held: the Pixel is not syncing, but nothing is lost and nothing is at risk while it waits.

### On the People page

The Syncthing column shows the chip and line above. The actions cell gains one secondary button before Edit, as an unpaired Pixel has Pair:

| State | Button | What it does |
| --- | --- | --- |
| 2a, 2b | "Pair again" (label for screen readers: "Pair again, {name}"; see the note below the table) | Opens the modal at the pairing step, as Edit Pixel's Pair again does (#322): the old ID stays until a new one is approved or saved. Until #323 lands, as for Pair, it opens the existing Edit Pixel form |
| 2c | "Share again" (label: "Share again, {name}'s folder"; see the note below the table) | Opens the confirmation below |
| 2d | none | Nothing on the row can help: the folder's place is set on the Pixel, not by pairing or sharing again |

The screen-reader labels were "Pair {name} again" and "Share {name}'s folder again" until the owner signed off the current ones on 2026-11-19 (given by the owner in a Claude Code session on 2026-11-19): an accessible name must contain the button's visible text (WCAG 2.5.3, label in name, which `make verify`'s accessibility audit checks), so a voice-control user can press the button by saying what it shows. Nothing visible changes.

The Edit Pixel form is unchanged: its Syncthing row shows the saved value in mono with Pair again, whatever the state. The pairing step does not offer sharing again: pairing is about the device, and in (c) the device is fine.

**The confirmation** is the existing modal:

- Title: "Share {name}'s folder again?"
- "Darkroom removed {name}'s folder from its Syncthing because the folder was changed outside Darkroom: it was no longer Send Only, or no longer at {name}'s folder on the photo share."
- "If you share it again, Darkroom adds the folder afresh, Send Only, at {name}'s folder, as soon as the photo share passes its check and the folder is not held. If the Syncthing app on {name} asks, accept the folder there."
- "If the folder is changed outside Darkroom again, Darkroom removes it again."
- Buttons: "Cancel" (secondary) and "Share folder again" (primary). The press posts the release with `confirmed: true`; while it is in flight both buttons are disabled.
- Refused (any 4xx, or no answer): the modal stays, and a danger-toned `role="alert"` line under the text reads "Couldn't share {name}'s folder again: {message}." with Darkroom's message unchanged, as the pairing step's refusals do. The line stays until the next press.
- Done: the modal closes, the page data reloads, and the Pixel shows row 3 (neutral "Checking", "Darkroom is checking its folder before sharing it.") until the reconciler's next run has recorded its state, then whatever row applies.

### On Status

The Syncthing card's Pixel row shows the same chip and line, with no button (the card has none; the actions stay on People, as for Pair). The card's own chip and sentence are unchanged: these are states of one Pixel, not of Syncthing. A Pixel in (a) or (b) has a device ID, so it counts as paired and is listed; "No Pixel is paired yet." is unchanged. The person card's Pixel row shows the chip after the Pixel's name, as for any state that is not `ok`.

### No new alert

None of the four raises an alert, and no alert kind is added. (a) and (b) can only come from IDs saved before #350, are fixed in the People page and are on every Pixel row that shows them; (c) follows a change someone made in Darkroom's Syncthing, and the reconciler records it as an event. As with "Not seen for 3 days", the existing "originals unmatched" alert covers photos that don't reach Google, which is what a Pixel left like this ends in.

### For the build

- The reconciler records one code per Pixel, and today `folder_removed` wins over `device_id_invalid` and `device_id_shared`. The build changes that order so the ID's problem is recorded first (`device_id_invalid` and `device_id_shared` ahead of `folder_removed`); the removal stays on record and shows once the ID is fixed.
- #350's "nothing to release" message (the 409 when nothing was removed) says "share again", as the button does.
- Row 2d (`folder_path_refused`) is built with the other three: chip, line, no button, no alert. It is described here in full, and the full-width redraw below draws it on the addendum boards (Alex's Pixel, after 2c, in the Pixels card and the Syncthing card).
- The other Pixels' names for (b) are worked out on the server from the stored IDs in canonical form, as the reconciler compares them; the reply carries names only, never an ID.
- Right after a release, the Pixel shows Checking because the release is on record, before the reconciler's next run rewrites its code.
- The demo seed gains one Pixel in each of the three states (and, for (b), its pair), and the People and Status images and a flow that presses Share again, then Share folder again, carry them, as #356's do.

### The owner's decisions (signed off, all as recommended)

The owner approved all nine as recommended on 2026-11-16, recorded on #322 (https://github.com/AnthonyPluth/Darkroom/issues/322#issuecomment-6926460427). Each is kept as decided, with the option not chosen.

1. **Rows 2a, 2b and 2c sit after Not paired and before Checking**, the ID's problems before the removed folder. Not chosen: after the Folder held rows (rows 4 and 5).
2. **All three are warn.** Not chosen: bad for (b) and (c).
3. **(a) and (b) get a "Pair again" button in the People row.** Not chosen: only through Edit Pixel.
4. **Sharing a removed folder again is a "Share again" button in the People row with its own confirmation**, not in the pairing step and not on Status. Not chosen: a button in the Edit Pixel form's Syncthing row.
5. **No new alert kind.** Not chosen: a `pixel_folder_removed` alert, warn "Folder removed", linking to `/people`.
6. **(b) names the other Pixels by their Darkroom names.** Not chosen: "Another Pixel has the same device ID".
7. **After sharing again the Pixel shows Checking** until the reconciler's next run. Not chosen: keeping "Folder removed" until then.
8. **Any signed-in member may share a folder again**, after the confirmation, as #350 built it. Not chosen: an owner role, which Darkroom does not have yet.
9. **A fourth code found while reading the reconciler, `folder_path_refused`** (the Pixel's folder resolves outside the photo share, so no folder is added), also fell through. Decided: a row 2d, warn "Folder refused", "Its folder leads outside the photo share, so Darkroom does not share it.", with no button and no alert, drawn in a later board. Not chosen: leaving it to fall through.

## Full-width sections on the #322 and addendum boards (signed off)

Status: **signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/322#issuecomment-6926954882): every card, section and dialog spans the full content column at desktop 1280, phone unchanged; person cards one per row.** (The owner's chat answers, recorded on #322: "Make modals full-width too", "One per row", "Sign off".) The owner asked for every section of the pairing, Syncthing status and People designs to be full width, not just some of them. The boards below are the signed-off layout.

The rule: at desktop width (1280, with the same 232 px left rail) every card and section on these boards spans the full content column, the space right of the rail less the page's 40 px padding each side (968 px at 1280), and none is narrower. That is the column boards 02 and 03 already draw and the built shell already has (`Shell.svelte`'s `main`: `flex-1`, `md:px-10`, no maximum width). The phone layout (390) is unchanged. Modal dialogs span the column too (the owner's decision, superseding an earlier "keep them 512 px"): at desktop width each dialog (the Pixel form, the pairing steps, the Share again confirmation) is as wide as the content column, 968 px at 1280, where it was at most 32 rem (512 px). It stays a dialog over the dimmed page, and on a phone it stays full width less 1 rem each side. The QR code keeps its fixed 192 px.

What changed, per board:

| Board | Change |
| --- | --- |
| `02-status-syncthing-desktop.dc.html` | The content column's 1040 px cap is gone (column 960 to 968 px, as boards 02 and 03). The person card loses its 520 px cap and spans the column, one card per row. The alerts and the Syncthing card already spanned it. Sub-heading and the person card's note updated. |
| `03-people-pairing-desktop.dc.html` | The content column's 1040 px cap is gone. The Pixels card already spanned it. Each of the 11 dialogs (Add Pixel, Edit Pixel, the 9 pairing steps) loses its 512 px cap and spans the column. Sub-heading updated; the board is 7000 px tall (was 6400, with the content overflowing to 7377). |
| `03-people-pairing-states-desktop.dc.html` | The content column's 1040 px cap is gone. The person card loses its 520 px cap and spans the column. Row 2d added (Alex's Pixel, warn "Folder refused", its line, Edit only) after 2c in the Pixels card and in the Syncthing card. The three Share again dialogs lose their 512 px cap and span the column. The refused example's message ends "so there is nothing to share again." (was "nothing to release"), as "For the build" says. Title, sub-heading and the Pixels card's note updated; the board stays 3300 px tall. |
| `03-people-pairing-states-phone.dc.html` | Layout unchanged. Row 2d and the refused example's wording as on desktop; title, sub-heading and note updated; the board is 400 px taller. |
| `02-status-syncthing-phone.dc.html`, `03-people-pairing-phone.dc.html` | Layout unchanged. Only the sub-heading: "Draft for #322, not signed off" becomes "Signed off on #322 2026-11-16". |

What the build changes:

- **#324 (PR #356, Syncthing on Status and People):** on Status, the People section's cards go from two columns at `md` (`grid gap-4 md:grid-cols-2` in `frontend/src/pages/Status.svelte`) to one column, each card the full width; this replaces board 02's two-column person grid on the built page. Row 2d is built as "For the build" above says. The Syncthing card, the alerts, Recent events and the People page's cards already span the column; nothing else moves. Its `status-desktop.png` shows the cards one per row.
- **`Modal` (`frontend/src/components/Modal.svelte`):** gets a full-width variant (`wide`) without its `max-w-lg`: from `md` up the dialog sits over the content column, right of the rail and inset by the page's `md:px-10`, as wide as the column; on a phone it is as today. Only the People page opens the full-width variant (its `FamilyEditor` passes `wide` to the one `Modal` for the person, Google account and Pixel forms, and through `Confirm` to every confirmation), so the change reaches no other page: the setup wizard renders the same `FamilyEditor` without `wide`, and its dialogs keep the 512 px they were signed off at; a page that later wants the full-width dialog goes through #16.
- **#323 (PR #355, the pairing screens):** a layout change: the Pixel form and every pairing step open in the full-width `Modal`, and their contents (fields, the QR code centred, the waiting list, the buttons) lay out across the wider dialog as the board draws. The People page's cards already span the column.
- **The addendum's Share again confirmation** (built with #324's Pixel rows) uses the full-width `Modal` too.
- None of this touches the shell or any other page; the whole app's page layout stays with #16.

## Unpairing a Pixel (addendum to #322, signed off)

Status: **signed off by the owner on 2026-11-16: all 12 drawing choices as recommended (https://github.com/AnthonyPluth/Darkroom/issues/322#issuecomment-6929613909); phone confirmation buttons side by side as the built Confirm draws them (owner's chat decision, 2026-11-16).** It adds to "Pairing, Syncthing status and the new alerts" and changes none of its signed-off rows, cards or alerts.

Why: with Darkroom's Syncthing on, the Pixel form shows the paired ID read-only with only "Pair again" (or "Pair"), so there is no way in the app to unpair. A Pixel that is lost, sold or replaced keeps its Syncthing device and its Send Only share of the photos until someone clears the ID with `PATCH /api/pixels/{id} {"syncthing_device_id": null}` by hand. The second review flagged this as access that outlives the device. The backend already does the rest: once the ID is cleared, the reconciler removes the device and the Pixel's folder from Darkroom's Syncthing within 60 s, and deletes no file (`tests/test_transport.py`, "a Pixel that is deleted or unpaired loses its folder and device and no file is deleted"). So this is a UI action on an existing route; no new route.

Boards: `03-people-unpair-desktop.dc.html` and `03-people-unpair-phone.dc.html`, six states stacked: Edit Pixel paired with Unpair, the confirmation, in flight, refused, then the Pixels card and Edit Pixel afterwards. They follow the full-width rule above: at 1280 every card and dialog spans the content column (968 px); the phone layout is as on the other phone boards. Existing tokens, chips, buttons, table and modal only. Names and the device ID are made up.

### The action

- **Where.** In Edit Pixel's read-only Syncthing row, after "Pair again": the ID in mono, then "Pair again" and "Unpair" side by side (on a phone, stacked full width, Pair again first). Shown whenever the Pixel has a saved device ID, whatever its per-Pixel state (including 2a and 2b, where the saved ID is the problem). Not shown when the Pixel is not paired. (It was also not shown with `DARKROOM_SYNCTHING` off, where the form kept the typed field; since #430 and the follow-up to decision 6 signed off 2026-11-18 there is no such form on the People page: when the pairing route can't be read the page shows its load failure card, "When Pixel pairing can't be read".)
- **Label.** "Unpair", with the accessible name "Unpair {name}".
- **Style.** The app's danger-outline secondary button (`btn btn-danger`), as Delete Pixel in the same form: it takes access away, so it is not a plain `btn`, and never primary. The form's own Save stays the only primary.
- **What it does.** It never applies on Save. Pressing it opens the confirmation the way Delete Pixel does today: the form closes (unsaved edits in it are dropped, as for Delete Pixel) and the existing `Confirm` dialog opens.

### The confirmation

The existing `Confirm` dialog, full width at desktop like every dialog since the full-width redraw.

- Title: "Unpair {name}?"
- Description (muted, `Confirm`'s message): "Darkroom stops sharing {name}'s folder with this device and removes the device from its Syncthing within a minute. Nothing new reaches {name} until it is paired again."
- Then: "No photo or file is deleted. Darkroom's copies stay on the photo share, and Darkroom does not touch the copies already synced to {name}: they stay on the Pixel until someone removes them there."
- Buttons, right-aligned as in `Confirm`: "Cancel" (`btn`) and "Unpair" (`btn btn-danger`, as `Confirm`'s action is for every delete). On a phone they stay side by side as the built `Confirm` lays them out, Cancel left and Unpair right: right-aligned, natural width, 12 px apart (`flex justify-end gap-3`), not stacked and not equal width.
- **Unpair** sends `PATCH /api/pixels/{id}` with only `{"syncthing_device_id": null}` (no other field), so nothing else in the form is saved by it.
- **In flight:** both buttons disabled and the dialog can't be dismissed (`Confirm`'s `busy`).
- **Refused** (any 4xx or 5xx, or no answer): the dialog stays, and the danger-toned `role="alert"` line (`FormError`) above the buttons reads "Couldn't unpair {name}: {message}." with Darkroom's message unchanged, for example "Darkroom is busy with another change; try again in a moment" or "no such Pixel". The line goes at the next press, as `Confirm` does today.
- **Done:** the dialog closes and the page data reloads.

### Afterwards

- The Pixel has no ID, so it shows row 2 at once: neutral "Not paired", "Pair it to start sending photos.", and the People row gets its "Pair" button before Edit, as an unpaired Pixel has (#322). It does not show a "removing" state for the minute the reconciler takes.
- Reopened, Edit Pixel's Syncthing row reads "Not paired" with "Pair"; no Unpair.
- On Status the Pixel is no longer listed as paired in the Syncthing card; with no other paired Pixel the card reads "No Pixel is paired yet." as signed off. No alert opens or closes because of it, and no alert kind is added.
- Pairing it again is the ordinary pairing step; nothing remembers the old ID.

### Drawing choices and open points (for the owner)

Each is drawn as recommended; the alternative is not drawn.

1. **Placement: in Edit Pixel's Syncthing row, after Pair again.** Recommended, because that row is where the device ID is shown and changed. Alternative: an "Unpair" button in the People row's actions beside Edit (one click closer, but a destructive action in a table row next to Edit, and a row already carries Pair again or Share again in some states).
2. **Danger-outline secondary (`btn-danger`), not plain secondary.** Recommended: it removes a device's access, like Delete Pixel. Alternative: plain `btn`, keeping danger styling for deletes only (nothing is deleted).
3. **A confirmation dialog, through the existing `Confirm`.** Recommended: unpairing a phone in use stops its backups until someone re-pairs it at the phone, which is not a one-click undo. Alternative: no dialog, with the "Not paired" row as the only sign.
4. **The form closes when the confirmation opens** (as Delete Pixel does), and unsaved edits in it are dropped. Alternative: the confirmation over the form and the form back afterwards, which needs a second dialog layer the app does not have.
5. **The PATCH carries only the device ID**, never the rest of the form. Alternative: clear the field in the form and apply it on Save (one save, but an unpair would wait on, and fail with, unrelated edits).
6. **Shown for any saved ID, including 2a (not valid) and 2b (shared).** Recommended: in 2b, unpairing the wrong Pixel is a direct fix. Alternative: only for a Pixel whose ID is valid and unique.
7. **The dialog does not show the device ID.** The form just showed it; the dialog names the Pixel. Alternative: repeat the ID in mono in the dialog.
8. **"Within a minute" is the only timing promise**, and the row shows Not paired at once, with no "removing" state. When Darkroom's Syncthing is not running (the card is not Running), the device is removed at the reconciler's first run after it starts, before anything syncs, so "within a minute" is not true then. Open: keep one text (recommended, it is the usual case), or swap the first sentence in that case for "Darkroom's Syncthing is not running now. Darkroom removes the device as soon as it is running again, before anything syncs."
9. **Already-synced copies on the Pixel are out of Darkroom's reach**, said plainly ("they stay on the Pixel until someone removes them there"). For a lost or sold phone this is the honest limit. Alternative: say nothing about the Pixel's copies, or add advice ("Reset a lost or sold Pixel from Google's Find My Device") that Darkroom can't check.
10. **Darkroom's own copies after unpairing.** The Pixel still exists and still serves its people, so copies for it keep being queued and placed in `_pixels/<pixel>/` and pruned as today; only the sharing stops. This design draws nothing about it. Open: keep it so (recommended; pairing again picks up where it left off), or stop queueing for an unpaired Pixel, which is a backend change outside this design.
11. **No alert, no event wording on the page.** The PATCH records `pixel_updated` as today; the reconciler's removal is in its own records. Alternative: a dedicated `pixel_unpaired` event shown in Recent events.
12. **No Unpair on Status**, as Pair and Share again live only on People (decision 4 of the addendum).

### For the build

- `frontend/src/forms/PixelForm.svelte` (as built by #323): the Unpair button in the Syncthing row when `syncthing_device_id` is set and Darkroom's Syncthing is on; an `onunpair` callback like `ondelete`.
- `frontend/src/pages/People.svelte`: an `unpairPixel` through `ask()` with the copy above. `Confirm` takes one `message`; the build either passes the second paragraph as a second line of text or lets `Confirm` take a body, without changing how other confirmations look.
- Checks: `PixelForm.svelte.test.ts` (Unpair shown only with an ID and the Syncthing on; it opens the confirmation and saves nothing), a People page test for the PATCH body (`{"syncthing_device_id": null}` only), the refused line, and the row turning to Not paired after reload; `make verify` images at phone and desktop width beside these boards, from a flow that opens Edit on a paired demo Pixel, presses Unpair, then Unpair in the dialog.

## First-run setup wizard (#303, document signed off)

Status: **document signed off by the owner on 2026-11-16 (#344, recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926281385); the boards signed off by the owner on 2026-11-16 (visual sign-off recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926640234).** #303 closes, and #305 builds against the boards, once #344 merges (decision 1 of `08-setup.md`). Every decision the owner made on #344 (1 to 18) is recorded in `08-setup.md`, "The owner's decisions", unchanged.

Boards: `08-setup-{welcome,closed,address,sign-in,test-failed,immich,people,accounts,pixels,alerts,done}-{desktop,phone}.dc.html`, the eleven pairs the document's board list names, each stacking the states that list names for it, in its order. They use the signed-off tokens only (no new colour, size or font) and the parts already built (card, buttons, fields, the danger alert line, chips, and the People page's table and modal on the People, Google accounts and Pixels steps). The copy is the document's, word for word; `{placeholders}` are filled with made-up values (addresses on 192.168.1.20 and example.com, made-up client and device IDs). `08-setup-test-failed-*` draws the server's own pages as `darkroom/server/common.py` renders them today, not the app's style. They were previewed in a browser at 390 and 1280 wide; no images are committed, as above.

## Checking Immich's address in the setup wizard (#347, signed off)

Status: **signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469): the 304 px left pane with 40 px padding on all 11 desktop wizard boards, the card filling the column as built in #363, the Immich check states, refusal status 422, an Immich save before the passing test refused with 409 and contacting nothing, choices B to H as recommended; phone boards, the test-failed desktop board and the add-person dialog unchanged.** The signed-off Immich step (boards `08-setup-immich-*`) claimed no check; the check now stands as drawn. #347 asks for the check (`POST /api/setup/immich` asks Immich before storing the address) and for its copy and states to be drawn and signed off on #303 first. The wizard API it builds on (#346) is merged (PR #358).

Boards: `08-setup-immich-check-desktop.dc.html` and `08-setup-immich-check-phone.dc.html`, four states: Checking Immich (in flight), then the three refusals (no answer, not Immich, TLS failed). They add to `08-setup-immich-*` and change none of its five states. The only change there is State 4's note, which said "The screen claims no check of Immich (#347)" and now points to the new board. Signed-off tokens and parts only (the card, the field, the primary button disabled as Welcome's "Starting...", the danger alert line). Addresses are made up. The copy, and the amended lines of `08-setup.md` ("Step 3 of 8: Immich", the error table, the API's messages, "What reaches beyond Darkroom" and an addendum to decision 8), are signed off there too.

### What it does

- Save and continue on the Immich step asks Immich's unauthenticated `GET /api/server/ping` at the address typed, through `darkroom/tls.py`, with a short time limit, sending no key, cookie or Darkroom header and following no redirect. It stores the address only on Immich's ping reply (`{"res": "pong"}`).
- ~~Only after the passing test. The wizard shows the step only then, but the route accepts the claim alone before it (today it stores the address there); see decision J.~~ Superseded by the owner's answer J below: before the passing test the route refuses the save with a 409, "Immich's address can be saved only after the test sign-in has passed", and contacts nothing.
- While it runs, the primary button is disabled and reads "Checking Immich...". An earlier error line is cleared on the press.
- A refusal shows "Couldn't save Immich's address: Immich didn't answer at {address}: {reason}." in the danger line under the button (`role="alert"`), with one of three fixed reasons: "nothing answered there", "the server there isn't Immich", "its TLS connection failed". Nothing is stored, the field keeps what was typed, the page state is not reloaded, and the line stays until the next press.
- A pass stores the address and goes on to People as today, with no new line.
- Set in the environment, Continue checks nothing, as it saves nothing.

### Decisions for the owner (recommendation first)

The owner answered A (422), I (the card as built in #363) and J (refuse before the test) after #366 merged; B to H stand as recommended. The owner then signed off the whole, the revision below included, on 2026-11-16 (recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469).


A. **The refusal's status code. Owner's answer: 422.** Superseded wherever #366 left it open (`{status}` in `08-setup.md` is now 422). #347 says 409. But the wizard already reads a 409 from a save as "set in the environment, the page is out of date" and reloads the page's state (`08-setup.md`'s error table; the built `ImmichStep.svelte` passes `outdated: reload`), so a 409 here would reload the page under the error line. Recommended: **422**, a reply about the address the person typed that cannot be mistaken for the environment's 409 and does not read as Darkroom failing. Not chosen: 409 as the issue says (the UI would have to tell two 409s apart by their message); 502, which the test sign-in uses when the issuer can't be reached (consistent with it, but reads as Darkroom's own failure, and a reverse proxy's 502 looks the same).
B. **The in-flight label** "Checking Immich..." Not chosen: "Saving..." (true but hides the wait, which can last the whole time limit).
C. **The three reasons' wording**, inside #347's fixed "Immich didn't answer at {address}: {reason}" and decision 10's "Couldn't save {what}: {message}.", so the line has two colons. Not chosen: rewording #347's sentence (for example "Couldn't save Immich's address: nothing answered at {address}."), which would change the issue's fixed message.
D. **What counts as each reason.** No answer: connection refused, the time limit passed, or the name did not resolve. Not Immich: any reply other than Immich's ping reply, a redirect included (not followed, so the check never reaches an address the person did not type). TLS failed: an https:// address whose certificate was not trusted or whose handshake failed. The line never carries the reply's body or headers, a redirect's target, or the certificate or error detail.
E. **No page reload, field kept, line until the next press**, as the other refusals on the step. Not chosen: reloading the state, as for the environment's 409.
F. **No status line when the check passes**: the step moves on, and coming back shows "Saved." as signed off. Not chosen: "Immich answered at {address}." on the People step, which would change the signed-off People board.
G. **No "Save anyway".** An address Immich doesn't answer at can't be stored from the wizard; `IMMICH_URL` in the environment remains for an Immich that is not up yet. The People step's keys need Immich to answer anyway. Not chosen: a second button that stores without the check.
H. **Every press checks again**, a stored address too. Not chosen: skipping the check when the address is unchanged.
I. **Layout. Owner's answer: as built in #363, the card fills the right column; superseded the text that follows.** ~~The README's full-width rule ("Full-width sections") is for the app shell's pages, with the 232 px rail and its content column. The wizard is outside the shell, and its signed-off desktop layout (decision 14) is two columns with the card at most 30 rem. The new states keep that layout, as the other ten wizard board pairs do; making the wizard's card span its column would change every wizard board and is not #347's. Not chosen: widening the card on these boards alone.~~ Phone unchanged. See "Revision" below.
J. **A save before the test** (the API only; the wizard never shows the step then). **Owner's answer: as recommended.** Recommended: refuse it with a 409, as Finish setup refuses before the test, with the message "Immich's address can be saved only after the test sign-in has passed", and contact nothing, so the address is only ever stored checked. Not chosen: storing it unchecked before the test, as today, and checking only after.

### For the build (not copy)

- The time limit: 5 seconds recommended (#347 says "short"). The copy names none.
- Only `GET /api/server/ping` (Immich's current ping route); an Immich old enough to have only `/api/server-info/ping` reads as "the server there isn't Immich".
- Tests as #347 lists them, with a loopback fake Immich for each reason, nothing stored on a refusal, the request through `tls.py`, and the negative test that nothing is contacted before the passing test. The wizard's `make verify` flow and the frontend tests gain the in-flight state and one refusal.

### Revision: a narrower left pane on every wizard desktop board (signed off)

Status: **signed off by the owner on 2026-11-16 (https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6929740469): the 304 px left pane with 40 px padding on all 11 desktop wizard boards, the card filling the column as built in #363, the Immich check states, refusal status 422, an Immich save before the passing test refused with 409 and contacting nothing, choices B to H as recommended; phone boards, the test-failed desktop board and the add-person dialog unchanged.** The owner found the left pane large on desktop and asked to trim it on the whole wizard, and chose answer I above (the card fills the column, as built in #363). The boards below are the signed-off layout.

- **The pane: 304 px (19 rem), with 40 px side padding**, on every wizard desktop board, the steps with lists included. It was half the page (640 px at 1280) on most steps and 420 px on People, Google accounts and Pixels. Why 304: the pane's longest line, "5. Google accounts" with its "Done" tag at 16 px plus the list's 24 px indent, needs about 205 px, and 304 less 80 px of padding leaves 224 px, so no step name wraps; the brand row needs less. Narrower than about 290 px wraps that line; the app shell's 232 px rail is too narrow for the step list at this size.
- **The card fills the column right of the pane**, inset by the column's padding (48 px, or 40 px on the steps with lists), with no 30 rem or 48 rem cap: 880 px at 1280 (896 px with lists). This supersedes `08-setup.md`'s "at most 30 rem wide (... at most 48 rem ...)" and matches the wizard as built in #363 (its card is `w-full`). Decision 14's two columns stay; only their widths change.
- **Boards changed** (desktop only; the change is the pane's width and padding and the card's cap, nothing else, and each board's subtitle says so): `08-setup-{welcome,closed,address,sign-in,immich,immich-check,people,accounts,pixels,alerts,done}-desktop.dc.html`. The People step's add-person dialog keeps its 512 px width (the wizard's dialogs are not part of this revision).
- **Not changed:** every phone board; `08-setup-test-failed-desktop.dc.html`, which draws the server's own failure pages as `darkroom/server/common.py` renders them, not the wizard's pane.
- **For the build:** #363's wizard (`frontend/src/setup/Setup.svelte`, today `md:grid-cols-2`, or `md:grid-cols-[26.25rem_minmax(0,1fr)]` on the steps with lists) moves to a 19 rem pane with `md:px-10` on every step; `make verify`'s `setup-*` desktop images show it.

## About card on Status (#421, approved 2026-11-18)

Status: **approved by the owner on 2026-11-18**, in the Claude Code session that built #421. The owner said they "don't need to sign off, just make the change", then answered "Yes, About card on Status" when asked to confirm where it goes. Those words reached the building agent through the coordinating session. Recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6986445014), the design issue, where the owner can correct the record.

- **Where.** A card titled "About", last on the Status page, after Recent events. Darkroom has no Settings page, so no navigation item is added.
- **What.** One row, "Version": the release and the build's short commit, `v4.7.1 (abc1234)`. A build without a release version (an image built from a checkout, or a local run) shows `dev (abc1234)` when it knows its commit and `dev` when it doesn't.
- **Style.** The existing card, its `dl` row and the muted label (`text-ink-muted`), as in the person cards. No new token, colour or component.
- **Failure.** The version loads on its own (`GET /api/version`, signed in only). While it loads the row reads "Loading...", and when it can't load, "Couldn't load the version" (muted). The rest of the page, its alerts included, is unchanged either way.
- **Boards.** `02-status-desktop.dc.html` and `02-status-phone.dc.html` draw the card with a release version. `02-status-syncthing-desktop.dc.html` and `02-status-syncthing-phone.dc.html` end with a block, "About: the Version row", that draws each state. These four boards were edited by hand in their own HTML, in the boards' existing classes (`card`, `kv`, `sub`, `state`), because the canvas could not be re-exported from the building session. They should be re-exported from the canvas the next time the design session touches them.

## Per-part import on Exports (#490, PP-13, signed off)

Status: **signed off by the owner on 2026-11-20.** The owner gave it in chat, and it was posted at their request on #424 (https://github.com/AnthonyPluth/Darkroom/issues/424#issuecomment-7052514535) and #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-7052514957). The #424 comment reads:

> - The per-part import boards in #506 (Exports desktop and phone, `docs/design/web/04-exports-ingest-*.dc.html`) stand as the PP-13 design, subject to the answers below.
> - Freed state and "Disk freed" total: **not shown before PP-10 (#487)**; the freed state stays on the board only as PP-10's, below the dashed line.
> - A refused part: **one fixed sentence is enough** ("Darkroom couldn't safely read this part, so it imported nothing from it and keeps the file. It doesn't try again on its own."). The real reason can come later with #504.
> - Imports page: **Option A, no change**. Option B (a run still taking parts) is dropped.
>
> This replaces the earlier decision 9 comment's lack of named boards: decision 9 named none; these are the boards PP-13 builds to.

The earlier decision 9 comment is https://github.com/AnthonyPluth/Darkroom/issues/424#issuecomment-7045031620. Boards: `04-exports-ingest-desktop.dc.html` (1280 wide) and `04-exports-ingest-phone.dc.html` (440 wide, the width `make verify` checks; the older phone boards are 390, and the content reflows the same way). They were hand-written in the existing boards' classes and the signed-off tokens only (no new colour, type or spacing value) and previewed in headless Chrome at 1280 and 440 wide; no images are committed, as above.

The boards change only the "current export parts" card of an export imported per part. The account card, schedule, Earlier exports and Check exports now are as in `04-exports-*` and its approved departures; the phone parts use the shared table's phone card (label and value on each line), as built in #30. The data is PP-12's (#489, #497) fields on `GET /api/exports`; nothing new is asked of the API.

**The Imports page is unchanged** (Option A): an export imported per part shows its progress on Exports, and its run appears on Imports once the export finishes, as runs do today. `05-imports-*.dc.html` stays the Imports design.

### When the card shows per-part import

An export is shown per-part when any of its parts has an `ingest_state` other than `not_queued`. An export whose parts are all `not_queued` (imported the old way, or already unpacking at deploy) keeps today's display exactly: no totals, today's state chips and today's sentence (board state 5).

### Totals (above the parts table)

| Label | Value | From |
| --- | --- | --- |
| Imported | "N of M parts" ("0 of 4 parts" before any) | `parts_ingested` of `part_count` (or the number of parts) |
| Held | "N items", then "Until the whole export is in. K of them are waiting for a sidecar."; "None" when 0 (the second sentence is left out when K is 0) | `held_waiting_sidecar` + `held_waiting_export`; K is `held_waiting_sidecar` |

PP-13 shows no "Disk freed" total (see below).

### Each part's State cell

One chip, then at most one detail line under it (13 px, muted). The download states come first; once a part is downloaded, its import state replaces "Downloaded".

| Part | Chip (tone) | Detail line |
| --- | --- | --- |
| Not downloaded, downloading, download failed | Waiting, Downloading (neutral), Failed (bad): today's, unchanged | none |
| Downloaded, `ingest_state` `queued` | Downloaded (neutral) | Waiting to import |
| `ingesting`, `entries` 0 | Importing (neutral) | Reading the file |
| `ingesting` | Importing (neutral) | "812 of 1,488 items" (`entries_done` of `entries`) |
| `ingested`, nothing held or failed | Imported (ok) | "1,517 items" (`entries`) |
| `ingested`, `held` > 0, no failed item | Holding N items (neutral) | "1,402 items. 21 wait for a sidecar, 2 for the whole export." (the part's `held_waiting_sidecar` and `held_waiting_export`; a zero half is left out) |
| `ingested`, `kept_reason` `failed_item` | Kept (warn) | "1,402 items, 2 failed" (`failed_items`) |
| `refused` (`kept_reason` is null) | Couldn't read (bad) | none |
| `failed` | Import failed (bad) | none |

Kept, Couldn't read and Import failed each add one sentence under the table, in part order, where the failed-download sentence sits today:

- Kept: "Part 2: 2 items couldn't be imported, so Darkroom keeps the part's file."
- Couldn't read: "Part 3: Darkroom couldn't safely read this part, so it imported nothing from it and keeps the file. It doesn't try again on its own." The same words for every refused part, as signed off: the API stores no reason for a refusal yet (`kept_reason` is null), and the real reason can come later with #504.
- Import failed: "Part 4: importing this part stopped with an error, so Darkroom keeps the file. It doesn't try again on its own."

No item name, path, file name inside a part or sidecar text is shown anywhere; only the counts and the fixed sentences above. The File column (the part's own Takeout file name) is today's, unchanged.

### The sentence under the parts

- Today, and for an export that never went per-part: "Google allows each part to be downloaded 5 times. Darkroom downloads each part once and keeps the file until it is imported."
- Per-part, built by PP-13 (phase 1, every file kept to the 14-day rule): "Google allows each part to be downloaded 5 times. Darkroom downloads each part once and imports it as soon as it arrives. It keeps every part's file until 14 days after the whole export is imported."

### Empty, loading and failure

- No current export: the card is not shown, as today. A per-part export with nothing imported yet shows the totals with "0 of N parts" and "None" (state 4).
- Loading and a failed load: the page's "Loading..." line and its "Couldn't load the exports" card with Try again, unchanged; the part fields come in the same reply, so they have no failure of their own (state 6).

### PP-10's freed state: not built by PP-13

As signed off, the freed state and the "Disk freed" total are **not shown before PP-10 (#487)**, and **PP-13 builds none of it**. Until PP-10 there is no freed part state and `bytes_freed` is always 0. The boards keep it below a dashed line marked "From PP-10 on: not built by PP-13" (state 7), as PP-10's: a Freed chip (ok) with "1,517 items imported, 50 GB freed" (the part's `bytes`), a third total "Disk freed" from `bytes_freed`, and the sentence "Google allows each part to be downloaded 5 times. Darkroom downloads each part once and imports it as soon as it arrives. Once everything in a part is safely in the library, Darkroom deletes the part's file to free space. A part with an item that failed is kept." PP-10's pull request builds it.
