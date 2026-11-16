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
| `03-people-unpair-desktop.dc.html`, `03-people-unpair-phone.dc.html` | People: unpairing a Pixel from Edit Pixel (addendum to #322, for the owner's sign-off, **not signed off**) |

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

## Unpairing a Pixel (addendum to #322, for the owner's sign-off)

Status: **drawn for the owner's sign-off, NOT yet signed off.** Nothing here is built until the owner approves it on #322. It adds to "Pairing, Syncthing status and the new alerts" and changes none of its signed-off rows, cards or alerts.

Why: with Darkroom's Syncthing on, the Pixel form shows the paired ID read-only with only "Pair again" (or "Pair"), so there is no way in the app to unpair. A Pixel that is lost, sold or replaced keeps its Syncthing device and its Send Only share of the photos until someone clears the ID with `PATCH /api/pixels/{id} {"syncthing_device_id": null}` by hand. The second review flagged this as access that outlives the device. The backend already does the rest: once the ID is cleared, the reconciler removes the device and the Pixel's folder from Darkroom's Syncthing within 60 s, and deletes no file (`tests/test_transport.py`, "a Pixel that is deleted or unpaired loses its folder and device and no file is deleted"). So this is a UI action on an existing route; no new route.

Boards: `03-people-unpair-desktop.dc.html` and `03-people-unpair-phone.dc.html`, six states stacked: Edit Pixel paired with Unpair, the confirmation, in flight, refused, then the Pixels card and Edit Pixel afterwards. They follow the full-width rule above: at 1280 every card and dialog spans the content column (968 px); the phone layout is as on the other phone boards. Existing tokens, chips, buttons, table and modal only. Names and the device ID are made up.

### The action

- **Where.** In Edit Pixel's read-only Syncthing row, after "Pair again": the ID in mono, then "Pair again" and "Unpair" side by side (on a phone, stacked full width, Pair again first). Shown whenever the Pixel has a saved device ID, whatever its per-Pixel state (including 2a and 2b, where the saved ID is the problem). Not shown when the Pixel is not paired, and not shown with `DARKROOM_SYNCTHING` off, where the form keeps today's typed field and clearing it is the way.
- **Label.** "Unpair", with the accessible name "Unpair {name}".
- **Style.** The app's danger-outline secondary button (`btn btn-danger`), as Delete Pixel in the same form: it takes access away, so it is not a plain `btn`, and never primary. The form's own Save stays the only primary.
- **What it does.** It never applies on Save. Pressing it opens the confirmation the way Delete Pixel does today: the form closes (unsaved edits in it are dropped, as for Delete Pixel) and the existing `Confirm` dialog opens.

### The confirmation

The existing `Confirm` dialog, full width at desktop like every dialog since the full-width redraw.

- Title: "Unpair {name}?"
- Description (muted, `Confirm`'s message): "Darkroom stops sharing {name}'s folder with this device and removes the device from its Syncthing within a minute. Nothing new reaches {name} until it is paired again."
- Then: "No photo or file is deleted. Darkroom's copies stay on the photo share, and Darkroom does not touch the copies already synced to {name}: they stay on the Pixel until someone removes them there."
- Buttons, right-aligned as in `Confirm`: "Cancel" (`btn`) and "Unpair" (`btn btn-danger`, as `Confirm`'s action is for every delete). On a phone they stack full width.
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
