# Managed Immich access boundary

First implementation slice of [#259](https://github.com/AnthonyPluth/Darkroom/issues/259), tracked in [#265](https://github.com/AnthonyPluth/Darkroom/issues/265). This adds a read-only foundation to the Darkroom app. The browser worker is unchanged. No scheduler or import path uses `ManagedReader` yet; there is no managed upload, retention cleanup or migration switch.

## Enforced now

The sole Immich provider rejects unlisted method/path combinations before outbound dispatch. Existing reads, POST metadata search and the existing administrative external-library scan remain allowed. DELETE, replacement, trash, asset mutation and uploads are absent from the allowlist. The old file-based import and reconcile keep their existing behavior.

`ManagedReader` requires an explicit person key and canonical expected owner ID. It rejects the configured administrative key, missing/invalid keys and credential-bearing URLs. It checks `/api/users/me`, `/api/server/about` and `/api/api-keys/me` on every operation, so a previous successful read does not bypass a later owner mismatch, revoked key, permission change or server upgrade.

Only `v3.3.6` is supported by this initial managed reader. Its key must have exactly `user.read`, `server.about`, `asset.read` and `asset.download`; broader or incomplete grants fail closed. This intentionally refuses `all`, upload, update, deletion and library grants. A future write capability needs an explicit reviewed extension after live validation, rather than adding a wildcard operation or reusing an administrative key.

Managed asset reads require the requested ID, expected owner, explicit `libraryId: null`, known type, and explicit untrashed/online flags. Missing or malformed custody fields are not normalised into proof of managed storage. Partner-owned, external, trashed, offline or changed assets cannot supply a managed download. Original downloads explicitly use `edited=false`, compare the current asset checksum to the caller's recorded checksum, then verify streamed bytes against that checksum. Consumers must exhaust the iterator successfully before treating scratch bytes as verified; receiving an early chunk is not a completed receipt.

Keys are omitted from client representations and fixed failure messages. These provider checks do not replace Darkroom's existing person authorization, encrypted key storage or future database/receipt checks. The future caller must load current person state and surface failures without marking an import complete. A successful read is not a backup or a cleanup authorization, and cannot lock out subsequent changes by other Immich clients.

## Evidence and remaining gates

Source inspection is pinned to Immich v3.3.6:

- [`ApiKeyController.getMyApiKey`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/controllers/api-key.controller.ts) uses the authenticated current key without requiring an additional permission. [`ApiKeyService.getMine`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/api-key.service.ts) looks it up for the authenticated user and rejects a non-key session or missing key. The [response DTO](https://github.com/immich-app/immich/blob/v3.3.6/server/src/dtos/api-key.dto.ts) includes permissions.
- [`ServerService.getAboutInfo`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/server.service.ts) returns a version prefixed with `v`; unsupported versions require explicit compatibility review.
- [`AssetMediaController`](https://github.com/immich-app/immich/blob/v3.3.6/server/src/controllers/asset-media.controller.ts) declares the original-download permission and query; downloaded bytes must still pass the checksum check.

Generated fake-server tests cover successful reads, per-operation revalidation, no administrative fallback, revoked/reassigned keys, excessive or malformed grants, invalid versions and IDs, rejected operations before dispatch, invalid custody, original checksum failures and credential canaries. Existing Immich tests cover the legacy workflow.

No disposable Immich endpoint or credentials are configured here. These are source and fake-server results, not live upload/metadata/compound-media verification. [#266](https://github.com/AnthonyPluth/Darkroom/issues/266) remains the live validation gate. Production managed writes, cleanup and migration remain disabled. Backup/restore and current recovery-point coverage must be implemented and validated before any source cleanup.

Rollback removes the unused managed reader and its new allowlist entries. No database, asset or filesystem migration needs reversing, and no uploaded asset should be deleted as compensation.
