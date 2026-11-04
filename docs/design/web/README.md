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
| `05-imports-desktop.dc.html`, `05-imports-phone.dc.html` | Imports and Missing GPS |
| `06-review-desktop.dc.html`, `06-review-phone.dc.html` | Review queue |
| `07-browser-desktop.dc.html`, `07-browser-phone.dc.html` | Remote browser |

Desktop boards are 1280 wide (a left rail) and phone boards 390 wide (a top bar and a bottom tab bar). A page is built only against its board; a web page without a signed-off board here is not built (AGENTS.md, promise 6).

Any later change to layout, flow or visual style goes back to issue #16 for the owner's sign-off, and the boards here are re-exported from the canvas in the same pull request that changes the UI.

Built so far: Sign-in (signed out and not allowed), the app shell, Status and People (#17), Imports and Review (#27). Exports and Browser arrive with their own issues.
