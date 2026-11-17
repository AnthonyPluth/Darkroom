# Running the live Immich spike (#266)

[#266](https://github.com/AnthonyPluth/Darkroom/issues/266) needs evidence from a real Immich server before managed uploads can be enabled ([design](../design/immich-managed-storage.md), section 2, "Live spike required before enabling imports"). `tools/spikes/immich_live.py` gathers it in one run and writes a Markdown result that is safe to paste into the issue. It uploads only media it generates in code (small JPEGs, a TIFF-based DNG and MJPEG AVI videos with made-up dates, places and descriptions), never real photos.

## What it never does

- It sends only `GET /api/server/about`, `GET /api/users/me`, `GET /api/api-keys/me`, `POST /api/assets`, `POST /api/assets/bulk-upload-check`, `POST /api/search/metadata` and `GET /api/assets/{id}` (and its `/original?edited=false`). Anything else, including every delete, trash, restore, update, job, library, admin or other-user request, is refused before it is sent (`allowed` in the script, held by `tests/test_spike_immich_live.py`).
- It reads only the key's own user, the assets it uploaded itself (found by their IDs or their checksums) and, before starting, the file names of up to 100 of the user's assets, to check the user is empty. Every file it uploads is named `darkroom-spike-<run>-<label>.<ext>`, and every sidecar's description carries the same marker.
- It refuses to start, before any upload, when a key's grants are not exactly `user.read`, `server.about`, `asset.read`, `asset.download` and `asset.upload` (so `all`, `asset.delete`, `asset.update` and any admin grant are refused), when the key's user is an admin, or when the user already has an asset whose name lacks the marker (not an empty disposable user).
- It never prints or writes a key or the server's address: both are replaced by `[redacted]`, and the result names only the kind of address. User IDs are recorded only as hashes. Keys come only from environment variables.

## Prerequisites

1. An Immich server: a disposable instance, or a server where two **empty disposable non-admin users** exist for this, with no partner sharing and no shared or external library. Never a person's real account.
2. In Immich, signed in as each disposable user, an API key with exactly these permissions: `user.read`, `server.about`, `asset.read`, `asset.download`, `asset.upload`. Nothing else.
3. A checkout of this repository with `make setup` done (Python 3.15 and Poetry; the script uses Pillow from the project's dependencies).
4. Optional: a third disposable user whose storage quota is set as small as Immich's form allows, 0 if it accepts it (Administration, Users, edit, Quota size), and a key for it with the same five permissions, for the quota row; and a key created then deleted in Immich (revoked), for the revoked-key row.

## Running it

On a machine that reaches the server (a made-up address shown):

```
export IMMICH_URL=http://192.168.1.20:2283
export IMMICH_LIVE_KEY_A=...        # first disposable user's key
export IMMICH_LIVE_KEY_B=...        # second disposable user's key (optional, see below)
export IMMICH_LIVE_QUOTA_KEY=...    # optional
export IMMICH_LIVE_REVOKED_KEY=...  # optional
poetry run python -m tools.spikes.immich_live --out immich-live-result.md
```

`IMMICH_URL` is `scheme://host[:port]` only. A private, LAN or loopback address is accepted as is. **A public address is refused unless `--allow-public-address` is given, and then only over https. Use that flag only for a production server with the disposable accounts above**, never for one where the keys could belong to a real person's library.

The run takes a few minutes: most of it is waiting for Immich's metadata extraction (each wait is bounded by `--extraction-timeout`, 180 seconds by default). The large-video test is off by default; `--large-video-mb 64` turns it on with a generated video of about that size (at most 256).

### From a session behind a credential-injecting proxy

Where an outbound proxy adds the real key to each request (as in a Claude Code cloud session), set `IMMICH_LIVE_KEY_A` to any placeholder such as `proxy-injected`. The script does not check the key's form; the grants check uses the server's own answer from `GET /api/api-keys/me`. It goes through `HTTPS_PROXY` and verifies TLS with the proxy's CA bundle (`SSL_CERT_FILE`, or `CURL_CA_BUNDLE`/`REQUESTS_CA_BUNDLE` when that is unset); verification is never turned off. Leave the quota and revoked-key variables unset there: the proxy would replace those keys too.

## One key at a time, and the follow-up phases

1. With only `IMMICH_LIVE_KEY_A`, the two-account rows (same bytes owned by two users, owner binding) say `not run: needs second key`.
2. To fill them, run again with the **second** user's key as `IMMICH_LIVE_KEY_A`, appending to the first result: `poetry run python -m tools.spikes.immich_live --phase second-account --append-results immich-live-result.md --out immich-live-result-2.md`. It uploads the first run's shared image again as the second user, checks the new asset is that user's own, and checks the second user cannot read the first user's copy. The server version must match the first run's.
3. With both keys set in one run (and not resolving to the same user), these rows run at once. Each run prints the IDs and file names of the assets it created; the result lists all of them.
4. The trashed-duplicate row needs a trashed asset, and the harness never trashes. After the main run, trash the asset named `darkroom-spike-<run>-trash-me.jpg` in the web UI, signed in as the first user, then run `--phase after-trash --append-results <latest result> --out <new result>` with the first user's key. It uploads the same bytes again and records what the bulk check and the upload answer, and whether the trashed asset stayed trashed.

## Reading the result

The table has one row per item of #266's "Required evidence", each `pass`, `fail`, `not verified` or `not run` with what was observed: the server version and the exact grants are listed above it. `not run` rows say why; partner-visible, offline/external-library, edited representations and existing-asset enrichment need access this harness refuses by design and stay open in #266. A `pass` on the extraction row records the values Immich reported, including which date won when EXIF and XMP disagree; a duplicate's row records whether the new sidecar changed anything. A row passes only on the specific evidence it names: a timed-out extraction fails, and an answer other than the expected one is `not verified` with the status shown. The cross-user read must be Immich v3.3.6's access denial, `400 Not found or no asset.read access` ([`utils/access.ts`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/utils/access.ts)), and the quota row needs `400 Quota has been exceeded!` ([`requireQuota`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/asset-media.service.ts)); a 500, a proxy limit or a broken connection is not taken for either. The exit status is 0 when every row that ran passed, 1 when one failed or was not verified, and 2 when the run was refused.

Paste the whole result file, including its `json immich-live-data` block (which `--append-results` reads), into a comment on #266. Check it first: it should contain no key, address, name or real file.

## Cleaning up

The harness never deletes. Signed in as each disposable user in Immich's web UI, search for `darkroom-spike` (or use the created-assets list in the result), select those assets, delete them and empty that user's trash. Then delete the API keys, and the disposable users themselves under Administration, Users, once the evidence is recorded.
