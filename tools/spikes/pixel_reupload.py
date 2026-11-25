from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from tools.spikes.compare_hashes import CHUNK, sha1_stream

LARGE_FIXTURES = {"live": "red", "trash": "green", "quota": "blue"}
SMALL_FIXTURES = {"restore": "orange", "purge": "magenta", "control": "cyan"}
VIDEO_BIT_RATE = "8M"


class SpikeFailed(Exception):
    pass


def encode(encoder: str, destination: Path, seconds: int, seed: int, color: str) -> None:
    source = (
        f"testsrc2=size=640x360:rate=24,noise=alls=15:allf=t+u:all_seed={seed},"
        f"drawbox=x=16:y=16:w=160:h=120:color={color}:t=fill"
    )
    command = [
        encoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-f", "lavfi", "-i", source, "-t", str(seconds), "-an", "-c:v", "libx264",
        "-threads", "2", "-preset", "ultrafast", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
        "-b:v", VIDEO_BIT_RATE, "-minrate", VIDEO_BIT_RATE, "-maxrate", VIDEO_BIT_RATE,
        "-bufsize", VIDEO_BIT_RATE, "-x264-params", "nal-hrd=cbr", "-movflags", "+faststart",
        str(destination.absolute()),
    ]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise SpikeFailed("FFmpeg could not generate a fixture. Check for libx264 and available disk space.") from None


def prepare(destination: Path, target_mb: int) -> dict[str, object]:
    if not 1 <= target_mb <= 512:
        raise SpikeFailed("Choose a target size between 1 and 512 MB.")
    encoder = shutil.which("ffmpeg")
    if encoder is None:
        raise SpikeFailed("Install FFmpeg with the libx264 encoder to generate fixtures.")
    try:
        destination.mkdir()
    except FileExistsError:
        raise SpikeFailed("Choose a new output directory.") from None

    run_id = secrets.token_hex(6)
    seed = secrets.randbits(30)
    videos = {}
    for index, (role, color) in enumerate((LARGE_FIXTURES | SMALL_FIXTURES).items()):
        name = f"darkroom-spike-{run_id}-{role}.mp4"
        path = destination / name
        encode(encoder, path, target_mb if role in LARGE_FIXTURES else 2, seed + index, color)
        with path.open("rb") as video:
            sha1 = sha1_stream(video)
            size = video.tell()
        if not size:
            raise SpikeFailed("FFmpeg produced an empty fixture.")
        videos[role] = {"file": name, "bytes": size, "sha1": sha1}
    return {"fixture_set": run_id, "videos": videos}


def compare(fixture: Path, observed: Path) -> tuple[bool, int, int]:
    if not fixture.is_file() or not observed.is_file():
        raise SpikeFailed("Compare needs two readable files.")
    with fixture.open("rb") as expected, observed.open("rb") as found:
        identical = True
        while True:
            left = expected.read(CHUNK)
            right = found.read(CHUNK)
            if not left and not right:
                break
            identical &= left == right
        fixture_bytes, observed_bytes = expected.tell(), found.tell()
    return identical and fixture_bytes > 0, fixture_bytes, observed_bytes


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare generated Pixel re-upload fixtures or compare two local files exactly.")
    commands = parser.add_subparsers(dest="mode", required=True)
    generator = commands.add_parser("prepare")
    generator.add_argument("--out", type=Path, required=True, help="new directory for the generated MP4 files")
    generator.add_argument("--target-mb", type=int, default=128, metavar="1..512")
    verifier = commands.add_parser("compare")
    verifier.add_argument("fixture", type=Path)
    verifier.add_argument("observed", type=Path)
    args = parser.parse_args(argv)
    out = out or sys.stdout
    try:
        if args.mode == "prepare":
            print(json.dumps(prepare(args.out, args.target_mb), indent=2), file=out)
            return 0
        identical, fixture_bytes, observed_bytes = compare(args.fixture, args.observed)
        print(json.dumps({"exact_match": identical, "fixture_bytes": fixture_bytes, "observed_bytes": observed_bytes}), file=out)
        return 0 if identical else 1
    except SpikeFailed as error:
        print(f"Stopped. {error}", file=out)
    except OSError:
        print("Stopped. Could not read or write a local file.", file=out)
    return 2


if __name__ == "__main__":
    sys.exit(main())
