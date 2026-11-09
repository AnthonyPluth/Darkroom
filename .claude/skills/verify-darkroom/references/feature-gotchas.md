# Feature gotchas

Hand-edited. `tools/user_feature_map.py` merges these into [feature-map.md](feature-map.md) (generated; never edit that one). A `## <name>` heading must be a page (`sign-in`, `status`, `people`) or a flow's name (a file in `frontend/verify/flows/`), and only `- ` bullets go under it. Demo data only; no real names, emails or photo paths. General advice is in [gotchas.md](gotchas.md).

## sign-in
- The browser is signed out: `tools/control goto sign-in` clears the demo session cookie, and any other `goto` signs it in again.
- The 401 from `/api/me` shows in the console as one error line. It is the signed-out answer, and `make verify` allows exactly that line here.
- `Continue with Pocket ID` leads to `/auth/login`, which a demo server answers with "Demo mode" and no provider.

## status
- The demo has one Google account that needs a sign-in, so Status lists it under "Needs you"; dates show relative to the frozen day (2026-06-15).
- "Open remote browser" and each person's link lead to other pages of the app; `goto` them by name.

## people
- Every person, account and Pixel has its own Edit button: name it with the person (`click button "Edit Avery Example"`) or pass `--nth`.
- Forms here change demo data only; `reset` puts it back. Put no real email, name or Immich key in one.

## open-people
- Starts on Status and follows the navigation to People: the tab bar at phone width, the sidebar at desktop.

## sign-out
- Starts signed in on People, clears the session cookie and reloads, so the last screenshot is the sign-in page.

## exports
- The demo has one Google account that needs a sign-in and one with a working session; Connect and Create schedule only queue browser jobs the demo has no worker for.

## imports
- With the import (make verify, `start --with-import`) the demo has two runs; a plain `start` has only the earlier one, a recorded row. The newer one is a real import of a made-up export (`tools/demo_import.py`) with 4 added, 7 skipped as duplicates, 2 sent to review and 3 missing GPS. The list of photos with no location is the three videos of the originals, whichever run it is under; filters and the paging change what the list shows, not the demo data.

## review
- The demo has one pending pair, the recorded one (IMG_0101); with the import (make verify, `start --with-import`) it has three, the other two being the likely re-encodes the import held back (IMG_0001(1) and IMG_0102(1)). Each pair is a library copy beside a slightly different held copy. Keep both, skip and undo change demo data; `reset` puts it back.

## browser
- A plain demo visit has no VNC address and shows the not-set-up state. `make verify PAGES="browser"` and the `browser-fullscreen` flow use a synthetic viewer served from the session's own loopback address, labelled as made-up demo data. This checks viewer sizing and full-screen controls without opening Google or a real VNC server.
