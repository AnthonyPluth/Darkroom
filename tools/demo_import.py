from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import run
from darkroom import config
from darkroom.dates import utc_now
from darkroom.domain import demo
from darkroom.storage import db
from darkroom.storage.models import Person
from tests.fakes.immich import FakeImmich, FakePerson
from tests.takeout_fixtures import Manifest, TakeoutBuilder

RESULT_FILE = "demo-import.json"
IMPORTER = "avery"
ADMIN_KEY = "demo-immich-admin-key-not-real"
EXPORT_FOLDER = "takeout-export"
EXPORT_SEED = 7
PARTS = 2
HEIC = ".HEIC"
JPEG = ".JPG"
VIDEO = ".MOV"
FOLDER_YEAR = 2026
LIKELY_RE_ENCODES = (("avery", "IMG_0001"), ("blair", "IMG_0102"))


def shot_of(slug: str, stem: str) -> demo.Shot:
    return next(shot for shot in demo.SHOTS[slug] if shot.stem == stem)


def phone_file(photos_root: Path, slug: str, stem: str, suffix: str) -> bytes:
    return (photos_root / slug / "originals" / f"{stem}{suffix}").read_bytes()


def variant(shot: demo.Shot, today: date, suffix: str, colour_step: int, seconds: int = 0) -> bytes:
    local, _ = demo.taken_at(shot, today)
    moved = dataclasses.replace(shot, colour=(shot.colour[0], shot.colour[1], min(255, shot.colour[2] + colour_step)))
    return demo.encoded(moved, local + timedelta(seconds=seconds), "HEIF" if suffix == HEIC else "JPEG")


def days_ago(today: date, days: int) -> int:
    return int(demo.noon(today)) - days * 86_400


def build_export(photos_root: Path, folder: Path, today: date) -> Manifest:
    builder = TakeoutBuilder(folder, seed=EXPORT_SEED, parts=PARTS)
    year = FOLDER_YEAR
    avery_gps = shot_of("avery", "IMG_0001").gps
    builder.photo("IMG_0001.JPG", year, data=phone_file(photos_root, "avery", "IMG_0001", JPEG), gps=avery_gps)
    builder.photo("IMG_0102.JPG", year, form="supplemental", part=0, sidecar_part=1, data=phone_file(photos_root, "blair", "IMG_0102", JPEG),
                  gps=shot_of("blair", "IMG_0102").gps)
    still = builder.photo("IMG_0003.HEIC", year, data=phone_file(photos_root, "avery", "IMG_0003", HEIC), gps=shot_of("avery", "IMG_0003").gps)
    movie = builder.add("IMG_0003.MOV", phone_file(photos_root, "avery", "IMG_0003", VIDEO), f"Photos from {year}", 0, "json", None, None, "")
    builder.manifest.live_pairs[movie.media_name] = still.media_name
    builder.photo("IMG_0002.JPG", year, data=variant(shot_of("avery", "IMG_0002"), today, JPEG, 12), gps=None)
    builder.photo("IMG_0103.HEIC", year, part=1, data=phone_file(photos_root, "blair", "IMG_0103", HEIC), gps=None)
    edit = builder.add("IMG_0103-edited.HEIC", variant(shot_of("blair", "IMG_0103"), today, HEIC, 30), f"Photos from {year}", 1, None, None, None, "")
    builder.manifest.edited_of[edit.media_name] = "IMG_0103.HEIC"
    for number, (slug, stem) in enumerate(LIKELY_RE_ENCODES):
        builder.add(f"{stem}(1).JPG", variant(shot_of(slug, stem), today, JPEG, 24, seconds=1), f"Photos from {year}", number, None, None, None, "",
                    sidecar_name=f"{stem}.JPG(1).json")
    builder.photo("IMG_5001.JPG", year, gps=demo.NORTH_ATLANTIC, description="A made-up harbour", taken=days_ago(today, 20))
    builder.photo("IMG_5002.JPG", year, part=1, gps=None, taken=days_ago(today, 5))
    google_still = builder.photo("IMG_5003.HEIC", year, gps=None, data=builder.image_bytes(HEIC), taken=days_ago(today, 8))
    google_movie = builder.add("IMG_5003.MOV", builder.video_bytes(), f"Photos from {year}", 0, "json", None, None, "", taken=days_ago(today, 8))
    builder.manifest.live_pairs[google_movie.media_name] = google_still.media_name
    return builder.build()


def fake_people() -> list[FakePerson]:
    return [FakePerson(who.slug, f"demo-user-{who.slug}", f"demo-library-{who.slug}", f"demo-key-{who.slug}", who.name, who.email) for who in demo.PEOPLE]


def link_people(settings: config.Settings, people: list[FakePerson]) -> None:
    engine = db.make_engine(db.database_url(settings))
    try:
        with db.session(engine) as opened:
            for fake in people:
                person = opened.query(Person).filter(Person.slug == fake.slug).one()
                person.immich_user_id = fake.user_id
    finally:
        engine.dispose()


@contextlib.contextmanager
def environment(**values: str):
    before = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old in before.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def seed_import(settings: config.Settings, today: date) -> dict[str, Any]:
    people = fake_people()
    link_people(settings, people)
    folder = settings.data_dir.parent / EXPORT_FOLDER
    manifest = build_export(settings.photos_root, folder, today)
    try:
        with FakeImmich(people, ADMIN_KEY) as fake, environment(IMMICH_URL=fake.url, IMMICH_ADMIN_KEY=ADMIN_KEY):
            code = run.run_import_takeout(IMPORTER, str(folder), False)
            scans = [{"library": scan.library_id, "person": next(p.slug for p in people if p.library_id == scan.library_id)} for scan in fake.scans()]
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    result: dict[str, Any] = {"code": code, "person": IMPORTER, "files": len(manifest.media), "parts": len(manifest.parts), "scans": scans}
    (settings.data_dir / RESULT_FILE).write_text(json.dumps(result, sort_keys=True))
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.demo_import")
    parser.add_argument("--data", required=True)
    args = parser.parse_args(argv)
    settings = dataclasses.replace(config.load(), data_dir=Path(args.data) / "data", photos_root=Path(args.data) / "photos")
    result = seed_import(settings, utc_now().date())
    print(f"demo import: {result['files']} files in {result['parts']} parts imported for {result['person']}, {len(result['scans'])} Immich rescans")
    return result["code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
