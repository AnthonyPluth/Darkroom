# Decisions

Answers to the owner's questions in issues #10, #199, #259 and #284, copied here as agents reach them. Until an item is answered, the proposed default in the issue applies. Historical decisions below describe the implementation they authorised; the #259 section records which storage assumptions are superseded as a target, without claiming that the migration has shipped, and the #284 section does the same for the one-container target.

| Item | Decision | Source |
| --- | --- | --- |
| 4. Google's edited copies | Skip (the default; unanswered): the `keep_edited_copies` setting defaults to false | #10, #24 |
| 5. Final name | Darkroom (the default; unanswered) | #10 |
| 9. Human-resolved tier-4 matches | Not proof (the default; unanswered): only automatic tier 1 and 2 decisions set `confirmed_in_google` | #10, #24 |
| 13. Tier-2 time when the offset is unknown | UTC when both sides have it, else wall-clock; a missing time on either side never matches (the default; unanswered) | #10, #24 |
| Immich-first intake (#199, option B) | Each iPhone backs up and clears its photos with Immich's own iOS app. Darkroom ships no iPhone app and decides nothing about what leaves a phone. Darkroom reads new assets from Immich | #199 |
| Deletion gap (#199, question 1) | Accepted: a photo can be cleared from an iPhone before Google has it, with Immich and the Unraid backup as the safety. Clearing happens at any time with Immich's own function and Darkroom plays no part; the gap to Google (via the Pixel) should be an hour or two at most | #199 |
| Copies and the Pixels' folder (#199, question 2) | Copies, not hardlinks. Darkroom writes the permanent copy into the person's `originals/` once. A separate transient folder per Pixel is synced by Syncthing and emptied once Google has the photos, because a Pixel has about 128 GB. `originals/` is no longer sent to the Pixel | #199 |
| Alert channel (#199, question 3) | The owner chose ntfy first (Nov 7, built in #241), then decided on Nov 8 that alerts go by email through Resend to ONE owner address instead of ntfy; ntfy is removed (#258). Alerts are also recorded as events and shown on the status page; without the three `RESEND_API_KEY`, `ALERT_EMAIL_FROM` and `ALERT_EMAIL_TO` settings nothing is emailed | #199, #258 |
| Rollout (#199, question 4) | Everyone at once: Darkroom isn't deployed yet, so there is no pilot phase | #199 |
| iOS code (#199, question 5) | Remove it: the app and its workflows in #200, the device, upload, push and cleanup backend in #201 | #199, #200, #201 |
| Pruning a Pixel's folder (#199, question 6) | Pruning is Darkroom deleting the transient per-Pixel copy; Syncthing carries the deletion to the Pixel, and `originals/` keeps the permanent copy. It may touch only the per-Pixel folder, held by its own Semgrep rules and tests (#216). The signal for when it may delete is the Takeout match (`confirmed_in_google`, an unreversed automatic tier 1 or 2 decision), replacing the fixed-delay, Pixel-report and by-hand options; waiting up to about two months is acceptable because the Pixel has the space. Removing a photo from a device doesn't remove the Google Photos copy once backed up | #199 |
| Immich behaviour the design relies on | Assumptions until the spike confirms them on the running server (asset listing and ownership, stored checksum, original download, what Immich's clearing removes) | #202 |
| Legacy-library import (#199, #217) | One-time, per person, read-only, after that person's Takeout import. A legacy file with no unreversed tier 1 or 2 match in that person's Takeout is not in Google: it is copied once into the person's `originals/legacy/` and queued for the Pixel folder. A matched file is recorded and not copied | #199, #217 |
| Per-destination confirmation (#199, Nov 6, decision 1) | A photo counts as confirmed for a Pixel only through an unreversed automatic tier 1 or 2 match in the Takeout of the Google account that Pixel uploads to; another person's Takeout never counts | #199, #216 |
| First-run order (#199, Nov 6, decision 2) | Takeout first, then the legacy library, then the Immich reconcile; only unmatched files reach the Pixels | #199 |
| Wrong match found after pruning (#199, Nov 6, decision 3) | Reversing the match re-queues the photo into that person's Pixel folder, copied from `originals/` | #199, #216 |
| Pixel flooding (#199, Nov 6, decision 4) | The per-Pixel folder is capped by size (`PIXEL_FOLDER_CAP_GB`, default 50); the next photos go in only after earlier ones are confirmed and pruned | #199, #216 |
| Late Immich photos (#199, Nov 6, decision 5) | Every photo that arrives in Immich is sent to the Pixel, even if that person's Takeout already has a match | #199, #216 |
| Duplicates on Unraid (#234, Nov 7) | In the owner's words: "Let's make sure we're not storing duplicate images permanently - figure out which one is higher quality and keep that one. If both images are identical, get rid of the duplicate." On how it goes: "if a duplicate exists on Unraid, delete the duplicate", with no trash area. When the Immich reconcile stores a phone original that matches a Takeout copy in `google-only/` (automatic tier 1 or 2), identical copies keep the phone original; otherwise the Takeout copy is kept only if it has strictly more pixels, and the other copy is deleted. Byte count and metadata never make a phone original lose. This supersedes "dedupe deletes nothing on Unraid" (promise 2) and the no-deletion half of promise 1 for the losing copy of a matched pair only, never anything else | #234, #199 |
| Freeing a Takeout part's zip early (#424, Nov 18, decision 1) | In the owner's words on #424: "Free a part's zip once its proof passes (amend promise 2 and the #259 retention clock): yes (phase 2, PP-10)." A part's zip is deleted once its proof holds (every entry has a verified outcome, what still waits is copied into the held store first, and the delete is journalled), not 14 days after the export is finished. Built in #487. It stays off for Immich-managed imports until the #259 design's recovery-point proof exists | #424, #487 |
| Held copies at an export's completion (#424, Nov 18, decision 4; confirmed in chat Nov 21) | In the owner's words on #424: "Held copies after the export completes: free them at completion, not after 14 days. This differs from the design's recommendation (14 days). It also deletes staging before the 14-day rule, so the promise 2 amendment for decision 1 must cover the held store too, and PP-8 and PP-10 need to say what proves it is safe to free at completion (every held item decided and placed or recorded)." The design advised the other option, keeping held copies 14 days after completion as staging is kept today; the owner chose freeing them at completion, and confirmed it in chat on 2026-11-21. A held copy goes once the export is imported, its item is placed, held for review, skipped by an automatic, unreversed decision or a copy, each verified again, and no entry of the export failed. A held copy whose item has no decision of its own (a repeat of an earlier export, or an item only in Trash) is kept, and an `export_held_kept` event counts it | #424, #487 |
| Takeout's Trash and Archive folders (#444, Nov 18) | Trash: a photo that is only in a folder named `Trash` or `Bin` (anything the person deleted in Google Photos) is not imported: it gets no dedupe decision and no file, and the run counts it as `in Trash or Bin` in its not-imported summary, with no names. A photo that is in a year folder or in an album as well follows that copy. Archive: the owner left it undecided, so today's behaviour stands (an archived photo is placed like any other); the `Archive` folder is not an album and the item is marked `archive`, nothing else changes. Folder names are the fixed list `Trash`, `Bin`, `Archive`, matched whole and ignoring case; other languages' names are not recognised until the owner supplies them from a real export (#11). The sidecar's own flags are not read: no real export has shown their names. Nothing is deleted: the trashed files stay in staging until cleanup | #444 |

## Immich-managed storage (#259, 2026-11-09)

**Owner decision:** Immich manages and organises the library. Darkroom is a coordinator and does not keep another permanent photo library. Takeout extras go through Immich's API into its own managed storage; Darkroom must never write into Immich's filesystem directly. [Issue #259](https://github.com/AnthonyPluth/Darkroom/issues/259) records the decision; [the approved design](design/immich-managed-storage.md) defines the target, evidence and implementation gates.

The ownership decision and detailed design are **owner approved, with sign-off recorded on #259**. Gated implementation follow-ups are authorised (#265, #266); API upload/metadata assumptions remain unverified on a live server. Production uploads, cleanup and migration remain blocked by live validation and the applicable safety and backup/restore gates. Keep the dedicated Darkroom share and all current safety checks until those follow-ups complete.

| Earlier decision | Effect of #259 |
| --- | --- |
| #199 question 2: permanent copies in `originals/` alongside the Pixel copies | Superseded as the target. Immich holds the durable original; temporary API downloads feed the separate, capped Pixel transport folder |
| #199 legacy import and first-run order | Read-only legacy intake and Takeout-first ordering remain. The destination becomes a verified managed Immich asset, not a new permanent `originals/legacy/` copy; existing rows and sources require backfill |
| #199 question 6 and per-destination confirmation | Preserved: an unreversed automatic tier 1/2 Takeout match for that Pixel's Google account and person is the pruning signal. Only the source for placement/reversal changes from `originals/` to a verified Immich original |
| #199 reversal, size cap and late Immich arrivals | Preserve requeue, the 50 GB default cap and the always-send rule for independently arriving phone resources. Source receipts must prevent Darkroom's Takeout uploads from becoming an import loop; detailed routing follows the approved design and its validation gates |
| External libraries for `google-only/` and library rescans | Superseded as the destination for new imports. Do not detach old libraries until custody and metadata are verified and all mappings are migrated |
| #234 local losing-copy deletion | Its quality preference remains. Its authority to unlink a losing Darkroom-owned file does not authorise deleting, trashing or replacing an Immich asset. The approved first version requires owner resolution of distinct-byte quality conflicts; automatic Immich deletion would require a separate explicit decision and safety review |
| #199 safety of a durable copy in both Immich and `originals/` | Superseded for the target. There is no independent Darkroom copy; an Immich database/media backup and rehearsed restore are prerequisites, and API read-back is not a backup |
| iPhone behaviour, browser credential policy and alert delivery | Unchanged by #259. Darkroom gains no authority over phone clearing, typing credentials or a new delivery channel |

**Proposals, not owner decisions yet:** 14 days of successful-import retention after whole-export verification, pinned unresolved imports/reviews, no automatic Immich asset deletion/replacement, and the staged migration/rollback sequence in the design. Existing `media_items` identities, `duplicate_removals` history and `pixel_copies` obligations are preserved through additive backfill; no migration automatically deletes old source folders. The design's other proposal, separately gated missing-field enrichment of an existing asset, was first decided against on 2026-11-24 and then brought back the same day, as the one-key design of #574 ([below](#existing-asset-enrichment-returns-548-reversed-574-2026-11-24)); it is designed there and not built.

Pinned v3.3.6 OpenAPI/source inspection supports the expected upload endpoint, multipart fields and `created`/`duplicate` responses. It does not prove a successful upload, cross-user isolation, metadata round trip or cleanup safety on the deployment. The prior #202 spike observed reads/checksums only. A disposable-instance write spike and the stronger-model deletion-safety review requested in #259 remain gates before implementation.

## One container for everything (#284, 2026-11-09)

**Owner decisions.** [Issue #284](https://github.com/AnthonyPluth/Darkroom/issues/284) records them; [the design](design/single-container.md) carries them out on paper. Nothing is implemented, and the current runtime (two containers and a hand-configured Syncthing) stays until the design's gated steps land.

| Item | Decision | Source |
| --- | --- | --- |
| One-stop shop | Darkroom handles the whole process end to end and is dead simple to get running: ultimately one container for everything, coordinated and executed by Darkroom | #284 |
| Pixel transport | Syncthing, or its sync function, is integrated into Darkroom. Darkroom owns pairing, folders, Send Only, the `*.xmp` ignore pattern, device IDs and removal, instead of a Syncthing configured by hand | #284 |
| The browser | The Takeout worker joins the same container and may do so in the first phase; the separate container remains as the fallback. Everything is local-only and never exposed publicly, so the owner sees no security concern in the browser sharing the container. The design states the residual risk (the browser is signed in to Google accounts and holds their profiles; Darkroom holds the sealed API keys and the key that unseals them) and what stays: no Darkroom secret in the browser's environment, Chrome's sandbox and its seccomp profile | #284 |
| Zero-configuration secrets and addresses | The browser token is generated by Darkroom at startup and is never a user setting; the browser's addresses (`DARKROOM_URL`, `VNC_URL`) are derived or go away; other hand-set values are generated, derived or asked once in a first-run wizard | #284 |
| Immich | Stays a separate application coordinated through its API, exactly as #259 decided | #259, #284 |

| Earlier decision or text | Effect of #284 |
| --- | --- |
| Two containers, `darkroom` and `darkroom-browser` (SPEC, Deployment and stack; `docs/deploy/browser.md`) | Superseded by one container, kept as the fallback for one minor release, then removed (#327); rollback is pinning the previous release |
| Syncthing set up by hand on Unraid as the Pixels' peer, with a hand-written `.stignore` (SPEC phases 1 and 5; #9) | Superseded as the target: Darkroom runs and configures it. The Pixels keep their own Syncthing app |
| #199 question 2 and question 6, and #216: a transient Send Only folder per Pixel, pruned by Darkroom on the Takeout match, capped by size | Unchanged. Only who configures the transport changes; Syncthing's progress never becomes a reason to prune |
| A shared `DARKROOM_BROWSER_TOKEN` and the `DARKROOM_URL` and `VNC_URL` settings (`docs/deploy/browser.md`) | Superseded: generated and internal, or gone. Removed with the fallback (#327); `check-config` and the start-up log name them as set and ignored |
| Promises 1 to 6 (`AGENTS.md`) and the #259 replacement promises | Unchanged |

**Answers of 2026-11-10 to the design's open questions** (#284):

| Item | Decision |
| --- | --- |
| Order | The browser moves into the container first, then the settings and wizard, then Syncthing |
| Users | One unprivileged user for everything (app, browser, Syncthing). No separate users, no group arrangement, no second mappings for permission reasons, no root init that drops privileges. This was not the design's recommendation. What it gives up, stated once: operating-system permissions no longer stop the browser or Syncthing from writing under the share, so promises 1 and 2 rest on the existing Semgrep rules, the tests and the provider boundaries, not on file permissions |
| Seccomp profile on Unraid | Kept, as one manual host step |
| Generated secret key | In appdata beside the database, with `DARKROOM_SECRET_KEY` as an override |
| First-run wizard | Open to the first visitor from a private address until the first successful sign-in. Amended on 2026-11-16 (below): the passing test sign-in no longer closes it |
| Volumes | State in appdata, including browser profiles; the working folders on a second mapping |
| Syncthing network | Local discovery only, with host networking |
| Syncthing identity | Fresh; each Pixel is paired once |
| Pairing | QR scan plus approval in Darkroom, never automatic |
| A Pixel holding files Darkroom already pruned | Fixed manually only |
| Deep scan #54 | Scoped re-scans after the browser phase and after the Syncthing phase |
| Separate browser container | Kept for one minor release after the one container is the default |

**Answers of 2026-11-16 on the first-run wizard** (#303, #344, #346), given in the session that built the wizard's API, amending the "First-run wizard" row above:

| Item | Decision |
| --- | --- |
| The wizard's order | The wizard keeps the full order of `design/single-container.md` section 8 (Welcome, Address, Sign-in with a test sign-in, Immich, People, Google accounts, Pixels, Alert emails, Done), and the API is extended so it can run. Asked how to handle the API as built, where a passing test sign-in closes the wizard before People, Google accounts and Pixels, the owner answered: "Extend the API - we should never provide a worse experience just so the tests pass" |
| First-run wizard, after the test sign-in | Before the test, unchanged: open to the one first visitor from a private address who claims it. The passing test sign-in signs that account in and ends that phase for good. The wizard then stays open, only to a request carrying both that browser's claim and the tested account's session under the same address and header conditions, until the first of Finish setup, the tested session ending, a sign-in by another account, or 7 days after the test. Asked how long after the passing test the wizard stays reachable, the owner chose "7 days (Recommended)" |
| When stored settings apply | At once, without a restart; precedence stays environment, then stored, then default (#346) |
| The six open points of #358 | Accepted as built: "Accept all as built (Recommended)". After the test the address and sign-in routes answer 409; a claim after the test answers 200 to the pair and 401 to everyone else; finishing before the test answers 409; a stored public address counts at once in the `Host` and `Origin` checks; another account's sign-in is detected by its user row; after the test the wizard does not close merely because `OIDC_*` is set in the environment |
| The wizard's design | Signed off: the owner commented "signed off" on [#344](https://github.com/AnthonyPluth/Darkroom/pull/344#issuecomment-6926275008); the sign-off is recorded on [#303](https://github.com/AnthonyPluth/Darkroom/issues/303#issuecomment-6926281385) |

Two known gaps from review remain open in the design (the order of the reconciler and the placed-copy check; the placement cutoff when a queued copy is adopted).

Everything the design says about Syncthing's REST API, Chromium in the merged image and Unraid's networking is unverified until its spikes are written up; none of it has been run.

## A date or place corrected in Google Photos wins (#438, 2026-11-18)

**Owner decision** (comment on [#438](https://github.com/AnthonyPluth/Darkroom/issues/438), relayed by the coordinating session): use the corrected value from Google Photos when the person edited a date or place there. The sidecar's edited values win, for dates and for places, and for what is carried into a matched phone original's XMP; the file's own EXIF is the fallback.

| Item | As built |
| --- | --- |
| Places | `geoData` (the person's edit) is read before `geoDataExif`; a non-zero sidecar place replaces the file's EXIF place in the record (`gps_source` `google`) and is written to XMP where it differs from the file's own. |
| Dates | The record's capture time stays the file's own, so matching against the phone copy and the `google-only/` folder are unchanged (the owner's decision on #469: keep the file's time for matching and carry Google's corrected date separately). A `photoTakenTime` more than 14 hours from the file's own capture time is a correction that is written to XMP only, recomputed from the stored sidecar JSON. |
| Matching time | A place Google corrected changes the record's place only. Every time field (instant, wall clock, offset, source) stays what the file itself gives, including a `gps_lookup` at the file's own GPS, so a Takeout copy matches its phone copy as before. |
| XMP | The same rules for a Takeout copy and for a matched phone original; a date is written for an original only as a correction, never to fill a gap, with the zone of Google's position, the file's position or the file's own offset, and with no offset when none is known (#445); an existing XMP value is never overwritten; nothing is written to media (promise 5). |
| Not decided by the owner | The 14-hour threshold (the issue's), no distance threshold for places (any difference beyond rounding counts), no review hold, and taking the file's own offset as a zone source for a correction. A correction with no zone follows #445: the clock with no offset, never `+00:00`. |
| A borrowed sidecar replaced by the video's own | A later export stores the incoming JSON and its flag together; the refresh rules are unchanged (no dates, no re-added values), so an own place that differs from a borrowed one the video's GPS never matched is not corrected by the refresh (a known limitation, not a regression against main). |
| A video whose place Google moved across zones | A QuickTime video (UTC instant) keeps the file's own wall clock, offset and month folder, so matching reads the same time on both sides; only the corrected place is carried (the owner's decision on #469). |

## The XMP date when the time zone is unknown (#445, 2026-11-18)

**Owner decision.** [Issue #445](https://github.com/AnthonyPluth/Darkroom/issues/445) asked what `xmp.created_text` should write when a Google-only item has Google's `photoTakenTime` instant but no position to give its zone. The owner answered, relayed by the coordinating session: "when a photo's time zone is unknown, write the local time with no offset in XMP. Do not invent `+00:00`."

| Item | Decision | Source |
| --- | --- | --- |
| `XMP-photoshop:DateCreated` with a position | Unchanged: the local time at the position's offset at that instant, with the offset | #445 |
| `XMP-photoshop:DateCreated` with no position | The clock time with no offset (`2019:07:04 12:30:15`). With no zone known, the clock written is the UTC reading of Google's instant, so it is not a claim about where the photo was taken; the stored `captured_utc` is unchanged. Never `+00:00` | #445 |
| Options not taken | No XMP date at all; a household default offset setting | #445 |

## One part at a time per export, and Google's download count (#500, 2026-11-20)

**Owner decision (#500).** The owner reported: "Darkroom is showing `Part 13 has used all 5 of Google's downloads, so Takeout won't let Darkroom download it again. Ask Takeout for a new export of Google Photos instead.` However Google is showing that it only used 4 of the 5 downloads. Can we disable multiple simultaneous downloads and get it to look at the json at the top of the page to determine number of attempts?" #500 asks for Google's per-part count to be the authority for the budget when it is known, lowering Darkroom's own count if need be (loosening the old rule "Google's count never lowers Darkroom's"), with Darkroom's own count as the fallback, and for one part of an export at a time, reversing the staggered downloads of #463 (a new part every 10 minutes, at most 2 downloading at once). Downloads for different accounts' exports still run at the same time (#496).

| Item | Decision | Source |
| --- | --- | --- |
| Parts of one export | One at a time: the next part is clicked when the one before has been kept or has failed. The interval, the "at most 2" and the back-off after prompts are gone, with no setting | #500 |
| Parts of different accounts' exports | Still download at the same time; the free-space reservation counts every downloading part | #496, #500 |
| The download budget | Google's `Number of times already downloaded` for the part when it is known, read before each attempt and by the status check, lower or higher than Darkroom's; Darkroom's own counter only when Google's is unknown | #500 |
| When the counter goes down | Only when Google's count is read for a part that is not downloading, and (status check) no download job for the export is open, and (download job) the job has not already clicked that part itself, since Google may not have counted that click yet. On such a retry the job still sends Google's count when it is higher than Darkroom's count after the last click, so it can only raise ours. Never while downloading; never on an unknown count. These limits only keep the count higher than #500 alone would; they were not asked for by the owner and are Darkroom's own guard against a count read before Google has caught up | #500 (limits: this change) |

## The scheduled Pixel job's key grants (#535, 2026-11-23)

**Owner decision.** Recorded on [pull request #555](https://github.com/AnthonyPluth/Darkroom/pull/555#issuecomment-7115935945) by the Claude Code session on the owner's behalf. The question: "The backfill reads and uploads with the person's key set up with five 'import' grants. The hourly Pixel job reads the same person's key, but its check demands exactly the four 'read' grants, so it would reject that key and every converted Pixel copy would stay held. How should the Pixel job read?" The owner chose **"Accept either approved key"**: "The Pixel job accepts the person's own non-admin key if it has exactly the read grants or exactly the import grants, and still only issues read requests. No new key to store or set up; the Pixel job simply doesn't reject a key that can also upload."

| Item | Decision | Source |
| --- | --- | --- |
| The Pixel job's reader | `ManagedReader` access `read_or_import` (`jobs.pixel_readers`): the person's own non-admin key with exactly the approved read grants or exactly the approved import grants; every other set, an admin user and the admin key are refused as before. It issues read requests only; `upload` and `bulk_check` still require access `import` | #535, [#555](https://github.com/AnthonyPluth/Darkroom/pull/555#issuecomment-7115935945) |
| Options not taken | A separate read-only key per person; not converting Pixel copies until such a key exists | #535 |

## The recovery-point gate's defaults (#545, 2026-11-24)

The owner accepted the proposed defaults of `docs/design/recovery-point-gate.md`: one acceptance of the remote-deletion boundary for the whole installation, and the accepted boundary stores a SHA-256 of the text shown, which the gate's check counts only when it matches the current text. Recorded on #545 (https://github.com/AnthonyPluth/Darkroom/issues/545#issuecomment-7131017264). The backup tool and the thresholds are still the owner's to answer; until then the design's own constants and the begin/finish command contract apply.

## Existing-asset enrichment returns (#548 reversed, #574, 2026-11-24)

**Owner decision, reversing the same day's earlier one.** On [#548](https://github.com/AnthonyPluth/Darkroom/issues/548#issuecomment-7121490970) the owner first decided "no `asset.update` grant ... So there is no enrichment path: Darkroom never changes the metadata of an asset already in Immich." He then reversed it ([comment 7132386267](https://github.com/AnthonyPluth/Darkroom/issues/548#issuecomment-7132386267)): existing-asset enrichment comes back, filling only fields the existing asset lacks and never overwriting a present value, opt-in per person, with a before and after receipt for every change, read back and verified; "Nothing is built, and the key check keeps refusing `asset.update`, until the design is approved, the live-spike enrichment row has run on a disposable user, and the review gates pass. `not_enriched` holds keep working as designed in the meantime."

**The answers below were recorded by the agent from the owner's answers given in session** (the comments on [#574](https://github.com/AnthonyPluth/Darkroom/issues/574) carry the comment ids named; they are not the owner's own text). **The owner approved the design in session on 2026-11-24, recorded by the agent in [#574 comment 7140641892](https://github.com/AnthonyPluth/Darkroom/issues/574#issuecomment-7140641892).** Nothing is built yet: the section 16 spikes must run on a disposable user before slice 2, the key checks keep refusing `asset.update` and `all`, and the review gates still apply. The design is [existing-asset-enrichment.md](design/existing-asset-enrichment.md); its section 18 lists every answer and where it is applied.

| Item | Decision | Source |
| --- | --- | --- |
| One key, no separate enrichment key | Each person keeps the one Immich key Darkroom already holds, and for a person who opts in it carries all permissions, including `asset.update`. Darkroom cannot check an exact permission set, so it checks the key's user (whoami, non-admin), the server version, the key's listing, ownership of every asset and a code allowlist whose only changing routes are the two import routes and `PUT /api/assets/<id>` from one function. Immich limits nothing then; Darkroom's code does. A person who has not opted in keeps exactly the read grants or exactly the import grants, as before | #574, 7134442957 and 7134540874 |
| Verification | A separate read-back after the write, with the same key, must show the intended values and that nothing else changed before the photo is marked safe | #574, 7134540874 |
| Present values | A present value is never overwritten; a Google value that differs from a present one keeps the hold (Q9, reversing the earlier "let Google's date win"). A file-time fallback date counts as missing (Q11) | #574, 7134540874 and 7134385591 |
| Order of the fields | Description ships first. Date and place fills wait until the live spikes E3, E4, E5, E8 and E16 have run and the owner has recorded them on #266 (Q4) | #574, 7134385591 |
| Opt-in and visibility | Opt-in per person by any signed-in member, one switch for every field (Q1, Q2); commands and events only, no status page (Q5); opting out erases the stored key (Q8); enrichment is refused where no sign-in is configured (S5) | #574, 7134385591 and 7134540874 |
| The key page | A page in the app enters the key, and the command line stays. The owner waives promise 6's design sign-off for that page and, by O2, for the opt-in switch on it. Any signed-in member may enter another person's key, bound to that person by whoami (O7). This waiver covers that page and switch only | #574, 7134385591 and 7135686330 |
| Key lifetime | The key does not expire: it is sealed until removed and ends with sign-in loss or opt-out (O3, S1, reversing the 90-day `expires_at`). For an email-admitted person there is no time limit at all, and the gap where no session can be rechecked is accepted (O10, O11) | #574, 7135686330, 7136524244 and 7139597639 |
| What the key's end stops | Erasing the one key (opt-out, sign-in ending) also stops that person's imports, backfill and Pixel source until a key is entered again (O1), and the reconcile, recorded and alerted (O9) | #574, 7135686330 and 7136524244 |
| Revoking | Darkroom does not revoke the key in Immich (no `DELETE`); the output and the alert tell the person to revoke it there (O4) | #574, 7139527473 |
| Tier 2 and 3 keepers | Enriched, reading "the file lacks it" from the keeper's bytes (Q6); a keeper whose bytes no longer match the version the decision used is refused (O5); enrichment stays limited to keepers of automatic tier 2 and 3 decisions, with exceptions staying with #554 and Live Photo halves with #553 (O6) | #574, 7134385591 and 7139527473 |
| The waiting hold | The hold list says "Waiting to fill in what Immich lacks."; after 7 days "Still waiting after 7 days. Needs your attention." (Q10, O8). This is the owner's approved wording | #574, 7134385591 and 7139527473 |
| Other answers | C2's offset on a present date is a fill (Q3); S2 a code boundary plus Semgrep and tests; S3 no write cap; S4 a freshness deadline of 2 s | #574, 7134385591 and 7134540874 |
| The person's Immich key | Keeps its meaning for a person who has not opted in: exactly the read grants or exactly the import grants ([design](design/immich-managed-storage.md), section 8). The code keeps refusing `asset.update` and `all` until the design's slice 2 lands | #548, #535 |
| `not_enriched` holds | Keep working as designed ([design](design/terminal-holds.md), section 3): shown for review and resolved by the owner (accept the asset as it is with Google's values kept recorded in Darkroom, record an offset in Darkroom, or check again after fixing it in Immich by hand). Enrichment, once built and on, is another way such a hold ends; it does not remove the owner's resolutions | #548, #574 |
| Superseded | The earlier 2026-11-24 decision "no `asset.update` grant, and no enrichment" and its rows "Existing-asset enrichment: Not built" and "A duplicate whose existing asset lacks Google's values: owner resolution only"; the managed-storage design's statement that the key never holds `asset.update`; promise 2's clause that the scheduled job's reader accepts only the read or import grants (the owner's decision of 2026-11-23 on #535), for a person who opts in, when the design's slices 0 and 2 land | #548, #574 |
