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
- **Under the buttons**, in `text-ink-muted`, one sentence says why the button is as it is. The disabled button is `disabled` like the others on the page, so the sentence is ordinary text in reading order, and the button names it with `aria-describedby`. The existing Create schedule sentence stays below it unchanged ("The schedule exports Google Photos every 2 months for 1 year." while the schedule is live, the longer one after it ended), as in every board state that has a schedule.
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
