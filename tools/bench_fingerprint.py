from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darkroom.domain import media
from darkroom.domain.ingest import part_ingest
from darkroom.providers import exiftool
from darkroom.storage.models import Base, TakeoutEntry
from tests.fixtures import media as fixtures

STILLS = {".jpg", ".jpeg"}
Hashes = Callable[[Path], media.PerceptualHashes]


def made_up_photos(folder: Path, count: int) -> list[Path]:
    return [fixtures.photo_jpeg(seed, folder / f"photo-{seed}.jpg", fixtures.PHONE_SIZES[seed % 3]) for seed in range(count)]


def own_photos(folder: Path, count: int) -> list[Path]:
    found = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in STILLS)
    return found[:count]


def timed(work: Callable[[], object]) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def timed_on(step: Callable[[Path], object], path: Path) -> float:
    started = time.perf_counter()
    step(path)
    return time.perf_counter() - started


def full(path: Path) -> media.PerceptualHashes:
    return media.perceptual_hashes(path, draft=None)


def reduced(path: Path) -> media.PerceptualHashes:
    return media.perceptual_hashes(path)


def decoded(hashes: Hashes) -> Callable[[Path], media.Decoded]:
    def decode(path: Path) -> media.Decoded:
        return media.Decoded(media.sha1_of(path), hashes(path))
    return decode


def per_file(photos: Sequence[Path]) -> dict[str, float]:
    tags = exiftool.read(list(photos))
    steps: dict[str, Callable[[Path], object]] = {
        "sha1": media.sha1_of,
        "perceptual, full decode": full,
        "perceptual, reduced decode": reduced,
        "metadata parse": lambda path: media.fingerprint(path, tags.get(path, {}), decoded=media.Decoded(("", 0), media.PerceptualHashes(None, None))),
    }
    found = {name: statistics.median(timed_on(step, path) for path in photos) for name, step in steps.items()}
    found["metadata read (exiftool, per file in one batch)"] = timed(lambda: exiftool.read(list(photos))) / len(photos)
    return found


def batch(photos: Sequence[Path], hashes: Hashes, workers: int) -> float:
    def run() -> None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(decoded(hashes), path) for path in photos]
            tags = exiftool.read(list(photos))
            for path, future in zip(photos, futures, strict=True):
                media.fingerprint(path, tags.get(path, {}), decoded=future.result())
    return timed(run)


def today(photos: Sequence[Path]) -> float:
    def run() -> None:
        tags = exiftool.read(list(photos))
        for path in photos:
            media.fingerprint(path, tags.get(path, {}), decoded=decoded(full)(path))
    return timed(run)


def distances(photos: Sequence[Path]) -> tuple[Counter[int], Counter[int]]:
    phash, dhash = Counter[int](), Counter[int]()
    for path in photos:
        old, new = full(path), reduced(path)
        if old.phash and new.phash and old.dhash and new.dhash:
            phash[(int(old.phash, 16) ^ int(new.phash, 16)).bit_count()] += 1
            dhash[(int(old.dhash, 16) ^ int(new.dhash, 16)).bit_count()] += 1
    return phash, dhash


def progress_query(entries: int) -> float:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[TakeoutEntry.__tablename__]])
    with Session(engine) as opened:
        opened.add_all(TakeoutEntry(export_id=1, part_number=1, name=f"entry-{n}", kind="media", bytes=1, crc32=0, listed_at=0.0,
                                    outcome=part_ingest.PLACED if n % 2 else part_ingest.LISTED) for n in range(entries))
        opened.commit()
        query = select(TakeoutEntry.outcome, func.count()).where(TakeoutEntry.export_id == 1, TakeoutEntry.part_number == 1).group_by(TakeoutEntry.outcome)
        return statistics.median(timed(lambda: opened.execute(query).all()) for _ in range(50))


def main() -> None:
    parser = argparse.ArgumentParser(description="Time part ingest's fingerprinting; prints counts and durations only")
    parser.add_argument("--count", type=int, default=100, help="files in the batch (part ingest's batch is 100)")
    parser.add_argument("--folder", type=Path, help="time your own JPEGs instead of made-up ones; no name or path is printed")
    parser.add_argument("--entries", type=int, default=9008, help="ledger rows for the progress query")
    arguments = parser.parse_args()
    workers = part_ingest.fingerprint_workers()
    with tempfile.TemporaryDirectory() as scratch:
        photos = own_photos(arguments.folder, arguments.count) if arguments.folder else made_up_photos(Path(scratch), arguments.count)
        if not photos:
            raise SystemExit("no JPEGs found")
        print(f"{len(photos)} JPEGs, {workers} workers")
        print("per file, median:")
        for name, seconds in per_file(photos).items():
            print(f"  {name}: {seconds * 1000:.1f} ms")
        baseline = today(photos)
        print(f"one batch of {len(photos)}:")
        for name, seconds in (("today (one thread, full decode)", baseline), ("reduced decode only", batch(photos, reduced, 1)),
                              (f"{workers} workers only", batch(photos, full, workers)), (f"{workers} workers and reduced decode", batch(photos, reduced, workers))):
            print(f"  {name}: {seconds:.2f} s, {baseline / seconds:.1f}x")
        phash, dhash = distances(photos)
        print(f"hash distance, full against reduced decode: phash {sorted(phash.items())}, dhash {sorted(dhash.items())}")
    print(f"progress query over {arguments.entries} ledger rows, median: {progress_query(arguments.entries) * 1000:.2f} ms")


if __name__ == "__main__":
    main()
