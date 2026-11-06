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
| Pruning a Pixel's folder (#199, question 6) | Pruning is Darkroom deleting the transient per-Pixel copy; Syncthing carries the deletion to the Pixel, and `originals/` keeps the permanent copy. It may touch only the per-Pixel folder and gets its own check in a later issue. The signal for when it may delete is the Takeout match (`confirmed_in_google`, an unreversed automatic tier 1 or 2 decision), replacing the fixed-delay, Pixel-report and by-hand options; waiting up to about two months is acceptable because the Pixel has the space. Removing a photo from a device doesn't remove the Google Photos copy once backed up | #199 |
| Immich behaviour the design relies on | Assumptions until the spike confirms them on the running server (asset listing and ownership, stored checksum, original download, what Immich's clearing removes) | #202 |
