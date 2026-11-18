# Managed Immich access boundary

First implementation slice of [#259](https://github.com/AnthonyPluth/Darkroom/issues/259), tracked in [#265](https://github.com/AnthonyPluth/Darkroom/issues/265). This adds a read-only foundation to the Darkroom app. The browser worker is unchanged. No scheduler or import path uses `ManagedReader` yet; there is no managed upload, retention cleanup or migration switch.

## Enforced now

The sole Immich provider rejects unlisted method/path combinations before outbound dispatch. Existing reads, POST metadata search and the existing administrative external-library scan remain allowed. DELETE, replacement, trash, asset mutation and uploads are absent from the allowlist. The old file-based import and reconcile keep their existing behavior.

`ManagedReader` requires an explicit person key and canonical expected owner ID. It rejects the configured administrative key, missing/invalid keys and credential-bearing URLs. It checks `/api/users/me`, `/api/server/about` and `/api/api-keys/me` on every operation, so a previous successful read does not bypass a later owner mismatch, revoked key, permission change or server upgrade.

The managed reader supports exactly `v3.3.6` and `v3.4.1` (`MANAGED_VERSIONS`), each pinned by source inspection; any other version, including `v3.4.0` and a later `v3.4.x`, is refused until it is reviewed the same way ([#411](https://github.com/AnthonyPluth/Darkroom/issues/411)). A reader has an `access`: `read` (the default) requires exactly `user.read`, `server.about`, `asset.read` and `asset.download`; `import`, for the future managed import, requires exactly those four plus `asset.upload`. Broader or incomplete grants fail closed on both: `all`, update, deletion, library, admin and any other extra grant are refused, and a read-only reader refuses the five-grant key. The key's user must report `isAdmin: false`. `import` adds no route: the provider's allowlist still has no upload, bulk-upload check, trash, update or delete. A further write capability needs another explicit reviewed extension after live validation, rather than adding a wildcard operation or reusing an administrative key.

Managed asset reads require the requested ID, expected owner, explicit `libraryId: null`, known type, and explicit untrashed/online flags. Missing or malformed custody fields are not normalised into proof of managed storage. Partner-owned, external, trashed, offline or changed assets cannot supply a managed download. Original downloads explicitly use `edited=false`, compare the current asset checksum to the caller's recorded checksum, then verify streamed bytes against that checksum. Consumers must exhaust the iterator successfully before treating scratch bytes as verified; receiving an early chunk is not a completed receipt.

Keys are omitted from client representations and fixed failure messages. These provider checks do not replace Darkroom's existing person authorization, encrypted key storage or future database/receipt checks. The future caller must load current person state and surface failures without marking an import complete. A successful read is not a backup or a cleanup authorization, and cannot lock out subsequent changes by other Immich clients.

## Evidence and remaining gates

Source inspection is pinned to Immich v3.3.6, and repeated for v3.4.1 with every file the reader relies on diffed against v3.3.6 (tag, commit and blob SHAs in the [design, section 2](../design/immich-managed-storage.md#pinned-v341-sources-411)); `api-key.service.ts` and `api-key.dto.ts` are identical, and the routes, permissions and fields below are unchanged:

- [`ApiKeyController.getMyApiKey`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/controllers/api-key.controller.ts) uses the authenticated current key without requiring an additional permission. [`ApiKeyService.getMine`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/api-key.service.ts) looks it up for the authenticated user and rejects a non-key session or missing key. The [response DTO](https://github.com/immich-app/immich/blob/v3.3.6/server/src/dtos/api-key.dto.ts) includes permissions.
- [`ServerService.getAboutInfo`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/server.service.ts) returns a version prefixed with `v`; unsupported versions require explicit compatibility review.
- [`AssetMediaController`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/controllers/asset-media.controller.ts) declares the original-download permission and query; downloaded bytes must still pass the checksum check.

Generated fake-server tests cover successful reads, per-operation revalidation, no administrative fallback or admin user, revoked/reassigned keys, the exact grant tables for both accesses, excessive or malformed grants, the accepted and refused version tables, invalid IDs, rejected operations before dispatch, invalid custody, original checksum failures and credential canaries. Existing Immich tests cover the legacy workflow.

No disposable Immich endpoint or credentials are configured here. These are source and fake-server results. [#266](https://github.com/AnthonyPluth/Darkroom/issues/266) remains the live validation gate.

The live spike was run on 2026-11-17 against Immich **v3.4.1** with two disposable non-admin users and generated media; the results table is in the [design, section 2](../design/immich-managed-storage.md#live-spike-result-2026-11-17-immich-v341) (12 rows passed, 7 not run). What it means for this reader:

- Confirmed: a person key reads only its own user's assets; another user's asset answers `400 Not found or no asset.read access`, which `ManagedReader.get_asset` turns into a failure. New uploads have `libraryId: null` and the uploader as owner, which is what the custody check requires. Unedited downloads matched the input SHA-1 and size.
- Usable on that server since [#411](https://github.com/AnthonyPluth/Darkroom/issues/411): `v3.4.1` is in `MANAGED_VERSIONS`, and the run's five-grant key is accepted by a reader with `access="import"`; a read-only reader still refuses it and needs a four-grant key.
- The provider sent urllib's default User-Agent, which a WAF in front of a self-hosted Immich (Cloudflare error 1010) refuses. Every provider request, including the address check, now sends `User-Agent: Darkroom`, held by `tests/test_immich.py` against the fake's banned-agent mode.
- Partner-visible, offline and external-library assets were not run, so the reader's refusal of them is still fake-server evidence only. Production managed writes, cleanup and migration remain disabled. Backup/restore and current recovery-point coverage must be implemented and validated before any source cleanup.

Rollback removes the unused managed reader and its new allowlist entries. No database, asset or filesystem migration needs reversing, and no uploaded asset should be deleted as compensation.
