from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from darkroom import config, monitoring, oidc
from darkroom.providers import apns, exiftool
from darkroom.dates import utc_now
from darkroom.domain import demo, index
from darkroom.domain.ingest import run as ingest_run
from darkroom.domain.ingest import unpack
from darkroom.server import auth, handler, jobs
from darkroom.storage import db
from darkroom.storage.models import Export, Person
from darkroom.storage.paths import NotInTheInbox, Photos

DEMO_SESSION_FILE = "demo-session.json"


def check_config() -> int:
    settings = config.load()
    found = config.problems(settings)
    try:
        exiftool.version()
    except (exiftool.Missing, OSError) as error:
        found.append(str(error))
    found += oidc.recheck_problems(settings, utc_now().timestamp())
    found += apns.key_problems(settings)
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


def unpack_only_export(opened: Session, photos: Photos, person: Person, export: Export, now: float) -> int:
    report = unpack.unpack(opened, photos, person, export, now)
    monitoring.log("unpacked", person=person.slug, export=export.id, parts=report.parts_unpacked, skipped=report.parts_skipped, files=report.files, problems=len(report.problems))
    for problem in report.problems:
        print(problem, file=sys.stderr)
    return 1 if report.problems else 0


def import_one(opened: Session, settings: config.Settings, export: Export, now: float) -> int:
    try:
        if export.status == "imported":
            ingest_run.request_import(opened, export.id, Photos(settings.photos_root), now)
            opened.commit()
        done = ingest_run.import_export(opened, settings, export, now)
    except ingest_run.AlreadyImported:
        print(f"Export {export.id} is already imported.")
        return 0
    except (ingest_run.Busy, ingest_run.NotReady) as error:
        print(f"export {export.id}: {error}", file=sys.stderr)
        return 1
    if done.status in ingest_run.WAITING:
        print(f"export {export.id}: the import did not fail; its last step could not be saved, so it will resume on the server's next import tick or when you run this command again", file=sys.stderr)
        return 1
    if done.status != "done":
        print(f"export {export.id}: {done.error or 'the import failed'}; see the events on the status page", file=sys.stderr)
        return 1
    print(ingest_run.summary_of(export, done))
    return 0


def run_import_takeout(slug: str | None, folder: str | None, unpack_only: bool, account: str | None = None) -> int:
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
                found = unpack.register_zips(opened, person, Path(folder), now, account) if folder else unpack.inspect_manual_inbox(opened, photos, person, now, account)
            except unpack.UnpackFailed as error:
                print(str(error), file=sys.stderr)
                found = []
                code = 1
            if not found and not code:
                print("no Takeout zip files found to import", file=sys.stderr)
                code = 1
            for export in found:
                code = max(code, unpack_only_export(opened, photos, person, export, now) if unpack_only else import_one(opened, settings, export, now))
    engine.dispose()
    return code


def revoke_sessions(email: str | None, sub: str | None, everyone: bool) -> int:
    if not (email or sub or everyone):
        print("revoke-sessions needs --email, --sub or --all", file=sys.stderr)
        return 1
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    with db.session(engine) as opened:
        ended = oidc.revoke_sessions(opened, email=email, sub=sub, everyone=everyone)
    engine.dispose()
    monitoring.log("sessions revoked", count=ended)
    print(f"ended {ended} session{'' if ended == 1 else 's'}")
    return 0


def demo_settings(folder: str) -> config.Settings:
    root = Path(folder)
    return dataclasses.replace(config.load(), data_dir=root / "data", photos_root=root / "photos", database_url="", demo=True)


def occupied(folder: Path) -> bool:
    return folder.exists() and (not folder.is_dir() or any(folder.iterdir()))


def write_demo_session(settings: config.Settings, token: str) -> Path:
    target = settings.data_dir / DEMO_SESSION_FILE
    body = json.dumps({"cookie": auth.SESSION_COOKIE, "token": token, "sub": demo.MEMBER.sub, "name": demo.MEMBER.name}).encode()
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "wb") as out:
        out.write(body)
    return target


def seed_demo(settings: config.Settings) -> tuple[demo.Summary, Path]:
    migrate(settings)
    engine = db.make_engine(db.database_url(settings))
    try:
        with db.session(engine) as opened:
            now = utc_now()
            summary = demo.seed(opened, Photos(settings.photos_root), settings, now.date())
            token = demo.sign_in(opened, settings, now.timestamp())
            return summary, write_demo_session(settings, token)
    finally:
        engine.dispose()


def unseed(settings: config.Settings) -> None:
    for root in (settings.photos_root, settings.data_dir):
        entries = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True) if root.is_dir() else []
        for path in entries:
            if path.is_dir():
                with contextlib.suppress(OSError):
                    path.rmdir()
            else:
                path.unlink(missing_ok=True)


def run_demo(folder: str | None) -> int:
    if not folder:
        print("demo needs --data DIR, an empty folder for its made-up data; it never uses the configured data folder or photo root", file=sys.stderr)
        return 1
    settings = demo_settings(folder)
    refused = config.demo_misused(settings)
    if refused:
        print(f"demo: {refused}", file=sys.stderr)
        return 1
    for where in (settings.data_dir, settings.photos_root):
        if occupied(where):
            print(f"demo: {where} already holds something; point --data at an empty folder", file=sys.stderr)
            return 1
    settings.photos_root.mkdir(parents=True, exist_ok=True)
    try:
        summary, session_file = seed_demo(settings)
    except (exiftool.Missing, OSError) as error:
        unseed(settings)
        print(f"demo: {error}; nothing was left under {folder}, so the same folder can be used again", file=sys.stderr)
        return 1
    print(f"Seeded {summary.people} made-up people with {summary.accounts} Google accounts, {summary.pixels} Pixels, {summary.devices} iPhones, "
          f"{summary.originals} originals ({summary.indexed} indexed, {summary.failed} failed) and {summary.alerts} open alert.")
    print(f"Serve it with: DARKROOM_DEMO=1 DARKROOM_DATA_DIR={settings.data_dir} DARKROOM_PHOTOS_ROOT={settings.photos_root} python run.py serve")
    print(f"The demo member's session cookie is in {session_file} (the browser driver of a later issue sets it for you); "
          f"or add DARKROOM_ALLOW_NO_AUTH=1 instead of DARKROOM_DEMO=1 to browse as the local owner.")
    return 1 if summary.failed else 0


def serve() -> int:
    settings = config.load()
    refused = config.refuses_to_start(settings)
    if refused:
        for line in refused:
            print(f"config: {line}", file=sys.stderr)
        return 1
    migrate(settings)
    try:
        Photos(settings.photos_root).share_inbox()
    except (OSError, NotInTheInbox) as error:
        monitoring.log("inbox folders not shared with the browser worker", error=type(error).__name__)
    engine = db.make_engine(db.database_url(settings))
    scheduler = jobs.build(settings, engine, utc_now().timestamp())
    scheduler.start(lambda: utc_now().timestamp())
    server = handler.make_server(settings, engine=engine)
    monitoring.log("serving", port=server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        scheduler.stop()
        server.server_close()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.py")
    parser.add_argument("command", choices=["serve", "check-config", "migrate", "jobs", "index", "import-takeout", "revoke-sessions", "demo"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--person")
    parser.add_argument("--dir")
    parser.add_argument("--data")
    parser.add_argument("--account")
    parser.add_argument("--unpack-only", action="store_true")
    parser.add_argument("--email")
    parser.add_argument("--sub")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return run_demo(args.data)
    if args.command == "revoke-sessions":
        return revoke_sessions(args.email, args.sub, args.all)
    if args.command == "index":
        return run_index(args.person)
    if args.command == "import-takeout":
        return run_import_takeout(args.person, args.dir, args.unpack_only, args.account)
    if args.command == "jobs":
        if not args.name:
            parser.error("jobs needs the name of a job")
        return run_job(args.name)
    if args.command == "migrate":
        return migrate()
    return serve() if args.command == "serve" else check_config()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
