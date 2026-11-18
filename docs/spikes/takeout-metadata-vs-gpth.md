# Spike: Takeout metadata as GooglePhotosTakeoutHelper documents it, against Darkroom's handling

Checks that the metadata Google Takeout gives us (the `*.json` sidecar next to each photo or video, and the EXIF inside the file) is read and used correctly by Darkroom's ingest (SPEC, "Takeout side (per export)" steps 2, 3 and 6). Refers to #284, the per-part ingest design #424 and its pairing pieces PP-1 (#428, merged in #431) and PP-4. Closes nothing.

## What was read

- **GooglePhotosTakeoutHelper ("GPTH")**, [TheLastGimbus/GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper), `main` at commit [`a9de187`](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/tree/a9de187efd9ce7cd072c5803dabc45079d416e64) (29 Jan 2025, `pubspec.yaml` version 3.4.3, the newest tag). Read: the README, `lib/date_extractors/` (JSON, EXIF and file-name date extraction), `lib/extras.dart` (edited-copy suffixes), `lib/grouping.dart` (duplicates and albums), `lib/folder_classify.dart`, `lib/moving.dart`, `lib/interactive.dart`, `bin/gpth.dart` and `test/gpth_test.dart`.
- **GPTH's issue tracker**, read on 18 Nov 2026: [#353](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/353) and [#448](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/448) (the `.supplemental-metadata` names), [#435](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/435) (the `.suppl.json` form), [#460](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/460), [#295](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/295) and [#180](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/180) (Live Photo and motion-photo videos), [#324](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/324) (Pixel `.MP` files), [#462](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/462) (DNG; title only), [#136](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/136) (negative timestamps), [#149](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/149) (`photoTakenTime` against EXIF and `creationTime`), [#236](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/236) (locations edited in Google), [#273](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/273) (the sidecar's fields, `people`), [#377](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/377) (a missing `geoDataExif`), [#143](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/143) (a sidecar that is not UTF-8; title and the code comment that cites it), [#14](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper/issues/14) (an EXIF time of `24:00`; title only).
- **The maintained community fork**, [Xentraxx/GooglePhotosTakeoutHelper](https://github.com/Xentraxx/GooglePhotosTakeoutHelper), README only (its packaging example names 5.0.9), for what the upstream no longer covers: special folders, extension fixing, time zones and EXIF writing. Its source was not read.

**Licensing.** GPTH is GPL-3.0. Nothing here copies its code or its tests or follows them line by line. This document describes the behaviour of Google Takeout that GPTH's code, tests and issues record, in our own words with links to the source, so that Darkroom's implementation and tests can be written independently. Every example file name below is made up. The issues opened from this spike follow the same rule.

**Darkroom revision.** `main` at `2a3e932` (after #431, PP-1). Darkroom's behaviour was checked by running its own code read-only in a worktree on made-up fixtures: plain files in a temporary folder, and a `tests/takeout_fixtures.py` `TakeoutBuilder` export (two parts) unzipped into a scratch folder and passed to `pairing.pair`. The scripts lived outside the repository and nothing was committed. "Probe" in the tables below means one of these runs.

**Marking.** **Documented** means GPTH's code, tests, README or an issue report says so. **Reported** means a single user's report in an issue, not confirmed by the maintainers. **Inferred** means our own reasoning, not stated anywhere we read.

## 1. What Takeout produces

### 1.1 The sidecar's fields

| Field | What it holds | Source |
| --- | --- | --- |
| `title` | The media's original file name as uploaded (for a Live Photo video, reportedly the still's name) | Documented (#273's sample; #460 reported for the video) |
| `description` | The caption typed in Google Photos | Documented (#273; the fork writes it to EXIF) |
| `photoTakenTime` | `{timestamp, formatted}`: the capture instant as Unix seconds in UTC, as a string. It reflects a date the user corrected in Google, not only EXIF (#136: a 1904 date set by hand gave a negative timestamp; #149: a scan dated 1979 in Google while its EXIF says 2020) | Documented |
| `creationTime` | Same shape: when the item was added to Google Photos, not when it was taken | Documented (#149) |
| `geoData` | `{latitude, longitude, altitude, latitudeSpan, longitudeSpan}`: Google's location, including one the user set with the location editor | Documented (#273 shape; #236 says edited locations are in the JSON and not in EXIF) |
| `geoDataExif` | Same shape: the location Google read from the file's EXIF. Present less often than `geoData`, and sometimes missing altogether | Documented (#377) |
| Both location blocks | `0.0, 0.0` (with `altitude` 0) means "no location", not the Gulf of Guinea | Documented by GPTH's community workaround in #377; Darkroom's fixture builder writes the same |
| `people` | `[{name}]`: the face groups the person named in Google | Documented (#273) |
| `googlePhotosOrigin` | How the item reached Google (`mobileUpload` with `deviceType` and `deviceFolder`, web upload, shared album and others) | Documented (#273) |
| `imageViews`, `url` | Counters and the item's Google link | Documented (#273) |
| `favorited`, `archived`, `trashed`, partner-sharing markers | Mentioned by the fork's README only as "partner sharing is detected from JSON metadata"; the field names were not seen in anything we read | Unverified (inferred from the fork's features) |

### 1.2 How the sidecar is named

| Behaviour | Example (made up) | Source |
| --- | --- | --- |
| Older exports: media name plus `.json` | `lake.jpg` / `lake.jpg.json` | Documented (GPTH's first rule) |
| Newer exports (from about Oct 2024): media name plus `.supplemental-metadata.json` | `lake.jpg` / `lake.jpg.supplemental-metadata.json` | Documented (#353, #448) |
| The whole sidecar name is limited to 51 characters with `.json` kept: the media name is kept whole if it can be, and `.supplemental-metadata` is cut at any point, down to `.s`, `.` or nothing | `holiday_snapshot_0042_from_tablet.jpg.supplem.json`, `uuid-like-name.jpg.suppl.json` | Documented for the old form (GPTH cuts the media name so that name plus `.json` is at most 51); reported for the new form (#353, #448, #435: one user measured 51 over about 46,000 sidecars) |
| A media name longer than 46 characters is itself cut, extension included | `a_very_long_name_from_some_messaging_app_0001.jpg` / `a_very_long_name_from_some_messaging_app_00.json` | Documented (GPTH's rule and tests: the cut can fall inside the extension) |
| A duplicate counter `(n)` on the media moves to just before `.json` on the sidecar, after the extension and after the (possibly cut) supplemental part | `beach(3).jpg` / `beach.jpg(3).json` or `beach.jpg.supplemental-metadata(3).json` | Documented (GPTH's bracket rule and its test with brackets elsewhere in the name; #353, #448) |
| A long name with a counter: where the counter goes when the media name is cut | not observed | Unverified: nothing we read shows one |
| Two long names that share their first 46 characters would produce the same cut sidecar name; how Takeout tells them apart (presumably a counter on one) is not described | `chat_export_from_a_messaging_app_long_title_AA_one.jpg` and `..._two.jpg` | Inferred |
| `-edited` copies have no sidecar of their own; they use the original's | `lake-edited.jpg` uses `lake.jpg.json` | Documented (GPTH's tests) |
| The edited suffix is localised to the account's language (`-bearbeitet`, `-modifié`, `-edytowane`, `-bewerkt`, `-modificato`, `-editat`, `-ha editado`, a Japanese form, and more), and Google's automatic creations add `-effects`, `-smile`, `-mix` | `lake-bearbeitet.jpg` | Documented (`lib/extras.dart`, with a test using a French name) |
| An edited copy can also carry a counter, and then uses the un-numbered original's sidecar | `lake-edited(1).jpg` uses `lake.jpg.json` | Documented (GPTH's tests; GPTH applies it only in its last-resort pass) |
| A file uploaded without an extension gets one added by Google, but its sidecar keeps the name as uploaded | `scan20030616.jpg` / `scan20030616.json` | Documented (GPTH's no-extension rule) |
| File names may arrive in NFD form (macOS) where the localised suffix list is NFC | — | Documented (GPTH normalises before comparing); matters only on a macOS unzip, inferred not to affect Darkroom's Linux unzip of the zip's names |

### 1.3 Folders, copies and pairs

| Behaviour | Source |
| --- | --- |
| Every item is in a `Photos from YYYY` folder; an album folder beside them holds a second, byte-identical copy (and its own sidecar). GPTH matches year folders only for 1800 to 2099 | Documented (`folder_classify.dart`, `grouping.dart`) |
| Items in the Archive and the Trash (localised: "Bin" and others) are in folders of those names and may have no year-folder copy at all | Documented (`interactive.dart`, `moving.dart`: "this happens with archive/trash"; the fork moves Archive, Trash and Locked Folder to "Special Folders") |
| A sidecar can be in a different folder from its media | Reported (#460) |
| A sidecar can be in a different zip part from its media | Darkroom's own SPEC; the per-part design #424 handles it |
| An iPhone Live Photo is two files with the same stem, a still (HEIC or JPG) and a video (MOV or MP4). The video usually has no sidecar; when it has one it is reportedly named after the still's extension (`clip.HEIC.supplemental-metadata.json` for `clip.MP4`) or after the video with `.metadata.json` | Documented that the video lacks one (#295, #180); the two naming forms are reported (#448, #460) |
| Camera stems repeat, so `IMG_0001.jpg` and `IMG_0001.JPG` in one folder can be different photos; only the same-case pair is a Live Photo | Reported (#295) |
| A Pixel motion photo comes as `PXL_….MP.jpg` with its sidecar and, in older exports, a separate `PXL_….MP` (an MP4 container) that has no sidecar; newer `.MP.jpg` files may embed the video. The fork also names `.MV` | Documented (#180, #324, the fork's README) |
| RAW files (DNG and others) are in Takeout and some tools skip them | Reported (#462, title) |
| A Storage Saver item can be a JPEG still named `.HEIC` (extension does not match content) | Documented by the fork's README (`--fix-extensions`) |

### 1.4 Dates and time zones

| Behaviour | Source |
| --- | --- |
| `photoTakenTime` is an instant in UTC. GPTH turns it into a local time on the machine running it; the fork adds a `--local-timezone` option and notes that Google Photos ignores EXIF's `OffsetTime` | Documented |
| `photoTakenTime` is the date Google shows, which may be the user's correction; EXIF in the file is not changed by such a correction (#149: EXIF 2020, Google 1979) | Documented |
| Pre-1970 dates give negative timestamps | Documented (#136) |
| A missing or unparseable `photoTakenTime` happens; the item then has only EXIF or nothing | Documented (GPTH handles a JSON without the field) |
| For items with no capture metadata Google uses the upload time as `photoTakenTime` | Inferred (common belief, not stated in anything we read) |
| EXIF date strings come with odd separators and `24:00` for midnight | Documented (GPTH's separator clean-up; #14) |
| Videos: the QuickTime `CreateDate` is in UTC by the QuickTime convention, unlike EXIF's wall-clock `DateTimeOriginal`; iPhone videos also carry an Apple `CreationDate` with an offset | Inferred from the QuickTime convention (GPTH v3 reads EXIF from images only; #448 says Live Photo videos fall back to QuickTime dates) |

### 1.5 GPS

| Behaviour | Source |
| --- | --- |
| `0, 0` in a location block means no location | Documented (#377 workaround) |
| `geoDataExif` can be missing while `geoData` is present | Documented (#377) |
| An edited location exists only in `geoData` and the JSON; the file's EXIF keeps the original or none | Documented (#236) |
| Altitude is given in both blocks | Documented (#273) |

### 1.6 Writing metadata back, and damage

- GPTH v3 writes **no** EXIF: it only sets each file's modification time to the extracted date (README "To do"; #149, #195). Its README suggests running exiftool afterwards to copy that time into EXIF dates when the file has none (`-if 'not defined DateTimeOriginal'`). The fork writes dates (with `OffsetTime*`), GPS and the caption into EXIF.
- GPTH removes byte-identical duplicates within a folder (size then SHA-256, keeping the shorter name), merges album copies into one item, and optionally drops edited copies (`--skip-extras`).
- GPTH falls back to guessing a date from the file name (`IMG_20190509_154733.jpg`, `Screenshot_2019-04-16-11-19-37.jpg` and others), but only when asked (`--guess-from-name`).
- Takeout renames: the cut names and moved counters above, Google adding an extension to a file uploaded without one, and a Storage Saver JPEG kept under `.HEIC`. Takeout damage reported: sidecars that are not valid UTF-8 (#143).

## 2. Darkroom's current handling

All references are to `main` at `2a3e932`.

- **Pairing** (`darkroom/domain/ingest/pairing.py`): `pair` walks the staging tree (`scan`), hashes media (`digests_of`) and calls the pure `pair_names`. A file is media only if its extension is in `MEDIA_SUFFIXES` (`STILL_SUFFIXES` plus `media.VIDEO_SUFFIXES`), and every other file but `*.json` is ignored. Sidecars are looked up in the media's own folder with four rules in order (`RULES`): `exact` (`name.json`), `supplemental` (`name` plus any prefix of `.supplemental-metadata`, then `.json`), `counter` (`base.ext(n).json`, also after a supplemental prefix) and `truncated` (a 46-character stem that is a prefix of `name.supplemental-metadata`). An `-edited` file without its own sidecar uses its original's (`edited_sidecar`, `original_name`; English `-edited` only, `EDITED`). Byte-identical copies in different folders become one `TakeoutItem` (`slots_of`), named by the year-folder copy, with `albums` and `in_year_folder`. Any folder not named `Photos from YYYY` is an album (`YEAR_FOLDER`, `album_of`). A video with exactly one same-stem still in its folder gets `live_pair_of` (`live_still`). `orphan_sidecars` and `unpaired_media` are computed, and, like `albums` and `live_pair_of`, feed no decision or report (`metadata.summarize` is used only by tests; #424 section 2 says the same).
- **Sidecar parsing** (`darkroom/domain/ingest/sidecar.py`): `parse` reads `photoTakenTime.timestamp` (`whole_seconds`: any finite value between year 1 and 9999 except exactly 0, so negative timestamps are kept), the location from `geoDataExif` first and `geoData` second (`place`: rejects `0, 0`, out-of-range values and non-numbers), `description`, `title` and `googlePhotosOrigin` (`origin_of`). It ignores `creationTime` (correct), altitude, `people`, and any favourite, archive or trash marker. Invalid JSON, including a byte-order mark, gives `None`.
- **Reading the sidecar** (`darkroom/domain/ingest/run.py`): `prepare` reads it with `staged_text`, which decodes strictly as UTF-8, inside the per-item `try`; any error there drops the item from `prepared.fields`, and `stage_place` then counts it as failed.
- **EXIF** (`darkroom/domain/media.py`): exiftool is run with `-j -n -G1` (`darkroom/providers/exiftool.py`, `read`). `capture` takes the first of `DateTimeOriginal`, `CreationDate`, `CreateDate` in any group (`tag` ignores the group), parses it as a wall-clock time (`parse_local`; `24:00` and zero dates are refused), then the offset from the string or `OffsetTime*`, else from the EXIF GPS position (`timezones.offset_minutes`), else unknown. `location` takes `GPSLatitude`/`GPSLongitude` (the `Composite:` value first) and refuses `0, 0`.
- **Combining** (`darkroom/domain/ingest/metadata.py`, `candidate`): EXIF GPS first, else the sidecar's (`gps_source` `google`). EXIF instant first; if EXIF gave none (no offset could be found, or no date), the sidecar's `photoTakenTime` becomes `captured_utc` (`time_source` `sidecar`), the offset comes from the GPS position at that instant if there is one, and `captured_local` keeps the EXIF wall clock if EXIF had one, else is computed from the offset. Nothing checks that the EXIF wall clock and the sidecar instant agree.
- **Dedupe** (`darkroom/domain/dedupe.py`): uses `captured_utc`, `captured_local`, dimensions and camera for tier 2 and 4, and `pairing.original_name` for edited copies (`is_edited`, tier 3).
- **Placing** (`darkroom/domain/ingest/place.py`): a tier-5 item goes to `google-only/YYYY/MM/` by `captured_local`, else `captured_utc` (`month_of`), else the undated folder. `place_google_only` then fills gaps with `xmp.fill(..., with_date=True)`; a matched phone original gets `xmp.fill(..., with_date=False)` (`fill_original`); a later export's tier-1 copy refreshes changed description and GPS (`refresh_takeout`, `xmp.refresh`).
- **XMP writer** (`darkroom/domain/xmp.py`): `gaps` writes GPS (`XMP-exif:GPSLatitude`/`GPSLongitude`, seven decimals, no altitude) only when the file has no EXIF GPS and the sidecar `.xmp` has none; a date (`XMP-photoshop:DateCreated`) only when the file has no `DateTimeOriginal`, `DateCreated`, `CreationDate` or `CreateDate`; a description only when the file has none. `created_text` writes the instant as local time at the GPS position's offset, and as UTC with `+00:00` when there is no position. Immich gives the XMP date precedence over EXIF ([immich-managed-storage.md](../design/immich-managed-storage.md), the live-run table), which is why the writer only fills missing values.

## 3. Gap table

"Risk" names what goes wrong for the person if the gap bites: **wrong date**, **lost GPS** (or wrong GPS), **wrong photo's metadata** (another item's date or place), **silent loss** (a photo or its metadata never reaches the library and nothing says so), or **cosmetic**.

| # | Behaviour | Darkroom handles it | Risk | Evidence |
| --- | --- | --- | --- | --- |
| 1 | `name.json` | Yes | — | `pairing.exact`; probe |
| 2 | `.supplemental-metadata.json` and every cut of it down to `.s.json` and `.json`, `.suppl.json` | Yes | — | `pairing.supplemental`, `truncated`; probe (`.suppl.json`, `.supplem.json`) |
| 3 | Counter after the extension, after a full or cut supplemental part | Yes | — | `pairing.counter`; probe (`beach.jpg.supplemental-metadata(3).json`, `beach.jpg.supplem(3).json`, and a builder export with the sidecar in another part) |
| 4 | Media name over 46 characters, cut inside the extension | Yes | — | `pairing.truncated`; `tests/test_pairing.py` |
| 5 | Long name with a counter, where the media name itself is cut | Partly | Lost GPS, wrong date (no sidecar) | Probe: `…_xyz(1).jpg` with `…_xyz.j(1).json` stays unpaired and the sidecar is an orphan; the same name with `…_xyz.jpg.(1).json` pairs. Takeout's real form is unverified |
| 6 | Two long names that share their first 46 characters | No | **Wrong photo's metadata** | Probe: both media pair with the same `…AAA.json`, and `…AAA(1).json` is an orphan. `truncated` takes the first sorted match with no uniqueness check |
| 7 | Upload without an extension (`scan20030616.jpg` / `scan20030616.json`) | No | Lost GPS, wrong date | Probe (plain files and builder): unpaired, sidecar orphaned |
| 8 | Localised and other edited suffixes (`-bearbeitet`, `-modifié`, …; `-effects`, `-smile`, `-mix`) | No | Lost GPS, wrong date; edited copy not recognised (tier 3 skipped, so it is placed as its own Google-only item) | Probe: `garden-bearbeitet.jpg` unpaired, `edited_of` none. `pairing.EDITED` is English only |
| 9 | `-edited(1)` using the un-numbered original's sidecar | No | Lost GPS, wrong date | Probe: `garden-edited(1).jpg` beside `garden.jpg` and `garden.jpg.json` is unpaired; `original_name` maps it to `garden(1).jpg` only |
| 10 | Extension case differs between media and sidecar | No | Lost GPS, wrong date | Probe: `walk.JPG` / `walk.jpg.json` unpaired. Whether Takeout ever does this is unverified, so no issue |
| 11 | Sidecar in another folder than its media (not another part) | No | Lost GPS, wrong date | Pairing is per folder by design. Reported once (#460); no issue until a real export shows it (#11) |
| 12 | Sidecar in another part | Yes when the parts are unpacked together; per part by #424 PP-4 | — | `tests/test_pairing.py`, `standard_export` |
| 13 | Year and album copies of the same bytes, metadata from either copy | Yes | — | `pair_names`, `slots_of`; probe |
| 14 | Archive and Trash/Bin folders | Partly: treated as albums | **Wrong photo** in the library: an item the person deleted in Google is placed as a Google-only photo; an archived one is fine | Probe (builder): `only_in_bin.jpg` becomes an item with album `Bin` and no year copy; `trashed_one.jpg` gets album `Trash`. `YEAR_FOLDER`/`album_of` |
| 15 | Files Darkroom does not count as media: RAW (`.CR2`, `.CR3`, `.NEF`, `.ARW`, `.RAF`, `.ORF`, `.RW2`), `.avif`, `.mts`/`.m2ts`, `.mpg`, `.wmv`, `.3g2`, Pixel `.MP`/`.MV`, files with no extension | No | **Silent loss**: the file is never fingerprinted, deduped or placed, its sidecar is an orphan, and staging is deleted after 14 days | Probe (plain files and builder): every one listed as "not media", sidecars orphaned. `MEDIA_SUFFIXES`, `media.VIDEO_SUFFIXES` |
| 16 | Live Photo video without a sidecar | Partly: the pair is found (`live_pair_of`) but unused | Lost GPS, wrong or missing date on the video | Probe: `clip0005.MP4` gets `live_of=clip0005.HEIC`, no sidecar; nothing reads `live_pair_of` |
| 17 | Live Photo video sidecar named after the still's extension, or `<video>.metadata.json` | No | Lost GPS on the video; orphan sidecar | Probe: `clip0006.MP4.metadata.json` is an orphan; a `clip0005.HEIC…json` sidecar goes to the still only |
| 18 | Pixel `.MP.jpg` with its sidecar | Yes | — | Probe |
| 19 | `photoTakenTime` UTC, negative, zero, out of range | Yes | — | `sidecar.whole_seconds`; probe (a 1904 value is kept, 0 is refused) |
| 20 | `creationTime` (upload time) never used as the capture time | Yes | — | `sidecar.parse` ignores it |
| 21 | A date the user corrected in Google (sidecar) against the file's EXIF | No (EXIF always wins, by SPEC) | **Wrong date** the person had fixed | Probe: a scan with EXIF `CreateDate` 2020 and a 1979 `photoTakenTime` keeps 2020 as its wall clock. Needs the owner's decision |
| 22 | EXIF wall clock and sidecar instant mixed in one record | No | **Wrong date**: `captured_local` and `captured_utc` disagree, by hours or by decades; the month folder follows the wall clock | Probe: scan above gives `local=2020-02-09T12:47:20`, `utc=1979…`, folder 2020/02. A photo with EXIF 19:30 and no offset and a sidecar instant four hours later keeps `offset=None` although the difference gives it |
| 23 | Video QuickTime `CreateDate` (UTC) read as a wall-clock time | No | **Wrong date**, on Takeout videos and on phone originals alike | Probe: `QuickTime:CreateDate 2019:07:04 23:30:00` with a New York position gives `utc` four hours late (`gps_lookup`); without an EXIF position, `captured_local` is the UTC clock labelled local, next to a −240 offset worked out from the sidecar's position. `media.capture` and `tag` ignore the group |
| 24 | EXIF date only in `CreateDate` (a scan's digitised time) | Partly | Wrong date when the sidecar has the real one | Counted as a capture date by `media.capture` and by `xmp.has_date`; part of row 22 |
| 25 | EXIF `24:00`, zero dates, odd separators | Partly: refused, so the sidecar's time is used | — (falls back) | Probe: `24:00:00` and `0000:00:00` give no EXIF date |
| 26 | Time zone when only the sidecar instant is known and there is no position | Partly | **Wrong date** shown: the XMP says `+00:00`, an offset Darkroom invented (AGENTS.md, "Time zones") | Probe: `xmp.created_text(…, None, None)` gives `2019:07:04 23:30:00+00:00`; `tests/test_xmp.py` asserts it |
| 27 | `0, 0` locations | Yes | — | `sidecar.place`, `media.location` |
| 28 | `geoDataExif` missing | Yes | — | Falls back to `geoData` |
| 29 | A location the user edited in Google (`geoData` differs from `geoDataExif`) | No | **Wrong GPS**: the place the person set is ignored | Probe: with both present `parse` takes `geoDataExif`. Needs the owner's decision with row 21 |
| 30 | Altitude | No | Cosmetic | `sidecar.place`, `xmp.gaps` write no altitude |
| 31 | Description | Yes, gap-only | — | `xmp.gaps`, `refresh` |
| 32 | `people`, favourites, albums as metadata | No | Cosmetic (Immich has its own faces and albums; `sidecar_json` keeps the raw text) | `sidecar.parse`; `TakeoutItem.albums` unused |
| 33 | Sidecar not valid UTF-8 | No | **Silent loss** of the photo itself, not just its metadata: the item is dropped from the import and counted only as failed | `run.prepare`, `staged_text` (strict decode inside the item's `try`); a Latin-1 byte decodes to `UnicodeDecodeError` in the probe |
| 34 | Sidecar with a byte-order mark | No | Lost GPS, wrong date | Probe: `sidecar.parse` returns `None`. Whether Takeout writes one is unverified |
| 35 | Storage Saver JPEG named `.HEIC` | Unknown | Cosmetic in the library; may matter to Immich upload (#266) | Pillow and exiftool read by content, so fingerprints are fine; Immich's handling of the mismatch was not checked |
| 36 | NFD file names | Unknown | Lost GPS for localised edited names only | The zips' names are read as stored; not tested |
| 37 | Upload time used as `photoTakenTime` for items without capture data | Unknown | Wrong date, undetectable from the sidecar alone | Inferred; no issue. A file-name date guess, as GPTH offers, would be an owner decision |

## 4. Priorities

**Can corrupt dates or GPS (the worst).** These write a wrong value into the library, through the record, the month folder or the XMP, and Immich then shows it:

1. Row 23, video QuickTime dates read as local time (#436). It affects every video without an Apple offset, on both sides of the dedupe, so tier-2 matches still line up but every such video's time is off by its offset.
2. Row 6, two long names taking one sidecar (#441): another photo's date, place and caption go into a sidecar.
3. Rows 22 and 24, an EXIF wall clock and a sidecar instant mixed into one record (#437).
4. Rows 21 and 29, corrections the person made in Google ignored (#438, owner decision first).
5. Row 26, an invented `+00:00` offset in XMP (#445).

**Silent loss.** Photos or videos that never reach the library and are not reported:

6. Row 15, files Darkroom does not count as media (#439).
7. Row 33, a sidecar that is not UTF-8 drops its photo (#443, with row 34).
8. Row 14, trashed items placed as if they were the person's photos (the opposite of a loss, but the same "wrong photo" class; #444, owner decision first).

**Orphan sidecars** (the media is placed, without its Google date, place and caption):

9. Rows 5, 7, 8 and 9, naming rules the pairing lacks (#440).
10. Rows 16 and 17, Live Photo and motion-photo videos (#442).

**Cosmetic, no issue now:** altitude, people, favourites and albums (rows 30, 32), extension case (row 10), a sidecar in another folder (row 11), NFD names (row 36), Storage Saver extensions (row 35, belongs to #266's upload checks), and file-name date guessing (row 37). Each is worth revisiting if a real export shows it (#11).

The per-part design (#424) assumes "at most one sidecar in a folder matches a media file under the rules" (section 7). Row 6 breaks the converse, one sidecar matching two media; #441 has to land in the pure pairing before PP-4 copies the rules into the per-part flow, or the order-property test will pass on a wrong pairing.

## 5. Issues opened

| Key | Issue | Gap rows |
| --- | --- | --- |
| G1 | #436 Read video QuickTime dates as UTC | 23 |
| G2 | #437 Keep the capture wall clock, offset and instant consistent when EXIF and the sidecar both give a time | 22, 24 |
| G3 | #438 Decide and apply whether a date or place corrected in Google Photos wins over the file's own | 21, 29 |
| G4 | #439 Stop skipping Takeout media Darkroom doesn't recognise | 15 |
| G5 | #440 Pair the sidecar names Takeout uses that the rules miss | 5, 7, 8, 9 |
| G6 | #441 Never give one sidecar to two different media | 6 |
| G7 | #442 Give Live Photo and motion-photo videos their still's Google metadata | 16, 17 |
| G8 | #443 Don't drop a photo because its sidecar isn't clean UTF-8 JSON | 33, 34 |
| G9 | #444 Decide and apply what happens to Takeout's Trash and Archive folders | 14 |
| G10 | #445 Don't write an invented `+00:00` offset into XMP dates | 26 |

## 6. What this spike could not verify

- No real Takeout export was available (#11). Every Takeout naming behaviour is from GPTH's code, tests and issue reports; the 51-character budget of the new supplemental form is one user's measurement.
- How Takeout names a long file that also has a counter (row 5), and how it tells apart two long names with the same first 46 characters (row 6).
- The names of the favourite, archive, trash and partner-sharing fields in the sidecar, and whether trashed items' sidecars mark them.
- Whether Takeout ever writes a byte-order mark, a non-UTF-8 sidecar in current exports, or a sidecar whose extension case differs from its media.
- Whether Google rewrites EXIF in the downloaded file when the person edits the date or place in Google Photos (we assume not, as #149 and #236 suggest).
- Immich's handling of a Storage Saver JPEG named `.HEIC`.
- The fork's source; only its README was read.
