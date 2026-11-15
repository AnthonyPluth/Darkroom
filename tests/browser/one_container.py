import base64
import json
import os
import re
import secrets
import signal
import socket
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


def relay():
    from darkroom import config, oidc
    from darkroom.storage import db
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    with db.session(engine, writing=True) as opened:
        oidc.remember_user(opened, oidc.Identity("relay-probe", "probe@example.test", "Probe", ("parents",), True), time.time())
        session = oidc.new_session(opened, settings, "relay-probe", time.time())
    engine.dispose()
    cookie = f"darkroom_session={session}"
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/viewer/vnc.html", headers={"Cookie": cookie}), timeout=10) as page:
        status, policy = page.status, page.headers.get("Content-Security-Policy", "")
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    with socket.create_connection(("127.0.0.1", 8000), timeout=10) as stream:
        stream.sendall((f"GET /viewer/websockify HTTP/1.1\r\nHost: 127.0.0.1:8000\r\nOrigin: http://127.0.0.1:8000\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nCookie: {cookie}\r\n\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply or len(reply.split(b"\r\n\r\n", 1)[1]) < 14:
            chunk = stream.recv(4096)
            if not chunk:
                break
            reply += chunk
    head, _, frames = reply.partition(b"\r\n\r\n")
    found = {"page": status, "framed_by_self": "frame-ancestors 'self'" in policy, "upgrade": head.split(b"\r\n")[0].decode(), "first_frame": frames[2:14].decode("latin-1")}
    print(json.dumps(found))
    return 0 if status == 200 and found["framed_by_self"] and found["upgrade"].startswith("HTTP/1.1 101") and found["first_frame"].startswith("RFB 003.") else 1


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
    commands = {"scan": scan_with_control, "viewer": viewer, "relay": relay, "kill-worker": kill_worker_under_browser,
                "enqueue": lambda: database(lambda opened: enqueue(opened, argv[1])),
                "status": lambda: database(lambda opened: opened.get(BrowserJob, int(argv[1])).status),
                "claimed-since": lambda: database(lambda opened: opened.query(BrowserJob).filter(BrowserJob.started >= float(argv[1])).count())}
    if argv:
        return commands[argv[0]]()
    print(json.dumps(pids()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
