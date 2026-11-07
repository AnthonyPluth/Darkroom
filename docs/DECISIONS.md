# Decisions

Answers to the owner's questions in issues #10 and #199, copied here as agents reach them. Until an item is answered, the proposed default in the issue applies.

| Item | Decision | Source |
| --- | --- | --- |
| 4. Google's edited copies | Skip (the default; unanswered): the `keep_edited_copies` setting defaults to false | #10, #24 |
| 5. Final name | Darkroom (the default; unanswered) | #10 |
| 9. Human-resolved tier-4 matches | Not proof (the default; unanswered): only automatic tier 1 and 2 decisions set `confirmed_in_google` | #10, #24 |
| 13. Tier-2 time when the offset is unknown | UTC when both sides have it, else wall-clock; a missing time on either side never matches (the default; unanswered) | #10, #24 |
| Immich-first intake (#199, option B) | Each iPhone backs up and clears its photos with Immich's own iOS app. Darkroom ships no iPhone app and decides nothing about what leaves a phone. Darkroom reads new assets from Immich | #199 |
| Deletion gap (#199, question 1) | Accepted: a photo can be cleared from an iPhone before Google has it, with Immich and the Unraid backup as the safety. Clearing happens at any time with Immich's own function and Darkroom plays no part; the gap to Google (via the Pixel) should be an hour or two at most | #199 |
| Copies and the Pixels' folder (#199, question 2) | Copies, not hardlinks. Darkroom writes the permanent copy into the person's `originals/` once. A separate transient folder per Pixel is synced by Syncthing and emptied once Google has the photos, because a Pixel has about 128 GB. `originals/` is no longer sent to the Pixel | #199 |
| Alert channel (#199, question 3) | Not decided. Until it is, alerts are recorded as events and shown on the status page | #199 |
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
| Duplicates on Unraid (#234, Nov 7) | Darkroom keeps no permanent duplicate. When the Immich reconcile stores a phone original that matches a Takeout copy in `google-only/` (automatic tier 1 or 2), the better copy stays and the other is deleted, with no trash area; identical copies keep the phone original. The quality rule (resolution, then file size, then metadata) is in `docs/SPEC.md`. This supersedes "dedupe deletes nothing on Unraid" (promise 2) for the losing copy of a matched pair only | #234, #199 |
