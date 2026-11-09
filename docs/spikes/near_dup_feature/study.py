from __future__ import annotations

import argparse
import math
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ai_edge_litert.interpreter import Interpreter
from PIL import Image

import fixtures
from darkroom.domain import media, near_duplicates

FEATURE_WIDTH = 1280
SWEEP = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)


class Features:
    def __init__(self, model: Path) -> None:
        self.interpreter = Interpreter(model_path=str(model), experimental_preserve_all_tensors=True)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        pooled = [d for d in self.interpreter.get_tensor_details() if int(np.prod(d["shape"])) == FEATURE_WIDTH and d["shape"][-1] == FEATURE_WIDTH]
        if not pooled:
            raise SystemExit(f"no {FEATURE_WIDTH}-wide tensor in the model")
        self.pooled = pooled[-1]

    def vector(self, image: Image.Image) -> np.ndarray:
        height, width = int(self.input["shape"][1]), int(self.input["shape"][2])
        pixels = np.asarray(image.convert("RGB").resize((width, height), Image.Resampling.BILINEAR), dtype=np.uint8)
        self.interpreter.set_tensor(self.input["index"], pixels[np.newaxis, ...])
        self.interpreter.invoke()
        raw = self.interpreter.get_tensor(self.pooled["index"]).reshape(-1).astype(np.float32)
        scale, zero = self.pooled["quantization"]
        if scale:
            raw = (raw - zero) * scale
        return raw / (np.linalg.norm(raw) or 1.0)


@dataclass(frozen=True)
class Pair:
    variant: str
    same_family: bool
    hashes: bool
    burst: bool
    feature: float


def frames(path: Path, item_id: int, captured: float | None) -> near_duplicates.Frame:
    found = media.perceptual_hashes(path)
    frame = near_duplicates.frame_of(item_id, found.phash, found.dhash, captured)
    if frame is None:
        raise SystemExit("a fixture could not be hashed")
    return frame


def build(model: Path, work: Path, kind: str) -> tuple[list[Pair], list[Pair], str]:
    features = Features(model)
    found = fixtures.bases(kind)
    originals: dict[str, tuple[near_duplicates.Frame, near_duplicates.Frame, np.ndarray]] = {}
    edited: dict[tuple[str, str], tuple[near_duplicates.Frame, near_duplicates.Frame, np.ndarray]] = {}
    for number, base in enumerate(found):
        path = work / f"{base.key}.png"
        base.image.save(path)
        originals[base.key] = (frames(path, number, None), frames(path, number, 1.0), features.vector(base.image))
        for name, make in fixtures.VARIANTS.items():
            image = make(base.image)
            variant_path = work / f"{base.key}--{name}.png"
            image.save(variant_path)
            edited[(base.key, name)] = (frames(variant_path, number, None), frames(variant_path, number, 1.0), features.vector(image))
    family = {base.key: base.family for base in found}
    positives: list[Pair] = []
    negatives: list[Pair] = []
    for key, (plain, timed, vector) in originals.items():
        for (other, name), (plain2, timed2, vector2) in edited.items():
            pair = Pair(
                name,
                family[key] == family[other],
                near_duplicates.related(plain2, plain, near_duplicates.DEFAULTS) is not None,
                near_duplicates.related(timed2, timed, near_duplicates.DEFAULTS) is not None,
                1.0 - float(vector @ vector2),
            )
            (positives if key == other else negatives).append(pair)
    return positives, negatives, features.pooled["name"]


def percent(count: int, total: int) -> str:
    return f"{100 * count / total:.0f}% ({count}/{total})" if total else "n/a"


def report(positives: list[Pair], negatives: list[Pair], tensor: str) -> str:
    lowest = min(pair.feature for pair in negatives)
    zero = math.floor(lowest * 1000) / 1000 - 0.001
    same_family = [pair for pair in negatives if pair.same_family]
    cross_family = [pair for pair in negatives if not pair.same_family]
    out = [
        f"Feature: `{tensor}` ({FEATURE_WIDTH} values), cosine distance.",
        f"Pairs: {len(positives)} duplicate pairs ({len(positives) // len(fixtures.VARIANTS)} per variant), {len(cross_family)} unrelated pairs, {len(same_family)} same-scene-different-shot pairs.",
        f"Smallest distance between two different photos: {lowest:.3f}. Zero-false-pair threshold: {zero:.3f}.",
        "",
        "| Variant | Hashes (strict) | Hashes (burst window) | Feature at zero-false threshold | Hashes (strict) or feature | Found only by the feature |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    totals = {"hashes": 0, "burst": 0, "feature": 0, "either": 0, "only": 0, "all": 0}
    for name in fixtures.VARIANTS:
        rows = [pair for pair in positives if pair.variant == name]
        hashes = sum(pair.hashes for pair in rows)
        burst = sum(pair.burst for pair in rows)
        feature = sum(pair.feature <= zero for pair in rows)
        either = sum(pair.hashes or pair.feature <= zero for pair in rows)
        only = sum(not pair.hashes and pair.feature <= zero for pair in rows)
        out.append(f"| {name} | {percent(hashes, len(rows))} | {percent(burst, len(rows))} | {percent(feature, len(rows))} | {percent(either, len(rows))} | {only} |")
        for key, value in (("hashes", hashes), ("burst", burst), ("feature", feature), ("either", either), ("only", only), ("all", len(rows))):
            totals[key] += value
    out.append(
        f"| **all** | {percent(totals['hashes'], totals['all'])} | {percent(totals['burst'], totals['all'])} | {percent(totals['feature'], totals['all'])} "
        f"| {percent(totals['either'], totals['all'])} | {totals['only']} |"
    )
    out += [
        "",
        f"Hash false pairs: strict {sum(pair.hashes for pair in negatives)}, burst window {sum(pair.burst for pair in negatives)} of {len(negatives)} unrelated or different-shot pairs.",
        "",
        "| Feature threshold | Duplicates found | False pairs, unrelated | False pairs, same scene |",
        "| --- | --- | --- | --- |",
    ]
    for limit in SWEEP:
        out.append(
            f"| {limit:.2f} | {percent(sum(pair.feature <= limit for pair in positives), len(positives))} "
            f"| {sum(pair.feature <= limit for pair in cross_family)} | {sum(pair.feature <= limit for pair in same_family)} |"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--kind", choices=("all", "natural", "procedural"), default="all")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        positives, negatives, tensor = build(arguments.model, Path(folder), arguments.kind)
    text = report(positives, negatives, tensor)
    sys.stdout.write(text + "\n")
    if arguments.out is not None:
        arguments.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
