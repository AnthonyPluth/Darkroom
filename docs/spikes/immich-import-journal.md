# Inactive managed-import receipt journal

[Issue #272](https://github.com/AnthonyPluth/Darkroom/issues/272) implements the first part of step 2 in the [approved storage design](../design/immich-managed-storage.md). Migration `0026` only adds tables. Existing media, duplicate-removal, Pixel, export and account records keep their IDs and values. No original is copied, moved or removed, and no managed upload, state-advancement job or cleanup path is added.

## Stored intent and evidence

`managed_import_intents` binds an export and destination person to an explicit Immich owner ID. Its version-1 resource manifest is an opaque text snapshot; it is not reconstructed from mutable person settings on retry. A unique person/owner/export identity prevents a second identical intent. A later explicit owner reassignment can retain a separate intent without overwriting the earlier binding.

`managed_import_receipts` records each intended upload resource's stable key, compound-resource group, immutable source SHA-1, expected byte count and metadata snapshot. Its unique intent/resource/checksum identity separates retries from changed source bytes. Expected and verified byte counts use PostgreSQL `BIGINT`, matching the existing portable `Bytes` type.

The stored states are `prepared`, `outcome_unknown`, `returned`, `bytes_verified`, `metadata_verified` and `committed`. Constraints require a returned asset/outcome before later states, matching source checksum and byte-count fields at byte verification, metadata evidence/timestamp at metadata verification, and a commit timestamp only at `committed`. Earlier states cannot carry later-state evidence. Created and duplicate upload outcomes remain distinguishable. Snapshots, Unicode and timestamp values are stored without normalisation.

These constraints check record shape, **not that an API call or verification actually occurred**. A receipt row is not custody proof, backup coverage or permission to delete a source. There are no production writers to either table. Before activation, a follow-up must validate canonical IDs/hashes and snapshot contents, current person/owner binding, immutable source identity, permitted transitions, fresh same-owner managed read-back, metadata obligations and atomic completion of every required compound member under the write lock. The resource catalogue, multiple origins, policy-match receipts, versioned Pixel references and whole-export completion remain separate follow-ups. [#266](https://github.com/AnthonyPluth/Darkroom/issues/266) still gates live upload/metadata assumptions.

## Rollback and retention

The normal rollback keeps these additive tables and disables their future readers/writers. An old database snapshot must not overwrite imports, decisions or Pixel changes made since that snapshot.

The migration downgrade refuses if either journal table contains any row, including an unverified intent. It takes PostgreSQL table locks, or SQLite's write lock, before checking emptiness so a concurrent insert cannot be silently lost between the check and a drop. An empty journal can be downgraded to `0025` without changing existing records. Stop workers before operating on the schema.

Foreign keys restrict deleting a referenced intent, export or person instead of cascading away journal evidence. There is no new user-facing deletion behavior while the journal has no producers. Before enabling producers, the affected deletion routes must report this dependency clearly and define explicit retirement of retained evidence; they must not turn a database constraint into an unexplained server error. This migration authorises no journal-pruning policy.

## Validation

The receipt tests cover snapshot and large-count round trips, separate owners and source versions, retry uniqueness, invalid state/evidence combinations, missing parents, cascade refusal and populated-journal downgrade refusal. The migration test checks schema/model parity, unchanged legacy rows and empty-journal downgrade/re-upgrade. The same tests run against SQLite and PostgreSQL; no live Immich credentials or personal media are needed.
