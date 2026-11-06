# Immich reconcile: what the job can rely on (spike for #202)

Findings for the reconcile job in #199, from read-only requests against the running server and from Immich's documentation. The server runs Immich v3.3.6 (`/api/server/about`). Nothing was written to the server. No key, file name, location, person or photo appears here; the server held 5 assets of one user, so what follows is field names, counts and structure.

Each answer is marked **observed** (seen on the server or in its own OpenAPI at `/api/spec.json`), **documented, not observed** (from Immich's documentation or release notes, with the link) or **not determined**.

## Summary

- List changed assets with `POST /api/search/metadata` and `filter.updatedAt` (observed). `updatedAfter` still works but is deprecated since v3.2.0.
- `checksum` is the base64 of the original file's SHA-1 (observed: a downloaded original matched). Hex is not what the API returns.
- v3.3.6 has no `deviceId`, `deviceAssetId` or `isExternal` anywhere in its API (observed). A phone upload is recognised by the `mobile-app` asset metadata key (with `iCloudId`) and `libraryId` being null; an external-library asset has a non-null `libraryId`.
- The original downloads from `GET /api/assets/{id}/original` with `Content-Length`, `Accept-Ranges: bytes` and no size cap seen. Immich keeps files under `upload/` and, after the storage template job, under `library/`, so a hardlink must not depend on the path.
- What "Free Up Space" matches on, and how it treats RAW+JPEG pairs, Live Photos and edited versions, is not stated in Immich's documentation. Not determined.

## 1. Listing new and changed assets

- Endpoint: `POST /api/search/metadata` (permission `asset.read`). It is a read-only search despite being a POST. **Observed.**
- With no `visibility` filter it returns every visibility, including the hidden Live Photo video halves (5 of 5 assets); `GET /api/assets/statistics` and `GET /api/server/statistics` count only the 1 visible asset, not the 4 hidden ones. A job that counts from those statistics will undercount. **Observed.**
- Changed since a time: `{"filter": {"updatedAt": {"gte": "<ISO time>"}}}`. The legacy top-level `updatedAfter` also works, but it, `page`, `order` and `withDeleted` are deprecated since v3.2.0 and the server rejects `withDeleted` together with `filter`, `orderBy` or `cursor` (HTTP 400). **Observed.**
- `updatedAt` has sub-millisecond precision in the database but the API shows milliseconds. A `gt` on the exact value shown for a row returned that row again, so the job should keep an overlap (for example the last `updatedAt` minus a minute) and dedupe by asset id and checksum. **Observed.**
- Paging: `size` from 1 to 1000 (default 250); the new way is `cursor` with `orderBy: {"field": ..., "direction": ...}`, where the response carries `nextCursor` until the last page (observed over three pages of 2). The old `page` and `nextPage` pair also worked. **Observed.**
- `orderBy.field` can be `fileCreatedAt`, `localDateTime`, `fileSizeInBytes` or `rating` only, not `updatedAt`, so a window defined by `updatedAt` has to be read to its last page before the new high-water mark is stored. **Observed.**
- Fields in each item that the job needs: `id`, `type`, `ownerId`, `libraryId`, `checksum`, `originalFileName`, `originalMimeType`, `originalPath`, `fileCreatedAt`, `fileModifiedAt`, `localDateTime`, `createdAt`, `updatedAt`, `visibility`, `livePhotoVideoId`, `isTrashed`, `isOffline`, `isEdited`, `duplicateId`, and `exifInfo` (including `fileSizeInByte`, `dateTimeOriginal`, `timeZone`, latitude and longitude) when `withExif` is set. **Observed.**
- Trashed assets: a plain search leaves them out (the legacy `withDeleted` includes them). Permanently deleted assets cannot be found by search at all; only the sync stream reports them (`AssetDeleteV1`). The server's trash window is 30 days (`trashDays` in `/api/server/config`). **Observed for the setting; the sync stream was not called, see below.**
- Locked-folder assets (`visibility: locked`) need an elevated session; asking for them with the API key returned 401 "Elevated permission is required". The job will not see those and should not need to. **Observed.**
- Other listings: `GET /api/timeline/buckets` and `GET /api/timeline/bucket` return per-month counts and columnar rows (id, owner, visibility, `livePhotoVideoId`, `fileCreatedAt`, `createdAt`) but no `checksum` and no `updatedAt`, so they cannot drive a reconcile. A default timeline request showed 1 asset where `visibility=hidden` showed 4. **Observed.**
- Sync stream: the spec has `POST /api/sync/stream` (types such as `AssetsV2`, `AssetExifsV1`, `AssetMetadataV1`) with deletions and acknowledgements via `POST /api/sync/ack`. It is how the phone app syncs and is the only way to learn of permanent deletions, but acknowledging changes server-side state, so it was not called. **Not determined (not exercised); spec only.**
- Rate limits: 60 sequential and 100 parallel (10 at a time) searches all returned 200, with no rate-limit or `Retry-After` headers. The server sits behind Cloudflare's proxy, whose own rules could still apply. No limit found. **Observed.**
- Per-user scope: the API key sees the owner it belongs to. `GET /api/admin/users` answered (1 user), so an admin key can list users, but search returned only the key owner's assets. A reconcile that covers each person needs one key per person, or an admin key plus a per-owner filter that was not tested with several users. **Observed for one user; multi-user behaviour not determined.**

## 2. Is `checksum` the SHA-1 of the original?

- Yes. The smallest original on the server was downloaded (2,743,537 bytes, HEIC), its SHA-1 computed locally, and the base64 of the digest equals the asset's `checksum`; the hex form does not equal it. The file was deleted afterwards and nothing was uploaded. **Observed.** The OpenAPI also describes it as "Base64 encoded SHA1 hash", 28 characters, 20 bytes decoded.
- Upload-side inputs accept either form: `POST /api/assets/bulk-upload-check` takes "Base64 or hex encoded SHA1 hash" and the `x-immich-checksum` upload header takes a SHA-1. The search filter `filter.checksum.eq` (and the legacy `checksum`) matches the base64 form; a lookup by checksum found the asset. **Observed.**
- Darkroom stores hex SHA-1 for its fingerprints, so convert with base64 decode then hex before comparing.
- Live Photos: the still image and its video half are two separate assets. The still carries `livePhotoVideoId` pointing at the video; the video has `visibility: hidden`, its own `id`, its own `checksum` and its own file. `POST /api/download/info` for the still alone returned 2 assets in one archive whose size equals the sum of both files. A Live Photo is therefore two checksums, one per resource, matching how eligibility counts every resource. **Observed.**
- Orphaned halves: 3 of the 4 hidden videos had no still pointing at them, and no metadata. They are presumably videos uploaded before their still arrived (the phone app uploads the video first), so the job should wait for the still rather than treat a hidden video with no parent as a standalone video. Why they have no still was not determined. **Observed (counts); cause not determined.**
- Edited versions: the asset has `isEdited`, and edits (crop, rotate, mirror) are stored separately and read with `GET /api/assets/{id}/edits`. `GET /api/assets/{id}/original?edited=true` is documented as returning the edited rendition if there is one. The `checksum` field is documented as the hash of the uploaded file. No edited asset existed to test, so that `checksum` stays the original's after an edit and what `edited=true` returns are **documented, not observed** (spec text); the original is the default of the download.
- RAW+JPEG pairs: nothing in the asset model links the two; they would arrive as separate assets with separate checksums. No pair existed to test. **Not determined.**

## 3. Phone upload or external library?

- `deviceId`, `deviceAssetId` and `isExternal` do not exist in the v3.3.6 OpenAPI (0 occurrences) or in any response. Immich's earlier fields for this are gone from the API on this version. **Observed.** When they were removed was not found in the release notes consulted.
- `libraryId`: null for every asset here (the server has no external libraries; `GET /api/libraries` returned an empty list). An external-library asset has a `libraryId` (it also appears in the sync model, `SyncAssetV2.libraryId`) and `GET /api/libraries/{id}` gives the library's `ownerId` and `importPaths`. A null `libraryId` means the asset was uploaded. **Observed for null and the empty library list; the non-null case is from the spec.**
- `ownerId` identifies the user; with one Immich user per person it maps an asset to a person. **Observed.**
- `GET /api/assets/{id}/metadata` returns key-value metadata. The phone app's still image has a `mobile-app` key whose value has the keys `iCloudId`, `createdAt`, `latitude` and `longitude`. The hidden video halves have no metadata. So `mobile-app` marks a phone upload of a still, and `iCloudId` links it to the phone's photo library entry, but it is absent on Live Photo video halves and would be absent on uploads from the web or the CLI. **Observed.**
- Immich 2.5.0's release notes say uploads now carry unique per-asset metadata so the server can return the checksum after a reinstall, and iOS users tap "Sync Cloud IDs" (App Settings, Sync Status) once to backfill existing uploads. Assets uploaded before that may lack `iCloudId`. **Documented, not observed:** https://github.com/immich-app/immich/releases/tag/v2.5.0
- A rule that holds up: owner by `ownerId`; phone upload when `libraryId` is null; a video half is whatever a still's `livePhotoVideoId` points at. Whether a CLI or web upload could be told apart from the phone app by `mobile-app` was not tested (no such asset).

## 4. Downloading the original, and where files sit

- `GET /api/assets/{id}/original` returned 200 with the exact bytes (`Content-Length` equal to `exifInfo.fileSizeInByte`), `Content-Type` of the original, `Content-Disposition: inline` with the file name, `Accept-Ranges: bytes`, a weak `ETag`, `Last-Modified` equal to the file's modification time (`fileModifiedAt`), and `Cache-Control: private, max-age=86400`. A `Range: bytes=0-9` request returned 206 with 10 bytes, so a resumed download works. 2.7 MB took about 1 second through the proxy. **Observed.**
- The response comes through Cloudflare (`Server: cloudflare`, `Cf-Cache-Status: DYNAMIC`). A 100 MB cap applies to request bodies on the Free and Pro plans, not to downloads, but only a 2.7 MB file was downloaded, so the behaviour for a multi-gigabyte video through the proxy was **not determined**. Darkroom runs on the same server, so it can use the internal address and skip the proxy altogether.
- The key needs `asset.download` (it had it). Large sets could use `POST /api/download/info` then `POST /api/download/archive`, but a per-asset download keeps one asset to one file and was enough. **Observed for `download/info`; the archive was not requested.**
- Storage layout: two layouts coexist. Files not yet moved by the storage template job sit at `<mount>/upload/<ownerId>/<2 hex>/<2 hex>/<uuid>.<ext>` (3 assets), with a generated name. Files after the storage template job sit at `<mount>/library/<storageLabel>/<yyyy>/<MM>/<dd>/<original file name>.<ext>` (2 assets). `<mount>` is `/photos` inside the Immich container. The user's `storageLabel` is a field on `GET /api/users/me`. **Observed.**
- Consequences: a path read from `originalPath` can change after the fact (the storage template job moves files after upload), the file name on disk can differ from `originalFileName`, and the container path is not the Unraid path. The host path of `/photos` and whether `upload/` and `library/` are on the same filesystem cannot be read through the API (only one pair of disk totals is reported by `/api/server/storage`, 80% used of about 40 TB), so a hardlink needs the Unraid mount list. **Not determined from the API.**
- Copy or hardlink: the owner chose copies (#199). If hardlinks come back, whether a link survives the template job's move depends on how Immich moves a file, which was not tested. Reading by API avoids all of this.

## 5. What "Free Up Space" removes and what counts as backed up

The phone app's feature is called "Free Up Space" (added in v2.5.0), not "clear backed-up photos". **Documented, not observed**, because nothing on the server runs it and the phone app was not used.

- It removes the phone's local copies of photos and videos that are already backed up to the server and are not in Immich's trash. Settings: a cutoff date, keep favorites, keep albums (WhatsApp albums by default), and keep all photos or all videos. A review screen lists what will go before anything is deleted. Items are moved to the phone's trash (Recently Deleted on iOS, 30 days) and space is only returned when that is emptied. Batches are 10,000 assets on iOS. Source: https://docs.immich.app/features/mobile-app
- With iCloud Photos on, the deletion also removes the photo from iCloud and every device on the same Apple Account; assets in an iCloud Shared Album are excluded from the scan. This matches the prerequisite in #199. Same source.
- The documentation does not say how the app decides a photo is backed up, nor how it treats RAW+JPEG pairs, Live Photos or edited versions. The 2.5.0 notes say per-asset metadata lets the server return the checksum, which suggests a checksum match, but that is an inference. https://github.com/immich-app/immich/releases/tag/v2.5.0
- Not determined: whether a Live Photo or a RAW+JPEG pair is cleared only when every resource is on the server, and what happens to an edited photo. Reading the mobile app's source for this release would settle it; it was not done here. A pilot with a made-up Live Photo on a test phone would settle it by observation.
- For the deletion gap in #199: from the server's side, a photo is "backed up" when its asset exists with a matching checksum, so a Live Photo whose video half has arrived but whose still has not (the orphans above) is the case to watch.

## 6. iOS background backup

**Documented, not observed**; this server has seen no phone backup run that was timed.

- iOS decides when background uploads run and Immich cannot schedule them; opening the app more often means more background runs. Uploads happen on open or resume and periodically in the background. "You must enable Background App Refresh." The documentation gives no reliability guarantee. https://docs.immich.app/features/mobile-backup
- By default uploads happen on Wi-Fi only (changeable). Albums with iCloud-stored items are fetched from iCloud, cached temporarily, hashed and uploaded. One asset can sit in several albums on iOS, so an album can be excluded by double-tapping it. Same source.
- For a gap of "an hour or two at most" (#199, answer 1): nothing documented guarantees it. The observed uploads above, a video then its still minutes apart and videos with no still 15 to 20 minutes later, are consistent with the phone app uploading in pieces.
- Live Photos, edited versions, RAW files and background upload time limits are not covered by the page. **Not determined.**

## What to decide or test next

- Whether the job pages by `cursor` over `filter.updatedAt` (observed, with an overlap), or reads the sync stream (spec only, and the only source of deletions).
- Use `mobile-app` metadata plus null `libraryId` as the phone-upload rule, and hold hidden videos that no still points at.
- Test with a made-up Live Photo, a RAW+JPEG pair and an edited photo on a test phone and Immich account: Free Up Space behaviour, checksums of each resource, and the `edited=true` download.
- Download a large video (over 100 MB) through the proxy and over the internal address.
- Check the Unraid mount of `/photos` for the filesystem question.
