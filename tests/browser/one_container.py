import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SHAPE = re.compile(rb"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])|DARKROOM_BROWSER_TOKEN=|DARKROOM_VNC_SECRET=")


def processes():
    found = []
    for folder in Path("/proc").glob("[0-9]*"):
        try:
            state, ppid = (folder / "stat").read_text().rsplit(")", 1)[1].split()[:2]
            args = [part.decode() for part in (folder / "cmdline").read_bytes().split(b"\0") if part]
            found.append({"pid": int(folder.name), "ppid": int(ppid), "state": state, "comm": (folder / "comm").read_text().strip(), "args": args})
        except (OSError, IndexError, ValueError):
            continue
    return [process for process in found if process["state"] != "Z"]


def scan():
    readable, found = 0, []
    for process in processes():
        for part in ("environ", "cmdline"):
            try:
                data = Path(f"/proc/{process['pid']}/{part}").read_bytes()
            except OSError:
                continue
            readable += 1
            if process["pid"] != os.getpid() and SHAPE.search(data.replace(b"\0", b"\n")):
                found.append({"pid": process["pid"], "comm": process["comm"], "where": part})
    return readable, found


def scan_with_control():
    readable, found = scan()
    with subprocess.Popen(["sleep", "1"], env={"CANARY": secrets.token_urlsafe(32)}) as planted:
        time.sleep(0.2)
        caught = [hit for hit in scan()[1] if hit["pid"] == planted.pid]
    print(json.dumps({"readable": readable, "found": found, "control_caught": caught}))
    return 0 if not found and caught and readable > 10 else 1


def under(pid, ancestors, parents):
    while pid in parents and pid not in ancestors:
        pid = parents[pid]
    return pid in ancestors


def pids(every=None):
    every = processes() if every is None else every
    parents = {p["pid"]: p["ppid"] for p in every}
    workers, browsers = [p["pid"] for p in every if "darkroom_browser" in p["args"]], [p["pid"] for p in every if p["comm"] == "chrome"]
    return {"workers": workers, "browsers": browsers, "strays": [pid for pid in browsers if not under(pid, workers, parents)]}


def kill_worker_under_browser():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        seen = pids()
        if seen["browsers"] and len(seen["workers"]) == 1:
            os.kill(seen["workers"][0], signal.SIGKILL)
            print(json.dumps({"worker": seen["workers"][0], "browsers": seen["browsers"], "at": time.time()}))
            return 0
        time.sleep(0.05)
    return 1


def viewer():
    args = next(p["args"] for p in processes() if any(part.endswith("websockify") for part in p["args"][:2]))
    status, source = 200, args[args.index("--auth-source") + 1]
    try:
        urllib.request.urlopen("http://127.0.0.1:6080/vnc.html", timeout=5).close()
    except urllib.error.HTTPError as error:
        status = error.code
    print(json.dumps({"auth_source": source, "status": status}))
    return 0 if source.isdigit() and status == 401 and "127.0.0.1:6080" in args else 1


def database(work):
    from darkroom import config
    from darkroom.storage import db
    engine = db.make_engine(db.database_url(config.load()))
    with db.session(engine, writing=True) as opened:
        print(work(opened))
    engine.dispose()
    return 0


def enqueue(opened, kind):
    from darkroom.domain import browser_jobs
    from darkroom.storage.models import GoogleAccount, Person
    account = opened.query(GoogleAccount).first()
    if account is None:
        person = Person(name="Jo", slug="jo", originals_path="jo/originals", google_only_path="jo/google-only", created=1.0)
        opened.add(person); opened.flush()
        account = GoogleAccount(email="jo.takeout@example.test", default_person_id=person.id, created=1.0)
        opened.add(account); opened.flush()
    return browser_jobs.queue(opened, account.id, kind, None, time.time()).id


def main(argv):
    from darkroom.storage.models import BrowserJob
    commands = {"scan": scan_with_control, "viewer": viewer, "kill-worker": kill_worker_under_browser,
                "enqueue": lambda: database(lambda opened: enqueue(opened, argv[1])),
                "status": lambda: database(lambda opened: opened.get(BrowserJob, int(argv[1])).status),
                "claimed-since": lambda: database(lambda opened: opened.query(BrowserJob).filter(BrowserJob.started >= float(argv[1])).count())}
    if argv:
        return commands[argv[0]]()
    print(json.dumps(pids()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
