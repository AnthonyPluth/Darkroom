from __future__ import annotations

import argparse
import sys
from pathlib import Path

from darkroom import config, monitoring
from darkroom.providers import exiftool
from darkroom.dates import utc_now
from darkroom.domain import index
from darkroom.domain.ingest import unpack
from darkroom.server import handler, jobs
from darkroom.storage import db
from darkroom.storage.models import Person
from darkroom.storage.paths import Photos


def check_config() -> int:
    settings = config.load()
    found = config.problems(settings)
    try:
        exiftool.version()
    except (exiftool.Missing, OSError) as error:
        found.append(str(error))
    for line in found:
        print(f"config: {line}", file=sys.stderr)
    if not found:
        print("Configuration looks complete.")
    return 1 if found else 0


def migrate(settings: config.Settings | None = None) -> int:
    settings = settings or config.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = db.make_engine(db.database_url(settings))
    db.migrate(engine)
    engine.dispose()
    monitoring.log("migrated")
    return 0


def run_job(name: str) -> int:
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    scheduler = jobs.build(settings, engine)
    if name not in scheduler.jobs:
        print(f"no job called {name}; known jobs: {', '.join(sorted(scheduler.jobs))}", file=sys.stderr)
        return 1
    scheduler.run_one(name, utc_now().timestamp())
    engine.dispose()
    return 0


def run_index(slug: str | None) -> int:
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    photos = Photos(settings.photos_root)
    code = 0
    with db.session(engine) as opened:
        people = opened.query(Person).order_by(Person.id)
        if slug:
            people = people.filter(Person.slug == slug)
        found = people.all()
        if slug and not found:
            print(f"no person called {slug}", file=sys.stderr)
            code = 1
        for person in found:
            report = index.index_person(opened, photos, person, utc_now().timestamp())
            monitoring.log("indexed", person=person.slug, new=report.new, unchanged=report.unchanged, changed=report.changed, failed=report.failed)
    engine.dispose()
    return code


def run_import_takeout(slug: str | None, folder: str | None, unpack_only: bool) -> int:
    if not slug:
        print("import-takeout needs --person", file=sys.stderr)
        return 1
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    photos = Photos(settings.photos_root)
    code = 0
    with db.session(engine) as opened:
        person = opened.query(Person).filter(Person.slug == slug).one_or_none()
        if person is None:
            print(f"no person called {slug}", file=sys.stderr)
            code = 1
        else:
            now = utc_now().timestamp()
            try:
                found = unpack.register_zips(opened, person, Path(folder), now) if folder else unpack.inspect_manual_inbox(opened, photos, person, now)
            except unpack.UnpackFailed as error:
                print(str(error), file=sys.stderr)
                found = []
                code = 1
            for export in found:
                report = unpack.unpack(opened, photos, person, export, now)
                monitoring.log("unpacked", person=person.slug, export=export.id, parts=report.parts_unpacked, skipped=report.parts_skipped, files=report.files, problems=len(report.problems))
                for problem in report.problems:
                    print(problem, file=sys.stderr)
                    code = 1
            if not unpack_only and not code:
                print("Unpacked; pairing and import arrive with later steps.")
    engine.dispose()
    return code


def serve() -> int:
    settings = config.load()
    migrate(settings)
    engine = db.make_engine(db.database_url(settings))
    scheduler = jobs.build(settings, engine, utc_now().timestamp())
    scheduler.start(lambda: utc_now().timestamp())
    server = handler.make_server(settings)
    monitoring.log("serving", port=server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        scheduler.stop()
        server.server_close()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.py")
    parser.add_argument("command", choices=["serve", "check-config", "migrate", "jobs", "index", "import-takeout"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--person")
    parser.add_argument("--dir")
    parser.add_argument("--unpack-only", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "index":
        return run_index(args.person)
    if args.command == "import-takeout":
        return run_import_takeout(args.person, args.dir, args.unpack_only)
    if args.command == "jobs":
        if not args.name:
            parser.error("jobs needs the name of a job")
        return run_job(args.name)
    if args.command == "migrate":
        return migrate()
    return serve() if args.command == "serve" else check_config()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
