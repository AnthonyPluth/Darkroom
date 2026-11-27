# Hold a different-byte same shot for the owner

Design for [#546](https://github.com/AnthonyPluth/Darkroom/issues/546), prepared 2026-11-24, part of the epic [#530](https://github.com/AnthonyPluth/Darkroom/issues/530). It also unblocks [#554](https://github.com/AnthonyPluth/Darkroom/issues/554) (backfill step 7c). **Draft: needs design review. The approach is the owner's decision of 2026-11-24 ([recorded on #546](https://github.com/AnthonyPluth/Darkroom/issues/546#issuecomment-7121463395)). The items in section 9 are still open.**

The owner's decision: a Takeout photo that is a different-byte same shot of an asset already in the person's Immich library goes into a `same_shot` hold that the owner resolves. Until the owner resolves it, nothing is uploaded and nothing is deleted. The design does not use a catalogue refresh into `managed_asset_versions`, and it does not upload both.

**Status after slice 3 ([#546](https://github.com/AnthonyPluth/Darkroom/issues/546)):** slices 1 to 3 are built. Until the owner answers section 9, the code uses the design's defaults: a 1 second window (question 1), a hold for trashed, offline and external candidates (question 2), the `run.py same-shot` command as the only way to resolve a hold (question 3) and an origin that proves no Google backup (question 5). Questions 4 and 6 concern slices 4 and 5, which are not built. Slice 3 changes one thing this design did not foresee: `managed_asset_origins` required a committed receipt for every `takeout` origin, and **Already in Immich** leaves the receipt `prepared`, so migration 0064 lets a `takeout` origin whose `source_key` starts with `same-shot:` carry its intent and no receipt. `same_shot.takeout_only` does not count such an origin as Darkroom's own upload.

This document builds on [immich-managed-storage.md](immich-managed-storage.md) (sections 2, 3, 4, 5 and 9) and does not change it. Where an implementing slice changes what that design or `AGENTS.md` says, the slice updates them, with its tests.

## 1. The gap

A managed import (step 5, #533, `darkroom/domain/managed_ingest.py`) uploads each automatic, unreversed tier 5 item: a Takeout photo that Darkroom's own index (`dedupe.decide` against the person's phone items) matched to nothing. Immich can still hold the same shot with other bytes. Darkroom's index is not Immich's catalogue: the phone may have uploaded since the last reconcile, the asset may predate Darkroom, or Google may have re-encoded the file so that tier 1 (SHA-1) misses and tier 2 lacks the phone row. The upload then returns `201 created`, and the person has two Immich assets for one shot. Darkroom may never delete either of them (promise 2 for managed imports; section 5 of the managed-storage design).

PR #541 first held such a photo (`329424f`: `ManagedReader.taken_near`, `takenAfter`/`takenBefore` in the fake Immich, `SAME_SHOT_SECONDS = 1.0`, and a `same_shot` value in the receipt's `metadata_hold`). `d37730d` took it out to keep #533 under 1,000 lines. That version had three gaps that this design closes:

- Nothing could resolve the hold. It stayed until someone edited the database.
- It matched any asset in the window whose checksum differed. A Live Photo's hidden video, a RAW beside a JPEG and Darkroom's own uploads from the same Takeout all counted.
- The fake filtered on `exifInfo.dateTimeOriginal`. Immich's search builder applies `takenAfter`/`takenBefore`, and the structured `filter.takenAt`, to the asset's `fileCreatedAt`, which slice 1 confirmed against the pinned sources (section 3 below).

The backfill (step 7a, #535, `darkroom/domain/backfill.py`) already lists the same kind of photo as an exception (`same_shot`: a `google-only/` item at tier 2 to 4 against a phone original, or an edited copy) and uploads nothing for it. #554 makes those items use the hold described here (section 7).

## 2. Where the check runs

| Path | Check | Why |
| --- | --- | --- |
| Managed import finish, tier 5 item (`managed_ingest.Uploads.send`) | Yes, before the first send | This is the gap |
| Keep both on a managed export's review pair (`keep_for_immich`, `KEPT` receipts) | No | The owner has already chosen to keep both copies, so that choice is the resolution |
| Backfill of a `google-only/` item listed `same_shot` (#554) | Yes, through the same function | Section 7 |
| Backfill of any other `google-only/` item | Yes, before the first send | The same gap: Immich can hold a shot that Darkroom's index lacks |
| Phone originals mapped by the backfill | No | They are mapped to their own stored asset, and nothing is uploaded |

The check runs once per receipt, right before Darkroom claims the first send: the receipt is `prepared`, `send_attempts` is 0, its intended metadata has no hold and it has a capture instant. Once a send has been attempted, the photo may already be in Immich, and Darkroom does not check again. That case belongs to the existing uncertain-outcome recovery (section 3 of the managed-storage design).

Immich is not locked between the check and the upload. A phone upload that lands in that window can still make a second asset. This is the same non-atomic boundary that the owner accepted in section 4 of the managed-storage design. Rechecking narrows the window but cannot close it.

## 3. How the match is made

### The query

The query is a new `ManagedReader.taken_near(instant, seconds)` on the `read`, `import` and `read_or_import` accesses. It is a read, and it needs no new grant (`POST /api/search/metadata` is `asset.read`, already on the allowlist). It runs after `check_access()`, as every reader method does. The request body uses the structured search shape (slice 1 pinned it; see "Pinned capture-time search" in section 2 of the managed-storage design):

- `filter.takenAt` with `gte` and `lte`, which are the instant minus and plus `seconds + 1`, in UTC with a `Z`. Immich compares them with `fileCreatedAt`. The extra second absorbs `fileCreatedAt` precision. The exact filter is applied by Darkroom afterwards (below).
- `withExif: true`, so that the capture time, make, model and dimensions come back.
- No trash filter: the structured shape returns trashed assets unless asked not to, so a trashed asset at the instant is seen too (see the owner-filter list below). The flat `takenAfter`, `takenBefore` and `withDeleted` fields cannot be combined with a cursor, and their path never answers `nextCursor`.
- `size: 100` and `orderBy` `fileCreatedAt` ascending. The provider follows `nextCursor` for up to 10 pages. A window that holds more than 1,000 assets is treated as a match (a hold, with the reason `crowded`) and never as "no match".

`taken_near` returns the owner's assets in the padded window, each with its instant, make, model, dimensions and duration, or `crowded` with none. Partner-shared assets are dropped. An answer it does not understand, a candidate with neither a capture time nor a file time, or a refused or failed request raises, and is never read as "no match". An asset in the owner's locked folder is never returned to an API key, so it cannot hold a photo.

Slice 1 pins these request fields and the filter column against the v3.3.6 and v3.4.1 sources, in the same way as section 2 of the managed-storage design, and adds a row to #266's live spike for the owner to run: a non-admin user, an asset uploaded with a capture time, and a different-byte upload with the same `fileCreatedAt` that `taken_near` must find, with exactly the read or import grants. Until that row passes, the hold is built and tested against the fake only. The fake must filter on the column the pinned source uses (expected to be `fileCreatedAt`), and not on `exifInfo`. The fake must also answer an asset whose extraction has not run yet with an upload-time `fileCreatedAt`.

### The instant

The incoming photo's instant is `intended.taken_utc`, which is the instant step 4 (`managed_metadata.intend`) will write and verify. A photo with no known offset (`unknown_offset`) or no instant (`no_instant`) is already held before upload and never reaches this check. No offset is invented here either.

A candidate's instant comes from its `exifInfo.dateTimeOriginal`. If that is absent, Darkroom uses the asset's `fileCreatedAt`, because Immich has not extracted the metadata yet or the file has no capture time. Both are instants. A wall clock (`localDateTime`) is never compared with an instant.

### The tolerance

A candidate matches when its instant is within **1 second** of the incoming instant (inclusive, compared as instants). This is the window from `329424f` and the same second that tier 2 uses (`dedupe.same_second`). Google's `photoTakenTime` and some phone exports drop subseconds, so an exact-instant test would miss real pairs. A wider window would hold bursts more often. Section 9 asks whether the owner wants the review window (2 s, `dedupe.REVIEW_MAX_SECONDS`) instead.

### Which assets can match

Darkroom applies these filters after the query, and each one has a negative test:

| Rule | Filter | Reason |
| --- | --- | --- |
| Same owner | `ownerId` equals the intent's owner (the person's Immich user) | Partner-shared and other users' assets are never a match and never authorise anything (sections 2 and 5 of the managed-storage design) |
| Other bytes | The candidate's checksum differs from the source SHA-1, and no asset in the window has the same checksum | An exact copy is tier 1, and the upload returns `duplicate` and is verified as today. If a same-byte asset is present, the photo is already in Immich and there is no new asset to prevent |
| Same kind | `IMAGE` with `IMAGE`, `VIDEO` with `VIDEO` | A still and a clip taken in the same second are different resources |
| Not a Live Photo's motion video | Skip an asset with `visibility: hidden`, and an asset that another asset in the window names as its `livePhotoVideoId` | A hidden motion video is part of a still, not a shot of its own |
| Same RAW-ness | `media.CAMERA_RAW_SUFFIXES` on `originalFileName` for both sides | A RAW and its JPEG are two distinct resources by design (the live spike confirmed that they are two unstacked assets) |
| Not Darkroom's own Takeout upload | Skip an asset whose verified managed version has a `takeout` origin and no `phone` or `other` origin for this person. A managed import records no version or origin yet (only the backfill does), so slice 2 also counts an asset that one of the person's receipts names with upload outcome `created` and a verified state as Darkroom's own upload, under the same `phone`/`other` exception | Google already held both items separately, and Darkroom's tiers kept them apart. Without this rule, two burst frames from one Takeout would hold each other |
| Any library and any state | Keep external-library, offline and trashed assets of the same owner | A hold loses nothing, and an upload might recreate a photo that the owner trashed on purpose. These candidates can only be resolved by `upload` or by leaving the hold (section 5) |

The rule does **not** require the camera or the size to agree. The owner chose a same-instant hold, and a hold is the safe outcome when the evidence is weaker. The hold records whether the make, model and dimensions (or duration in milliseconds, which is how Immich reports it) agree, and which side has more pixels. The owner sees this when resolving the hold, but it decides nothing by itself (section 5 of the managed-storage design).

### Live Photo halves and edited copies

- **Live Photo halves.** A Takeout Live Photo half is already held before upload (`item.live`) until #547 designs Live compounds. After #547, the check runs on the compound's still only, and a hold covers the whole compound: no member is sent until the hold is resolved, and the resolution applies to every member. #547's design states this as its own requirement.
- **Edited copies.** An edited copy (`-edited` name, `TakeoutEntry.is_edited` or `edited_of`) goes through the same check. Its unedited phone original in Immich is a candidate like any other, so the copy is held with the flag `edited`. The owner then chooses whether Immich gets the edited version too. An edited copy whose original came from the same Takeout is not held by that original, because the original's asset has only a `takeout` origin. The existing tier 3 setting `keep_edited_copies` still decides first, so a skipped edited copy never reaches the upload.

## 4. The hold

### What is stored

A new table, `managed_same_shot_holds` (one Alembic migration after main's newest, with its test on SQLite and PostgreSQL):

| Column | Meaning |
| --- | --- |
| `id`, `person_id`, `owner_id` | The person and their Immich user, fixed when the hold is made |
| `receipt_id` | The held receipt (unique). For #554, the backfill receipt |
| `found_at` | When the check found the candidates |
| `candidates` | ASCII-escaped JSON: each candidate's asset ID, checksum (hex), kind, library state (managed, external, offline or trashed), instant and agreement flags (camera, size, more pixels), plus the flags `edited` and `crowded`. No file names, places or descriptions are stored. The resolution step reads the candidates again when it needs them |
| `status` | `pending`, `upload`, `represented` or `void` |
| `resolved_at`, `resolved_by`, `asset_id` | Who resolved the hold and when. For `represented`, `asset_id` is the chosen candidate |

The receipt itself keeps `state = 'prepared'`, `send_attempts = 0` and `metadata_hold = 'same_shot'`. Like the other holds before upload, `managed_ingest.TERMINAL` and `count` treat `same_shot` as held, not pending, so the export can finish. The hold is made in one transaction with the receipt's move, under the write lock. Before the move, the receipt is read again (`prepared`, no attempts), and a second worker's move matches nothing.

### What the hold does

- **Nothing is sent.** `Uploads.send` returns before `complete_receipt` while `metadata_hold` is `same_shot`, and so does the backfill. A pending hold is never released by age, by the candidate disappearing or by a later reconcile. Only the owner releases it.
- **The source is pinned.** The photo's inbox copy (or, for #554, its `google-only/` file, which the backfill never writes or removes anyway) is pinned like an unresolved review item (section 4 of the managed-storage design). Cleanup and part freeing already never remove a managed export's inbox copies. Cleanup also leaves a held copy alone after the hold is resolved, until the receipt's own outcome lets it go.
- **The export finishes with an alert.** An export whose only open items are holds completes with one alert per export and person: "N photos look like ones already in Immich and wait for you". The alert has a fixed text and a count, and the person's slug in its key. It closes when the export has no pending hold left.
- **No reassignment or reversal while pending.** Reassigning the photo or reversing its decision answers 409 with a fixed reason until the hold is resolved, as reassignment already does for a photo the backfill may have sent. A pending hold never moves to another person's owner.

## 5. How the owner resolves a hold

Each action is one owner decision about one hold. It is recorded as a `same_shot_resolved` event with the hold ID, the action and who made it, and it re-checks under the write lock that the hold is still `pending` and that its receipt is still `prepared` with no attempts. **No action deletes, trashes, restores, updates or replaces an Immich asset, and none writes under `originals/` or `google-only/`.** If the owner wants only one asset in Immich, they remove the other one in Immich themselves, as section 5 of the managed-storage design requires.

| Action | What it does | Refused when |
| --- | --- | --- |
| **Upload** ("keep both") | Sets the hold to `upload` and clears the receipt's `metadata_hold`. The next import tick (or backfill run) sends the photo through steps 3 and 4 exactly as for any tier 5 item. Because the export may already be imported, and `finish` refuses an imported export, the tick also drains the `takeout-entry:` receipts of imported exports whose hold is `upload` (the same shape as `managed_ingest.send_kept` for `review:` receipts), claiming each under the write lock like any send. The same-shot check does not run again, because the owner has decided. If the upload comes back `duplicate`, the photo was already there byte for byte and is verified as usual | Never refused while the hold is pending |
| **Already in Immich** (`represented`) | The owner names one candidate. Darkroom reads that asset again with the unedited read-back (owner, `libraryId: null`, not trashed or offline, checksum unchanged, download SHA-1 and size), records or refreshes its `bytes_verified` managed version, and adds a `takeout` origin for this photo with the hold as its source. This is a policy match, not an exact-byte receipt (section 3, step 6 of the managed-storage design). Nothing is sent. The receipt stays `prepared` as an audit record, and the export counts the photo as settled. Google's metadata for the photo stays in the receipt's `expected_metadata` and is not written to the existing asset, because existing-asset enrichment is not built (it needs `asset.update`, which the owner decided on 2026-11-24 to bring back as [existing-asset-enrichment.md](existing-asset-enrichment.md), whose spike rows are on #266) | The candidate is not among the hold's candidates, is now missing, trashed, offline, external, another owner's, or has other bytes than recorded. The answer is 409 with that reason, and the owner can still choose Upload |
| **Leave** | Nothing changes. This is the default | Never refused |
| **Undo** | Puts an `upload` or `represented` hold back to `pending`. An `upload` hold can be undone only while its receipt has not been claimed for sending. A `represented` hold can be undone only while no Pixel reference or later decision depends on the origin it added | After a send has been claimed, the answer is 409: "already sent; Darkroom never takes a photo back out of Immich" |

The `takeout` origin from **Already in Immich** follows section 7 of the managed-storage design. It means that the photo came from that Google account's Takeout, so no Pixel copy goes back to the same account. It does not remove the asset's phone origin or the phone origin's always-send obligation. Because it is an explicit owner action and not an automatic tier 1 or 2 decision, it **does not prove** Google backup and never lets a Pixel copy be pruned (section 3, step 6 of the managed-storage design: "An explicit owner skip is distinct from verification and never proves Google backup").

### Where the owner acts

There is no web screen until a design for one is signed off (promise 6; `AGENTS.md`, "No design ships without sign-off"). Until then, the owner uses a command that `run.py` adds, in the same way as `managed-imports` and the opt-in command (#544):

```
python run.py same-shot list --person <slug>
python run.py same-shot resolve --person <slug> --hold <id> --action upload|represented|undo [--asset <immich asset id>]
```

`list` prints, for each pending hold, its ID, the capture time at its own offset, the flags and each candidate's asset ID and library state, with a link to the asset in the person's Immich web app (`<IMMICH_URL>/photos/<asset id>`, which needs no grant and sends no key). It never prints a file name, a place or a description, and it never writes one to a log. Section 9 asks whether this command is acceptable as the interim way to resolve holds.

The review screen is a **dependency, not part of this design**: a same-shot review needs its own signed-off design in `docs/design/web/` (for example beside `06-review-*`) before any page or route for it is built. That design has to settle at least three things:

- How the Immich side is shown. A thumbnail needs `asset.view`, which no approved key has, so the screen either links to Immich or the owner decides on a new grant.
- How the screen relates to the existing review queue, which today offers only Keep both.
- How the screen relates to the resolution of terminal holds in #548.

## 6. Failure paths

| What fails | What happens | What the owner sees |
| --- | --- | --- |
| Immich unreachable, 5xx, a refused or unknown answer, or an unreadable page | No hold is made and nothing is sent. The receipt stays `prepared` and pending, and the next tick checks again | Nothing at first. After a day, the existing "not committed" alert for the export, as for any receipt left pending |
| Wrong grants, an admin key, or a version that is not in `MANAGED_VERSIONS` | Refused before any read (`check_access`), and the receipt stays pending | The existing key and version alert |
| More than 1,000 assets in the window | A hold, flagged `crowded` with no candidates listed. Only **Upload** or **Leave** can resolve it | The hold in `list` |
| Crash between the check and the hold's commit | The transaction is rolled back, so there is no hold and nothing was sent. The next tick checks again | Nothing |
| Two workers check the same receipt | One move under the write lock wins. The other worker reads a held or claimed receipt and does nothing | Nothing |
| A phone upload lands between the check and the send | A second asset (accepted boundary, section 2) | Two assets in Immich, which the owner may tidy in Immich |
| The candidate is trashed or removed before **Already in Immich** | Refused with 409, and the hold stays pending | The reason, and the choice of **Upload** |
| The export fails, expires or is retried while a hold is pending | The hold and its pinned source stay. A retried export finds the existing receipt and hold | The hold in `list` |
| Disk pressure from pinned sources | The existing stalled-retention alert and download pause (section 4 of the managed-storage design). Nothing is deleted to make room | The existing alert |

No log line carries a file name, an asset's original file name, a place, a description, a key or Immich's answer body. Logs carry fixed texts, counts and internal IDs. `tests/privacy.py`'s `no_leaks` canaries cover `taken_near`, the hold, `list` and every refusal.

## 7. How #554's backfill reuses it

Step 7a lists a `google-only/` item as `same_shot` in two cases: when `backfill.same_shots` finds it at tier 2 to 4 against a phone original of the person, or when it is an edited copy. #554 changes only what happens next:

1. The item gets a backfill receipt (`managed_import_intents.kind = 'backfill'`), as other `google-only/` items already do, with its stored Google sidecar.
2. The receipt goes through the same `same_shot` check (section 3) before any send. The phone original's own Immich asset is usually the candidate, so most such items are held. If Immich holds no different-byte asset at the instant (for example, the phone asset was removed in Immich), the item is backfilled like any other: nothing in Immich would be duplicated.
3. A held item's `managed_backfill_items` row keeps `reason = 'same_shot'` and points to its hold. **Already in Immich** maps the item to the candidate's verified version (`outcome = 'mapped'`, with the `takeout` origin from section 5). **Upload** lets the backfill send it. Both use the same domain functions and the same command (`same-shot resolve`).
4. #554 adds one bulk owner action, `same-shot resolve --person <slug> --tier2 --action represented`. It applies **Already in Immich** to every pending backfill hold whose item has an automatic, unreversed tier 2 decision against a phone original that is the hold's only candidate, is mapped and is verified, and whose Takeout copy has no more pixels. The owner runs it, and nothing runs it automatically. Each hold it resolves is checked and recorded as if the owner had resolved it individually. Section 9 asks whether the owner wants this.
5. The backfill still deletes nothing, calls no remover and never replays a recorded losing-copy removal (promise 2). A Takeout copy that the old local rule kept because it had more pixels is shown with **more pixels** set, so the owner can choose **Upload**.

## 8. Slices

Each slice is one pull request under 1,000 added lines including tests. Each slice is off until its gate, and none of them deletes anything in Immich. The estimates are added lines.

| Slice | Change | Tests | About |
| --- | --- | --- | --- |
| 1. `taken_near` | `ManagedReader.taken_near` with the request fields in section 3, bounded pagination and the owner filter. The sources are pinned in the managed-storage design's section 2 (by that slice, in its own section). The fake Immich filters on `fileCreatedAt`. The #266 live row is described for the owner. No caller | The query body, pagination limit, refusals for each access, answers that are not understood, and `no_leaks` | 300 |
| 2. Hold at the managed import finish | Migration for `managed_same_shot_holds`, the pure match function (`darkroom/domain/same_shot.py`, every rule in section 3), the check in `Uploads.send`, the counts, the export alert, the pin and the 409 on reassignment and reversal. Updates `AGENTS.md` promise 2 and the managed-storage design's step 5 paragraph, which today lists this as a follow-up | A negative test for each rule in section 3, the crash and race boundaries, SQLite and PostgreSQL locking, the migration test, and no upload while held | 800 |
| 3. Resolution | Domain functions for **Upload**, **Already in Immich** and **Undo**, the `same-shot list/resolve` command, events, and the origin recording on the version | Each refusal in section 5, the read-back before `represented`, the undo rules, no Pixel proof from `represented`, and `no_leaks` on `list`, and a completed export whose hold is resolved to Upload is sent by a later tick | 700 |
| 4. Backfill (#554) | Backfill receipts and the same check for `same_shot` items and for other `google-only/` items. The mapping on **Already in Immich** and the bulk `--tier2` action. `managed_backfill_items` points to the hold | Tier 2, tier 3 and 4 and edited items, removed candidates, the bulk action's conditions, and nothing deleted (`BackfillDeletesNothing`) | 700 |
| 5. Review screen | Blocked until the owner signs off a design in `docs/design/web/`. Then the routes (session auth, contract types, a test each) and the page | `make verify` images at both widths | To be decided |

Slices 1 and 2 can merge before #266's new row passes, because nothing outside the tests sets `managed_imports` yet. Turning managed imports on for a person needs that row, as section 2 of the managed-storage design already requires for every row. Slice 4 depends on slice 3. Slice 5 depends on slice 3 and on the signed-off design, and it may share its screen with #548.

## 9. Open questions for the owner

1. **Tolerance.** Is 1 second right, or should the window be the review window of 2 seconds (more holds, fewer missed pairs)?
2. **Trashed and external candidates.** A trashed, offline or external-library asset of the same owner also causes a hold in this design. Should a trashed one instead be ignored, so that the Takeout copy is uploaded?
3. **Interim command.** Is a `run.py same-shot` command acceptable as the way to resolve holds until a review screen is designed and signed off, or should no hold be resolvable before the screen exists?
4. **Bulk tier 2 action for #554.** Should the backfill offer the owner-run `--tier2 --action represented` bulk resolution in section 7, step 4, or should every pair be resolved one by one?
5. **Google proof.** **Already in Immich** is an owner action, so by the approved design it never proves Google backup for Pixel pruning. Is that intended for these pairs, or does the owner want a separate decision on it?
6. **The review screen.** Should the Immich side be a link to Immich (no new grant), or should Darkroom show a thumbnail, which needs `asset.view` added to the approved grants?
