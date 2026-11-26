# Disposable Immich v3.4.1 evidence

Live observations for [#266](https://github.com/AnthonyPluth/Darkroom/issues/266), the enrichment rows in [#574](https://github.com/AnthonyPluth/Darkroom/issues/574) and the Live Photo rows in [#547](https://github.com/AnthonyPluth/Darkroom/issues/547). The runs took place on November 25–26, 2026, Central time. They supplement the [earlier disposable-user run](https://github.com/AnthonyPluth/Darkroom/issues/266#issuecomment-6962919531).

## Setup

- Immich reported `v3.4.1`. The executable and bundled web app came from the `linux/amd64` release image, manifest `sha256:4351dd621899ecc8ed317ae87cdcaeb968ec8ba4f9d8053d913a73eb6f6dd7c2`, source `22b95194c80a2f2e6201093916b6c8c3d6d271dd`. The rootless Docker image pull exhausted disk space. The same release binaries ran as local processes, with fresh PostgreSQL 14.19, VectorChord 0.4.3 and Valkey 9.1.2. PostgreSQL support files missing after the interrupted unpack came from the matching PostgreSQL 14.19 Debian package. All three processes listened on loopback. The server ran with machine learning, update checks and OAuth disabled.
- The upload, supplemental enrichment and Live Photo runs used separate disposable non-admin accounts and generated media. Import keys had exactly `user.read`, `server.about`, `asset.read`, `asset.download` and `asset.upload`. Enrichment keys listed exactly `all`. E10 used a separate read-only key with the import grants minus `asset.upload`. E14 used a disposable key revoked before the probe. Signed-in admin and owner sessions set up the isolated accounts and ran the supplemental operations described below.
- The ordinary and enrichment harness ran from `9d398d3fedfd88702ffa5544e2ebef6d08826d03`. The enrichment run used the default 600-second observation in E4. The supplemental checks used fresh generated fixtures unless a row explicitly says it reread the first fixture. The Live Photo rows used the separate [#601 harness](https://github.com/AnthonyPluth/Darkroom/pull/601) with its corrected L6 assertion.

## Existing-asset enrichment

| Row | Observation |
| --- | --- |
| E1 | `GET /api/users/me` showed the same non-admin account as its import key. `GET /api/api-keys/me` listed `all`. The second enrichment key belonged to a different disposable non-admin account. |
| E2 | Description-only `PUT` returned 200 and filled the empty description. The other compared fields stayed the same. The original downloaded with the import key matched the generated input's SHA-1 and size. |
| E3 | A latitude alone returned 400. With both coordinates the `PUT` returned 200 and stored 12.5, −20.25. City stayed unset, and the read-back found no other changed fields beyond those allowed by the check. |
| E4 | Both date updates returned 200. An offset-bearing edit stored `10:30:15Z` with `UTC+2`. The offsetless edit stored `12:30:15Z` and later showed `UTC+0`. The 600-second readings and the implications for fallback-date detection are below. |
| E5 | An independent run waited for the EXIF wall clock to be extracted before the edit. The generated offsetless `14:30` read as `14:30Z`, with no zone. The `PUT` of `14:30+02:00` returned 200. Immich read back `12:30Z` and `UTC+2`, with `localDateTime` still at `14:30` on immediate and subsequent reads. |
| E6 | A second description `PUT` returned 200 and overwrote a verified present value. |
| E7 | Between the first asset read and the enrichment-key `PUT`, the fixture owner saved a description in Immich's web Info panel. The save returned 200 and its description was read back. The asset's `updatedAt` had stayed the same on that read. The following enrichment-key `PUT` returned 200 and overwrote the browser edit. |
| E8 | The sidecar job created a sidecar for previously bare assets and updated an existing XMP sidecar. In the existing-sidecar case, the new description and original XMP GPS both remained after re-extraction. Owner-session re-extraction returned 204. The original SHA-1 and size stayed unchanged in all three cases. See the date comparison below. |
| E9 | Null date and null coordinate updates each returned 400 with date and place unchanged. An empty description returned 200 and cleared it. |
| E10 | The read-only key returned 403, `Missing required permission: asset.update`, and the asset remained unchanged. The all-permission key downloaded the original with its input SHA-1 and size. The unmodified harness reported this row as failed because the first thumbnail request returned 404. A later read of that same thumbnail returned 200. A fresh fixture reproduced 404 immediately and 200 after 0.5 seconds of polling, with the same 403 denial and original-byte equality. |
| E11 | The owner session moved a generated image to the locked folder and verified its locked visibility. Its all-permission key's `PUT` returned 400, `Not found or no asset.update access`. |
| E12 | The owner session moved a generated image to Trash and verified `isTrashed`. Its all-permission key's `PUT` returned 200 and the description changed. The image stayed trashed. |
| E13 | Both the unrelated user and the user with partner read access got 400, `Not found or no asset.update access`. The owner read back an unchanged description for each fixture. The partner's read of the shared image returned 200 before the update attempt. |
| E14 | A revoked key returned 401 and the asset remained unchanged. The probe checked the server refusal. The proposed Darkroom key lifecycle was outside this run. |
| E15 | Three identical updates each returned 200. Subsequent reads showed the same metadata. The asset appeared once in the `updatedAt` window and once by checksum, and its checksum stayed the same. The final repeat moved `updatedAt`. |
| E16 | A `filter.takenAt` search around the new instant returned the date-edited asset. |
| E17 | With the sidecar queue paused by a disposable admin session, the description `PUT` returned 200 and the description was immediately readable. Resuming the queue returned 200. |
| E18 | The harness verified that both reads of an asset without an album returned 200 and an empty list. In the supplemental one-album case both reads returned 200 and the same single album, with the successful description update between them. |

### E4 and E8 date evidence

The original E4 harness first read an `exifInfo` record before its capture time was filled. This made its fresh-fallback equality observation false. The supplemental E8 check waited until extraction had set that time. It observed equal `dateTimeOriginal` and `fileCreatedAt` on an untouched generated fallback. In the table, times are expressed in UTC and `localDateTime` shows Immich's wall-clock value.

| Fixture and stage | `dateTimeOriginal` | `timeZone` | `fileCreatedAt` | `localDateTime` |
| --- | --- | --- | --- | --- |
| E4 offset edit, immediate | 2019-07-04 10:30:15 | `UTC+2` | 2024-01-02 03:04:05 | 2024-01-02 03:04:05 |
| E4 offset edit, after 600 s | 2019-07-04 10:30:15 | `UTC+2` | 2019-07-04 10:30:15 | 2019-07-04 12:30:15 |
| E4 offsetless edit, immediate | 2019-07-04 12:30:15 | unset | 2024-01-02 03:04:05 | 2024-01-02 03:04:05 |
| E4 offsetless edit, after 600 s | 2019-07-04 12:30:15 | `UTC+0` | 2019-07-04 12:30:15 | 2019-07-04 12:30:15 |
| E8 untouched fallback, extraction finished | 2024-01-02 03:04:05 | unset | 2024-01-02 03:04:05 | 2024-01-02 03:04:05 |
| E8 offset edit, after explicit re-extraction | 2020-04-05 04:07:08 | `UTC+2` | 2020-04-05 04:07:08 | 2020-04-05 06:07:08 |
| E8 offsetless edit, after explicit re-extraction | 2019-07-04 12:30:15 | `UTC+0` | 2019-07-04 12:30:15 | 2019-07-04 12:30:15 |

An edited capture time can equal `fileCreatedAt` after the sidecar is processed and after explicit re-extraction. Equality therefore also matches an edited field. The [enrichment design's C1 fallback rule](../design/existing-asset-enrichment.md#16-live-spike-rows-to-add-on-266) keeps this case held. E5's offset for a wall-clock tag was observed independently of C1.

## Additional managed-import observations

The upload harness verified a generated AVI of 67,154,340 bytes. Upload took 4.9 seconds and duration metadata appeared after the first 0.1-second poll, showing 79 seconds. The owner, managed-library status, unedited SHA-1 and size were verified. A 1-byte quota account with zero bytes used refused an upload with 400, `Quota has been exceeded!`, and stored no matching asset. A revoked key returned 401 for both `users/me` and bulk upload check. The run also repeated the ordinary upload, sidecar extraction, owner-binding, checksum-resume, RAW/JPEG and simple Live Photo checks.

## Generated Live Photo observations

| Row | Observation |
| --- | --- |
| L1 | A HEIC and MOV with a shared Apple content identifier linked without a video ID in the upload. Read-back linked the still to the MOV and showed the video as `hidden`. |
| L2 | Uploading a duplicate still with a video ID returned 200 duplicate with the existing still. It stayed unlinked. |
| L3 | A new still linked to a previously `timeline` video, whose visibility changed to `hidden`. |
| L4 | The default video started at `timeline` and an explicit hidden upload started at `hidden`. With either upload, the read-back after linking showed `hidden`. |
| L5 | A JPEG with embedded motion linked to an asset whose original bytes matched the embedded clip. That link replaced the separately supplied Live Photo video link. |
| L6 | Both the nonexistent video ID and another disposable user's video ID returned HTTP 400, `Not found or no asset.update access`. In both cases checksum search found no still. The initial harness run recorded the refusals as unverified under its earlier expected message. The corrected harness passed both assertions on a fresh pair of generated stills. |

## Evidence still needed

Offline and external-library duplicates, edited representations and the installation's backup recovery point and restore rehearsal still need evidence. Production imports, enrichment, retention cleanup and migration retain their separate release gates, including the owner's remote-deletion boundary decision where applicable.
