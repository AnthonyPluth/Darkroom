# Spike: hash comparison

Settles four open questions in `docs/SPEC.md#open-questions` by hashing real files. The tool is `tools/spikes/compare_hashes.py` (the standard library of the Python the repository targets, 3.15, and `exiftool`; nothing from the app; it reads files and writes nothing, and by default prints no file or folder names: the few items it lists are labelled `item-1`, `item-2`, …, and an error that stops it names only the kind of error). The owner runs it on Unraid against the hand-downloaded export and the sample iPhone files from issue #11, and fills in the Results table.

## Method

```text
python tools/spikes/compare_hashes.py takeout --export <folder of zips, one zip, or an extracted tree> --originals <a person's originals/ folder>
python tools/spikes/compare_hashes.py phone --phone-files <folder of files exported straight from an iPhone> --originals <the same originals/ folder>
```

`takeout` reads every photo and video in the export (zip members are streamed in 1 MiB chunks, never extracted, and each part is opened once, so a multi-part export of any size is never held in memory) and compares SHA-1 with the originals, then by name and size, then by name alone. For files with the same name it compares the tags exiftool reads on each side and lists the ten tags that differ most often. It also reports Live Photos (a still in the originals with a video of the same name: is the video in the export, by hash or by name), items that sit only under album folders (no copy under a `Photos from YYYY` folder), and items whose sidecar lacks `googlePhotosOrigin.mobileUpload`, with up to five sidecars shown under `item-N` labels as key names and value types only. A video beside a still of the same name in the export is counted as a Live Photo video and left out of that count, since Takeout usually gives the video no sidecar of its own (docs/spikes/takeout-metadata-vs-gpth.md, row 16). The video half may come out as `.MP4` where the original is `.MOV`; the Live Photo check matches by hash first and then by stem, ignoring the extension, but only for stems that one original Live Photo uses.

The `googlePhotosOrigin.mobileUpload` key as the marker of an item uploaded from a phone is assumed from the sidecar format; check it against your export's own sidecars and say so in the table if it is different.

`phone` compares files exported unmodified from an iPhone (AirDrop or Files with "All Photos Data") with the copies on Unraid: the ones Möbius Sync delivered, or originals downloaded from Immich once its app backs the phone up.

## What the owner runs

On Unraid, from a checkout of the repository, with `exiftool` on the path (without it the tag list is empty and everything else still works):

```text
python3 tools/spikes/compare_hashes.py takeout --export /mnt/user/<the #11 Takeout download> --originals /mnt/user/<person>/originals
python3 tools/spikes/compare_hashes.py phone --phone-files /mnt/user/<the #11 iPhone samples> --originals /mnt/user/<person>/originals
```

Both modes only read. Paste the default output of each into #20: it holds counts, percentages, exiftool tag names, sidecar key names and `item-N` labels, and no file name, folder or sidecar value. `--show-names` swaps the labels for real file names and lets errors through in full, to find a listed item on your own machine; never paste its output into an issue or a pull request (AGENTS.md, "Personal data in the repo"). Fill in the table below from the default output and close the issue.

## Results

| Question | Result (owner fills in) | Consequence |
| --- | --- | --- |
| 1. Are Takeout copies byte-identical to the originals? | | Mostly identical: tier 1 does the work. Mostly different with the same capture time: tier 2 matters more (#24, merged; the defaults for #10 items 4, 9 and 13 in docs/DECISIONS.md apply). |
| 2. Does Takeout export both halves of a Live Photo? | | Video halves missing: a Live Photo's video never gets a tier 1 or 2 match, so it is never `confirmed_in_google` and its copy waits in the Pixel's folder instead of being pruned (#216). Answer #10 item 14 with this. |
| 3. What do photos shared into albums look like? | | Decides shared-album handling (#10 item 3, unanswered in docs/DECISIONS.md): whether they go only to the export owner's library, and whether they can be the tier 1 or 2 proof for someone else's photo (never across Google accounts, #24). |
| 4. Does Möbius Sync (or Immich's download) deliver byte-identical originals? | | Different: the copies already in `originals/` and what Immich holds won't match at tier 1, which is #202's open question about duplicates when `originals/` is imported. |

## Issue references since #199

The issue (#20) was written before #199. Two of the consequences it names now point at closed work: #37 (the eligibility engine for deleting from an iPhone) and #35 (the Darkroom iPhone app's first-run upload) were closed as not planned on 2026-11-06, because Darkroom ships no iPhone app and decides nothing about what leaves a phone (docs/DECISIONS.md, #199 questions 1 and 5). The table above names what each answer affects now: pruning a Pixel's folder on the Takeout match (#216) for Live Photos, and Immich's copy of the originals (#202, #259) for question 4. #24 is merged, and #10 items 3 and 14 are still unanswered in docs/DECISIONS.md.
