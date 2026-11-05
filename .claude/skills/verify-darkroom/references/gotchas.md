# Gotchas

What usually goes wrong when driving Darkroom with `tools/control`, and what to do about it.

## The clock is frozen

- The server and the seed run under libfaketime at `FROZEN_NOW` in `tools/verify.py` (2026-06-15, noon, UTC), and the browser's clock is fixed at the same moment with its time zone set to UTC. "Today" on every page is that day, whatever the real date is. The demo's check-ins and photos are dated relative to it, so "1 hour ago" never moves.
- Without libfaketime nothing starts. `doctor` says so and prints the install line (`sudo apt-get install libfaketime`). It works on Linux only, because macOS has no `LD_PRELOAD`.
- A bug that only happens as time passes can't be reproduced here: write a test with fake time.

## Signed in, and signed out

- A session's browser carries the demo member's session cookie (from `demo-session.json` in the seed). `goto sign-in` clears it and opens the sign-in page; the next `goto` of any other page signs the browser in again. The sign-in page's `Continue with Pocket ID` link leads to `/auth/login`, which on a demo server says it has no sign-in provider: don't click it.
- A signed-out visit asks `/api/me` and gets a 401, which Chromium logs as a console error ("Failed to load resource ... 401"). `make verify` and `flow` allow that one line on a signed-out page and nothing else.
- The app never reads the body of that 401, so Chromium counts the request as still in flight and the network never goes idle on the sign-in page. `goto sign-in` and a signed-out screenshot wait for the page's `load` event and the Loading text to go instead, and a note is not raised for it.
- The app ships no favicon, so the browser's own `/favicon.ico` request would be a 404 console error. The session and `make verify` answer it with 204.

## Flows

- make verify runs up to `VERIFY_JOBS` servers (at most 4, one copy of the seed each). Pages and flows share them: a flow sees its own earlier changes and those of the flows queued before it on the same server (all of them, with `VERIFY_JOBS=1`), never another server's. So a flow starts from the seed only when nothing before it changed data; `frontend/verify/flows/README.md` says how to write one that doesn't depend on that. `tools/control flow <name>` runs on your session's data: if you changed something, run `reset` first. A flow that changes data doesn't pass twice on the same data.
- There is no Google, Immich, Pocket ID or iPhone behind the demo server: it can't reach the internet, so a page that needs one fails or shows its error state.
- A flow with `viewports` only runs there. Run it at the other width and it may fail at its first step; the `notes` in the answer say so.
- After every screenshot the flow checks again the `expect_text` steps since its last action. A screenshot that scrolls or closes something fails the flow with `check_lost`.
- A flow's images go to `artifacts/control/flows/`, at every width (make verify writes them at phone and desktop only, to `artifacts/verify/`), and are never committed.

## What a session doesn't check

- make verify also runs an accessibility audit on every page and flow (axe WCAG 2.2 AA, a visible focus ring on every tab stop, tap targets at phone width) and a reduced-motion pass. A session doesn't: a clean `console` and `network` here is not a pass. Run `make verify PAGES="people"` (or `poetry run python tools/verify.py run --flow <name>`) before you push a UI change.
- Tap targets under 44 px at phone width are listed as notes, not failures, for now: the app's `html` font size is 14 px, so `min-h-11` buttons come out 39 px tall. `TAP_TARGETS_FAIL` in `frontend/verify/a11y.mjs` makes them fail once the UI is fixed.

## Sizes and the theme

- Phone is 440×956 and desktop is 1280×800, the only two widths make verify shoots. `laptop` (1024×768) and `wide` (1440×900) are there for the flows that list them. The app's layout switches at `md` (768 px): look at 768 to 1023 px in a browser by hand.
- The app is dark only: there is no light theme to check.
- `screenshot` is the whole page; `screenshot --viewport` is only the window, where the page is scrolled to now (the first screen until you scroll). Only the whole-page one scrolls to the top first. The phone's bottom tab bar is fixed, so in a full-page image it sits where the first screen ended.

## Settling and flakes

- `screenshot`, `goto` and `wait-settle` wait as make verify does before an image: the network idle (up to 3 seconds), fonts loaded, no animation running, no Loading text, then a short settle. If it never settles the answer says what was still going, in `notes` or `stillLoading`.
- Raster flakes are rare: an edge that comes out one level apart between runs. Take the shot again before you believe a one-pixel difference. Two `make verify` runs on one machine give identical images.

## One session per checkout

- A control session never takes make verify's ports or data, so it can run beside one, but it adds load. `start` refuses a machine that is busy (more than 1.5 runnable processes per CPU) unless you pass `--anyway`.
- There's one session per checkout. A second `start` says one is running. `stop` it, or keep using it.
- A session stops by itself after 30 idle minutes (`--idle-minutes`). If a command then reports `stale_session` or `no_session`, run `start` again.

## Targets

- Use the names `snapshot --text` shows: `click button "Save"` is `click button Save`. Text matching ignores case and matches substrings; add `--exact` for an exact match.
- More than one visible match is an error that lists them (every person has an Edit button): pick one with `--nth`, or name it more exactly (`click button "Edit Avery Example"`).
- Text that starts with `-` goes after `--`: `type textbox Amount -- -5`.
- The flows use Playwright selectors (`a[href='/people'] >> visible=true`); a target that starts like a selector (`#id`, `.class`, `[attr]`, `tag[attr]`, `css=`, `text=`) is used as one.
