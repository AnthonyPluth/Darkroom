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

**Proposals, not owner decisions yet:** 14 days of successful-import retention after whole-export verification, pinned unresolved imports/reviews, no automatic Immich asset deletion/replacement, separately gated missing-field enrichment, and the staged migration/rollback sequence in the design. Existing `media_items` identities, `duplicate_removals` history and `pixel_copies` obligations are preserved through additive backfill; no migration automatically deletes old source folders.

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
| Two containers, `darkroom` and `darkroom-browser` (SPEC, Deployment and stack; `docs/deploy/browser.md`) | Superseded as the target by one container. Still the deployed layout, and kept as the fallback |
| Syncthing set up by hand on Unraid as the Pixels' peer, with a hand-written `.stignore` (SPEC phases 1 and 5; #9) | Superseded as the target: Darkroom runs and configures it. The Pixels keep their own Syncthing app |
| #199 question 2 and question 6, and #216: a transient Send Only folder per Pixel, pruned by Darkroom on the Takeout match, capped by size | Unchanged. Only who configures the transport changes; Syncthing's progress never becomes a reason to prune |
| A shared `DARKROOM_BROWSER_TOKEN` and the `DARKROOM_URL` and `VNC_URL` settings (`docs/deploy/browser.md`) | Superseded as the target: generated and internal, or gone. They keep working in the fallback layout |
| Promises 1 to 6 (`AGENTS.md`) and the #259 replacement promises | Unchanged |

**Answers of 2026-11-10 to the design's open questions** (#284):

| Item | Decision |
| --- | --- |
| Order | The browser moves into the container first, then the settings and wizard, then Syncthing |
| Users | One unprivileged user for everything (app, browser, Syncthing). No separate users, no group arrangement, no second mappings for permission reasons, no root init that drops privileges. This was not the design's recommendation. What it gives up, stated once: operating-system permissions no longer stop the browser or Syncthing from writing under the share, so promises 1 and 2 rest on the existing Semgrep rules, the tests and the provider boundaries, not on file permissions |
| Seccomp profile on Unraid | Kept, as one manual host step |
| Generated secret key | In appdata beside the database, with `DARKROOM_SECRET_KEY` as an override |
| First-run wizard | Open to the first visitor from a private address until the first successful sign-in |
| Volumes | State in appdata, including browser profiles; the working folders on a second mapping |
| Syncthing network | Local discovery only, with host networking |
| Syncthing identity | Fresh; each Pixel is paired once |
| Pairing | QR scan plus approval in Darkroom, never automatic |
| A Pixel holding files Darkroom already pruned | Fixed manually only |
| Deep scan #54 | Scoped re-scans after the browser phase and after the Syncthing phase |
| Separate browser container | Kept for one minor release after the one container is the default |

Two known gaps from review remain open in the design (the order of the reconciler and the placed-copy check; the placement cutoff when a queued copy is adopted).

Everything the design says about Syncthing's REST API, Chromium in the merged image and Unraid's networking is unverified until its spikes are written up; none of it has been run.
