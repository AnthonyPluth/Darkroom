# Spike P3-02: does Immich read XMP sidecars from a read-only external library?

Decides the sidecar filename form and whether a rescan picks up sidecar edits (#25, #26). Issue: #21.

## Method

1. Generate the samples: `python3 tools/spikes/make_xmp_sample.py /tmp/xmp-spike` (needs Pillow and exiftool).
   - `sample-a.jpg` + `sample-a.jpg.xmp`: sidecar named after the full file name.
   - `sample-b.jpg` + `sample-b.xmp`: sidecar named after the stem.
   - `sample-c.jpg`: control, no sidecar.
   - Every JPEG embeds the wrong values: date 2001-01-01 01:01:01, GPS 10.0 N 20.0 E, description "WRONG embedded description".
   - Every sidecar holds the right values: date 2024-06-15 12:00:00, GPS 45.4340 N 12.3388 E (a city square), description "RIGHT sidecar description".
2. Copy the five files into a scratch person's `google-only/spike/` folder, which Immich mounts read-only.
3. Trigger a library scan for that person's external library.
4. In Immich, open each sample and record whether it shows the sidecar's date, location and description or the embedded ones.
5. Pick a sidecar that step 4 showed Immich actually loaded (if neither form was read, skip steps 5 and 6 and say so in the Results). Edit its date to 2023-03-03 09:00:00 and rescan. Record whether Immich shows the new date.
6. Delete that same sidecar and rescan. Record what Immich shows (embedded values, stale sidecar values, or the asset gone).
7. Remove the scratch files from `google-only/spike/` and rescan.

## Results

Owner fills this in; #25 reads it before choosing the filename form.

| Question | `sample-a` (`.jpg.xmp`) | `sample-b` (`.xmp`) | `sample-c` (control) |
| --- | --- | --- | --- |
| Sidecar read at all (yes/no) | | | n/a |
| Date: sidecar overrides EXIF (yes/no) | | | n/a |
| Location: sidecar overrides EXIF (yes/no) | | | n/a |
| Description: sidecar overrides EXIF (yes/no) | | | n/a |
| Edited sidecar picked up by rescan (yes/no) | | | n/a |
| After deleting the sidecar and rescanning | | | n/a |

| Conclusion | |
| --- | --- |
| Filename form to use in #25 | |
| What to do after a sidecar change (plain rescan, force rescan, other) | |
| Immich version tested | |
