# Recovery-point gate for managed cleanup

Design for [#545](https://github.com/AnthonyPluth/Darkroom/issues/545) (part of the epic [#530](https://github.com/AnthonyPluth/Darkroom/issues/530)), prepared 2026-11-24. **Draft for the owner's review; no code yet.** It fills in the gate that [`immich-managed-storage.md`](immich-managed-storage.md) (sections 4, 8, 9 and 10) requires before Darkroom may delete anything an Immich-managed import left in the inbox. Until the slices in section 9 land and the owner records what section 6 asks for, cleanup keeps every zip, held copy and review hold of a managed export, as it does today.

## 1. Where things stand

An Immich-managed import is an export with a `managed_import_intents` row of kind `import` (#533). Its Takeout-only photos go to Immich through `managed_upload` and `managed_metadata`; nothing goes into `google-only/`. After that, Darkroom's inbox is the only copy outside Immich, and the approved target (#259) keeps it for 14 days after the export completes, then lets it go. Deleting it is safe only if Immich, and a backup of Immich, still hold every photo the inbox copy stood for.

What the code does today:

| Where | What it does for a managed export |
| --- | --- |
| `darkroom/domain/ingest/cleanup.py`, `clean_export` | `place.managed_import(...)` is true, so it counts the export as kept and records one `cleanup_kept` event with `managed_ingest.KEPT_ONLY` ("cleanup of an Immich-managed import stays off until the owner records the restore rehearsal (#266)"). Nothing is removed. |
| `cleanup.py`, `clean_holds` | `managed_ingest.hold_pinned(review)` is true, so the review hold is kept. |
| `darkroom/domain/ingest/part_free.py`, `shared_checks` | `managed(export)` raises `Refused("managed")`, so phase 2 frees neither a part's zip nor a held copy. |

History on PR #541 (commits d37730d and 05c8df0) had a first version of the gate: `managed_ingest.RecoveryPoint(taken_at, healthy, assets)`, `managed_ingest.Retention(rehearsed_at, boundary_accepted_at, recovery_point, reader)` and `release_refusal(opened, export, now, retention)`, which refused, in order: no rehearsal, no accepted boundary, a receipt not committed, no healthy recovery point within 14 days, an asset the point does not list or committed after it, and an asset whose fresh read failed or whose checksum differed. `tests/test_managed_ingest.py`'s `RetentionTests` had a negative for each. It was taken out of #533 because three things had no answer: where the rehearsal and the acceptance live (they were constructor arguments with no storage), what produces a `RecoveryPoint` (a callable no code implemented), and when the Immich read happens (`clean_export` called `release_refusal` before taking the write lock, and only the per-part path called it again under the lock, once per export rather than before each removal). This design keeps that refusal order and its reasons, and answers the three.

## 2. What the gate protects, and what it cannot

The gate lets a file in a managed export's inbox go only when, at the moment of the delete:

- every photo the file stands for is in the person's Immich library as a managed original (same owner, `libraryId: null`, not trashed, not offline, same checksum), read again under Darkroom's write lock;
- each of those assets was committed before a recovery point began, and that recovery point is a successful backup of Immich's database and media and of Darkroom's database and secret key, recent enough and still kept by the backup tool;
- the most recent backup did not fail;
- the owner has recorded a restore rehearsal and has accepted the remote-deletion boundary.

It cannot make Immich and Darkroom atomic. Another Immich client can trash or delete an asset after Darkroom's last read and before the unlink; the write lock coordinates Darkroom's own jobs only. The fresh read under the lock narrows that window to the few milliseconds between the read and the unlink, and the recovery point is what covers it: an asset removed in that window is still in a backup Darkroom has evidence for. That is the boundary the owner is asked to accept (section 6). The gate also cannot check that a backup really restores; it relies on the owner's rehearsal and on the backup job's own report of success.

## 3. Data model

One new migration, numbered after main's newest when the first slice lands (0052 at the time of writing), with its own `test_<revision>_…` in `tests/test_migrations.py`, on SQLite and PostgreSQL. It adds tables and nullable columns only; no existing row changes. Times are UTC epoch seconds (`Float`), as elsewhere.

### `retention_attestations`: what the owner records

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | integer, primary key | |
| `kind` | string, one of `restore_rehearsal`, `remote_deletion_boundary` | |
| `recorded_at` | float, not null | When the command ran (from `dates.py`) |
| `evidence_url` | string, not null | The #266 comment that holds the report or the decision. Checked to be exactly `https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-<digits>` |
| `rehearsed_on` | string (`YYYY-MM-DD`), null for the boundary | The day of the rehearsal, as the report says |
| `immich_version` | string, null for the boundary | The Immich version restored, which must be in `MANAGED_VERSIONS` when recorded |
| `darkroom_version` | string, null for the boundary | The Darkroom release restored |
| `database_kind` | string, one of `sqlite`, `postgresql`, null for the boundary | Which Darkroom database the rehearsal restored |
| `withdrawn_at` | float, null | Set by `withdraw`; a withdrawn row never counts again |

Constraints: a check that a `restore_rehearsal` row has all four rehearsal columns and a boundary row none; a partial unique index on `kind` where `withdrawn_at IS NULL` (`sqlite_where` and `postgresql_where`), so at most one active row of each kind. Rows are never deleted; a new record withdraws the old one in the same transaction.

### `recovery_points`: what the backup job reports

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | integer, primary key | Printed by `begin`, passed back to `finish` |
| `opened_at` | float, not null | When `begin` ran; every covered asset was committed no later than this |
| `state` | string, one of `open`, `succeeded`, `failed`, `abandoned` | |
| `manifest_count` | integer, not null | Rows in `recovery_point_assets` |
| `manifest_sha256` | string(64), not null | SHA-256 over the sorted `(owner_id, asset_id, sha1)` lines, so a changed manifest is detectable |
| `finished_at` | float, null | When `finish` ran |
| `taken_at` | float, null | When the backup tool says its snapshot was taken; must satisfy `opened_at <= taken_at <= finished_at` |
| `kept_until` | float, null | `taken_at` plus the days the backup tool keeps this snapshot, as `finish` reports it; the point stops counting after it |
| `scopes` | string, null | Sorted comma list from `immich-db`, `immich-media`, `darkroom-db`, `darkroom-secret`; the gate counts a point only when all four are there |
| `tool` | string, null | A short label for the backup tool (`[a-z0-9-]{1,32}`); display only |
| `snapshot_ref` | string, null | The tool's snapshot identifier (`[A-Za-z0-9._:-]{1,128}`, never a path); display only, never logged |
| `failure` | string, null | A fixed code: `reported_failed`, `scopes_missing`, `not_finished` |

Checks: `succeeded` needs `finished_at`, `taken_at`, `kept_until` and `scopes`; `open` has none of them; `failed` and `abandoned` have a `failure`. Index on `(state, opened_at)`.

### `recovery_point_assets`: the manifest

| Column | Type | Meaning |
| --- | --- | --- |
| `recovery_point_id` | foreign key, `ON DELETE CASCADE` | |
| `owner_id` | string, not null | The Immich user |
| `asset_id` | string, not null | |
| `sha1` | string(40), not null | Hex, as receipts store it |
| `source` | string, one of `receipt`, `version` | A committed `managed_import_receipts` row, or a `managed_asset_versions` row with `bytes_verified_at` (a keeper a tier 1 to 3 decision relies on, or one the backfill mapped) |

Primary key `(recovery_point_id, owner_id, asset_id)`. Manifest rows of a point are deleted (rows only, never a file) once its `kept_until` has passed and a newer point has succeeded; the point row itself stays for the audit.

### Release journal columns

- `export_parts.release_point_id` and `takeout_entries.held_release_point_id`, nullable foreign keys to `recovery_points`: which point authorised a managed release. The existing `free_state` and `held_state` journals (`freeing`, then `freed`) are reused unchanged, so a crash finishes or refuses on the next run exactly as phase 2 does.
- `review_items.hold_free_state` (null, `freeing`, `freed`) and `review_items.hold_release_point_id`: the same journal for a managed review hold, which today is removed without one.

## 4. The proof

A **release unit** is one file of a managed export: a part's zip, a held copy, or a review hold. Its **dependency set** D(U) is the Immich assets it stands for:

- a zip: for each media entry in the part, following a copy to its root: a tier 5 item's committed receipt; an automatic, unreversed tier 1, 2 or 3 decision's keeper, which must be a verified managed asset version of the same owner (a keeper that is only a file in `google-only/` or `originals/` and not yet mapped by the backfill has no managed version, and pins the zip); an item held for review contributes its review hold, which must still be present (the zip goes before or after the hold, never instead of it); an item the owner skipped by decision contributes nothing;
- a held copy: its root's disposition, as for a zip entry;
- a review hold: if resolved Keep both, the receipt of the kept copy; if resolved for the other copy, that keeper's managed version; if still pending, it is not a candidate.

Conditions, checked in this order. The first that fails is the refusal, recorded with its fixed reason; nothing later runs.

| # | Condition | Fixed reason when it fails | Negative test |
| --- | --- | --- | --- |
| G1 | The export is `imported`, its last run is `done`, and its completion (`cleanup.imported_at`) is at least 14 days ago | `retention_clock` (not recorded: not yet a candidate) | `test_a_managed_zip_is_not_a_candidate_before_14_days` |
| G2 | The managed release setting is on (section 9, slice D) | `release_off` | `test_the_setting_off_keeps_every_managed_file` |
| G3 | An active `restore_rehearsal` attestation exists | `no_rehearsal` (keeps `KEPT_ONLY`'s wording) | `test_no_rehearsal_keeps_the_zip`, `test_a_withdrawn_rehearsal_keeps_the_zip` |
| G4 | An active `remote_deletion_boundary` attestation exists | `boundary_not_accepted` | `test_no_accepted_boundary_keeps_the_zip`, `test_a_withdrawn_boundary_keeps_the_zip` |
| G5 | Every receipt the export needs is `committed` and none is held (`managed_ingest.uncommitted` is false; no `metadata_hold`) | `receipt_not_committed` | `test_a_metadata_verified_receipt_keeps_the_zip`, `test_a_missing_receipt_keeps_the_zip`, `test_a_held_receipt_keeps_the_zip` |
| G6 | Every member of D(U) is known: a committed receipt or a verified managed version of the intent's owner; a tier 1 to 3 keeper with no managed version, a failed entry or an unknown entry pins it | `keeper_not_managed` | `test_a_tier_1_keeper_only_in_google_only_keeps_the_zip`, `test_a_failed_entry_keeps_the_zip`, `test_another_owners_version_keeps_the_zip` |
| G7 | Metadata handled: a receipt is committed (so `metadata_verified`); for a tier 1 to 3 keeper, every field `managed_metadata.intend` would carry from Google's sidecar (date, offset, place, description) is what Immich already shows, from the fresh read of G12 | `metadata_not_in_immich` | `test_a_keeper_missing_googles_place_keeps_the_zip`, `test_a_keeper_with_every_field_lets_it_go` |
| G8 | The latest recovery point that is not `open` is `succeeded`, and no point has been `open` longer than `OPEN_LIMIT` | `backup_failed` | `test_a_failed_latest_backup_pauses_cleanup_even_with_an_older_good_point`, `test_an_abandoned_point_counts_as_failed`, `test_a_begin_that_cannot_reach_immich_after_an_older_success_pauses_cleanup_and_opens_the_alert` |
| G9 | A `succeeded` point R exists with all four scopes, `now - 14 days <= R.taken_at`, and `now < R.kept_until` | `no_recovery_point` | `test_no_point_keeps_the_zip`, `test_a_point_older_than_14_days_keeps_the_zip`, `test_a_point_past_kept_until_keeps_the_zip`, `test_a_point_missing_darkroom_secret_keeps_the_zip` (one per scope) |
| G10 | R's manifest lists every member of D(U) with the same owner, asset ID and SHA-1, and each member was committed (receipt `committed_at`, version `bytes_verified_at`) no later than `R.opened_at` | `not_covered` | `test_an_asset_missing_from_the_manifest_keeps_the_zip`, `test_an_asset_committed_after_begin_keeps_the_zip`, `test_a_manifest_row_with_other_bytes_keeps_the_zip` |
| G11 | The file proof of phase 2 holds, re-run under the write lock: ledger and listing, identities, every entry accounted for, the account's import lock held, no earlier export waiting, no undo or reassignment in progress, no claim left over (`part_free.prove_part` and `prove_held`, with the managed export's "placed" read as G5's committed receipt instead of a `google-only/` file) | the existing `part_free` reasons (`hash_differs`, `not_export_folder`, …) | the existing `tests/test_part_free.py` negatives, run once more with a managed export |
| G12 | Fresh Immich read under the write lock, right before the unlink: the person's key passes `check_access` (exact grants, non-admin, supported version) once for the unit, then `ManagedReader.custody` for every member of D(U) answers owner = the intent's owner, `libraryId` null, not trashed, not offline, `sha1` equal, a Live Photo still linked to its video; all within `READ_DEADLINE` | `immich_unconfirmed` (any refusal, error or timeout) | `test_a_trashed_asset_keeps_the_zip`, `test_an_offline_asset_keeps_the_zip`, `test_a_changed_checksum_keeps_the_zip`, `test_an_asset_moved_to_another_owner_keeps_the_zip`, `test_an_external_library_asset_keeps_the_zip`, `test_immich_down_keeps_the_zip_and_stops_the_tick`, `test_an_admin_or_extra_grant_key_keeps_the_zip`, `test_reads_past_the_deadline_keep_the_zip`, `test_a_change_made_after_the_unlocked_check_is_seen_under_the_lock` |

And one test that only a passing gate lets the file go: `test_only_every_condition_together_removes_the_zip` (from 05c8df0's `test_only_a_passing_gate_lets_the_14_day_cleanup_remove_the_zip`), which also checks that the asset is still in the fake Immich and that the journal names R.

### One release, step by step

This follows `part_free.free_part`'s existing order, with the gate added. Everything runs inside the account's import lock (`run.account_lock`), as phase 2 does.

1. Unlocked: G1 to G10, then G11's first proof with its hashing (`prove_part(..., hashing=False)` and `hash_first`), as a filter. Anything that refuses is kept with its reason and skipped.
2. Journal `freeing` with `release_point_id = R.id` and commit. A crash from here on finds `freeing` (section 7).
3. Take `db.write_lock(opened)` (`part_free.lock`) and, in that one lock hold: re-run G1 to G10 from the database and G11's second proof from the disk, with `require_held=True`, and `confirm` that the files are those hashed in step 1. Anything that changed since step 1 (a reassignment, an undo, a new failed backup, a withdrawn attestation, a different R) refuses here.
4. Still in the same lock hold, G12: the fresh Immich reads of every member of D(U).
5. Still in the same lock hold, remove through the existing removers (`paths.remove_part_zip`, `paths.remove_held_copy`, and for review holds the new `paths.remove_review_hold`, which claims into `.freeing-<hash>/` and checks the inode in the same way), then record `freed` only when `paths.inbox_entry_absent` says so, and commit, which ends the lock.
6. On any refusal in steps 3 to 5: roll back, take the lock again, clear `freeing` and `release_point_id`, record the reason, commit. When G12 failed because Immich could not be reached or the key was refused, the tick stops for this person (Immich being unavailable for one unit means it is for the rest).

On PostgreSQL the write lock is a transaction-scoped advisory lock, so steps 3 to 5 are one transaction with no commit in between: the Immich reads and the unlink happen while no other Darkroom writer can change what step 3 checked.

`READ_DEADLINE` bounds how long the write lock is held for network reads. A zip of about 50 GB can stand for thousands of assets; a read takes tens of milliseconds on a LAN, so one zip can hold the lock for minutes and other jobs wait. When the deadline passes, the unit is kept with `immich_unconfirmed` and the lock is released; a `managed_release_slow` event records the count read. The value is an open question (section 10).

The provider gains one read helper, `ManagedReader.custody_all(asset_ids, deadline)`, that calls `check_access` once and then `custody` for each ID; it issues GET requests only and adds no route to the allowlist. The gate uses a reader with access `read_or_import`, so a person's import key also works and nothing in the gate can upload.

## 5. Recovery points and backup health

### How a recovery point is made

Darkroom has no code for Immich's backup and must never read Immich's storage. The integration is a contract with the owner's backup job, through two commands in `run.py` (inside the container, as the other owner commands are; no HTTP route, so no new kind of credential):

1. `python run.py recovery-point begin` runs before the backup starts. For every person with managed assets it reads each committed receipt and each verified managed version (pure reads, no write lock), confirms it with `custody` as in G12, and writes a `recovery_points` row (`open`) and the manifest rows for the confirmed ones in one transaction. It prints the point's ID and the counts (listed, confirmed, refused). An asset whose read fails is left out of the manifest, not failed: it is simply not covered by this point. If Immich cannot be reached at all, it writes a point already `failed` with `immich_unreachable` and no manifest (so G8 sees the latest attempt as failed, and an older good point cannot cover releases once Immich is back), prints so and exits non-zero, so the backup script can stop.
2. The backup job then backs up Immich's database and media and Darkroom's appdata (database and `secret-key`; with PostgreSQL, a `pg_dump` and the key), in whatever order its tool needs. Since `begin` committed before the Darkroom backup, the restored Darkroom database holds the receipts and the open point.
3. `python run.py recovery-point finish <id> --status succeeded|failed --taken-at <UTC ISO-8601> --kept-for-days <n> --scopes immich-db,immich-media,darkroom-db,darkroom-secret --tool <label> [--snapshot <ref>]` records the outcome. `finish` refuses an ID that is not `open`, a `taken-at` outside `[opened_at, now]`, an unknown scope or a malformed label, and prints why. A `succeeded` report missing a scope is stored as `failed` with `scopes_missing`, so it can never count.

A point left `open` longer than `OPEN_LIMIT` is marked `abandoned` with `not_finished` by the next cleanup tick: a backup that never reported is treated as a failed one.

A worked example for the deployment docs (slice B) shows the hook in a generic shell script: `begin`, stop or snapshot as the tool needs, back up both systems, `finish` with the tool's exit status. It names no real host, share or person.

### Why this proves coverage

An asset is covered by R when it is in R's manifest. That means it was committed in Darkroom's database before `begin` (so the Darkroom backup holds its receipt), and Immich answered for it as a managed original of that owner with that checksum at `begin` (so it existed when the backup started). The backup tool's success report and its retention (`kept_until`) say the snapshot exists and can still be restored; the owner's rehearsal says that kind of snapshot does restore. A photo removed from Immich between `begin` and the snapshot is the remote-deletion race again, and is the reason G12 reads again before every delete.

### When backups fail or go stale

| Situation | What cleanup does | What the owner sees |
| --- | --- | --- |
| The latest finished point is `failed` or `abandoned` | Pauses every managed release (G8), even where an older point would cover | An alert `retention:backup_failed`, opened once, "The last Immich backup Darkroom was told about did not succeed, so Darkroom keeps every Immich import's files in the inbox. Check the backup job." It closes when a later point succeeds |
| No succeeded point inside the window, but units are waiting on G9 or G10 | Keeps them | After `STALE_AFTER` with units waiting, an alert `retention:backup_stale`: "No recent Immich backup covers the photos Darkroom is keeping, so their inbox files stay." Closes when a covering point succeeds |
| `begin` cannot reach Immich | Writes no point; nothing else changes | The command's output; the stale alert follows if it keeps happening |
| A tick's G12 cannot reach Immich or the key is refused | Keeps the unit, stops this person's releases this tick | A `cleanup_kept` event with `immich_unconfirmed`; after a day of that, the existing per-person Immich alert path |
| No rehearsal or no accepted boundary | Keeps everything | One `cleanup_kept` event per export with the fixed reason, as today; no alert (it is the expected state until the owner records them) |
| Disk pressure while files are kept | Unchanged: new downloads pause (design section 4) | The existing disk alert |

Alerts use `alerts.open_alert` with a fixed message and no names, paths or asset IDs. Logs carry counts and point IDs only; `snapshot_ref` and `evidence_url` are never logged. `tests/privacy.py`'s `no_leaks` covers the commands and the tick with canary values in the key, the snapshot reference and the tool label.

## 6. What the owner records, and how

Three things, all by command in the container, each printing what it stored:

1. **The restore rehearsal.** After running row 8 of the owner runbook for #266 (`docs/spikes/immich-owner-runbook.md` on its branch) and pasting the report on #266: `python run.py retention record-rehearsal --evidence <the #266 comment URL> --rehearsed-on <YYYY-MM-DD> --immich-version <vX.Y.Z> --darkroom-version <tag> --database sqlite|postgresql`. It refuses a URL that is not a #266 comment, a future date and an Immich version outside `MANAGED_VERSIONS`.
2. **The remote-deletion boundary.** After saying so on #266 (runbook row 9): `python run.py retention accept-boundary --evidence <the #266 comment URL>`. It prints the boundary from section 2 and stores nothing unless the command is given `--accept`.
3. **The backup hook**, section 5: wired once into the owner's backup job.

`python run.py retention withdraw rehearsal|boundary` withdraws one; from the next tick nothing is released. `python run.py retention status` prints, without names or paths: the active attestations and their dates, the latest recovery points and their states, and how many managed units are waiting on each condition.

Why the database, not an environment variable or a file: an attestation must be deliberate (a variable can be copied between setups unseen), auditable (who recorded it, when, with which evidence) and part of the Darkroom backup, so a restore keeps it. Why the command line, not a web page: a web page needs a design the owner signs off first (promise 6), and these are rare owner-only actions; a page can follow if the owner wants one (section 10).

## 7. Failure paths

| Failure | Result |
| --- | --- |
| Crash after `freeing` was committed, before the unlink | Next run finds `freeing`, puts back any claim (`recover_claim`), re-runs the whole proof including G12, and either removes or clears the journal |
| Crash after the unlink, before `freed` | Next run sees `freeing`, confirms absence with `paths.inbox_entry_absent`, records `freed`; it never records `freed` from a folder it can't read |
| A file swapped in at any step | The claim's inode check refuses it and puts it back (`tests/test_part_free.py`'s swap tests, extended to review holds) |
| Immich answers for some assets and fails for one | The unit is kept; none of its files go |
| The person's key is rotated, revoked or given an extra grant | `check_access` refuses; kept |
| The owner reassigns a photo, undoes a review or a decision is reversed while a release is pending | The locked re-check (step 2) refuses; the dependency set changes and the unit is proved again on a later tick |
| A recovery point is reported `succeeded` with a `taken-at` before `begin` | `finish` refuses it; the point stays `open` until abandoned |
| Two `begin` runs overlap | Each makes its own point; coverage takes any succeeded point that covers, and G8 looks at the latest finished one |
| The database is restored from a backup taken before some releases | The restored journal says `freeing` or nothing for files that are already gone; the absence check records them `freed` only through `inbox_entry_absent`, and receipts are unaffected |
| The clock jumps back | `finish` refuses a `taken-at` after now; G9's window uses `dates.py`'s now, so a backwards jump only delays releases |

## 8. Checks that hold it

- `tests/test_retention_gate.py`: every condition's negative in section 4, the attestation and recovery-point commands' refusals, abandonment, the two alerts opening once and closing, and `no_leaks`.
- `tests/test_managed_release.py`: the locked re-check, G12 under the lock (a fake Immich that changes an asset between the unlocked pass and the lock), the deadline, the journal's crash boundaries and the swap tests, on SQLite and PostgreSQL (`make test-pg`).
- Semgrep: `darkroom-part-zip-one-caller` keeps `part_free.py` as the only caller of the removers; a new `darkroom-review-hold-one-remover` and `-one-caller` hold `paths.remove_review_hold` to `paths.py` and `part_free.py`; a new `darkroom-recovery-point-one-writer` lets only `darkroom/domain/retention_gate.py` write `recovery_points`, `recovery_point_assets` and `retention_attestations`. Each with failing and passing examples in `.semgrep/examples/`.
- `tests/test_dedupe_invariants.py`'s AST check gains `retention_gate.py`: it calls no remover and no Immich method other than reads.
- The provider's allowlist test: `custody_all` sends GET requests only.
- AGENTS.md promise 2 is amended in slice D, in the same pull request that first deletes a managed export's file, naming these checks.

## 9. Slices

Each its own pull request under 1,000 added lines including tests, landing in order. None enables anything by itself: releases need slice D's setting on and the owner's records.

| Slice | Change | About |
| --- | --- | --- |
| A | Migration (the three tables and the journal columns) with its test; models; `retention_gate.py` for attestations; `run.py retention record-rehearsal`, `accept-boundary`, `withdraw`, `status`; tests. No behaviour change: cleanup still keeps everything | 600 lines |
| B | `run.py recovery-point begin` and `finish`; `ManagedReader.custody_all`; manifest building; abandonment in the cleanup tick; the two alerts; the deployment docs' hook example; tests and `no_leaks` | 850 lines |
| C | `retention_gate.refusal(opened, unit, now)` with G1 to G10 and the per-condition negatives; `cleanup.py` records the specific reason instead of `KEPT_ONLY`; `retention status` counts per condition. Still keeps everything | 700 lines |
| D | Managed zip and held-copy release in `part_free.py`: the managed "placed" clause, the locked re-check, G12 with the deadline, `release_point_id` in the journal; the setting `DARKROOM_MANAGED_RELEASE` (off by default, in `config.py` and `.env.example`); the AGENTS.md promise 2 amendment and Semgrep examples; crash and swap tests. Labelled `safety-critical` | 950 lines |
| E | Managed review holds: `paths.remove_review_hold` with its claim, the hold journal, release in `part_free.py`, the Semgrep rules, tests; the runbook section on what the owner records | 800 lines |

If a slice goes over the budget it splits again, D first (held copies can move to E).

## 10. Open questions

Each is something a user would notice; this design does not choose them.

1. **The backup integration.** Is the `begin`/`finish` hook from the owner's backup job acceptable, and which tool will back up Immich's database and media together with Darkroom's appdata? Or does the owner want a tool's own API queried instead, or a manual attestation per backup (which would make cleanup depend on the owner running a command every few days)?
2. **Health thresholds.** `OPEN_LIMIT` (how long a backup may run before it counts as failed), `STALE_AFTER` (how long units may wait on coverage before the stale alert) and whether the stale alert should be pushed or only shown.
3. **When a rehearsal goes stale.** Should a recorded rehearsal stop counting after an Immich or Darkroom upgrade, a change between SQLite and PostgreSQL, or a fixed age? Any of these pauses cleanup until the owner rehearses again. The record stores the versions so any answer can be enforced.
4. **Release before 14 days.** Once the gate passes, may a managed export's zips and held copies go as soon as the proof holds (phase 2 for managed imports, as decision 1 on #424 did for own-folder imports), or only after the 14-day window, as this design assumes from section 4 of the managed-storage design?
5. **Matched photos whose Google metadata is not in Immich.** G7 keeps a zip while a tier 1 to 3 keeper lacks a field Google's sidecar has, because existing-asset enrichment is not built. That may keep many managed zips indefinitely. Should these stay pinned until enrichment exists, or should the owner get an explicit discard (recording that the field was not carried), as section 4 of the managed-storage design allows?
6. **Keepers not yet in Immich's managed library.** A zip whose matched keeper is a `google-only/` or `originals/` file the backfill has not mapped stays pinned (G6). Is waiting for the backfill right, or should those zips follow the old-folder rules?
7. **Where the owner records.** Command line only, or also a page in the app (which needs a signed-off design), and should the Exports page say "kept: waiting for a backup" per export?
8. **Scope of the boundary acceptance.** One acceptance for the whole installation, as designed, or per person (each person whose photos would lose the inbox copy)?
9. **How long the write lock may wait on Immich.** `READ_DEADLINE`: other jobs wait while a large zip's assets are read under the lock. A short deadline may mean a large zip never goes; a long one pauses imports and placement for minutes.
