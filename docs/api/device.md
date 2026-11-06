# Device API

What the Darkroom iPhone app (#35) talked to. The app is gone (#199) and its code was deleted from the repository; the backend that served it is being removed (#201), so this page describes routes that no client calls. The request and reply shapes are also in `darkroom/server/contract.py` and the generated `docs/openapi.json`; this page says how the pieces fit.

## Signing in

1. The app makes a fresh random `state` for this attempt (22 to 128 characters of `A-Z a-z 0-9 - _`, from a cryptographic generator, used once) and opens `GET /auth/device/start?name=<iPhone name>&state=<state>` in an `ASWebAuthenticationSession` (callback scheme `darkroom`). The name is what the People page shows for this iPhone: 1 to 100 printable characters.
2. Darkroom runs the same Pocket ID sign-in as the web app (authorization code with PKCE and a nonce) and comes back to `/auth/callback`.
3. On success Darkroom answers `303` to `darkroom://signed-in#token=<token>&state=<state>`, echoing the state of that attempt. The app installs the token only if the echoed state equals the one it made for the attempt in progress, and refuses a callback with a missing, different or already used state. A start without a valid `state` is a 400 page and no sign-in begins. When the person cannot be signed in for a reason they can ask to have fixed, Darkroom answers `303` to `darkroom://signed-in#error=<reason>&state=<state>` instead of a page, and creates no device: `error=no_person` (no single person on the People page has the verified email) or `error=not_allowed` (the account is not in `OIDC_DEVICE_GROUPS`). The fragment carries only that code and the state, never an email, a name or a token, and is sent only for an attempt whose one-use state was valid and is now spent; the app accepts it only when `state` equals the attempt in progress. Any other failure (an unknown, expired, replayed or other-browser attempt, a provider that can't be reached, a sign-in that went wrong) stays a page in the sheet. The token is in the fragment, so it is never sent to a server. Keep it in the Keychain; Darkroom stores only its SHA-256 and never shows it again.

Who may sign in an iPhone: someone in one of `OIDC_DEVICE_GROUPS` (default `family,parents`) whose verified email is the email of exactly one person on the People page. That person owns everything the iPhone uploads. Otherwise no device is created: the app gets the `error=not_allowed` or `error=no_person` redirect from step 3, and the sheet shows an error page (not allowed, or "No person with this email") only when the attempt's stored app state is unusable.

Each sign-in creates a new device with its own token, bound to the Pocket ID account (subject) that signed in. Signing in again after a revoke is how an iPhone gets back in; the old token stays refused. When a different account signs in an iPhone for the same person (the email moved to another account, or the account was recreated), the person's devices bound to other accounts are revoked.

## Calling the API

Every device route takes `Authorization: Bearer <token>`. Darkroom answers `401 {"error": "sign in required"}` when:

- the token is missing or unknown;
- the device was revoked on the People page (`revoked_at` is set);
- the person's access lapsed: the account that signed the device in no longer has the person's email verified, is no longer in `OIDC_DEVICE_GROUPS`, or has not signed in to Darkroom (web or app) for 30 days;
- another verified account with the person's email has signed in to Darkroom more recently than the device's account (the email moved, or the account was recreated);
- Darkroom has no sign-in provider configured.

When the token matches a device, the 401 also says why, in `reason` (nothing else is added, and an unknown, missing or malformed token gets the plain `{"error": "sign in required"}`):

- `device_revoked`: the device was revoked (on the People page, or because another account signed in an iPhone for the person) The token never works again; a new sign-in is a new device.
- `sign_in_lapsed`: no sign-in to Darkroom for 30 days. The same token works again after the person signs in.
- `sign_in_unavailable`: Darkroom has no sign-in provider configured.
- `sign_in_refused`: the account's access lapsed otherwise (email no longer verified or matching, no longer in `OIDC_DEVICE_GROUPS`, or the email moved to a newer account). The same token works again if that is undone.

On a 401 the app stops uploading and asks the person to sign in again. Every accepted request records the device's last check-in.

## Routes

### `POST /api/device/assets/check`

Declares assets and their resources, and asks which still need uploading.

```json
{"assets": [{"id": "<PhotoKit local identifier>", "resources": [
  {"kind": "photo", "sha1": "<40 hex>", "bytes": 2481152, "filename": "IMG_0042.HEIC"},
  {"kind": "live_video", "sha1": "<40 hex>", "bytes": 3311104, "filename": "IMG_0042.MOV"},
  {"kind": "adjusted", "sha1": "<40 hex>", "bytes": 2655232, "filename": "IMG_0042_adjusted.HEIC"}]}]}
```

- Up to 500 assets per call, each with one to three resources and no kind twice. `kind` is `photo`, `live_video` or `adjusted`.
- `filename` is PhotoKit's original filename for the photo; the Live Photo's video is `<stem>.MOV`; an edited version is `<stem>_adjusted.<ext>`. A filename is a plain name: no `/` or `\`, not starting with `.`, not ending in `.xmp`, at most 200 characters.
- The list of resources is the asset's whole current set: a kind left out is forgotten, and a resource whose SHA-1 or filename changed needs uploading again. Either change clears any eligibility recorded for the asset.

Reply: `{"needed": [{"id": "...", "kind": "photo"}]}`. A resource is not needed when one with the same SHA-1 was already uploaded from any iPhone of the same person, or when the person's originals already hold a file with that SHA-1 (for example one Möbius Sync delivered); Darkroom links it and nothing is sent.

### `PUT /api/device/assets/{id}/resources/{kind}`

Uploads one resource that the last check declared. `{id}` is the PhotoKit identifier percent-encoded as one path segment (`/` becomes `%2F`).

Headers: `X-Darkroom-SHA1` (the resource's SHA-1), `X-Darkroom-Filename` (its filename) and `Content-Length`; both must match what the check declared. The body is the file's bytes, not chunked.

- The body streams to the person's `originals/.uploads_tmp/` while it is hashed, then is saved once as `originals/<filename>`. There is no size cap. A connection that sends nothing for `UPLOAD_READ_TIMEOUT` seconds (default 3600; a whole number, at least 1, or Darkroom refuses to start) is dropped.
- If `originals/<filename>` already holds other bytes, the file is saved as `<stem>~<first 8 of the SHA-1>.<ext>`; if it holds the same bytes, nothing is written.
- Darkroom never replaces, moves or deletes a file under `originals/`.

| Status | Meaning |
| --- | --- |
| 200 | Saved, or already there. `{"id", "kind", "sha1", "already": bool, "indexed": bool}`; `indexed` is false when the file could not be read as a photo or video (it is still kept). |
| 400 | A bad kind, SHA-1, filename or asset id, or the body ended before its `Content-Length`. |
| 401 | See above. Nothing is read or written. |
| 404 | The asset or kind was not declared by a check. |
| 408 | The upload went idle. |
| 409 | The SHA-1 or filename is not what the check declared (check again), or both names are held by other bytes. |
| 411 | No `Content-Length`, or a chunked body. |
| 422 | The bytes' SHA-1 is not `X-Darkroom-SHA1`. Nothing is kept. |

Sending the same resource again after it was saved answers 200 with `"already": true` without reading the body, so retries are safe.

### `GET /api/device/me`

Who this iPhone uploads for. Reply: `{"person": "Sam", "device": "Sam's iPhone"}` and nothing else: the person's name on the People page and this iPhone's name. The email, groups, subject and ids are never in the reply. 401 as for every device route, including the `reason`. Like any accepted device request, it records the device's last check-in.

### `POST /api/device/checkin`

`{"pending_uploads": 12, "app_version": "0.1.0"}` (the version is optional). Records the count and version with the check-in time; the People and Status pages show them per iPhone. Reply: `{"ok": true}`.

### `PUT /api/device/push-token`

`{"token": "<APNs device token, hex>"}`. Send it after sign-in and whenever iOS hands the app a new one (`didRegisterForRemoteNotificationsWithDeviceToken`), as lowercase or uppercase hex, 64 to 200 characters. Darkroom stores it sealed with `DARKROOM_SECRET_KEY` (`secretbox`) plus its SHA-256, never in plain text, and alerts for this device's person are pushed to it. If another of this person's devices had registered the same token, it moves to this one; so does one held by another person's device that is revoked or whose owner can no longer sign in (whether or not its stored copy can still be read). A push token is only used while its device's owner can still sign in. Sending the same token again only records that it was seen, unless the stored copy can't be read with the current key (for example after `DARKROOM_SECRET_KEY` changed), in which case it is sealed again. Reply: `{"ok": true}`; 400 for anything that isn't a device token; 401 when the device isn't signed in, including when it is revoked at any point before the token is written (the write is guarded by the device still being unrevoked, so a revoked device never keeps a token); 409 when another person's signed-in device holds the same token; 503 when the server has no `DARKROOM_SECRET_KEY`.

### `DELETE /api/device/push-token`

Forgets this device's push token (for example when notifications are turned off in the app). Revoking a device forgets its token too. Reply: `{"ok": true}`, also when there was none; 401 when the device isn't signed in, including when it is revoked at any point before the clear is written.

## Cleanup (removed)

The cleanup routes (`POST /api/device/eligible`, `POST /api/device/eligible/confirm` and `POST /api/device/deleted`), the People page's cleanup switch (`PATCH /api/devices/{id}`) and the eligible counts on `/api/status` were removed with the iPhone app (#199, #201). Darkroom no longer decides or records deletions from an iPhone; those routes answer 404.
