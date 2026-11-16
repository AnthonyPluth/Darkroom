from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from xml.etree import ElementTree  # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml -- only Syncthing's own config.xml is parsed; Python's parser resolves no external entity

from darkroom_init.plan import SYNCTHING_SOCKET, Secret
from darkroom_init.sockets import Refused, clear_stale_socket, private_folder
from darkroom_init.supervisor import become_quietly, log, run_quietly

BINARY = "/usr/local/bin/syncthing"
SOCKET = SYNCTHING_SOCKET
CONFIG = "config.xml"
SOCKET_MODE = "0600"
LISTEN = ("tcp://0.0.0.0:22000", "quic://0.0.0.0:22000")
OFF = {"globalAnnounceEnabled": "false", "relaysEnabled": "false", "natEnabled": "false", "crashReportingEnabled": "false", "urAccepted": "-1",
       "autoUpgradeIntervalH": "0"}
FIRST_START = OFF | {"localAnnounceEnabled": "false", "startBrowser": "false", "setLowPriority": "false"}
GENERATE_SECONDS = 120.0
TIMEOUT = 5.0
Runner = Callable[[list[str], float], None]


def parsed(config: Path) -> ElementTree.ElementTree[ElementTree.Element]:
    return ElementTree.parse(config)  # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse -- Syncthing's own config.xml in appdata, written by Syncthing or this init; Python's parser resolves no external entity


def setting(parent: ElementTree.Element, name: str) -> ElementTree.Element:
    found = parent.find(name)
    return found if found is not None else ElementTree.SubElement(parent, name)


def sections(tree: ElementTree.ElementTree[ElementTree.Element]) -> tuple[ElementTree.Element, ElementTree.Element]:
    root = tree.getroot()
    options, gui = root.find("options"), root.find("gui")
    if options is None or gui is None:
        raise Refused("Syncthing's configuration has no options or GUI section")
    return options, gui


def write_first_start_options(config: Path, socket_path: Path) -> None:
    tree = parsed(config)
    options, gui = sections(tree)
    for name, value in FIRST_START.items():
        setting(options, name).text = value
    for listened in options.findall("listenAddress"):
        options.remove(listened)
    for address in LISTEN:
        ElementTree.SubElement(options, "listenAddress").text = address
    gui.set("tls", "false")
    setting(gui, "address").text = str(socket_path)
    setting(gui, "unixSocketPermissions").text = SOCKET_MODE
    tree.write(config, encoding="utf-8", xml_declaration=True)


def not_off(config: Path, socket_path: Path) -> list[str]:
    options, gui = sections(parsed(config))
    found = [name for name, value in OFF.items() if (options.findtext(name) or "").strip() != value]
    wanted = {"address": str(socket_path), "unixSocketPermissions": SOCKET_MODE}
    return found + [f"gui {name}" for name, value in wanted.items() if (gui.findtext(name) or "").strip() != value]


def generate(home: Path, socket_path: Path, binary: str, run: Runner) -> None:
    fresh = Path(tempfile.mkdtemp(prefix=".syncthing-", dir=home.parent))
    try:
        run([binary, "generate", f"--home={fresh}", "--no-port-probing"], GENERATE_SECONDS)
        write_first_start_options(fresh / CONFIG, socket_path)
        os.rename(fresh, home)
    except BaseException:
        shutil.rmtree(fresh, ignore_errors=True)
        raise


def prepare(home: Path, socket_path: Path = SOCKET, binary: str = BINARY, run: Runner = run_quietly) -> None:
    if not private_folder(socket_path.parent):
        raise Refused("the folder for Syncthing's API socket is not private to the container's user")
    if not (home / CONFIG).exists():
        if home.exists() and any(home.iterdir()):
            raise Refused("the Syncthing folder in appdata holds files but no configuration; move it aside to start with a fresh identity")
        generate(home, socket_path, binary, run)
        log("syncthing identity generated")
    left_on = not_off(home / CONFIG, socket_path)
    if left_on:
        raise Refused(f"Syncthing's configuration differs from what Darkroom keeps: {', '.join(left_on)}")
    clear_stale_socket(socket_path)


def serve_argv(home: Path, binary: str = BINARY) -> list[str]:
    return [binary, "serve", f"--home={home}", "--no-browser", "--no-upgrade", "--no-restart", "--paused"]


def usable_key(key: str) -> bool:
    return bool(key) and key.isascii() and key.isprintable() and " " not in key


def status(socket_path: Path, method: str, path: str, key: str, body: object | None = None, timeout: float = TIMEOUT) -> int:
    if not usable_key(key):
        raise Refused("an API key that cannot go in a header")
    payload = b"" if body is None else json.dumps(body).encode()
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost", f"X-API-Key: {key}", "Connection: close", f"Content-Length: {len(payload)}"]
    if body is not None:
        lines.append("Content-Type: application/json")
    head = b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(timeout)
        stream.connect(str(socket_path))
        stream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload)
        while b"\r\n" not in head and len(head) < 4096:
            more = stream.recv(4096)
            if not more:
                break
            head += more
    first = head.split(b"\r\n", 1)[0].split()
    if len(first) < 2 or not first[0].startswith(b"HTTP/") or not first[1].isdigit():
        raise Refused("Syncthing did not answer in HTTP")
    return int(first[1])


def persisted_key(home: Path) -> str:
    return (parsed(home / CONFIG).getroot().findtext("gui/apikey") or "").strip()


def rotate(home: Path, socket_path: Path, new: str) -> bool:
    old = persisted_key(home)
    if old != new and status(socket_path, "GET", "/rest/system/version", new) != 200 and status(socket_path, "PATCH", "/rest/config/gui", old, {"apiKey": new}) != 200:
        return False
    return status(socket_path, "GET", "/rest/system/version", new) == 200 and (old == new or status(socket_path, "GET", "/rest/system/version", old) == 403)


def key_rotated(home: Path, held: Mapping[Secret, str], socket_path: Path = SOCKET) -> Callable[[], bool]:
    def ready() -> bool:
        try:
            done = rotate(home, socket_path, held["syncthing"])
        except (OSError, ElementTree.ParseError, Refused):
            return False
        if done:
            log("syncthing started paused; its API key was rotated")
        return done
    return ready


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("/"):
        log("syncthing not started", reason="the init gives the wrapper one absolute home folder")
        return 2
    home = Path(argv[1])
    try:
        prepare(home)
    except Refused as refused:
        log("syncthing not started", reason=str(refused))
        return 1
    except (OSError, ElementTree.ParseError) as error:
        log("syncthing not started", reason="its home or first configuration could not be prepared", error=type(error).__name__)
        return 1
    become_quietly(serve_argv(home))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
