# Device API

What the Darkroom iPhone app (#35, and cleanup in #38) talks to. The request and reply shapes are also in `darkroom/server/contract.py` and the generated `docs/openapi.json`; this page says how the pieces fit.

## Signing in

1. The app opens `GET /auth/device/start?name=<iPhone name>` in an `ASWebAuthenticationSession` (callback scheme `darkroom`). The name is what the People page shows for this iPhone: 1 to 100 printable characters.
2. Darkroom runs the same Pocket ID sign-in as the web app (authorization code with PKCE and a nonce) and comes back to `/auth/callback`.
3. On success Darkroom answers `303` to `darkroom://signed-in#token=<token>`. The token is in the fragment, so it is never sent to a server. Keep it in the Keychain; Darkroom stores only its SHA-256 and never shows it again.

Who may sign in an iPhone: someone in one of `OIDC_DEVICE_GROUPS` (default `family,parents`) whose verified email is the email of exactly one person on the People page. That person owns everything the iPhone uploads. Otherwise the sign-in view shows an error page (not allowed, or "No person with this email") and no device is created.

Each sign-in creates a new device with its own token. Signing in again after a revoke is how an iPhone gets back in; the old token stays refused.

## Calling the API

Every device route takes `Authorization: Bearer <token>`. Darkroom answers `401 {"error": "sign in required"}` when:

- the token is missing or unknown;
- the device was revoked on the People page (`revoked_at` is set);
- the person's access lapsed: their email no longer belongs to a verified Pocket ID user in `OIDC_DEVICE_GROUPS`, or that user has not signed in to Darkroom (web or app) for 30 days;
- Darkroom has no sign-in provider configured.

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

- The body streams to the person's `originals/.uploads_tmp/` while it is hashed, then is saved once as `originals/<filename>`. There is no size cap. A connection that sends nothing for `UPLOAD_READ_TIMEOUT` seconds (default 3600) is dropped.
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

### `POST /api/device/checkin`

`{"pending_uploads": 12, "app_version": "0.1.0"}` (the version is optional). Records the count and version with the check-in time; the People and Status pages show them per iPhone. Reply: `{"ok": true}`.
