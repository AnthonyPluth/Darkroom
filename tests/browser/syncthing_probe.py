import http.client
import json
import os
import signal
import socket
import stat
import sys
import time
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree

SOCKET = "/run/darkroom/syncthing.sock"
CONFIG = "/data/syncthing/config.xml"
KEPT_OFF = ("globalAnnounceEnabled", "localAnnounceEnabled", "relaysEnabled", "natEnabled", "crashReportingEnabled", "urAccepted", "autoUpgradeIntervalH", "setLowPriority")


class OnSocket(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(SOCKET)


def key():
    return ElementTree.parse(CONFIG).getroot().findtext("gui/apikey")


def answer(method, path, body=None):
    connection = OnSocket()
    connection.request(method, path, body=None if body is None else json.dumps(body), headers={"X-API-Key": key(), "Content-Type": "application/json"})
    reply = connection.getresponse()
    data = reply.read()
    connection.close()
    return reply.status, data


def call(method, path, body=None):
    status, data = answer(method, path, body)
    if status != 200:
        raise SystemExit(f"{method} {path} answered {status}")
    return json.loads(data) if data.strip() else None


def listening():
    ports = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in Path(table).read_text().splitlines()[1:]:
            parts = row.split()
            if parts[3] == "0A":
                ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    return sorted(ports)


def facts():
    version = call("GET", "/rest/system/version")
    config = call("GET", "/rest/config")
    options = config["options"]
    return {
        "version": version["version"], "release": version["isRelease"],
        "devices": {device["deviceID"]: device["paused"] for device in config["devices"]},
        "folders": {folder["id"]: folder["paused"] for folder in config["folders"]},
        "options": {name: options[name] for name in KEPT_OFF},
        "socket_mode": oct(stat.S_IMODE(os.lstat(SOCKET).st_mode)), "socket_folder_mode": oct(stat.S_IMODE(os.lstat(os.path.dirname(SOCKET)).st_mode)),
        "listening": listening(),
    }


def add(device, address, folder, path, kind):
    call("POST", "/rest/config/devices", {"deviceID": device, "name": "ci-peer", "addresses": [address], "paused": False})
    call("POST", "/rest/config/folders", {"id": folder, "path": path, "type": kind, "paused": False, "rescanIntervalS": 5, "fsWatcherEnabled": True,
                                          "devices": [{"deviceID": device}]})


def resume(device, folder):
    call("PATCH", f"/rest/config/devices/{device}", {"paused": False})
    call("PATCH", f"/rest/config/folders/{folder}", {"paused": False})


def connected(device):
    return call("GET", "/rest/system/connections")["connections"].get(device, {}).get("connected") is True


def query(**values):
    return urllib.parse.urlencode(values)


def folder(identity):
    found = [each for each in call("GET", "/rest/config")["folders"] if each["id"] == identity]
    if not found:
        return None
    return {"type": found[0]["type"], "paused": found[0]["paused"], "path": found[0]["path"], "devices": sorted(device["deviceID"] for device in found[0]["devices"])}


def need(identity):
    status = call("GET", f"/rest/db/status?{query(folder=identity)}")
    return status["needFiles"] + status["needDeletes"]


def knows(identity, name):
    status, data = answer("GET", f"/rest/db/file?{query(folder=identity, file=name)}")
    return status == 200 and json.loads(data).get("global") is not None


def scan(identity):
    call("POST", f"/rest/db/scan?{query(folder=identity)}")


def monitor():
    found = {}
    for folder in Path("/proc").glob("[0-9]*"):
        try:
            if (folder / "comm").read_text().strip() == "syncthing":
                found[int(folder.name)] = int((folder / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError):
            continue
    return [pid for pid, parent in found.items() if parent not in found]


def restart():
    before = monitor()
    if len(before) != 1:
        raise SystemExit(f"expected one Syncthing monitor, found {len(before)}")
    os.kill(before[0], signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(1)
        now = monitor()
        if now and now != before:
            try:
                call("GET", "/rest/system/version")
                return
            except (OSError, SystemExit):
                continue
    raise SystemExit("the init did not restart Syncthing")


def main(command, *args):
    if command == "id":
        print(call("GET", "/rest/system/status")["myID"])
    elif command == "facts":
        print(json.dumps(facts(), sort_keys=True))
    elif command == "add":
        add(*args)
    elif command == "resume":
        resume(*args)
    elif command == "connected":
        return 0 if connected(args[0]) else 1
    elif command == "folder":
        print(json.dumps(folder(args[0]), sort_keys=True))
    elif command == "need":
        print(need(args[0]))
    elif command == "knows":
        return 0 if knows(*args) else 1
    elif command == "scan":
        scan(args[0])
    elif command == "restart":
        restart()
    elif command == "key-in-stdin":
        print("present" if key() in sys.stdin.read() else "absent")
    else:
        raise SystemExit(f"unknown command {command}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
