# Spike: a model feature as a near-duplicate signal

Answers #270 (part of #63): does a vision model's feature vector find near-duplicates that Darkroom's pHash/dHash grouping (`darkroom/domain/near_duplicates.py`, tiers 3 and 4) misses, without adding false groups? Nothing in `darkroom/` changes; the study is `docs/spikes/near_dup_feature/`.

## Recommendation

Ship nothing. The feature finds things the hashes miss, but not the things Darkroom's flow produces, and it cannot tell a different shot of the same scene from an edit of the same shot without losing most of its gain.

- A feature comparison (cosine distance on a quantized MobileNet-v2's pooled output) found 26 to 45 points more duplicates than the hashes alone (32 on the mixed set), but only in framing and overlay edits: a 90% crop, a 4% shift, a 5 degree rotation, a watermark, a brightness and contrast edit. Hashes already find 100% of re-compressed and downscaled copies, which is what a Takeout copy of a phone original is, and the feature is blind to colour changes (grayscale and tint, 14% and 12% on the mixed set).
- Google's own edited copies are matched through their original (tier 3), so what the feature adds is mostly a crop, rotation or overlay made elsewhere that arrives as a separate file.
- The price of the gain is the false pairs. A distance of 0.065 or less had no false pair at all, but it keeps only the cases in the table below. At 0.10, 5 of 43,648 unrelated pairs and 20 of 704 different-shot-of-the-same-scene pairs matched; at 0.15, 580 and 155.
- Re-open this only if the review queue shows real duplicates that hashes missed. A follow-up issue would then have to decide where the model runs (it is 3.4 MB and needs a TFLite runtime for the Python the image uses), where 1280 values per photo are stored, a time gate to cut unrelated pairs (not tested here: the fixtures have no capture times), and that a feature match only proposes a tier 4 review item and never an automatic decision. The Coral Edge TPU, the worker and the queue in #63 are not needed for any of it: one inference is tiny, and the study ran on a CPU.

## Method

`fixtures.py` makes the images. A base set of 64 images: 24 procedural scenes in pairs (a family shares a background and has different shapes: the same place, a different moment) and 8 public-domain sample photos from `scikit-image`'s bundled data (astronaut, camera, chelsea, coffee, rocket, coins, clock, brick), each split into a left and a right half. The sample photos are loaded at run time and never committed; no personal photo is involved. Each base gets 11 variants: recompressed, thumbnail, retouched (brightness and contrast), heavily edited, grayscale, tinted, crop 90%, crop 70%, rotated 5 degrees, watermarked, shifted 4% (a burst shot).

`study.py` scores every pair of an original and a variant:

- Hashes: the files go through `darkroom.domain.media.perceptual_hashes` and the pair through `near_duplicates.related` with the default thresholds, once without capture times (the strict thresholds: pHash 6 and dHash 8) and once with equal capture times (the burst thresholds: 10 and 14, as for two shots within 2 seconds).
- Feature: the 1280-value pooled tensor of MobileNet-v2 (w8a8 TFLite, ImageNet), L2-normalised, cosine distance.
- A duplicate pair is an original with one of its own variants. A false pair is an original with a variant of another image; the same-scene ones, a variant of the other half of the same photo or the other scene of the same family, are counted separately.
- The zero-false threshold is the largest distance below the smallest distance between two different photos. It is fitted on the same pairs it is scored on, so it flatters the feature.

The model is `mobilenet_v2-tflite-w8a8.zip` from the Qualcomm AI Hub release v0.66.0 (SHA-256 of the `.tflite` is `ba4c39fdda9a3a5b0a6f69a6c7542aba57fbabfced91fb37086298461fdeaf06`; its Hugging Face card lists Apache-2.0, check it before reuse). The release's `release_assets.json` links the zip (`https://huggingface.co/qualcomm/MobileNet-v2`). The model has no feature output, so the study reads its last 1280-wide tensor with `experimental_preserve_all_tensors`.

To re-run (versions used: Python 3.13, `ai-edge-litert` 2.3.0, `scikit-image` 0.26.0, `ImageHash` 4.3.2, `SQLAlchemy` 2.1.8, `alembic` 1.20.1, `pillow-heif` 1.8.1, `timezonefinder` 9.2.0, Pillow 12.3.0, NumPy 2.5.3; the last five are only there because `darkroom.domain.near_duplicates` imports them):

```text
python -m venv ~/spike-venv
~/spike-venv/bin/pip install ai-edge-litert scikit-image imagehash sqlalchemy alembic pillow-heif timezonefinder
PYTHONPATH=. ~/spike-venv/bin/python docs/spikes/near_dup_feature/study.py --model mobilenet_v2.tflite [--kind all|natural|procedural] [--out result.md]
```

Nothing is added to `pyproject.toml` or the images.

## Results

All 64 bases: 704 duplicate pairs (64 per variant), 43,648 unrelated pairs, 704 same-scene pairs. Zero-false threshold 0.065 (the smallest distance between two different photos is 0.066).

| Variant | Hashes (strict) | Hashes (burst window) | Feature at 0.065 | Hashes (strict) or feature | Found only by the feature |
| --- | --- | --- | --- | --- | --- |
| recompressed | 98% | 100% | 92% | 100% | 1 |
| thumbnail | 100% | 100% | 100% | 100% | 0 |
| retouched | 62% | 92% | 92% | 95% | 21 |
| heavily-edited | 52% | 84% | 61% | 83% | 20 |
| grayscale | 100% | 100% | 14% | 100% | 0 |
| tinted | 100% | 100% | 12% | 100% | 0 |
| crop-90 | 3% | 47% | 98% | 98% | 61 |
| crop-70 | 0% | 0% | 22% | 22% | 14 |
| rotated-5 | 0% | 14% | 36% | 36% | 23 |
| watermarked | 66% | 88% | 100% | 100% | 22 |
| shifted-4pct | 0% | 9% | 97% | 97% | 62 |
| **all** | 53% | 67% | 66% | 85% | 224 |

Hashes had no false pair, strict or burst, in 44,352 pairs. The feature by threshold:

| Feature threshold | Duplicates found | False pairs, unrelated | False pairs, same scene |
| --- | --- | --- | --- |
| 0.05 | 56% | 0 | 0 |
| 0.10 | 80% | 5 | 20 |
| 0.15 | 87% | 580 | 155 |
| 0.20 | 92% | 5,218 | 350 |
| 0.25 | 98% | 13,051 | 503 |
| 0.30 | 100% | 18,978 | 598 |

On the 16 natural bases alone (176 duplicate pairs, 2,464 unrelated, 176 same-scene) the picture is the same: hashes 60%, feature at 0.065 70%, either 86%, with 46 found only by the feature (13 of 16 crops at 90%, 14 of 16 shifts, 11 of 16 rotations); at 0.10 there are 0 false unrelated pairs but 20 same-scene ones, at 0.15 95. On the 48 procedural bases alone: hashes 51%, feature at 0.096 78%, either 96%. Procedural scenes (flat shapes) give the model little to separate, which is where most unrelated false pairs come from.

## What this does not show

- The fixtures are 64 images, 16 of them natural, one model, one quantization. The numbers show a direction, not a rate on a real library.
- Real phone photos and real Takeout copies were not used, so how often the cases the feature adds occur in the household's library is not measured.
- No capture times: production groups by time too (`REVIEW_MAX_SECONDS`, the 2 second burst window), which removes most unrelated pairs for hashes and would do the same for a feature signal. The study measures the signals' strength alone, which is harder than production.
- The zero-false threshold is chosen on the pairs it is scored on.
- Speed on the Coral Edge TPU, Pixel-sized images and HEIC input are not measured.
