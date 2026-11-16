# Web UI design (signed off)

The owner signed off this design on 2026-11-04 in issue #16. It is **dark mode only**: the app has one dark theme and no light one, and `frontend/src/app.css` holds exactly the token set in `00-tokens.dc.html` (the `@theme` names given there).

The boards are exported as read from the Design canvas: https://claude.ai/artifact/BvpF7AujGCxiVA9bKvWiUR. Each `.dc.html` file is one board; they reference the canvas runtime (`./support.js`), which is not kept here, so they read as source rather than open as pages. No screenshots are committed; a pull request that changes the UI describes what `make verify` and the demo server showed.

| File | Board |
| --- | --- |
| `Main.dc.html` | Cover and screen map |
| `00-tokens.dc.html` | Theme tokens: colour, type, spacing, radius |
| `01-signin-desktop.dc.html`, `01-signin-phone.dc.html` | Sign-in, signed out |
| `01-signin-error-desktop.dc.html`, `01-signin-error-phone.dc.html` | Sign-in, not allowed |
| `02-status-desktop.dc.html`, `02-status-phone.dc.html` | Status (home) |
| `03-people-desktop.dc.html`, `03-people-phone.dc.html` | People |
| `04-exports-desktop.dc.html`, `04-exports-phone.dc.html` | Exports |
| `04-exports-renew-desktop.dc.html`, `04-exports-renew-phone.dc.html` | Exports, Renew now states (signed off, #164, recorded on #16 (https://github.com/AnthonyPluth/Darkroom/issues/16#issuecomment-6720414904)) |
| `05-imports-desktop.dc.html`, `05-imports-phone.dc.html` | Imports and Missing GPS |
| `06-review-desktop.dc.html`, `06-review-phone.dc.html` | Review queue |
| `07-browser-desktop.dc.html`, `07-browser-phone.dc.html` | Remote browser |
| `03-people-pairing-desktop.dc.html`, `03-people-pairing-phone.dc.html` | People: pairing a Pixel and its Syncthing column (signed off, #322) |
| `02-status-syncthing-desktop.dc.html`, `02-status-syncthing-phone.dc.html` | Status: the Syncthing card, the Pixel row and the new alerts (signed off, #322) |
| `08-setup.md` | First-run setup wizard: **document signed off by the owner on 2026-11-16 (#344, recorded on #303); the boards signed off by the owner on 2026-11-16 (#303)**. #305 builds it, after #346 extends the API |
| `08-setup-welcome-desktop.dc.html`, `08-setup-welcome-phone.dc.html` | Setup wizard: Welcome (signed off, #303) |
| `08-setup-closed-desktop.dc.html`, `08-setup-closed-phone.dc.html` | Setup wizard: Setup isn't open, and open in another browser (signed off, #303) |
| `08-setup-address-desktop.dc.html`, `08-setup-address-phone.dc.html` | Setup wizard: Step 1, Address (signed off, #303) |
| `08-setup-sign-in-desktop.dc.html`, `08-setup-sign-in-phone.dc.html` | Setup wizard: Step 2, Sign-in (signed off, #303) |
| `08-setup-test-failed-desktop.dc.html`, `08-setup-test-failed-phone.dc.html` | Setup wizard: After Pocket ID: the server's failure pages (signed off, #303) |
| `08-setup-immich-desktop.dc.html`, `08-setup-immich-phone.dc.html` | Setup wizard: Step 3, Immich (signed off, #303) |
| `08-setup-people-desktop.dc.html`, `08-setup-people-phone.dc.html` | Setup wizard: Step 4, People (signed off, #303) |
| `08-setup-accounts-desktop.dc.html`, `08-setup-accounts-phone.dc.html` | Setup wizard: Step 5, Google accounts (signed off, #303) |
| `08-setup-pixels-desktop.dc.html`, `08-setup-pixels-phone.dc.html` | Setup wizard: Step 6, Pixels (signed off, #303) |
| `08-setup-alerts-desktop.dc.html`, `08-setup-alerts-phone.dc.html` | Setup wizard: Step 7, Alert emails (signed off, #303) |
| `08-setup-done-desktop.dc.html`, `08-setup-done-phone.dc.html` | Setup wizard: Step 8, Done (signed off, #303) |

Desktop boards are 1280 wide (a left rail) and phone boards 390 wide (a top bar and a bottom tab bar). A page is built only against its board; a web page without a signed-off board here is not built (AGENTS.md, promise 6).

Any later change to layout, flow or visual style goes back to issue #16 for the owner's sign-off, and the boards here are re-exported from the canvas in the same pull request that changes the UI.

Built so far: Sign-in (signed out and not allowed), the app shell, Status and People (#17), the account part of Exports and the Remote browser (#29), Imports and Review (#27), and the schedule, current export parts, earlier exports and "Check exports now" on Exports (#30).

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

Everything here applies only with `DARKROOM_SYNCTHING` on in the one container. With it off (today's layout, a Syncthing configured by hand) the Pixel form, the People page and the Status page stay exactly as they are now, and the Syncthing card is not drawn; the share check alert is the one exception, because the share check runs in both layouts.

The app calls it **Syncthing** wherever a person reads it (the form already says "Syncthing device ID"); "transport" stays a word for the code and the design documents.

### Pairing, in the People page's Pixel form

The form stays in the existing modal (`Modal`, at most 32 rem wide, full width less 1 rem each side on a phone). The device ID field leaves the details step and pairing becomes a second step of the same modal.

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

A card titled "Syncthing", after the People section and before Recent events. Its first line is the title and a chip for Darkroom's Syncthing as a whole, then one sentence; under that, one row per Pixel (name, its chip and line from the table above), or "No Pixel is paired yet." with a link "Open People". It is not drawn with `DARKROOM_SYNCTHING` off. The person card's Pixel row becomes "{Pixel}, synced {ago}" (as board 02 already shows) followed by the per-Pixel chip when it is not `ok`, and "{Pixel}, not synced yet" before the first sync.

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
| `share_check_failed`, Darkroom's Syncthing on | Photo share check failed | bad "Share check failed" | "The photo share did not pass Darkroom's check, so placing, pruning and syncing to the Pixels are paused, and nothing on the share is changed. If the share is not mounted, fix the mapping and restart the container. If it really was replaced, bind the new one as the runbook says. The Status page gives the reason." |
| `share_check_failed`, a Syncthing set up by hand | Photo share check failed | bad "Share check failed" | "The photo share did not pass Darkroom's check, so placing and pruning are paused, and nothing on the share is changed. Pause the Syncthing that serves the Pixels until it passes again. If the share is not mounted, fix the mapping and restart the container. If it really was replaced, bind the new one as the runbook says." |
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
6. **With `DARKROOM_SYNCTHING` off the pages are exactly as today** (the typed field in the form, no Syncthing column or card); only the share check alert is new. Not chosen: the column and card shown as "Set up by hand".
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

## First-run setup wizard (#303, document signed off)

Status: **document signed off by the owner on 2026-11-16 (#344, recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926281385); the boards signed off by the owner on 2026-11-16 (visual sign-off recorded on #303 https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926640234).** #303 closes, and #305 builds against the boards, once #344 merges (decision 1 of `08-setup.md`). Every decision the owner made on #344 (1 to 18) is recorded in `08-setup.md`, "The owner's decisions", unchanged.

Boards: `08-setup-{welcome,closed,address,sign-in,test-failed,immich,people,accounts,pixels,alerts,done}-{desktop,phone}.dc.html`, the eleven pairs the document's board list names, each stacking the states that list names for it, in its order. They use the signed-off tokens only (no new colour, size or font) and the parts already built (card, buttons, fields, the danger alert line, chips, and the People page's table and modal on the People, Google accounts and Pixels steps). The copy is the document's, word for word; `{placeholders}` are filled with made-up values (addresses on 192.168.1.20 and example.com, made-up client and device IDs). `08-setup-test-failed-*` draws the server's own pages as `darkroom/server/common.py` renders them today, not the app's style. They were previewed in a browser at 390 and 1280 wide; no images are committed, as above.

