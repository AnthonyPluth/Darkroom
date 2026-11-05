# Feature gotchas

Hand-edited. `tools/user_feature_map.py` merges these into [feature-map.md](feature-map.md) (generated; never edit that one). A `## <name>` heading must be a page (`sign-in`, `status`, `people`) or a flow's name (a file in `frontend/verify/flows/`), and only `- ` bullets go under it. Demo data only; no real names, emails or photo paths. General advice is in [gotchas.md](gotchas.md).

## sign-in
- The browser is signed out: `tools/control goto sign-in` clears the demo session cookie, and any other `goto` signs it in again.
- The 401 from `/api/me` shows in the console as one error line. It is the signed-out answer, and `make verify` allows exactly that line here.
- `Continue with Pocket ID` leads to `/auth/login`, which a demo server answers with "Demo mode" and no provider.

## status
- The demo has one Google account that needs a sign-in, so Status lists it under "Needs you"; the iPhones show check-ins relative to the frozen day (2026-06-15).
- "Open remote browser" and each person's link lead to other pages of the app; `goto` them by name.

## people
- Every person, account, Pixel and iPhone has its own Edit or Revoke button: name it with the person (`click button "Edit Avery Example"`) or pass `--nth`.
- Forms here change demo data only; `reset` puts it back. Put no real email, name or Immich key in one.

## open-people
- Starts on Status and follows the navigation to People: the tab bar at phone width, the sidebar at desktop.

## sign-out
- Starts signed in on People, clears the session cookie and reloads, so the last screenshot is the sign-in page.

## exports
- The demo has one Google account that needs a sign-in and one with a working session; Connect and Create schedule only queue browser jobs the demo has no worker for.

## imports
- The demo has one imported export (its run is listed) and three photos with no location (the videos); filters and the paging change what the list shows, not the demo data.

## review
- The demo has one pending pair: a library copy and a slightly different held copy of the same photo. Keep both, skip and undo change demo data; `reset` puts it back.

## browser
- The remote browser view needs a VNC address the demo doesn't have, so the page shows its not-set-up state.
