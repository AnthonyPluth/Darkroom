# Remote browser: several accounts waiting for a sign-in

Signed off by the owner, AnthonyPluth, on 2026-11-20 (#494, #496). A text spec for one state of the Remote browser page (`07-browser-desktop.dc.html`, `07-browser-phone.dc.html`); nothing else on the page changes, and there are no new controls.

**When it shows.** Two or more Google accounts have a browser job waiting for a person, and Darkroom knows which job's window is on the virtual screen (`on_screen_job` in `GET /api/browser/view`). With one account waiting, or when no waiting job is known to be on screen, every sign-in card reads as in the signed-off boards.

**The card of the account whose window is on screen.** Its paragraph starts with:

> The window below is {email}'s.

followed by the card's usual text (sign in in the window below, Darkroom confirms the account, and so on).

**The card of every other waiting account.** Its chip and title stay as they are; its paragraph is only:

> Waiting for the window below, which shows {email} now. Its window comes up once that sign-in is done.

where `{email}` is the account on screen.
