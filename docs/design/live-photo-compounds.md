# Live Photo compounds in Immich-managed imports

Design for [#547](https://github.com/AnthonyPluth/Darkroom/issues/547), part of the Immich-managed storage epic [#530](https://github.com/AnthonyPluth/Darkroom/issues/530), prepared 2026-11-24. It also settles the shared part of [#553](https://github.com/AnthonyPluth/Darkroom/issues/553) (backfilling Live Photos from `google-only/`). The owner signed off on the design in [PR #563](https://github.com/AnthonyPluth/Darkroom/pull/563#issuecomment-7121859590). The interim rules in section 11 apply until the open questions are answered. The approved parent design is [immich-managed-storage.md](immich-managed-storage.md) (cited below as "#259, section N").

## 1. Where things stand

- A Takeout export of an iPhone Live Photo holds two files in the same folder: a still (`IMG_0001.HEIC`, `.JPG`) and its motion video (`IMG_0001.MOV`, `.MP4`), usually with one JSON sidecar, named for the still. A Pixel motion photo can arrive as `PXL_0001.MP.jpg` with a video `PXL_0001.MP`. File names here are made up.
- `pairing.live_match` pairs a video with the one still in the same folder whose stem is the video's stem (or, for a `.MP` video, its whole name), and only when exactly one such still exists. The video gets `live_pair_of`; when it has no sidecar of its own it borrows the still's (`sidecar_from_still`).
- At a managed export's finish (#533, PR #541), `finish_export.live_roots` marks both halves and `managed_ingest.Uploads.due` counts them as held: they are pinned in the inbox and the export's `managed:held:<export>` alert counts them. Keep both on either half is refused (`managed_ingest.keep_for_immich`, using `finish_export.live_decisions`), and the pair stays in review.
- `managed_metadata` (#532) already has what a pair needs once its receipts exist: roles `live_still` and `live_video` in the intended-metadata snapshot, `complete_receipt` refusing a still without a video id (and a video with one), `complete_live_photo` (video first, then the still with `livePhotoVideoId`), and `commit_compound`, which commits every receipt of a compound key in one transaction after a fresh read showing the still linked to its hidden video. It "commits the receipts that exist for the compound key, so its caller creates every member's receipt, checked against the intent's resource manifest, before calling it" (#259, section 3). That caller and that manifest are what this design adds.
- The intent's `resource_manifest` is today the constant `{"compound": "single", "resource": "takeout_entry", "schema": 1}` (`managed_ingest.MANIFEST`), and every receipt's `compound_key` equals its `resource_key` (`takeout-entry:<id>`).
- Nothing outside the tests sets `people.managed_imports` or `people.managed_backfill`, so no production intent or receipt exists yet.

## 2. Evidence

Three levels, as in #259 section 2: **live** (the 2026-11-17 spike on Immich v3.4.1), **source** (the pinned v3.3.6 and v3.4.1 sources) and **unverified on a live server**.

| Fact this design relies on | Level |
| --- | --- |
| A still uploaded with `livePhotoVideoId` naming the owner's video is linked to it, and the video's visibility becomes `hidden` | Live, for a generated JPEG still and AVI video, video sent first, found again by checksum, then the still (spike row "Live Photo pairing, interrupted between uploads") |
| Interrupting between the halves and resuming works: the video is found again by checksum before the still is sent | Live (same row) |
| `livePhotoVideoId` must name a video the caller owns (`requireAccess` with `asset.update`, an ownership check, not a key grant); another user's video answers `400 Not found or no asset.update permission` | Source, v3.4.1. The fake Immich answers `Not found or no asset.update permission` (aligned with the source text in slice 2) |
| A duplicate upload returns the existing ID and applies nothing new from the request (its sidecar does not change the existing asset's metadata) | Live, for a sidecar. **Unverified** for `livePhotoVideoId`: whether a duplicate still sent with a video id gets linked. The fake does not link it |
| `GET /api/assets/{id}` returns `type`, `checksum`, `livePhotoVideoId`, `visibility`, `isTrashed`, `isOffline`, `ownerId`, `libraryId` | Source, v3.4.1; live for the fields the spike read |
| A RAW and its JPEG are two unstacked IMAGE assets, not a compound | Live |
| The video's visibility in the interval between its upload and the still's | **Unverified**: the spike recorded visibility only after the still was sent. Assumed `timeline` (a short visible video) |
| Immich linking a real iPhone HEIC and MOV by their Apple content identifier during metadata extraction, independently of `livePhotoVideoId` | **Unverified** |
| A still that embeds its motion video (Pixel motion photo) uploaded with a separate `livePhotoVideoId`: which video Immich keeps linked. v3.4.1's metadata service extracts embedded motion and replaces "an old motion video on disk only when Immich extracted it" | Source only; the combination is **unverified** |
| The upload DTO's optional `visibility` field | Source; never sent by Darkroom's codec, **unverified** live |
| Large video upload, proxy body size, extraction delay | **Unverified** (#266's open rows) |

Section 9 lists the spike rows that would close the unverified ones.

## 3. Grouping and the resource manifest

### 3.1 The Takeout grouping

A compound is formed from the export's canonical pairing (`finish_export.canonical`), the same one that decides and places today, and only when all of these hold:

1. The video is a canonical root (`Unit.root`, not a copy) whose `live_pair_of` is a still that is also a canonical root of the same export.
2. That still is the `live_pair_of` of exactly one root video. A still claimed by two videos (`IMG_0001.MOV` and `IMG_0001.MP4`) forms no compound.
3. Neither half is in Trash (`special == "trash"`). Both halves are in the same folder by construction of `live_match`.
4. The still is not a Pixel motion photo that embeds its own video (exiftool's `MotionPhoto`, `MicroVideo` or an embedded-video trailer tag), until the spike row in section 9 shows how Immich treats it. Such a pair is held as `embedded_motion`.

A video with `live_pair_of` that fails rule 2, and any still or video whose match `live_match` refused as ambiguous, is not a Live Photo for this purpose. What happens to it is open question Q2; until the owner answers, both stay held exactly as today. A pair that fails rule 4 is also held. "Held" here means what PR #541 already does with a Live Photo half: pinned in the inbox, counted in the export's held alert, listed with its reason (section 7).

The still's `live_pair_of` is computed again on every finish tick from the ledger, so the grouping is **frozen** the first time a receipt of either half is about to be created (section 3.3). After that, the members are read from the database, never re-derived, so a later change in the canonical pairing (a sidecar renamed, a part read again) cannot turn half of a committed compound into a single, or attach a third member.

### 3.2 Roles

| Role | Resource | Immich asset | Sidecar sent | Intended metadata |
| --- | --- | --- | --- | --- |
| `live_still` | the still's root entry | `IMAGE`, visible, `livePhotoVideoId` = the video's asset | from the still's chosen sidecar, under the Takeout-copy rules (`corrects=True`) | `managed_metadata.intend(..., role="live_still")` |
| `live_video` | the video's root entry | `VIDEO`, `hidden` once the still is linked | from its own sidecar when it has one; otherwise the still's, borrowed (`corrects=False`, section 4) | `intend(..., role="live_video")` |

Roles are already part of the intended-metadata snapshot (`managed_metadata.ROLES`), so no receipt column changes. A role is decided once, when the compound is frozen, and the receipt's snapshot carries it; `commit_compound` already refuses any other mix of roles.

### 3.3 Data model (described, not written)

One new Alembic migration, numbered after main's newest when the slice lands (0052 at the time of writing; another pull request adding a migration lands first and this one renumbers), with its `test_<revision>_…` in `tests/test_migrations.py`, on SQLite and PostgreSQL:

**`managed_compounds`**, one row per frozen compound:

| Column | Type | Rule |
| --- | --- | --- |
| `id` | integer, primary key | |
| `intent_id` | FK `managed_import_intents.id`, `RESTRICT` | |
| `compound_key` | string | unique with `intent_id`; `takeout-live:<still root entry id>` for an import, `backfill-live:<still media item id>` for the backfill (section 8) |
| `kind` | string | `live_photo` (a check constraint; the only kind for now) |
| `hold` | string, nullable | a compound-level hold (section 7), `NULL` while it may proceed |
| `frozen_at` | float | |
| `committed_at` | float, nullable | set in the same transaction that commits its receipts |

**`managed_compound_members`**, one row per resource of a compound:

| Column | Type | Rule |
| --- | --- | --- |
| `id` | integer, primary key | |
| `compound_id` | FK `managed_compounds.id`, `RESTRICT` | |
| `resource_key` | string | `takeout-entry:<id>` (or `media-item:<id>` for the backfill); unique with the compound's intent, so a resource belongs to at most one compound |
| `role` | string | `live_still` or `live_video`; unique with `compound_id` |
| `source_sha1` | string | 40 characters, the bytes the receipt must carry |
| `expected_bytes` | `models.Bytes` | `>= 0` |

The intent's `intent_id` is repeated on the member (with a composite foreign key to `managed_compounds (id, intent_id)`) so that the uniqueness of `(intent_id, resource_key)` is a database constraint rather than a query.

**`resource_manifest`, schema 2**: `{"compound": "members", "resource": "takeout_entry", "schema": 2}` for an import, `"resource": "media_item"` for a backfill intent. It declares the rule rather than listing members: a receipt whose `compound_key` has a `managed_compounds` row belongs to that compound, and its members are exactly the member rows; a receipt whose `compound_key` has no such row is a single, and its `compound_key` must equal its `resource_key`. The member rows are the manifest's content, kept in a table because an export can hold thousands of items, rows are written once at freezing, and uniqueness belongs in constraints.

The migration rewrites every existing intent's manifest from schema 1 to schema 2. That is safe because schema 1 never produced a compound receipt (Live Photo halves were held), and because no production intent exists; the migration test checks both a schema-1 intent and one with an unknown manifest (left as it is). Code reading an intent refuses a manifest whose schema it does not know (the export stays pending with a fixed reason), rather than guessing.

`commit_compound` gains one check: for a compound key with a `managed_compounds` row, the set of `(resource_key, role, source_sha1, expected_bytes)` of its receipts must equal its member rows exactly, under the write lock, and `committed_at` is set in the same transaction; for a key without one, there must be exactly one receipt and no member row names it. Receipts keep their existing unique key and state machine.

### 3.4 Freezing

At the finish, after every item is decided and the sidecars are canonical (where `send_to_immich` runs today), for each compound candidate of section 3.1 whose halves are both due (section 5):

1. Read both halves' tags in the same batch (both halves are put into the same chunk of `send_to_immich`'s batching), and work out both intended snapshots.
2. Under the write lock, re-read both roots' decisions and check that no receipt or member row already names either resource. Then insert the `managed_compounds` row, both member rows and both receipts (`prepared`, `compound_key` = the compound's key, roles in their snapshots) in **one transaction**.
3. If either half's snapshot is held before upload (`unknown_offset`, `no_instant`, `unwritable`), the compound is frozen with `hold` = that reason and nothing is ever sent for it (open question Q5 asks whether the video's own hold should be relaxed).

A compound is frozen at most once per intent; a second attempt finds the rows and goes on from them.

## 4. Borrowed sidecars

The video of a Takeout Live Photo usually has no JSON of its own. The rules stay those the index and `managed_metadata` already apply:

- The video's intended metadata comes from its own QuickTime tags first and fills only what they lack from the still's sidecar, with `corrects=False`: a borrowed sidecar never corrects a value the video carries, and gives no quarter-hour offset agreement (#437's rule; #259 section 3, step 4).
- A QuickTime instant is exact in UTC; it takes the offset of the place Immich will see, which may be the still's borrowed `geoData`. With no place and no offset of its own the video is held `unknown_offset`, and so is its compound (section 3.4).
- The borrowed sidecar is the still's **chosen** sidecar at the time of freezing (`Unit.sidecar_id` of the still), and the snapshot frozen into the receipt is what is sent and verified; a later change of the still's chosen sidecar does not change a frozen receipt.
- The XMP sent with the video carries only the fields it lacks (`xmp.upload_sidecar`), so a description borrowed from the still is written to the hidden video too. It is never shown in the timeline; it is verified like any other field, so a mismatch holds the compound like a single.
- When the video has its own sidecar, it is used and nothing is borrowed (`sidecar_from_still` false).

## 5. Which compounds are uploaded

A compound goes to Immich only when **both** halves are wanted:

| Still's decision | Video's decision | Result |
| --- | --- | --- |
| automatic, unreversed tier 5 | automatic, unreversed tier 5 | frozen at the finish and uploaded (section 6) |
| tier 4 in review | tier 5 | the video waits for the still's review; the compound is frozen when Keep both is chosen on the still, and the import tick sends it. Undecided, both stay pinned; the export may complete, as with any review hold |
| tier 5 | tier 4 in review | the same, the other way round |
| tier 4 | tier 4 | frozen when Keep both has been chosen on both; until then each kept half waits |
| tier 1, 2 or 3 (a match in the person's index) | tier 5 | **mixed**: the compound is not formed and the tier 5 half is held as `live_partial` (open question Q1) |
| tier 5 | tier 1, 2 or 3 | the same |
| matched | matched | nothing to upload, as today |

Keep both on a half therefore no longer refuses for being a Live Photo half: it records the decision and, when the other half is also wanted, freezes the compound and its two receipts (the kept half's receipt is created by the freeze, not as a separate `review:<id>` single). When the other half is not yet wanted, Keep both records the decision and says the photo waits for its other half; it is still not undone afterwards, since the compound may be sent. When the other half is matched, Keep both is refused with the `live_partial` reason, as today's refusal is.

A Keep both that freezes a compound marks the kept half's review resolved only when the freeze committed; the existing `send_kept` tick is extended to drive frozen compounds whose receipts are not committed, through the same path as the finish.

## 6. Upload and linking sequence

The sequence for one compound, with what each crash boundary leaves behind. "Tick" is the finish of an unfinished export, or the import tick for a compound frozen by Keep both.

| # | Step | Writes | A crash after it leaves | The next tick |
| --- | --- | --- | --- | --- |
| 0 | Pairing, decisions, tags read | nothing new | nothing | starts again |
| 1 | Freeze (section 3.4) | compound, 2 members, 2 `prepared` receipts, one transaction | rows only; nothing in Immich | goes on at step 2 |
| 2 | **Preflight**: `bulk_check` both checksums, then a checksum search for each. Accept only (a) neither exists; (b) only the video exists and either it is the asset this compound's video receipt recorded (a resume), or the video receipt is `outcome_unknown` with no asset ID (a lost answer) and the asset's checksum equals the receipt's, which step 3's checksum recovery then records as `returned` before anything else is sent; a video with the same checksum but no such receipt of this compound is still a conflict; (c) both exist as the owner's managed, untrashed assets with the still's `livePhotoVideoId` equal to the video's ID (the whole Live Photo is already there). Anything else holds the compound as `compound_conflict` | nothing, or the hold | nothing in Immich | re-runs the preflight |
| 3 | Video: `prepared` → `outcome_unknown` (the existing lease), send, `returned`, unedited read-back, `bytes_verified` | video receipt | a video asset that may be visible on its own (section 2: unverified), or an unknown outcome | `managed_upload`'s checksum recovery finds the video and records it; never a second video |
| 4 | Video metadata: bounded polling, `metadata_verified` | video receipt | a verified, unlinked video | skips to step 5 |
| 5 | **Fresh video read** just before the still: owner, `libraryId: null`, not trashed or offline, checksum, type `VIDEO` (another still linking it in the meantime is caught by step 7's read-back) | nothing, or the compound's hold `unusable` | as step 4 | re-runs step 5 |
| 6 | Still: send with `livePhotoVideoId` = the video receipt's asset ID; read-back; metadata polling, including the link (`managed_metadata.problem`'s `not_linked`) | still receipt | a still that is linked, or an unknown outcome | checksum recovery finds the still; its read-back decides whether it is linked |
| 7 | `commit_compound`: fresh read of both assets; still `IMAGE` and linked to the video; video `VIDEO` and `hidden`; receipts equal the member rows; both `committed` and `committed_at` set in one transaction | both receipts, the compound | the Live Photo, committed | nothing to do |

What the resumes rely on:

- **Only the video uploaded** (crash or failure between steps 3 and 6): the video receipt is `bytes_verified` or `metadata_verified`; the next tick sends only the still. This is the spike's interrupted-pair row.
- **Still sent but its outcome unknown**: the next send is preceded by the bulk check and checksum search (#531); a found still is recorded as `returned`/`duplicate` and read back. If it is linked to this compound's video, it goes on; if it is not linked (a duplicate that kept its old state, or a still someone else uploaded between steps 2 and 6), it is `not_linked`.
- **`not_linked` settles**: today a `not_linked` hold stays pending for ever. Like `mismatch`, it becomes the terminal `not_settled` a day after the still's bytes were verified (`managed_ingest.gave_up` extended to it), so the export completes with the compound held and alerted instead of retrying without end.
- **Never sent twice, never sent alone**: the still is never sent before its video's receipt is `metadata_verified` (already enforced by `complete_receipt`), and never with any other video ID than its own compound's receipt. Nothing sends a still as a single once it is a member.
- **Nothing is undone in Immich**: a video left visible and unlinked by a terminal hold stays in Immich. Darkroom never trashes, hides, updates or deletes it (promise 2's managed paragraph); it is listed (section 7). Open question Q3 asks whether to send the video hidden so that this never shows.

Duplicates:

- A whole Live Photo already in the owner's library (preflight case c) gives two `duplicate` receipts, each read back and verified, and commits without a new asset, as an exact single duplicate does today. Its metadata may be `not_enriched`, as for a single.
- A duplicate video that is a visible standalone video of the owner, or a duplicate still that is linked to another video or to none, is `compound_conflict`: linking would change an asset Darkroom did not create (the upload with `livePhotoVideoId` hides the video), and a duplicate still is not shown to accept a new link (section 2). Open question Q4.

## 7. Failure paths and what the person sees

Every hold keeps both halves' inbox copies pinned (cleanup and part freeing already keep a managed export's copies) and counts in the existing alerts; no new screen is added.

| Reason (fixed text, no file name) | When | Terminal | Seen as |
| --- | --- | --- | --- |
| `live_partial` | one half matched, the other wanted (section 5) | yes, until Q1 is answered | the export's `managed:held` count; Keep both on the wanted half refused with this reason |
| `live_ambiguous` | rules 2 or `live_match`'s ambiguity (section 3.1) | yes, until Q2 is answered | `managed:held` count |
| `embedded_motion` | rule 4 | yes, until its spike row passes | `managed:held` count |
| before-upload holds (`unknown_offset`, `no_instant`, `unwritable`) of either half | freezing | yes | `managed:held` count; on Keep both, refused with the existing reason |
| `compound_conflict` | preflight (section 6, step 2) | yes | `managed:held` count |
| `unusable` | the video changed, was trashed or went offline before the still was sent, or an asset of the compound failed its read-back | yes | `managed:held` count; nothing restored or removed |
| `not_linked`, then `not_settled` | still not linked after polling, for a day | after a day | pending (`managed:pending`) for a day, then `managed:held` |
| `mismatch`, `not_enriched` | metadata of either half, as for a single | as for a single | as for a single |
| Immich down, refusing, uncertain answer, key not usable | any step | no | `managed:pending`; the export stays unfinished and is retried each tick |
| quota exceeded on the still after the video went | step 6 | no, retried | `managed:pending`; the visible video stays as in Q3 |

A Keep-both compound that ends in a terminal hold gives the existing `managed:kept:<review>` alert once; one still not committed a day after Keep both gives it too (`managed_ingest.overdue`). Logs carry receipt and compound IDs, counts and fixed reasons, never a file name, a path or an Immich payload (`no_leaks` canaries).

## 8. Interaction with the backfill (#553)

The backfill (#535, step 7a) lists Live Photo halves as the `live_photo` exception today. #553 uses this design's tables, roles, sequence and holds unchanged; it differs only in how it groups:

- **Grouping in `google-only/`.** A placed Takeout half keeps no link to its partner (`media_items` keeps only `sidecar_from_still` on the video), and placement may have renamed a file on a name collision, so stems in `google-only/` are not evidence. Group instead by each item's recorded Takeout path: two Takeout items of the person whose unreversed decisions in the **same export** have `source_path`s that `pairing.live_match` would pair (same folder, the stem rule, exactly one still), applying section 3.1's rules 2 to 4 to those paths. `backfill.live_halves`'s companion index, which already finds halves this way, becomes the candidate list; a half whose partner cannot be found this way (its export's decisions are gone, or the partner is not a Takeout item of the person) stays the `live_photo` exception.
- **Keys.** `managed_compounds.compound_key` = `backfill-live:<still media item id>` under the person's backfill intent (`kind = 'backfill'`); members' `resource_key` = `media-item:<id>`; the intent's manifest is schema 2 with `"resource": "media_item"`.
- **Borrowed sidecar.** The video's stored `sidecar_json` (copied from the still at ingest) with `sidecar_from_still` gives `corrects=False`, as the backfill already does for singles.
- **Mixed pairs.** A losing-copy removal or a match may have left one half only. Section 5's mixed row applies: the remaining half stays the exception (`live_partial`), subject to Q1.
- **Mapping instead of uploading.** When the preflight finds the whole Live Photo already in the person's managed library (case c), both members are mapped to verified versions with `takeout` origins, as a single duplicate is.
- **Phone Live Photos** in `originals/immich/` (an `immich_assets` row with a linked `live_video`) are already in Immich as a compound. #553 maps them by the unedited read-back of both assets and the link check of section 6, step 7, with no upload.
- **Shared code.** Freezing, the preflight, the sequence and `commit_compound`'s member check are one module (`darkroom/domain/managed_compound.py`) called by both `managed_ingest` and `backfill`. The backfill keeps its own rules: it reads and writes nothing under `google-only/` or `originals/`, deletes nothing, and moves only existing Pixel copies, and a compound it may have sent is neither reassigned nor undone (409 with the reason), for both halves.

## 9. Parity checks, spike rows and negative tests

### Parity, per compound

Each member: the receipt's asset ID is the owner's (`ownerId` = the intent's owner), managed (`libraryId: null`), not trashed or offline; the decoded checksum equals `source_sha1`; the unedited download's SHA-1 and size equal the source's; the metadata obligations of its role hold. The compound: the still is `IMAGE` and its `livePhotoVideoId` is the video receipt's asset ID; the video is `VIDEO` and `hidden`; the two asset IDs differ; the receipts equal the member rows exactly (resource, role, SHA-1, bytes). All of these are read again in the transaction that commits; counts and names never count as parity.

### Spike rows to add to #266 (owner-run, disposable users, generated media)

1. A real-format pair (HEIC still and MOV video carrying a shared content identifier, generated, no real photo): does Immich link them by itself during extraction, and does that agree with `livePhotoVideoId`?
2. A duplicate still sent with `livePhotoVideoId`: is it linked, left as it was, or refused?
3. A still sent with `livePhotoVideoId` naming the owner's pre-existing visible video: is the video hidden?
4. The video's visibility between its upload and the still's, and the effect of sending `visibility: hidden` with it (Q3).
5. A JPEG still embedding a motion video, sent with a separate video's `livePhotoVideoId`: which video stays linked, and is a second motion asset created?
6. The answer for a `livePhotoVideoId` that does not exist and for another user's video, to pin the 400 bodies.

Rows 2 and 3 decide whether section 6's preflight can ever accept those cases; until then it refuses them. Row 5 gates rule 4. No row needs a key beyond the five import grants.

### Negative tests (each on the fake Immich, SQLite and PostgreSQL where it touches locking)

- A still is never sent before its video is `metadata_verified`, and never sent as a single once it is a member; a still sent with any other video ID is refused before sending.
- A crash at each boundary of section 6 (after the freeze, after the video's `outcome_unknown`, after its `bytes_verified`, after the still's `outcome_unknown`, between the receipts' `metadata_verified` and the commit) resumes to the uninterrupted result with one video and one still in Immich.
- A lost answer for the video, then for the still: each found by checksum, no second asset.
- The preflight refuses: a visible standalone duplicate video; a duplicate still linked to another video; a duplicate still linked to none; either checksum owned by another user only (not a conflict: owner-scoped, the upload goes ahead); a trashed duplicate.
- The video trashed, offline, changed or gone between steps 4 and 6: the still is not sent and the compound is `unusable`.
- A still returned unlinked: `not_linked`, pending, then `not_settled` exactly at a day and not before.
- A third receipt or member appearing, or a member's SHA-1 changing, while `commit_compound` checks: nothing commits.
- The canonical pairing changing after the freeze (a sidecar renamed, a part read again): the frozen members are used; no half becomes a single.
- Two videos claiming one still, two stills of one stem, a trashed half, a Pixel still with embedded motion: no compound, each held with its reason.
- Mixed decisions in each direction: no compound, the wanted half held `live_partial`, nothing sent.
- Keep both on a half whose partner is tier 5: the compound is frozen and sent by the import tick; on a half whose partner is still in review: the decision recorded, nothing sent; on a half whose partner matched: refused with the reason, no receipt.
- Either half's snapshot held before upload: the compound held, nothing sent for either half.
- A borrowed sidecar never corrects the video's own instant; a video with no offset and no place holds the compound.
- A manifest of unknown schema: the export stays pending, nothing sent.
- The migration's rewrite of a schema-1 intent, and an unknown manifest left alone.
- Nothing in any path sends trash, delete, restore, update or a visibility change to Immich (the provider's allowlist test, extended to the compound path).
- `no_leaks` canaries for the key, a file name, a place and the Immich URL in every new log line and alert.
- For #553: two halves renamed apart in `google-only/` are grouped by their recorded Takeout paths; a half whose partner was removed as a losing copy stays an exception; the backfill writes nothing under `google-only/` or `originals/`.

## 10. Slicing

Each slice is one issue and one pull request under 1,000 added lines including tests, with behaviour unreachable outside the tests until the switch (#530) lands.

| Slice | Content | Estimate |
| --- | --- | --- |
| 1. Schema and grouping | The migration (`managed_compounds`, `managed_compound_members`, manifest schema 2 and the rewrite), models, `managed_compound.groups` over a `Canon` (section 3.1's rules, pure), `freeze`, and `commit_compound`'s member check; the migration test and grouping and freeze tests | ~600 |
| 2. Finish-time compounds | `Uploads.due` stops holding Live Photo halves; same-chunk batching of both halves; the preflight; the fresh video read; `not_linked` settling; holds and counts; the fake's 400 text; the crash-boundary, conflict and resume tests | ~850 |
| 3. Keep both and mixed decisions | `keep_for_immich` freezing a compound when both halves are wanted, waiting or refusing otherwise; `send_kept` driving frozen compounds; the Keep-both and mixed tests | ~550 |
| 4. Backfill Live Photos (#553) | Grouping by recorded Takeout paths, backfill intent keys, phone Live Photo mapping, the backfill tests | ~800, its own issue |
| Spike rows | The six rows of section 9, run by the owner on #266 | no code in the repo beyond the harness's new rows (~250) |

Slices 1 to 3 depend in order; slice 4 depends on 1 and 2. The answers to Q1 to Q5 change only which branch of section 5 or 6 a hold takes, so slices 1 and 2 can be built before they are answered, with the interim "held" behaviour.

## 11. Open questions for the owner

These are user-visible and are not chosen here. Each has an interim behaviour that keeps today's state (the half or pair held in the inbox and counted in the alert), which the slices build until the owner answers.

- **Q1. A lone half.** When one half of a Live Photo matched something the person already has (or, in the backfill, its partner was removed as a losing copy), should the other half go to Immich on its own, as a separate still or a visible short video, or stay held? Interim: held as `live_partial`.
- **Q2. Ambiguous pairs.** When the pairing cannot tell which still a video belongs to (two stills of one name, or two videos for one still), should the files go to Immich as separate photos and videos (what the old `google-only/` placement did), or stay held for a person to look at? Interim: held as `live_ambiguous`.
- **Q3. The video between the halves.** After the video is uploaded and before the still links it, the person's timeline may show it as a separate short video, and if the still then fails for good, it stays visible (Darkroom never hides or removes an asset). Accept that, or send the video hidden (an unverified field, spike row 4), so that a failed pair leaves a video nobody sees in the timeline? Interim: sent as the live spike did, without `visibility`.
- **Q4. Linking to something already in Immich.** When the video is already in the person's Immich as a visible standalone video, or the still is already there unlinked or linked to another video, should Darkroom link the pair (which hides the existing video, a change to an asset Darkroom did not create), or hold it for the person to fix in Immich? Interim: held as `compound_conflict`.
- **Q5. The hidden video's metadata.** The video of a Live Photo is hidden in Immich, but its capture time, offset and place are verified like any photo's. When only the video's offset is unknown (a QuickTime time with no place), should the whole Live Photo stay held, or should the video's offset be allowed to stay unknown while the still's metadata is complete? Interim: held, as for any resource with an unknown offset.
