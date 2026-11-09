# Inactive managed asset catalogue and origins

[Issue #274](https://github.com/AnthonyPluth/Darkroom/issues/274) continues step 2 of the [approved storage design](../design/immich-managed-storage.md) after the [receipt journal](immich-import-journal.md). Migration `0027` adds asset versions and independent origin records. It enables no production collector, matcher, upload, Pixel operation or cleanup, and changes no media path or legacy row.

## Asset versions

`managed_asset_versions` identifies a byte version by person, Immich owner, asset ID and SHA-1. A changed checksum is a distinct row; identical bytes belonging to different owners or asset IDs are not merged. An observed size can remain unknown rather than becoming a fabricated zero. Known sizes use the existing portable PostgreSQL `BIGINT` type. Metadata snapshots and timestamps retain their supplied values.

States are `observed`, `bytes_verified`, `missing`, `trashed`, `offline`, `external` and `superseded`. The byte-verified shape requires a size, verification timestamp and null external-library ID. An external state requires a library ID. A previous verification timestamp can remain as history after an asset becomes unavailable; it is not fresh custody proof. The linked video asset ID is only an observed remote value, not a verified compound relationship.

There is no producer or invalidation loop in this slice. Before activation, the collector must validate canonical IDs/checksums and ownership, verify unedited bytes, refresh metadata independently of `updatedAt`, mark superseded/unavailable versions and invalidate dependent assumptions. Consumers must freshly recheck the relevant version; a historical byte-verified row cannot authorize source cleanup or Pixel pruning.

## Independent origins

`managed_asset_origins` preserves phone, Takeout, legacy and other origins independently. A later Takeout upload cannot overwrite a phone origin. The stable source key must distinguish its source event/export/account; using only a filename would lose provenance across exports. Distinct origin records can point at the same byte version.

Composite foreign keys bind each origin to the exact person/owner/asset/checksum version. An attached upload receipt must belong to the same person/owner intent and the same asset/checksum, and be committed. Partial references, different owners or source versions, and uncommitted receipts are rejected. Takeout origins require this receipt; phone origins cannot use an upload receipt as their source. Legacy/other origins can retain independent evidence or an applicable committed receipt. Supporting unique indexes are added to the existing journal without rebuilding its tables or changing its data.

These constraints enforce association shape, not the truth of a claimed origin or an API verification. Future producers must verify classification from independent evidence, preserve immutable source events and handle revocation/reassignment. An origin is not Google confirmation: destination-specific unreversed Takeout matching, late-phone policy, upload-loop prevention and fresh custody checks still belong to later workflow changes. Policy matches with different bytes are not represented as exact upload origins. Versioned Pixel references and runtime source conversion remain separate slices. [#266](https://github.com/AnthonyPluth/Darkroom/issues/266) still gates live upload/metadata/compound-media behavior.

## Rollback

Keep populated additive tables and their migration history, and disable future producers/consumers with a forward patch. Do not restore a stale database snapshot or start an older image that does not recognize `0027`.

The downgrade locks the new tables before querying emptiness and refuses if any asset version or origin exists, including unverified observations. An empty catalogue can return to `0026` while retaining a populated receipt journal; only the new tables and supporting indexes are removed. Stop workers before schema operations. Foreign keys restrict deletion or rewriting of referenced identities instead of cascading away history. Explicit evidence retirement and user-facing dependency handling must be defined before production writers are enabled; this migration adds no pruning policy.

## Validation

GitHub Actions runs all tests, per the owner's instruction. Added cases cover schema/model parity, upgrade over an existing receipt journal, empty-catalogue downgrade, metadata/large-count round trips, independent owners and versions, multiple origins, bad custody shapes, cross-owner/asset/checksum/receipt associations, retained binding identities and populated-catalogue downgrade refusal on SQLite and PostgreSQL. Tests for the older journal capture the starting revision so they continue to assert that a refused rollback preserves the schema revision as migrations are added.
