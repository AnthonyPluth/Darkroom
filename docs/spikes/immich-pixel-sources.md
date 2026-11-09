# Inactive versioned Pixel sources

[Issue #277](https://github.com/AnthonyPluth/Darkroom/issues/277) adds the Pixel-reference portion of step 2 in the [approved storage design](../design/immich-managed-storage.md), after the [asset catalogue](immich-asset-catalog.md). Migration `0028` adds an empty source-reference table and supporting indexes. It leaves existing queue records and files unchanged. There is no producer, backfill, download, runtime source selector, placement, pruning or cleanup integration.

## Identity and historical generations

`managed_pixel_sources` binds a stable Pixel copy/Pixel/item/person identity to an exact same-person Immich owner/asset/checksum/byte-size version. Composite foreign keys reject a different copy identity, a media item belonging to another person, a mismatched owner/hash/size or a catalogue version whose size is still unknown. Byte sizes use PostgreSQL `BIGINT` through the existing portable type.

Each row records the queue generation, destination Google account ID and email, opaque copy-state snapshot and association time. Only one source is allowed per copy/generation/account ID. Account emails can change, so the email is also captured independently; a new email does not authorize replacing that generation's source. Snapshots retain their supplied Unicode, whitespace and timestamp values; they are historical data, not instructions to restore stale queue state.

The generation and destination are intentionally not foreign-keyed to the mutable `PixelCopy.requeued` counter, the Pixel's current account or that account's current email. Older bindings must survive requeue, account reassignment and email changes. Current copy/item hashes and byte counts are also not pinned by these historical references: later generations can use different content without rewriting earlier source history. The source hash/size is pinned to its catalogue version. Stable copy, item and person identities remain restricted while referenced.

The reference is **not** proof that the current Pixel copy has these bytes, that the destination is still current, that Google has uploaded anything or that Immich still holds an available original. Before enabling producers, validate the current copy/item/person, serving relationship, generation, account ID/email, hash and size under the write lock and freshly verify the owner/version. Future consumers must compare those current values again and refuse stale or unavailable sources. Account/email changes require an explicit new generation before binding the new destination; snapshots must not be rewritten to make old proof look current. A historical generation must never supply confirmation for a later generation or a different account. This slice stores no Google-confirmation flag and changes no existing proof, cap, always-send or reversal rule.

## Preservation and rollback

Supporting unique indexes are added to `pixel_copies`, `media_items` and `managed_asset_versions` without rebuilding their tables. Existing queue states, names, sizes, hashes, placement/proof timestamps, requeue counts and always-send flags remain intact. Existing inconsistent legacy associations are not repaired or promoted into verified references.

Foreign keys restrict deletion of referenced copies, media items, catalogue versions and historical destination accounts. Before production writers are enabled, affected routes must handle these dependencies explicitly; a constraint must not become an unexplained server error. No automatic retirement or pruning of source history is authorized here.

Keep populated references and migration history, and disable future producers/consumers with a forward patch. The downgrade takes PostgreSQL table locks or SQLite's write lock before checking emptiness and refuses if any reference remains. An empty reference table can return to `0027`, preserving populated queue, catalogue, origin and receipt tables and their earlier indexes. Stop workers before schema operations. An older image requires a successful empty-reference downgrade; restoring a stale database snapshot would lose intervening state.

## Validation and remaining gates

All test execution is in GitHub Actions, per the owner's instruction. Added SQLite/PostgreSQL cases cover schema/model parity, upgrade over populated queue/catalogue/journal data, exact rollback preservation, snapshots and large sizes, cross-identity failures, duplicate generations, requeue/content/account changes, protected history and populated-reference downgrade refusal. Older catalogue rollback tests capture their starting revision as the schema advances.

Runtime Pixel conversion remains step 6. Live upload, metadata, compound-media and recovery behavior remains gated on [#266](https://github.com/AnthonyPluth/Darkroom/issues/266). Managed imports and source cleanup remain disabled.
