# Resolve terminal holds in Immich-managed imports

Design for [#548](https://github.com/AnthonyPluth/Darkroom/issues/548), part of the Immich-managed storage epic [#530](https://github.com/AnthonyPluth/Darkroom/issues/530), prepared 2026-11-24. **Approved by the owner on 2026-11-24 (defaults accepted, recorded on #548).** It builds on [immich-managed-storage.md](immich-managed-storage.md) (sections 3 to 5 and 8) and on what #533 / PR #541 and #535 built. Nothing here turns on a managed import: `people.managed_imports` and `people.managed_backfill` stay unset outside the tests until their own switches land, so everything below is reachable only from tests until then.

## 0. Owner decision this design rests on

**No `asset.update` grant** (the owner's answer of 2026-11-24, recorded on #548). The person's key keeps exactly the read grants or the import grants of section 8 of the managed-storage design; `asset.update` is never added. So there is **no enrichment path**: Darkroom never changes the metadata of an asset already in Immich, never sends `PUT /api/assets/{id}` or any other update, and a `not_enriched` hold is shown for review and resolved by the owner (accepting the asset as it is, keeping Google's values recorded in Darkroom, or recording an offset in Darkroom), never by writing to Immich.

This changes what the managed-storage design says about future enrichment. Its section 2 ("Existing-asset enrichment needs its own result…", the `PUT /api/assets/{id}` paragraph and "Do not enable enrichment…"), the "Still open" list and step 1 of "Before imports can be enabled" (edited representations and enrichment), section 5's "complete the approved missing-field enrichment before releasing held source material", section 6's promise 1 ("Metadata changes require a separately verified, owner-approved missing-field API path"), section 8's "`asset.update` is added only with the approved enrichment path" and section 10's "Metadata and API compatibility" row all describe an enrichment path that now will not be built. This document does not edit that shared file; a follow-up docs change (slice 0 below) rewrites those sentences to say enrichment is not built by the owner's decision, and that the owner's resolution of section 3 here is what "completed metadata handling" means for a duplicate.

## 1. What a terminal hold is today

A managed receipt (`managed_import_receipts`) ends in a terminal hold when it can neither commit nor usefully retry. `managed_ingest.TERMINAL` lists five; `unusable` is the sixth, reported but not stored.

| Hold | Raised by | Receipt state when raised | Stored as | What happens now |
| --- | --- | --- | --- | --- |
| `unknown_offset` | `managed_metadata.intend` (`taken_of`): a wall clock with no offset of its own that Google's instant and the place Immich will see don't settle; a date Google corrected with no position and no offset; a QuickTime instant with no place; a wall clock resolved at the file's own position when the place Immich will see is in another zone | `prepared`, nothing sent | `metadata_hold` and `expected_metadata.hold` | Never sent. Import: counted held, `managed:held:<export>` alert, export completes, inbox copy pinned. Keep both: refused before a receipt is made. Backfill: `managed_backfill_items` exception |
| `no_instant` | `intend`: no capture time at all (no EXIF date, no Google `photoTakenTime`) | `prepared`, nothing sent | as above | as above |
| `unwritable` | `intend`: the description can't be carried by the upload XMP (`xmp.upload_sidecar` raises, e.g. a lone surrogate or a control character) | `prepared`, nothing sent | as above | as above |
| `not_enriched` | `verify_metadata`: an upload answered `duplicate` whose sidecar had something to add, and Immich's existing asset doesn't show it | `bytes_verified`, the asset is Immich's existing one | `metadata_hold` | Bytes verified, metadata not; not polled again; held alert |
| `not_settled` | `managed_ingest.gave_up`: a `mismatch` still there a day (`SETTLE_SECONDS`) after the bytes were verified | `bytes_verified` | `metadata_hold` | Not polled again; held alert |
| `unusable` | `managed_upload` / `verify_metadata` / `commit_compound`: the asset is trashed, offline, in an external library or another person's, or holds other bytes | `returned` or `bytes_verified` | **not stored**: only the `Report`; the backfill stores it as an exception reason | Import: counted held in that tick, but the receipt has no hold, so a later pass may try it again; kept: one `managed:kept:<review>` alert |

A receipt belongs to one of three sources, all of which this design covers with the same records:

- **Import**: `takeout-entry:<id>` under an export's `import` intent; the bytes are in that export's part zip (phase 2 and cleanup stay off for a managed export, so the zip is kept).
- **Kept from review**: `review:<id>` (Keep both on a managed pair); the bytes are the review hold in the inbox.
- **Backfill**: `backfill:media-item:<id>` under a `backfill` intent; the bytes are the person's file in `google-only/` (read, never written).

Nothing today shows a hold to anyone: there is an alert per export or per kept pair, and the backfill's exception rows, but no route lists the receipts or says what the owner could do.

## 2. Principles

1. **No offset is invented.** Darkroom never picks an offset, never lets Immich pick one, and never pre-fills one for the owner. An offset or a capture time comes from the file, from Google, or from the owner typing it, and each is recorded with its provenance (`media`, `sidecar`, `sidecar_offset`, and a new `owner`).
2. **Metadata goes only where it goes today.** A value the owner supplies reaches Immich only inside the XMP sidecar sent with a first upload (`sidecarData` on `POST /api/assets`), exactly as Google's values do (promise 5: a sidecar, never rewritten media). It is never written beside an original, into `google-only/`, into the inbox copy, or into Immich's filesystem. After an asset exists, nothing Darkroom does changes it (section 0).
3. **Nothing is deleted.** No resolution deletes an inbox copy, a review hold, a zip, a receipt or an Immich asset. Skip is a recorded decision, not a discard. Restoring a trashed asset is the owner's action in Immich; Darkroom only reads again.
4. **A known value is never changed.** A resolution fills what is missing (an offset, a capture time) or drops what can't be carried (a description); it never moves an instant Darkroom already knows, or a place.
5. **Every resolution is recorded and, until something was sent, undoable**, restoring exactly what was there.

## 3. Per-hold resolutions

"Shown" is what the hold list (section 4) gives for each hold: the facts, never a guess. "Writes" lists every place a resolution changes; anything not listed is not changed. Every resolution also writes one `managed_hold_resolutions` row and one event (section 5), and none sends anything to Immich except the first upload a pre-upload hold was waiting for.

| Hold | Cause, in the owner's words | What the owner sees | Allowed resolutions | What each writes, and where |
| --- | --- | --- | --- | --- |
| `unknown_offset` | "The photo says what time it was on the clock, but not which time zone, and Google's time and the place don't settle it." | The file name; the person and export; the wall-clock time as the file gives it (or, for a video with a UTC time and no place, that UTC instant); Google's capture instant in UTC if it has one; whether a place is known (yes/no, never coordinates); the fixed reason | **Supply the offset** (UTC−12:00 to UTC+14:00 in 15-minute steps). **Keep held** (the default: do nothing). **Skip** (not sent to Immich) | *Supply*: the resolution row (offset in minutes, who, when). At the next tick, the receipt's `expected_metadata` is worked out again from the bytes with the owner's offset (`taken_from: owner`, the instant from the wall clock and that offset, or the wall clock from the known instant and that offset) and `metadata_hold` cleared, both by compare-and-set while the receipt is still `prepared` with no send attempt; then the normal upload sends the bytes and an XMP with `exif:DateTimeOriginal` and `photoshop:DateCreated` at that offset; the read-back must show that instant and offset (section 3 of the managed design) before it commits. *Skip*: the resolution row only; the receipt stays `prepared` and held |
| `no_instant` | "Nothing in the photo or from Google says when it was taken." | File name, person, export; the fixed reason; that neither the file nor Google gives a time | **Supply the capture time**: a date and wall-clock time together with its offset (both required; a time without an offset is refused, so the owner can't create an `unknown_offset` by hand). **Keep held**. **Skip** | *Supply*: as for `unknown_offset`, with `taken_from: owner` for both the clock and the offset. *Skip*: the row only |
| `unwritable` | "Google's description has characters that can't go into the file Immich reads, so nothing was sent." | File name, person, export; the fixed reason; the description's length and that it has characters XMP can't carry (the text itself only on the photo's own detail, as Review shows names today; see open question 5) | **Send without the description** (Immich gets the photo and the rest of its metadata; Google's description stays recorded in Darkroom). **Keep held**. **Skip** | *Send without*: the row; at the tick, `expected_metadata` worked out again without the description (`description` and `description_from` from the file's own tag if it has one, else none), hold cleared by compare-and-set, normal upload. Google's text stays in the takeout ledger and in the row's saved snapshot (ASCII-escaped JSON, as receipts store it today). *Skip*: the row only |
| `not_enriched` | "Immich already had this photo, so it kept its own copy, which lacks something Google knows (a time, an offset, a place or a description). Darkroom can't add it to Immich's copy." | File name, person, export; which fields differ, as field names ("capture time", "offset", "place", "description"), and for time and offset both values (what Immich shows and what Google gives); the fixed reason, saying Darkroom doesn't change photos already in Immich | **Accept as it is in Immich**, keeping Google's values recorded in Darkroom. **Record an offset in Darkroom** (when the difference is the offset: the owner's offset is stored with the receipt as Darkroom's record of where it was taken, never sent). **Check again** (after the owner fixed it in Immich by hand). **Keep held** | *Accept* and *Record an offset*: the row (with the offset for the second); the receipt moves `bytes_verified` → `metadata_verified` by compare-and-set with `metadata_evidence` set to what Immich shows on a fresh read at that moment, `metadata_hold` cleared, then `commit_compound` as today. `expected_metadata` keeps Google's values untouched, so the gap stays on record. *Check again*: no row change until the result; the tick re-reads `exifInfo` with the usual bounded poll; if it now equals the intended values, the receipt verifies the normal way and the row says `rechecked`; otherwise it stays `not_enriched` and the row says it still differs |
| `not_settled` | "Immich read the photo, but for a day it showed different metadata than Darkroom sent." | File name, person, export; the fields that differ with both values (as above); when the bytes were verified | **Check again**. **Accept as it is in Immich**. **Keep held** | As for `not_enriched`. Accepting a `not_settled` asset that Darkroom created means Immich shows something other than what Darkroom sent: the row records that the owner accepted it, and the intended values stay in `expected_metadata` |
| `unusable` | "Immich's copy is in the trash, offline, in an external library or someone else's, so Darkroom can't count it." | File name, person, export; which of those it is (trashed, offline, external, other owner, other bytes), from the last read; the fixed reason, saying Darkroom never restores or moves photos in Immich | **Check again** (after the owner restored it in Immich's trash, or otherwise fixed it there). **Keep held**. **Skip** | *Check again*: the tick reads the asset again (owner, `libraryId: null`, not trashed or offline, checksum, then the unedited download's SHA-1 and size); if usable, the receipt continues from where it stopped (`returned` → `bytes_verified` → metadata) and the row says `rechecked`; otherwise it stays `unusable` with the fresh reason. Nothing is restored, trashed, moved or re-uploaded. *Skip*: the row only |

Rules that hold across the table:

- **Offset validation.** An owner offset is an integer number of minutes from −720 to +840 and a multiple of 15. A capture time is a calendar date-time between 1900-01-01 and the server's `dates.py` now plus one day, with its offset. Anything else is refused with 422 and a fixed reason, before anything is recorded.
- **An offset that disagrees with the place.** When the place Immich will see (the file's or Google's) has, at that instant, an offset other than the one supplied, the resolution is refused with 422: the managed design already holds such a photo because Immich is expected to read the place's zone, so the read-back would never match. Whether to allow it anyway is open question 3; the live spike row for unknown-offset handling (managed design section 2, "Still open") is what would settle it.
- **One hold becomes another.** Working the metadata out again with the owner's value can raise a different pre-upload hold (an offset supplied, but the description is still unwritable). The receipt then takes the new hold, the row is marked `applied` with the new hold named, and the list shows the new hold; nothing is sent.
- **Live Photo halves and same-shot pairs** are not terminal holds of a receipt (they never get one); they stay with #553 and #554 and are out of scope here.
- **Skip** never makes a receipt committed and never proves anything was backed up. A skipped item's inbox copy stays pinned like any other held item while cleanup of managed exports stays off; what happens to it once cleanup exists is open question 6.

## 4. Showing holds for review (a requirement, not a screen)

Holds are shown on the Review page, since #548 says they are resolved "for review". **No screen is designed here.** Promise 6: before any frontend change, a design for showing and resolving holds is drawn and signed off by the owner in `docs/design/web/` (phone and desktop boards, beside `06-review-*.dc.html`), and only then built. That design must cover:

- Where holds appear (a section or tab of Review, or its own page; open question 9) and how they are counted next to pending pairs.
- One row per held item: its file name, person, export or source (import, kept from review, backfill), the hold's reason in the owner's words (section 3), and the facts listed under "What the owner sees", never coordinates and never a suggested offset.
- Each allowed resolution as an action, with the offset entry (sign, hours, minutes in 15-minute steps) and the capture-time entry (date, time and offset together), their validation messages, and what the page says while a resolution waits for the next tick ("Saved; it goes to Immich on the next import tick").
- Undo of a resolution while nothing has been sent, and the text when it can't be undone ("Already sent to Immich, so it can't be undone"; open question 1 covers the wording).
- Empty, loading and failed-to-load states, and a reload that fails leaving no stale list that looks current (AGENTS.md "Failure paths").
- Whether a held import photo shows a thumbnail (it would be read from the part's zip; open question 7).

The page reads one new route and writes through two (section 6), and builds nothing else.

### The Review summary for a pair kept in Immich

Independent of the hold list, and part of #548 (the Second review advisory `f:3db76cfc` on PR #541): after Keep both on a managed pair, the summary gives `undo_until: null` and the page shows `undoUnavailable(review)`, which says "Too late to undo" seconds after the decision. The real reason is that Darkroom never takes a photo back out of Immich.

- `contract.ReviewSummary` gains `kept_in_immich: bool`, true only when the review is `kept_both` and its export is Immich-managed (`review.kept_in_immich`); a pending pair of a managed export is `false`. `make api-contract` regenerates `docs/openapi.json` and `frontend/src/lib/api-types.ts`.
- `undoUnavailable` returns, in order: "Reversed, so it can't be undone" when reversed (unchanged); the kept-in-Immich text when `kept_in_immich`; otherwise "Too late to undo" (unchanged). Approved text: **"Kept in Immich, so it can't be undone"**, approved by the owner on 2026-11-24 and recorded on #548 (https://github.com/AnthonyPluth/Darkroom/issues/548#issuecomment-7122813221); no layout changes.
- `frontend/src/lib/review.test.ts` gains a case: `undoUnavailable({ ...summary, kept_in_immich: true })` is the new text and not "Too late to undo"; with `reversed: true` as well it stays "Reversed, so it can't be undone". The existing two cases stay. A backend test asserts the flag is `true` for a kept managed pair and `false` for a pending managed pair and for an old-way pair, with `undo_until` unchanged in each case.

## 5. Data

One new table and one new column, in one Alembic migration numbered after main's newest when the slice lands (today that would be 0052), with its `test_<revision>_…` on SQLite and PostgreSQL.

`managed_hold_resolutions`:

| Column | Meaning |
| --- | --- |
| `id` | key |
| `receipt_id` | FK to `managed_import_receipts`, `RESTRICT` |
| `hold` | the hold being resolved (one of the six) |
| `action` | `offset`, `capture_time`, `drop_description`, `accept`, `record_offset`, `recheck`, `skip` (checked against the hold: section 3's table is the allowed pairs) |
| `offset_minutes` | for `offset`, `capture_time`, `record_offset`; else null |
| `taken_local` | for `capture_time`: the wall clock as typed, ISO `YYYY-MM-DDTHH:MM:SS`; else null |
| `state` | `recorded` (saved, not yet applied), `applied` (the receipt changed), `undone`, `superseded` |
| `result` | fixed code once applied: `cleared`, `rehold:<hold>`, `still_differs`, `still_unusable`, `rechecked` |
| `previous_metadata` | the receipt's `expected_metadata` and `metadata_hold` before it was applied, so undo restores them byte for byte |
| `decided_by_person_id`, `decided_at`, `applied_at`, `undone_at` | who and when |

At most one row per receipt is `recorded` or `applied` and not undone (a partial unique index, written for both databases). An applied resolution is retired when its result leaves the receipt under another hold (`rehold:<hold>`, or an upload whose read-back ends `not_settled`): in the same write-lock hold that records the new hold, its row becomes `superseded`, which is not undone and keeps its sent effects, so the owner can record the next resolution (for example offset, upload, `not_settled`, then `accept`). Undo never reaches a superseded row. `managed_import_receipts` gains `hold_facts` (Text, ASCII-escaped JSON, nullable): the facts the list shows, written when the hold is recorded, because today's snapshot keeps `taken_utc` and `offset` null for a pre-upload hold and so loses the wall clock, and what Immich showed for `not_enriched`/`not_settled` is not kept anywhere (`metadata_evidence` is only for a verified receipt). `unusable` is stored as a `metadata_hold` value with its kind in `hold_facts`, and joins `managed_ingest.TERMINAL`, so a pass no longer tries an unusable receipt again unless the owner asks it to check.

`managed_metadata.SOURCES` gains `owner`; snapshots already stored read as before. `intend` takes the owner's values as an explicit argument and never reads them from anywhere else.

Events, with fixed text and ids only: `managed_hold_resolved` (`receipt <id>`, the hold and action), `managed_hold_applied` (the result), `managed_hold_undone`.

## 6. Routes

All `session`-auth (open question 2 asks whether to narrow who may resolve), typed in `contract.py`, bodies through `validate.py`, each with a test.

- `GET /api/holds?person=&source=&after=`: the terminal holds, newest first, paged like `/api/review`: receipt id, source (`import`, `kept`, `backfill`), person id, export id, file name, hold, reason text, `hold_facts`, the allowed actions, the current resolution (action, state, result, when, by whom) and whether it can still be undone.
- `POST /api/holds/{id}/resolve` `{action, offset_minutes?, taken_local?}`: records a resolution. 422 for a value outside section 3's rules or an action the hold doesn't allow; 409 when the receipt is no longer in that hold, already has an open resolution, or was committed meanwhile.
- `POST /api/holds/{id}/undo`: marks the open resolution `undone` and, if it was applied, restores `previous_metadata` and the hold by compare-and-set. 409 "already sent to Immich, so it can't be undone" once a send was attempted (`send_attempts > 0` or a lease is held) or the receipt is committed.

The request writes only the database. It reads no file, runs no exiftool and calls no Immich: the next tick does that.

## 7. Applying a resolution

A new step of the import tick, `managed_holds.apply`, after `send_kept` and with the person's managed reader (`import` access, so the key's grants are exactly the import set; never `asset.update`). For each `recorded` row, under the write lock taken before it reads (AGENTS.md):

1. Re-read the receipt; if its state, hold or `updated_at` differ from what the row was recorded against, mark the row `superseded` and do nothing else.
2. Pre-upload holds: read the bytes from where the receipt's source keeps them (the part's zip through the finish machinery's scratch, checked against `source_sha1`; the review hold; the `google-only/` file, read only), read their tags, run `intend` with the owner's values, and compare-and-set `expected_metadata` and `metadata_hold` while the receipt is still `prepared` with no send attempt. Then send through `managed_metadata.complete_receipt` and `commit_compound` exactly as a fresh receipt is sent.
3. `accept` / `record_offset`: a fresh read of the asset (owner, managed, not trashed or offline, checksum) and of `exifInfo`; compare-and-set `bytes_verified` → `metadata_verified` with that evidence; `commit_compound`.
4. `recheck`: run the existing read-back and bounded metadata poll; record the result.
5. A backfill receipt then lets `backfill.py` re-evaluate its item (its `managed_backfill_items` row moves to `mapped` or to the new reason); an import receipt is counted in `uploaded` for its export; a kept receipt clears its `managed:kept:<review>` alert.
6. When an export has no terminal hold left, its `managed:held:<export>` alert closes; it never closes while one remains.

## 8. Failure paths

| What fails | What the owner sees | What stays |
| --- | --- | --- |
| Invalid offset or time, or an action the hold doesn't allow | 422 with the fixed reason; nothing recorded | The hold, unchanged |
| Offset disagrees with the place Immich will see | 422 "this place was at UTC±hh:mm then, so Immich would not show the offset you gave" | The hold |
| Two people resolve the same hold at once | The second gets 409 "this hold already has a resolution" | The first resolution |
| The receipt changed between recording and the tick (another resolution undone, a retry moved it) | The row shows `superseded`; the list shows the hold as it now is | The receipt as it is |
| The source bytes can't be read or don't hash to `source_sha1` (zip gone or changed, hold replaced, file in `google-only/` changed or the share not mounted) | The row stays `recorded`; the list says "Darkroom couldn't read the photo's copy; it tries again"; an alert once it has waited a day | The hold, the copy, the receipt |
| Working it out again raises another hold | The list shows the new hold with the row's `rehold:<hold>` | Nothing sent |
| Immich unreachable, key refused, upload 4xx/5xx or uncertain | The row stays `applied`, the receipt pending as any upload is today, with the existing pending alert after a day | The receipt's own recovery (checksum search before any resend) |
| Read-back after an owner offset shows another offset | The receipt goes to `mismatch`, then `not_settled` after a day; the list shows both values | No second upload; the owner can accept or keep it held |
| Accept, but the asset is now trashed or changed | The row's result is `still_unusable`; the hold becomes `unusable` | Nothing committed |
| Undo after a send was attempted | 409 "already sent to Immich, so it can't be undone" | The resolution |
| The hold list fails to load | The page's failed-load state, not an empty list | Nothing |

## 9. Negative tests

Each a test that fails before the change (`tests/test_managed_holds.py` unless named):

- No resolution sends anything to Immich but `POST /api/assets` and `bulk-upload-check` (and the existing reads): the fake Immich records every request, and the test fails on any `PUT`, `PATCH`, `DELETE`, trash, restore or update route, for every action including `accept` and `recheck`.
- The key: a resolution never needs or accepts `asset.update`; a key with it is refused as today, and the apply step uses `import` access.
- No offset is invented: for every `unknown_offset` and `no_instant` case in `test_managed_metadata.py`, nothing resolves without an owner value; a `capture_time` without an offset is refused; the list and the route never return a suggested offset.
- Range and step: −735, +855, +7 and a non-integer offset are refused; −720, +840, +345 (UTC+05:45) and −30 are accepted where the place allows.
- An offset disagreeing with the place is refused and records nothing.
- A known instant is never moved: resolving `unknown_offset` on a QuickTime UTC instant keeps `taken_utc` exactly.
- Nothing written to disk: a whole-tree SHA-1 of `originals/`, `google-only/` and the inbox before and after every action is unchanged, and no `.xmp` appears anywhere.
- Nothing deleted: skip, accept, undo and supersede leave every receipt, inbox copy, review hold and zip; cleanup and part freeing still keep a managed export.
- Supersession: an owner offset uploads the photo, read-back ends `not_settled`, the first resolution becomes `superseded` and a following `accept` or `recheck` is recorded and applied; undo of the superseded row is refused.
- Undo round trip with awkward values: an `unwritable` hold whose description has a lone surrogate, an offset of −00:30 and +14:00, a `capture_time` at 1900-01-01T00:00:00 — `expected_metadata` and `metadata_hold` after undo equal the originals byte for byte.
- Undo after a send attempt, after a lease, and after commit is refused.
- Concurrency: two resolves on one hold, a resolve racing the tick, an undo racing the apply, each on SQLite and PostgreSQL write locking; exactly one wins.
- Wrong hold for the action (`accept` on `unknown_offset`, `offset` on `not_enriched`) is refused.
- `unusable` stored: an unusable receipt is not tried again on later passes until a `recheck`; a recheck after the asset is restored continues from where it stopped without a second upload.
- A source that changed: a zip entry, review hold or `google-only/` file swapped for other bytes leaves the row `recorded` and sends nothing.
- `no_leaks` canaries: a file name, a description, a place and the key never appear in logs or error bodies; events carry ids and codes only.
- Access: every route answers 401 without a session, and `make feature-map-check` finds a test for each.
- The Review summary: `kept_in_immich` true only for a kept managed pair; `review.test.ts` as in section 4.

## 10. Slicing

Each its own pull request under 1,000 added lines including tests, landing in order; nothing reachable outside tests until the managed switches exist.

| Slice | Content | Needs first |
| --- | --- | --- |
| 0 | Docs only: update `immich-managed-storage.md` (sections 2, 5, 6, 8, 10) and `docs/DECISIONS.md` for the no-`asset.update` decision | Owner's approval of this design |
| 1 | `kept_in_immich` in `ReviewSummary`, `undoUnavailable` wording, `review.test.ts` case and the backend test; `make api-contract` (about 60 lines) | The owner's sign-off of the exact text (open question 1) |
| 2 | Store `unusable` as a terminal hold, `hold_facts` (migration), `GET /api/holds` with its contract and tests | This design approved |
| 3 | `managed_hold_resolutions` (migration), `resolve` and `undo` routes and their validation, events; no apply step | Slice 2 |
| 4 | Apply step for pre-upload holds: `intend` with owner values, re-reading each source, sending; superseding | Slice 3 |
| 5 | Apply step for post-upload holds: accept, record offset, recheck (including `unusable`); alert closing; backfill re-evaluation | Slice 4 |
| 6 | The hold list's boards in `docs/design/web/` (phone and desktop) | Owner sign-off (promise 6) |
| 7 | The Review page builds the boards, with `make verify` screenshots | Slices 3 and 6 |

## 11. Open questions for the owner

1. **Wording** of the undo text for a pair kept in Immich: "Kept in Immich, so it can't be undone"; and for a resolution that was already sent: "Already sent to Immich, so it can't be undone". Closed: the owner approved both texts on 2026-11-24, recorded on #548 (https://github.com/AnthonyPluth/Darkroom/issues/548#issuecomment-7122813221).
2. **Who may see and resolve a hold**: any signed-in member, as Review today (proposed), or only the photo's person and the owner.
3. **An owner offset that disagrees with the place** Immich will see: refuse (proposed), or allow and let the read-back decide (it would likely end `not_settled`).
4. **Facts only, or a hint**: the list shows the wall clock, Google's UTC instant and whether a place is known, with no suggested offset (proposed). Showing the offset that would make the clock agree with Google's instant would be a suggestion the owner could just accept.
5. **The unwritable description**: offer only "send without the description" (proposed), or also let the owner type a replacement (more UI, and the text must pass the same XMP check). Also whether the list shows Google's description text or only its length.
6. **A skipped item's copy**: stays pinned in the inbox while managed cleanup is off (proposed). Once the recovery-point cleanup exists, does a skip let it go by the 14-day rule, or only an explicit discard (managed design section 4)?
7. **Thumbnails** for held import photos, read from the part's zip on request, or name and facts only.
8. **Accepting as it is** (`not_enriched`, `not_settled`) counts as "completed metadata handling" for the future cleanup gate, so the inbox copy may then go by its rules, with Google's values kept only in Darkroom's database (proposed).
9. **Where the hold list lives**: a section of Review (proposed), a tab, or its own page.
