# Spike: hash comparison

Settles four open questions in `docs/SPEC.md#open-questions` by hashing real files. The tool is `tools/spikes/compare_hashes.py` (Python 3.14 and `exiftool` only; it reads files and writes nothing, and prints file names only, never folders). The owner runs it on Unraid against the hand-downloaded export and the sample iPhone files from issue #11, and fills in the Results table.

## Method

```text
python tools/spikes/compare_hashes.py takeout --export <folder of zips, one zip, or an extracted tree> --originals <a person's originals/ folder>
python tools/spikes/compare_hashes.py phone --phone-files <folder of files exported straight from an iPhone> --originals <the same originals/ folder>
```

`takeout` reads every photo and video in the export (zip members are streamed, never extracted) and compares SHA-1 with the originals, then by name and size, then by name alone. For files with the same name it compares the tags exiftool reads on each side and lists the ten tags that differ most often. It also reports Live Photos (a still in the originals with a video of the same name: is the video in the export, by hash or by name), items that sit only under album folders (no copy under a `Photos from YYYY` folder), and items whose sidecar lacks `googlePhotosOrigin.mobileUpload`, with up to five sidecars shown as key names and value types only.

The `googlePhotosOrigin.mobileUpload` key as the marker of an item uploaded from a phone is assumed from the sidecar format; check it against your export's own sidecars and say so in the table if it is different.

`phone` compares files exported unmodified from an iPhone (AirDrop or Files with "All Photos Data") with the copies Möbius Sync delivered.

## Results

| Question | Result (owner fills in) | Consequence |
| --- | --- | --- |
| 1. Are Takeout copies byte-identical to the originals? | | Mostly identical: tier 1 does the work. Mostly different with the same capture time: tier 2 matters more (#24). |
| 2. Does Takeout export both halves of a Live Photo? | | Video halves missing: never delete a Live Photo (#10 item 14, #37). |
| 3. What do photos shared into albums look like? | | Decides shared-album handling (#10 item 3, #24). |
| 4. Does Möbius Sync deliver byte-identical originals? | | Different: the app's first run re-uploads instead of linking (#35). |
