from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from darkroom import config, monitoring, oidc
from darkroom.providers import exiftool, immich, resend
from darkroom.dates import utc_now
from darkroom.domain import demo, immich_reconcile, index, legacy, share_bind, share_check
from darkroom.domain.ingest import run as ingest_run
from darkroom.domain.ingest import unpack
from darkroom.server import auth, handler, jobs, wizard
from darkroom.storage import browser_copy, db, secret_key, secretbox, share, stored_settings
from darkroom.storage.settings_keys import SHARE_SENTINEL
from darkroom.storage.models import Export, Person
from darkroom.storage.paths import PIXELS, NotInTheInbox, Photos

DEMO_SESSION_FILE = "demo-session.json"
PIXELS_NOTE = f"{PIXELS}/"


def check_config() -> int:
    loaded = stored_settings.for_check(config.load())
    settings = loaded.settings
    found = [*loaded.problems, *config.problems(settings)]
    try:
        exiftool.version()
    except (exiftool.Missing, OSError) as error:
        found.append(str(error))
    found += oidc.recheck_problems(settings, utc_now().timestamp())
    found += resend.problems(settings)
    missing_key = secret_key.missing_key_problem(settings)
    if missing_key:
        found.append(missing_key)
    for line in found:
        print(f"config: {line}", file=sys.stderr)
    if config.ignored_alert_variables():
        print(f"config: warning: {config.IGNORED_VARIABLES_NOTE}", file=sys.stderr)
    unbound = share_warning(loaded)
    if unbound:
        print(f"config: warning: {unbound}", file=sys.stderr)
    print(alert_channel(settings))
    print(syncthing_line(settings))
    print(key_origin_line(settings, missing_key))
    for line in source_lines(loaded):
        print(line)
    if not found:
        print("Configuration looks complete.")
    return 1 if found else 0


SHARE_WARNING = "the working share is not bound ({problem}), so Darkroom places and prunes no Pixel copies; run `python run.py bind-share` once in the container (docs/deploy/share.md)"


def share_warning(loaded: stored_settings.Effective) -> str | None:
    if stored_settings.NOT_READ in loaded.problems or config.demo_allowed(loaded.settings):
        return None
    problem = share_check.sentinel_problem(Photos(loaded.settings.photos_root).root, loaded.recorded.get(SHARE_SENTINEL))
    return None if problem is None else SHARE_WARNING.format(problem=problem)


def alert_channel(settings: config.Settings) -> str:
    if not config.resend_configured(settings):
        return "Alerts: shown on the Status page only (set RESEND_API_KEY, ALERT_EMAIL_FROM and ALERT_EMAIL_TO to email them to the owner)."
    if resend.of(settings) is None:
        return "Alerts: shown on the Status page only, because the Resend settings above need fixing."
    return "Alerts: emailed to the owner through Resend, from the sender address you set."


SYNCTHING_OFF = "Syncthing: off (DARKROOM_SYNCTHING=1 in the one container starts Darkroom's own Syncthing, paused)."
SYNCTHING_ON = "Syncthing: on; the one container's init starts it paused with a fresh API key, and nothing syncs until a later release resumes it."


def syncthing_line(settings: config.Settings) -> str:
    return SYNCTHING_ON if settings.syncthing and settings.one_container else SYNCTHING_OFF


KEY_ORIGIN_LINES = {
    "environment": "Secret key: from DARKROOM_SECRET_KEY.",
    "file": "Secret key: from the secret-key file in DARKROOM_DATA_DIR.",
    "none": "Secret key: none yet; `python run.py serve` generates one in DARKROOM_DATA_DIR on first start (or set DARKROOM_SECRET_KEY).",
}
NO_KEY_TO_GENERATE = "Secret key: none, and `python run.py serve` will not generate one until the problem above is fixed."


def key_origin_line(settings: config.Settings, missing_key: str | None) -> str:
    return NO_KEY_TO_GENERATE if missing_key else KEY_ORIGIN_LINES[settings.key_origin]


def source_lines(loaded: stored_settings.Effective) -> list[str]:
    return [SOURCES_HEADING, *(f"  {variable}: {source}" for variable, source in loaded.sources.items())]


SOURCES_HEADING = "Where each setting comes from (the environment, then a stored value, then the default; secret values are never shown):"


def seal_secret() -> int:
    settings = config.load()
    if not settings.secret_key:
        print("seal-secret needs DARKROOM_SECRET_KEY or the secret-key file the service generates in DARKROOM_DATA_DIR on first start, the same key the service runs with", file=sys.stderr)
        return 1
    plain = sys.stdin.readline().strip()
    if not plain:
        print("seal-secret reads the value to seal from standard input, for example: printf %s \"$TOPIC\" | python run.py seal-secret", file=sys.stderr)
        return 1
    print(secretbox.seal(plain, settings.secret_key))
    return 0


def copy_browser_state(source: str | None, target: str | None, set_aside: bool) -> int:
    if not source or not target:
        print("copy-browser-state needs --source and --target, for example: python run.py copy-browser-state --source /photos/_browser --target /data/browser", file=sys.stderr)
        return 1
    found = browser_copy.leftovers(Path(target))
    if found:
        print(f"copy-browser-state: {len(found)} unfinished earlier copy folder(s) next to the target, which you may remove by hand: {', '.join(str(each) for each in found)}", file=sys.stderr)
    try:
        copied = browser_copy.copy_browser_state(Path(source), Path(target), utc_now().strftime("%Y%m%dT%H%M%SZ"), config.load().photos_root, set_aside)
    except OSError as error:
        print(f"copy-browser-state: {error}. The source was not changed; an unfinished copy folder next to the target, if any, can be removed by hand.", file=sys.stderr)
        return 1
    print(f"Copied and verified {copied.files} files in {copied.folders} folders into {target}; left out {copied.locks_left_out} browser lock links. The source was not changed.")
    if copied.set_aside is not None:
        print(f"The folder that was at {target} is now {copied.set_aside}.")
    return 0


def migrate(settings: config.Settings | None = None) -> int:
    settings = settings or config.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = db.make_engine(db.database_url(settings))
    db.migrate(engine)
    engine.dispose()
    monitoring.log("migrated")
    return 0


def with_stored(settings: config.Settings, engine: Engine) -> config.Settings | None:
    loaded = stored_settings.effective(settings, engine)
    for line in loaded.problems:
        print(f"config: {line}", file=sys.stderr)
    return None if loaded.problems else loaded.settings


def run_job(name: str) -> int:
    from_environment = config.load()
    engine = db.make_engine(db.database_url(from_environment))
    settings = with_stored(from_environment, engine)
    if settings is None:
        engine.dispose()
        return 1
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
    from_environment = config.load()
    engine = db.make_engine(db.database_url(from_environment))
    settings = with_stored(from_environment, engine)
    if settings is None:
        engine.dispose()
        return 1
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
                skipped = unpack.skipped_zips(Path(folder) if folder else photos.manual_inbox(person.slug))
                print(f"{skipped} zip files were skipped because they are links or hard links; copy them in as plain files" if skipped
                      else "no Takeout zip files found to import", file=sys.stderr)
                code = 1
            for export in found:
                code = max(code, unpack_only_export(opened, photos, person, export, now) if unpack_only else import_one(opened, settings, export, now))
    engine.dispose()
    return code


def run_import_legacy(slug: str | None, folder: str | None) -> int:
    if not slug:
        print("import-legacy needs --person", file=sys.stderr)
        return 1
    from_environment = config.load()
    engine = db.make_engine(db.database_url(from_environment))
    settings = with_stored(from_environment, engine)
    if settings is None:
        engine.dispose()
        return 1
    code = 0
    with db.session(engine) as opened:
        person = opened.query(Person).filter(Person.slug == slug).one_or_none()
        if person is None:
            print(f"no person called {slug}", file=sys.stderr)
            code = 1
        else:
            now = utc_now().timestamp()
            try:
                if folder:
                    legacy.set_source(opened, Photos(settings.photos_root), person, folder, now)
                    opened.commit()
                done = legacy.import_person(opened, settings, person, now, queue=legacy.PixelFolderQueue())
            except (legacy.BadSource, legacy.NoSource, legacy.NotReady, legacy.Busy) as error:
                print(f"import-legacy: {error}", file=sys.stderr)
                code = 1
            else:
                print(f"{slug}: {done.new} new files, {done.matched} matched in Google, {done.unmatched} not in Google "
                      f"({done.copied} copied to originals, {done.already_there} already there), {done.already_seen} seen before, {done.ignored} not photos or videos, {done.failed} could not be read")
                code = 1 if done.failed else 0
    engine.dispose()
    return code


def run_reconcile_immich(slug: str | None) -> int:
    from_environment = config.load()
    engine = db.make_engine(db.database_url(from_environment))
    settings = with_stored(from_environment, engine)
    if settings is None:
        engine.dispose()
        return 1
    code = 0
    with db.session(engine) as opened:
        people = opened.query(Person).order_by(Person.id)
        found = people.filter(Person.slug == slug).all() if slug else people.all()
        if slug and not found:
            print(f"no person called {slug}", file=sys.stderr)
            code = 1
        for person in found:
            try:
                done = immich_reconcile.reconcile_person(opened, settings, person, utc_now().timestamp(), queue=immich_reconcile.PixelFolderQueue())
            except (immich_reconcile.NotReady, immich_reconcile.Busy, immich.ImmichFailed) as error:
                print(f"reconcile-immich: {person.slug}: {error}", file=sys.stderr)
                code = 1
            else:
                print(f"{person.slug}: {done.listed} listed, {done.stored} stored ({done.copied} copied to originals, {done.already_there} already there), "
                      f"{done.queued} queued for the Pixel folder, {done.ignored} not phone uploads, {done.waiting} waiting, {done.failed} failed")
                code = 1 if done.failed else code
    engine.dispose()
    return code


def legacy_report() -> int:
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    with db.session(engine) as opened:
        for entry in legacy.totals(opened):
            print(legacy.describe(entry))
    engine.dispose()
    return 0


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


SYNCTHING_PAUSED = "paused"
CONFIRM_BIND = "bind"
NOT_SET_UP = "bind-share: the database has no settings table yet; start the service once (`python run.py serve` migrates it), then run this again"
SYNCTHING_PROMPT = (f"Syncthing is configured by hand, outside Darkroom. Pause it for every folder under {PIXELS_NOTE} on this server now: an empty replacement would otherwise reach each Pixel as deletions. "
                    f"Type {SYNCTHING_PAUSED} once it is paused: ")
BIND_BUSY = "bind-share: another bind-share is running in this container; nothing was written. Let it finish, then run this again if it failed"
BIND_PROMPT = f"Type {CONFIRM_BIND} to write a new sentinel on this share and record it: "
RESUME_NOTE = (f"Bound. Placing and pruning resume at the next hourly tend (or now with `python run.py jobs pixels.tend`); the placed-copy check first writes back every placed copy that is missing. "
               f"Resume the outside Syncthing for {PIXELS_NOTE} only when Darkroom reports the refill complete: the `pixel folders tended` log line shows held=0.")


def asked(prompt: str, expected: str) -> bool:
    print(prompt, end="", flush=True)
    return sys.stdin.readline().strip() == expected


def describe_share(found: share_bind.Found) -> list[str]:
    return [
        f"Share root: {found.root}",
        f"Mounted from: {found.mount.source} ({found.mount.kind}), separate from the container's own filesystem",
        f"Entries at the root: {found.entries}; files under {PIXELS_NOTE}: {found.pixel_files}",
        f"Pixel copies recorded as placed: {found.placed} (any that are missing are written again before the folders are tended)",
        "No sentinel is on this share yet.",
    ]


def bind_locked(settings: config.Settings, engine: Engine, wired: bool, held: share.BindLock) -> int:
    with db.session(engine) as opened:
        try:
            found = share_bind.survey(opened, Photos(settings.photos_root).root, wired, held)
        except share_bind.Refused as refused:
            print(f"bind-share: {refused}", file=sys.stderr)
            return 1
        for line in describe_share(found):
            print(line)
        if not asked(SYNCTHING_PROMPT, SYNCTHING_PAUSED):
            print("bind-share: the outside Syncthing was not confirmed paused; nothing was written", file=sys.stderr)
            return 1
        if not asked(BIND_PROMPT, CONFIRM_BIND):
            print("bind-share: not confirmed; nothing was written", file=sys.stderr)
            return 1
        try:
            share_bind.bind_share(opened, found, utc_now().timestamp(), held)
        except share_bind.Refused as refused:
            print(f"bind-share: {refused}", file=sys.stderr)
            return 1
        except OSError as error:
            print(f"bind-share: the sentinel could not be written ({type(error).__name__}); placing and pruning stay stopped, and the command can be run again", file=sys.stderr)
            return 1
    monitoring.log("share bound")
    print(RESUME_NOTE)
    return 0


def bind_share() -> int:
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    try:
        if not stored_settings.has_table(engine):
            print(NOT_SET_UP, file=sys.stderr)
            return 1
        wired = jobs.tend_checks_placed_copies(jobs.build(settings, engine))
        with contextlib.ExitStack() as stack:
            try:
                held = stack.enter_context(share.bind_lock(settings.data_dir))
            except share.BindBusy:
                print(BIND_BUSY, file=sys.stderr)
                return 1
            except OSError as error:
                print(f"bind-share: the lock in DARKROOM_DATA_DIR could not be taken ({type(error).__name__}); nothing was written", file=sys.stderr)
                return 1
            return bind_locked(settings, engine, wired, held)
    finally:
        engine.dispose()


REOPENED = "the setup wizard is reopened: the next visitor from a private address may claim it, unless someone has already signed in to this Darkroom"
CLOSED_AFTER_TEST = "the setup wizard is closed for good: its test sign-in had passed, so nothing was reopened; finish the rest on Darkroom's own pages"


def reopen_setup() -> int:
    engine = db.make_engine(db.database_url(config.load()))
    with db.session(engine, writing=True) as opened:
        reopened = wizard.reopen(opened)
    engine.dispose()
    monitoring.log("setup wizard reopened" if reopened else "setup wizard closed after its test sign-in; nothing reopened")
    print(REOPENED if reopened else CLOSED_AFTER_TEST)
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
    print(f"Seeded {summary.people} made-up people with {summary.accounts} Google accounts, {summary.pixels} Pixels, "
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
    try:
        settings = secret_key.ensure(settings, engine)
    except secret_key.Refused as refused:
        engine.dispose()
        print(f"config: {refused}", file=sys.stderr)
        return 1
    loaded = stored_settings.effective(settings, engine)
    settings = loaded.settings
    unusable = [*loaded.problems, *config.refuses_to_start(settings)]
    if unusable:
        engine.dispose()
        for line in unusable:
            print(f"config: {line}", file=sys.stderr)
        return 1
    scheduler = jobs.for_serving(settings, engine, utc_now().timestamp())
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
    parser.add_argument("command", choices=["serve", "check-config", "migrate", "jobs", "index", "import-takeout", "import-legacy", "reconcile-immich", "legacy-report", "revoke-sessions", "demo", "seal-secret", "bind-share", "reopen-setup",
                                            "copy-browser-state"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--person")
    parser.add_argument("--dir")
    parser.add_argument("--data")
    parser.add_argument("--account")
    parser.add_argument("--unpack-only", action="store_true")
    parser.add_argument("--email")
    parser.add_argument("--sub")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--target")
    parser.add_argument("--set-aside", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return run_demo(args.data)
    if args.command == "seal-secret":
        return seal_secret()
    if args.command == "bind-share":
        return bind_share()
    if args.command == "reopen-setup":
        return reopen_setup()
    if args.command == "copy-browser-state":
        return copy_browser_state(args.source, args.target, args.set_aside)
    if args.command == "revoke-sessions":
        return revoke_sessions(args.email, args.sub, args.all)
    if args.command == "index":
        return run_index(args.person)
    if args.command == "import-takeout":
        return run_import_takeout(args.person, args.dir, args.unpack_only, args.account)
    if args.command == "import-legacy":
        return run_import_legacy(args.person, args.dir)
    if args.command == "reconcile-immich":
        return run_reconcile_immich(args.person)
    if args.command == "legacy-report":
        return legacy_report()
    if args.command == "jobs":
        if not args.name:
            parser.error("jobs needs the name of a job")
        return run_job(args.name)
    if args.command == "migrate":
        return migrate()
    return serve() if args.command == "serve" else check_config()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
