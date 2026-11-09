# Streaming upload codec, without upload transport

[Issue #279](https://github.com/AnthonyPluth/Darkroom/issues/279) begins step 3 of the [approved storage design](../design/immich-managed-storage.md) with pure multipart construction and response parsing. No route is added to the outbound allowlist: generic `POST /api/assets` is still refused before dispatch. There is no upload caller, TLS change, scheduler, receipt-state writer, retry, metadata merge or cleanup integration.

## Pinned source contract

Source inspection targets Immich v3.3.6. The [request DTO](https://github.com/immich-app/immich/blob/v3.3.6/server/src/dtos/asset-media.dto.ts) declares `assetData`, optional `sidecarData`, `fileCreatedAt`, `fileModifiedAt`, optional `filename` and `livePhotoVideoId`. This first codec supports that subset; it does not synthesize capture metadata, favourites, visibility or generic key-value metadata.

The [upload controller](https://github.com/immich-app/immich/blob/v3.3.6/server/src/controllers/asset-media.controller.ts) declares 201 for a created asset and 200 for a duplicate. The [response DTO](https://github.com/immich-app/immich/blob/v3.3.6/server/src/dtos/asset-media-response.dto.ts) contains a UUIDv4 ID and `created`/`duplicate` status. The [service](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/asset-media.service.ts) uses `dto.filename || file.originalName` for the original name. These are source facts, not live upload results; [#266](https://github.com/AnthonyPluth/Darkroom/issues/266) remains unverified.

## Bounded body construction

`UploadSource` receives a caller-owned binary reader, positive declared byte count and lowercase SHA-1. `MultipartUpload` constructs bounded metadata/framing and computes the full Content-Length without reading the media. Iteration reads at most the existing download chunk size, hashes the bytes, rejects early EOF or excess data, and checks the recorded checksum before finishing each file part. It does not seek, rewind, close or rewrite the source. A body is single-use, including after partial consumption or failure. Retrying requires a new body and deliberately reopened/revalidated immutable inputs.

An unpredictable boundary and controlled ASCII file-part names keep user metadata out of multipart headers. The original basename, including Unicode and quotes, is preserved in the `filename` field. Path separators, NUL, CR/LF, missing/unsupported extensions and oversized/invalid UTF-8 names are refused rather than silently renamed. Optional XMP uses its own independently checked reader and fixed `sidecar.xmp` header filename.

Created/modified timestamps must be valid RFC 3339 strings with seconds and a known explicit offset; naive and unknown `-00:00` offsets are refused. Accepted strings are transmitted unchanged. These file timestamps are not an inferred replacement for EXIF-first capture metadata or missing XMP offsets. Canonical UUIDv4 validation applies to an optional Live Photo video ID, without claiming that another resource or its ownership was verified.

The `complete` property means the body iterator was exhausted successfully, including byte/hash checks. It is not an upload result or custody receipt. Receiving an early server response, exhausting some chunks or getting a duplicate ID cannot establish metadata enrichment or whole-item completion.

## Response and failure boundaries

`upload_result` recognizes only canonical UUIDv4 IDs paired with `(201, created)` or `(200, duplicate)`. All other response shapes/outcomes fail closed with fixed messages. The result ID is hidden from representations. Reader errors and invalid metadata do not echo private filenames, paths or raw responses; the codec accepts no credentials and performs no HTTP calls.

A future transport must persist its intent before dispatch, revalidate owner/version/grants and use a separately reviewed narrow write capability. Any source/read/network/response failure after dispatch may leave an uncertain outcome, even if the body never completed. Keep sources and reconcile by owner/checksum before any bounded retry; do not compensate by deleting an asset. Ownership, managed custody, unedited read-back, size/hash, metadata, compound links, whole-export completion and backup/recovery gates remain mandatory before cleanup. Production writes stay disabled until the relevant live validation passes.

## Validation

All test execution runs in GitHub Actions. Generated in-memory tests use an independent MIME parser to check framing, exact length, fields and bytes; track bounded reads and source ownership; reject changed/short/long/unreadable sources and body reuse; exercise malformed timestamps/names/IDs/outcomes; check private-value canaries; and confirm that generic upload transport still refuses before networking. No test uploads or personal media are used.
