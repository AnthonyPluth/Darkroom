# Scripted flows

Each `*.json` file here is a flow that `make verify` runs after visiting the pages, at phone and desktop widths (or the `viewports` it lists). A pull request adds steps by adding or editing a file. `tools/control flow <name>` runs one on a session, step by step.

```json
{
  "name": "open-people",
  "page": "status",
  "viewports": ["phone", "desktop"],
  "steps": [
    { "click": "a[href='/people'] >> visible=true" },
    { "expect_text": { "selector": "h1", "text": "People" } },
    { "screenshot": "people" }
  ]
}
```

## Fields

- `page` is the page the flow starts on, one of `PAGES` in `verify.mjs` (default `status`). The flow starts signed in as the demo member, except on `sign-in`, where it starts signed out.
- `viewports` defaults to phone and desktop. `laptop` (1024 px) and `wide` (1440 px) are there for a flow that lists them. The app is dark only, so every flow runs in the dark colour scheme.
- `allow_console` (a list of texts) names console errors a flow expects. A page that is signed out when the flow ends (it starts on `sign-in`, or a `sign_out` step ran) allows the browser's own "401" line: the app asks `/api/me` and a signed-out visitor is answered with 401.

A flow ends with a screenshot, and fails the run on a failed step, a console error, a 5xx response or a request to any address but the demo server's own.

## Steps

A step has one action and an optional `timeout` in milliseconds (default 10000). Selectors are [Playwright selectors](https://playwright.dev/docs/other-locators) (CSS, `text=…`, `role=…`).

**Moving around and interacting**

- `{"goto": "/people"}` opens a path of the app (or a full URL on the demo server).
- `{"click": selector}`, `{"fill": {"selector": …, "text": …}}` and `{"press": {"selector": …, "key": "Enter"}}`.
- `{"hover": selector}` moves the pointer over it; the hover state shows in the next screenshot.
- `{"select": {"selector": "label:has-text('Default') select", "index": 1}}` chooses an option of a `<select>` by its position.
- `{"click_if_present": selector}` clicks it when it shows up within a second (`timeout` changes that) and does nothing when it doesn't, for a step that only one width needs.
- `{"upload": {"selector": "input[type=file]", "file": "tests/fixtures/…"}}` chooses a file of the repository (made-up data) in a file input.
- `{"scroll_to": selector}` scrolls it to the top of the screen. An image is always the whole page, taken from the top, so this only matters to a later step.
- `{"sign_out": true}` clears the session cookie and reloads: the page is then the sign-in page, and the flow no longer needs `allow_console` for the 401.
- `{"reload": true}` reloads the page.

**Checking and capturing**

- `{"wait_for": selector}` waits for it to appear.
- `{"expect_text": {"selector": …, "text": …}}` fails unless it contains the text within the timeout; it keeps looking while the page settles.
- `{"screenshot": "name"}` (letters, digits, `-` or `_`: it becomes part of a file name) saves the full page as `flow-<flow name>-<name>-<viewport>.png` to `artifacts/verify/` (ignored by git), at phone and desktop only. Other widths run the steps and checks and write no image. A flow's images are for you to look at; the committed images are the pages' in `docs/screenshots/`.
- `{"download": {"selector": "button:has-text('Export')", "name": "export"}}` clicks it, waits for the file the browser downloads and saves it as `flow-<flow name>-<name>-<viewport>.png` in the same place.

## Writing a flow

Flows run against the demo data only, on one of several servers that `make verify` runs at once (`VERIFY_JOBS`), each with its own copy of the data. A flow sees its own earlier changes and those of the flows queued before it on the same server, never another server's. So a flow:

- finds what it needs in the untouched demo data, and doesn't rely on another flow having added or removed something;
- puts back what it changes if a later flow could trip on it;
- works alone (`poetry run python tools/verify.py run --flow NAME`);
- looks for a person by what the page shows today, not by a date you remember, because dates in the demo data are relative to the frozen clock.
