# Owner runbook: finishing the live Immich evidence for #266

[#266](https://github.com/AnthonyPluth/Darkroom/issues/266) is the release gate for Immich-managed storage ([design](../design/immich-managed-storage.md)). The owner's live spike on 2026-11-17 (Immich v3.4.1, [result](../design/immich-managed-storage.md#live-spike-result-2026-11-17-immich-v341)) passed 12 rows and left 7 not run. The issue also needs a restore test and the owner's acceptance of remote deletion races before inbox cleanup can be enabled. This page says, for each of those, what the owner must provide, what to run, what to record and what to paste on #266.

**No result is recorded here.** Every row below is still open until the owner pastes evidence on #266 and it is folded into the design. Nothing on this page authorises tests against production media or a person's real account: use a disposable instance, or empty disposable non-admin users, with generated media only. The repository is public, so before pasting replace every address, host name, user name and file name with the words this page uses ("the disposable user", "a made-up capture"), and paste no key.

## What can be run from this repository, and what cannot

`tools/spikes/immich_live.py` ([how it runs](immich-live-run.md)) sends only `GET /api/server/about`, `GET /api/users/me`, `GET /api/api-keys/me`, `POST /api/assets`, `POST /api/assets/bulk-upload-check`, `POST /api/search/metadata` and `GET /api/assets/{id}` (and its `/original?edited=false`), and refuses every other request, an admin user and any key whose grants are not exactly `user.read`, `server.about`, `asset.read`, `asset.download` and `asset.upload`. With `--enrich` (row 4, below) it also reads an asset's thumbnail and its album list, and the all-permission keys of that run send one more request, `PUT /api/assets/{id}`, and nothing else that changes anything. That decides how each row below is closed:

| # | Row | Closed by |
| --- | --- | --- |
| 1 | Partner-visible assets | Not runnable with the harness (it never reads another user's asset and the disposable users have no partner sharing). Needs a reviewed harness extension |
| 2 | Offline or external-library duplicate versus managed upload | Not runnable with the harness (needs an admin key, which it refuses). Needs a reviewed harness extension |
| 3 | Edited representations | Not runnable with the harness (needs an edit in Immich's editor and `asset.update`, which it refuses) |
| 4 | Existing-asset metadata enrichment (release gate of its own path) | Harness, `--enrich`, rows E1 to E18 of [the enrichment design](../design/existing-asset-enrichment.md#16-live-spike-rows-to-add-on-266); E7, E8, E11, E12 and E17 need the owner's hand, see [Row 4](#row-4-existing-asset-enrichment-e1-to-e18) |
| 5 | Large video and async extraction delay | Harness, `--large-video-mb` |
| 6 | Quota error | Harness, `IMMICH_LIVE_QUOTA_KEY` |
| 7 | Revoked-key error | Harness, `IMMICH_LIVE_REVOKED_KEY` |
| 8 | Restore of Immich and Darkroom | Owner's rehearsal, from the deployment docs |
| 9 | Acceptance of remote deletion races | Owner's decision, recorded on #266 |

Rows 1 to 3 are not something to improvise around. The repository has no command that closes them, and this page invents none. Either ask for the extension as its own issue (see [Rows 1 to 3](#rows-1-to-3-need-a-reviewed-extension-first)), or record what the Immich web UI shows and say plainly in the report that it is a UI observation and not an API result.

## Before anything: the common setup

1. An Immich server: a disposable instance, or a server with **empty disposable non-admin users** and no real person's account involved. Note its version with `GET /api/server/about` as the harness reports it (the result's first lines); do not rely on the image tag.
2. A checkout of this repository on a machine that reaches the server, with `make setup` done (Python 3.15, Poetry).
3. Per disposable user, signed in as that user in Immich: an API key with **exactly** `user.read`, `server.about`, `asset.read`, `asset.download`, `asset.upload`. Nothing else. Keys go into environment variables only, never into an issue, chat or file.
4. `IMMICH_URL=scheme://host[:port]`. A LAN or loopback address is accepted as is. A public address is refused unless `--allow-public-address` is given, and then only over https; use that only for a server where the keys belong to the disposable users above.
5. If a WAF answers 403 with Cloudflare error 1010, allow the User-Agent `immich-spike/1.0` (or the runner's address) first.

## Rows 5, 6 and 7: one run

Set up, in Immich, signed in as the owner of each account:

- **Quota (row 6):** a third disposable user whose storage quota is as small as the form allows, 0 if it accepts it (Administration, Users, edit, Quota size), and a key for that user with the same five permissions.
- **Revoked key (row 7):** a key for any disposable user with the same five permissions, created and then deleted in Immich, so it is revoked.
- **Large video and delay (row 5):** nothing extra in Immich. If the server is reached through a proxy or a Cloudflare-proxied hostname, run it over the route the phones and Darkroom will really use: Cloudflare's Free and Pro plans cap request bodies at 100 MB ([runbook](../runbook.md)), and that limit is part of what this row checks.

The access to hand the runner: the server address, the disposable users' keys, the quota user's key, the revoked key, and (for the proxy limit) the intended route.

Run, from the repository root (made-up address shown; the first key is the one for the empty disposable user, and a second key is optional but fills the two-user rows again):

```
export IMMICH_URL=http://192.168.1.20:2283
export IMMICH_LIVE_KEY_A=...
export IMMICH_LIVE_KEY_B=...
export IMMICH_LIVE_QUOTA_KEY=...
export IMMICH_LIVE_REVOKED_KEY=...
poetry run python -m tools.spikes.immich_live --large-video-mb 64 --out immich-live-result-3.md
```

Notes taken from the harness and its page:

- `--large-video-mb` generates a video of about that size, at most 256. Start with 64 as the harness page does; to test the real limit of your route, run again with a size above it (for example past the 100 MB proxy cap) and say which size passed and which failed. Each wait for metadata extraction is bounded by `--extraction-timeout` (180 seconds by default); raise it if the row fails by timing out and say so.
- The quota and revoked-key keys are read only in the main phase. Behind a credential-injecting proxy leave both unset, because the proxy would replace those keys too.
- Exit status: 0 when every row that ran passed, 1 when one failed or was not verified, 2 when the run was refused.
- The quota row passes only on `400 Quota has been exceeded!` with the bytes not stored; the revoked-key row needs the server's refusal, not a proxy limit or broken connection. Any other answer is `not verified` with the status shown.
- A new main run uses a new run ID and uploads new generated bytes. The disposable users must still hold only assets named `darkroom-spike-...`.

**Record for each of the three rows:** the server version; the result file's row (`pass`, `fail`, `not verified` or `not run`) and its observation text; for row 5 the generated size, the route (LAN, Tailscale, proxied hostname), the time extraction took, whether the original's SHA-1 and size matched and whether duration was read in milliseconds as before; for row 6 the quota set and the answer; for row 7 the answer. Paste the whole result file including its `json immich-live-data` block after checking it contains no key, address, name or real file.

## Rows 1 to 3 need a reviewed extension first

The design ([section 2](../design/immich-managed-storage.md#live-spike-required-before-enabling-imports)) names what each must show. None of rows 1 to 3 can be produced by the current harness, and adding a request type to the harness or the provider is a reviewed change of its own, not a flag.

| Row | What the owner must provide | What the design needs shown | What to record |
| --- | --- | --- | --- |
| 1. Partner-visible assets | Two disposable users with Immich's partner sharing turned on from one to the other (the setting is in Immich's own account settings), a generated asset owned by the sharing user, and the receiving user's key with the five grants | Ownership stays separate; no import, match, receipt or cleanup is authorised by another owner's asset | The asset's owner as the receiving user's key sees it, its `libraryId`, and whether the receiving user can read or download it |
| 2. Offline or external-library duplicate | A disposable Immich instance (not a server holding anyone's real library) with an external library over a folder of generated media, an admin user to create it, and a non-admin disposable user whose key has the five grants | Whether uploading the same bytes creates a usable managed asset or returns the external one; an external asset is never counted as migrated; plus what bulk check says (it does not filter external libraries) and how an offline asset (file removed from the library folder, rescanned) is reported | The upload status and ID, whether the result has `libraryId: null`, the bulk check's `action` and `reason`, and the offline asset's `isOffline` |
| 3. Edited representations | A disposable user, a generated image, an edit made in Immich's own editor, and a key with the permission the extension needs | That the unedited download (`/original?edited=false`) still hashes to the input and that an edit does not change the original's identity or checksum | SHA-1 and size of `edited=false` against the input; the default download for comparison; the asset's checksum before and after |

**To get a harness for these**, open a follow-up issue from the "Agent task" template that names the rows, says which read-only requests the harness may send, keeps the disposable-user and no-delete refusals, and updates `allowed` in `tools/spikes/immich_live.py` and its test `tests/test_spike_immich_live.py`. Until that exists, rows 1 to 4 remain open on #266 and the managed import stays disabled. If you record a UI-only observation meanwhile, label it as that: it does not satisfy the rows.

## Row 4: existing-asset enrichment, E1 to E18

The owner's reversal of 2026-11-24 on #548 brings enrichment back as [a design](../design/existing-asset-enrichment.md) that gates every code slice from the second on until these rows are recorded on #266 (section 16 there). Run them only on **disposable non-admin users with generated media**, on the version the app will enable (v3.4.1). Nothing here is part of an import: `--enrich` runs alone, never with the upload rows, the large video or another phase.

**What the owner provides, in Immich, signed in as each disposable user:**

- The import key of the first user (the five grants, as above): the harness uploads each generated fixture and downloads its unedited original with it, so the update is judged by a different key than the one that sent it.
- `IMMICH_LIVE_ENRICH_KEY`: a key of the **same** user with **all permissions** (the form the app will hold). An admin user's key is refused.
- `IMMICH_LIVE_ENRICH_KEY_2`: an all-permission key of a **second** disposable user, for E13 (optional).
- `IMMICH_LIVE_ENRICH_READ_KEY`: a key of the first user with only read grants, for E10 (optional; a key holding `all` or `asset.update` is refused).
- `IMMICH_LIVE_REVOKED_KEY`: a key created and then deleted, for E14 (optional, as in rows 5 to 7).

An all-permission key could delete, so the harness sends no delete, trash, restore, job, bulk-update or key request, whatever a row asks (its allowlist and `tests/test_spike_immich_live.py` hold that), and only a key made for `--enrich` ever sends `PUT /api/assets/{id}`.

```
export IMMICH_URL=http://192.168.1.20:2283
export IMMICH_LIVE_KEY_A=...
export IMMICH_LIVE_ENRICH_KEY=...
export IMMICH_LIVE_ENRICH_KEY_2=...
export IMMICH_LIVE_ENRICH_READ_KEY=...
poetry run python -m tools.spikes.immich_live --enrich --out immich-live-enrich.md
```

Add `--append-results <earlier result>` to merge the E rows into an earlier result of the same disposable user, `--settle-seconds` for how long E4 waits before it reads the edited dates again (600 by default: the design asks for ten minutes), and `--extraction-timeout` as before. The result lists the all-permission keys' grants **as Immich lists them** (E1), never a key.

**Rows the harness cannot run, and what the owner does:**

| Row | Owner's step | What to record |
| --- | --- | --- |
| E7 | In Immich's own interface edit an asset's description in the moment between a read and the update, by timing | Whether anything signals the conflict (a changed `updatedAt`, a precondition) |
| E8 | After an update, look at the instance's storage for a sidecar beside the original, for an asset uploaded with a sidecar whether it merges, then have an admin run "extract metadata" once from the UI | That a sidecar is written, that the values survive extraction, that the original's checksum is unchanged, and what extraction does to `localDateTime`, `dateTimeOriginal` and `fileCreatedAt` |
| E11 | Move an asset to the locked folder by hand, then run the update on it | Status and message |
| E12 | Trash an asset by hand, then run the update on it | Whether it is accepted (Darkroom refuses it regardless) |
| E17 | Have an admin pause Immich's sidecar job queue, then run an update | Whether the read-back still shows the fields |

E13's partner-shared half and E18's asset-in-an-album half also need the owner's hand (partner sharing; an album made in the UI). **Record for each row** the version, the row's result and its text, and that no key, address or file name is in what is pasted. E4, E5, E8 and E16 gate the date and place fills, and until the owner records them on #266 only the description fill ships. A `fail` row stays a finding: it does not mean the harness is wrong.

## Row 8: the restore test

The issue asks for a successful restore of Immich's database and media and of Darkroom's database and secrets, a backup recovery point inside the retention window, and, from the design ([section 6](../design/immich-managed-storage.md#6-replacement-safety-promises-and-checks)), a restored sample uploaded before the backup with its metadata and Live Photo link. This spike is not that evidence, and an earlier rehearsal is not either: [S5a](restore-app-browser.md) records that its rehearsal was never done.

Do it on a disposable copy, never over the production Immich or Darkroom.

**What the owner provides:** a disposable Immich instance (or a restored copy of the real one on separate storage), a disposable Darkroom one container with its appdata, and the means Immich's own backup uses on this install. Darkroom has no code for Immich's backup; follow Immich's documented backup and restore for the version in use and name the method in the report.

**Steps**

1. On the disposable Immich, upload generated media with the harness (rows above), including a Live Photo pair and a sidecar-dated image; note the asset IDs the harness prints and the time.
2. On the disposable Darkroom, set up what a restore must bring back and write it down: a person with a stored Immich key (a disposable user's key with the five grants, entered the way the app asks), one sign-in session (stay signed in in one browser window), and the stored settings you set. If the Darkroom has a Takeout import and any legacy import done for that person, the reconcile in step 9 can run; say if it cannot.
3. Take Immich's database and media backup by the method you will use in production, and note the recovery point's time. It must be within the approved retention window (14 days, [design section 4](../design/immich-managed-storage.md#4-what-remains-on-darkrooms-disk)) of what it covers. Then upload one more generated asset with the harness and note its ID: it is newer than the recovery point and is expected to be gone after the restore (record that it is).
4. Take Darkroom's backup as [docs/deploy/one-container.md](../deploy/one-container.md#backup-and-restore) says: stop the container, back up the appdata folder (database, `secret-key`, browser profiles together). The photo share (`_inbox/`, `originals/`, `google-only/`) is not part of it. With `DARKROOM_SECRET_KEY` set, keep that key with the backup, matched to the same time. **On PostgreSQL** (`DATABASE_URL` set) also take a `pg_dump` at the same stopped point ([postgres.md](../deploy/postgres.md)) and keep the `secret-key` file with it.
5. Destroy the disposable Immich database and media, and Darkroom's appdata. **On PostgreSQL** also drop the Darkroom database.
6. Restore Immich by its method.
7. Restore Darkroom: stop the container, put the appdata folder back (owner 99:100). **On PostgreSQL** restore the `pg_dump` into a fresh, empty disposable database (not the one dropped in step 5 under the same name by accident, and never production), set `DATABASE_URL` of the restored Darkroom to that database, and restore the `secret-key` file (or set `DARKROOM_SECRET_KEY`) from the same backup point. Start it and confirm it serves.
8. Run `python run.py check-config` on the restored Darkroom. It says where the key comes from and reports stored settings and a missing key, **but it does not prove the key can open the stored Immich keys or sessions**: `secret_key.missing_key_problem` accepts any non-empty key and the check reads only the stored settings, so a wrong key can pass it. Treat a clean `check-config` as necessary, not as the recovery check.
9. Check that the restored key really opens a sealed value, with what the repository offers: `python run.py reconcile-immich --person <slug>` unseals that person's stored Immich key and calls Immich with it ([runbook](../runbook.md), the `immich.reconcile` job). A key that cannot open it fails with "the stored Immich key cannot be read with DARKROOM_SECRET_KEY". Run it only against the disposable Immich, because it also writes into the disposable Darkroom share. It skips, without saying so, a person whose Takeout (and legacy import, if set) is not done; a run that prints no line for the person proves nothing. For the sealed sign-in session there is no command: the owner observes that the window signed in before the backup is still signed in, and that a new sign-in works, and records that this is an observation. If neither can be done, record the sealed values as not checked.
10. Compare the restored Darkroom with the state written down in step 2: the person, the Immich key present (the app's people page shows whether one is stored; it never shows the key), the stored settings, the session. Anything missing or different is a finding, not a pass.
11. On the restored Immich, check the sample: the assets from step 1 exist with the same IDs and owners, `libraryId` null, originals whose SHA-1 equals the input (the restored Immich's own download of the unedited original, compared with the generated file), date, offset and GPS still as before, and the Live Photo still linked to its hidden video. Check the disposable users' API keys still work (a key revoked after the recovery point comes back with the restored database, so note any that did). Confirm the asset from step 3 is absent.

**Record:** the Immich version and the Darkroom release tag; the database (SQLite or PostgreSQL) and the backup method for each system; the recovery point's time and its age against the 14-day window; for each of steps 7 to 11 pass, fail or not checked with the observed text, and for step 9 which of the two checks was done; what was missing or different after the restore; time to restore. If the Darkroom key was supplied from outside appdata, say so, and whether `serve` refused without it.

## Row 9: owner acceptance of remote deletion races

This is a decision, not a test. The design (section 4) says Darkroom's write lock does not lock Immich: another Immich client or user can remove, trash or change an asset after Darkroom's last read and before its cleanup, and rechecking narrows that window without closing it. After the 14-day retention nothing independent of Immich holds the photo. The owner must say, on #266, that they accept this boundary, or that they do not. Nothing in the repository can answer it, and it is not implied by earlier approvals of the retention period.

## What happens next

The owner pastes the report below on #266, one comment per batch of rows. A pull request then folds the recorded rows into the design's live-spike table and keeps the rest open. Imports, uploads, inbox cleanup and migration stay disabled until the four items under [Before imports can be enabled](../design/immich-managed-storage.md#before-imports-can-be-enabled) are done: all seven rows, the grants decision, the restore evidence and the accepted deletion races.

## Cleaning up

The harness never deletes. Signed in as each disposable user in Immich's web UI, search for `darkroom-spike`, select those assets (or use the created-assets list in the result), delete them and empty that user's trash. Then delete the API keys, and the disposable users, once the evidence is recorded. Remove any external library, partner sharing and disposable instance created for rows 1 to 4 and 8.

## Report-back template for #266

Paste this, one block per row, with `<...>` filled in. A row that was not run says `not run` and why. Replace every address, host name, user name and file name first; paste no key.

```text
Live Immich evidence for #266 (docs/spikes/immich-owner-runbook.md)
Immich version (from the harness result), date, who ran it: <v...>, <date>, <who>
Instance: <disposable instance / disposable users on a server>; route to it: <LAN / Tailscale / proxied hostname>
Keys: exactly user.read, server.about, asset.read, asset.download, asset.upload for <n> disposable non-admin users; extra grants used for any row: <none / which, and why>

Row <1-9>: <name>
Status: <pass / fail / not verified / not run, and why>
How: <harness command and flags / UI observation (say so) / owner's decision>
Observed: <the result row text, or the answers, statuses and values the runbook says to record>
Evidence pasted: <result file with its immich-live-data block, checked for keys, addresses, names and real files / none>
Cleanup done: <assets, trash, keys, users, external library, partner sharing, instance>

Restore (row 8), if run:
Immich backup method and recovery point time, and age against the 14-day window: <...>
Darkroom backup method (SQLite appdata / PostgreSQL dump) and key source (generated file / DARKROOM_SECRET_KEY): <...>
Restore of Immich, restore of Darkroom (and of the pg_dump into a fresh database, if PostgreSQL), check-config (necessary, not proof), key check (reconcile-immich output, or session observation, or not checked), state compared with before, sample asset check (IDs, owners, libraryId null, SHA-1 of original, date/offset/GPS, Live Photo link): <pass / fail each, with what differed>

Remote deletion races (row 9): <accepted / not accepted>, <date>, <any condition>
```
