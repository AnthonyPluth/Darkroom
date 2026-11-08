# Device API

What the Darkroom iPhone app (#35) talked to. The app is gone (#199) and its code was deleted from the repository; the backend that served it is being removed (#201), so this page describes routes that no client calls. The request and reply shapes are also in `darkroom/server/contract.py` and the generated `docs/openapi.json`; this page says how the pieces fit.

## Signing in

1. The app makes a fresh random `state` for this attempt (22 to 128 characters of `A-Z a-z 0-9 - _`, from a cryptographic generator, used once) and opens `GET /auth/device/start?name=<iPhone name>&state=<state>` in an `ASWebAuthenticationSession` (callback scheme `darkroom`). The name is what the People page shows for this iPhone: 1 to 100 printable characters.
2. Darkroom runs the same Pocket ID sign-in as the web app (authorization code with PKCE and a nonce) and comes back to `/auth/callback`.
3. On success Darkroom answers `303` to `darkroom://signed-in#token=<token>&state=<state>`, echoing the state of that attempt. The app installs the token only if the echoed state equals the one it made for the attempt in progress, and refuses a callback with a missing, different or already used state. A start without a valid `state` is a 400 page and no sign-in begins. When the person cannot be signed in for a reason they can ask to have fixed, Darkroom answers `303` to `darkroom://signed-in#error=<reason>&state=<state>` instead of a page, and creates no device: `error=no_person` (no single person on the People page has the verified email) or `error=not_allowed` (the account is not in `OIDC_DEVICE_GROUPS`). The fragment carries only that code and the state, never an email, a name or a token, and is sent only for an attempt whose one-use state was valid and is now spent; the app accepts it only when `state` equals the attempt in progress. Any other failure (an unknown, expired, replayed or other-browser attempt, a provider that can't be reached, a sign-in that went wrong) stays a page in the sheet. The token is in the fragment, so it is never sent to a server. Keep it in the Keychain; Darkroom stores only its SHA-256 and never shows it again.

Who may sign in an iPhone: someone in one of `OIDC_DEVICE_GROUPS` (default `family,parents`) whose verified email is the email of exactly one person on the People page. That person owns everything the iPhone uploads. Otherwise no device is created: the app gets the `error=not_allowed` or `error=no_person` redirect from step 3, and the sheet shows an error page (not allowed, or "No person with this email") only when the attempt's stored app state is unusable.

Each sign-in creates a new device with its own token, bound to the Pocket ID account (subject) that signed in. Signing in again after a revoke is how an iPhone gets back in; the old token stays refused. When a different account signs in an iPhone for the same person (the email moved to another account, or the account was recreated), the person's devices bound to other accounts are revoked.

## Calling the API (removed)

The `device` auth kind and every route that took a device token were removed with the iPhone app (#199, #201): `GET /api/device/me`, `POST /api/device/checkin`, the upload routes (`POST /api/device/assets/check`, `PUT /api/device/assets/{id}/resources/{kind}`) and the push-token routes. A device token gets `401 {"error": "sign in required"}` on every route, with no `reason`, the same as no token (`tests/test_every_route.py`).

### Push tokens (removed)

`PUT /api/device/push-token` and `DELETE /api/device/push-token` were removed with the iPhone app (#199, #201): Darkroom no longer stores a push token or pushes anything to an iPhone.

## Cleanup (removed)

The cleanup routes (`POST /api/device/eligible`, `POST /api/device/eligible/confirm` and `POST /api/device/deleted`), the People page's cleanup switch (`PATCH /api/devices/{id}`) and the eligible counts on `/api/status` were removed with the iPhone app (#199, #201). Darkroom no longer decides or records deletions from an iPhone; those routes answer 404.
