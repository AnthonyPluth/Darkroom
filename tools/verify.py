from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN_NOW = "2026-06-15 12:00:00"
FROZEN_NOW_ISO = "2026-06-15T12:00:00Z"
FAKETIME_LIBRARIES = (
    "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1",
    "/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1",
    "/usr/lib/faketime/libfaketime.so.1",
    "/usr/local/lib/faketime/libfaketime.so.1",
)
FAKETIME_INSTALL = "sudo apt-get install libfaketime"
KEPT_ENV = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "LANG", "LANGUAGE", "TZDIR", "VIRTUAL_ENV", "LD_LIBRARY_PATH",
                      "NODE_PATH", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "CI"})
KEPT_ENV_PREFIXES = ("LC_", "PYTHON", "PLAYWRIGHT_", "XDG_", "FONTCONFIG_")
DEMO_SECRET = "demo-secret-key-not-real"
NO_OUTSIDE = "http://127.0.0.1:9"
LOOPBACK_ONLY = "127.0.0.1,localhost"
DEMO_SESSION_FILE = "demo-session.json"
BUSY = 1.5
PR_SET_PDEATHSIG = 1


class Instance(NamedTuple):
    root: str
    url: str
    server: subprocess.Popen[bytes]
    log: str


def data_dir(root: str) -> str:
    return os.path.join(root, "data")


def photos_root(root: str) -> str:
    return os.path.join(root, "photos")


def clean_env(root: str, base: dict[str, str] | None = None, port: int | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if base is None else base).items() if k in KEPT_ENV or k.startswith(KEPT_ENV_PREFIXES)}
    public = f"http://127.0.0.1:{port}" if port else "http://127.0.0.1"
    env.update(DARKROOM_DEMO="1", DARKROOM_HOST="127.0.0.1", DARKROOM_DATA_DIR=data_dir(root), DARKROOM_PHOTOS_ROOT=photos_root(root),
               DARKROOM_PUBLIC_URL=public, DARKROOM_SECRET_KEY=DEMO_SECRET, PYTHONUNBUFFERED="1",
               HTTP_PROXY=NO_OUTSIDE, HTTPS_PROXY=NO_OUTSIDE, http_proxy=NO_OUTSIDE, https_proxy=NO_OUTSIDE, NO_PROXY=LOOPBACK_ONLY, no_proxy=LOOPBACK_ONLY)
    if port:
        env["DARKROOM_PORT"] = str(port)
    return env


def find_faketime(candidates: tuple[str, ...] = FAKETIME_LIBRARIES) -> str | None:
    return next((c for c in candidates if os.path.isfile(c)), None)


def frozen_env(env: dict[str, str], library: str) -> dict[str, str]:
    return {**env, "LD_PRELOAD": library, "FAKETIME": FROZEN_NOW, "FAKETIME_DONT_FAKE_MONOTONIC": "1", "TZ": "UTC"}


def missing_faketime_message() -> str:
    return (f"verify: libfaketime isn't installed, and verify needs it to freeze the clock so a screenshot changes only when the UI or its data does. "
            f"Install it with: {FAKETIME_INSTALL} (Debian and Ubuntu; verify runs on Linux, as macOS doesn't support LD_PRELOAD)")


def demo_command(root: str) -> list[str]:
    return [sys.executable, os.path.join(ROOT, "run.py"), "demo", "--data", root]


def server_command() -> list[str]:
    return [sys.executable, os.path.join(ROOT, "run.py"), "serve"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url: str, server: subprocess.Popen[bytes], timeout: float = 60) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if server.poll() is not None:
            raise RuntimeError(f"the server exited with code {server.returncode} before it was ready")
        try:
            with opener.open(url + "/healthz", timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"the server wasn't answering at {url} after {timeout:.0f} seconds")


def log_tail(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()[-2000:]
    except OSError:
        return ""


def seed_demo(root: str, library: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(demo_command(root), env=frozen_env(clean_env(root), library), cwd=ROOT, capture_output=True, text=True)


def die_with_parent(parent: int | None = None, sig: int = signal.SIGTERM) -> None:
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, int(sig))
    if parent is not None and os.getppid() != parent:
        os.kill(os.getpid(), sig)


def seed(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python tools/verify.py seed")
    p.add_argument("--data", required=True)
    a = p.parse_args(argv)
    library = find_faketime()
    if not library:
        print(missing_faketime_message(), file=sys.stderr)
        return 2
    try:
        os.makedirs(a.data)
    except OSError as e:
        print(f"verify: couldn't make {a.data} for the demo data: {e.strerror or e}", file=sys.stderr)
        return 2
    done = seed_demo(a.data, library)
    if done.returncode:
        shutil.rmtree(a.data, ignore_errors=True)
        print(f"verify: couldn't fill the demo database:\n{done.stdout}{done.stderr}", file=sys.stderr)
        return 2
    print(f"verify: demo data seeded in {a.data}; the demo member's session is in {os.path.join(data_dir(a.data), DEMO_SESSION_FILE)}")
    return 0


def serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python tools/verify.py serve")
    p.add_argument("--parent", type=int, required=True)
    p.add_argument("--seed", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args(argv)
    die_with_parent(a.parent)
    library = find_faketime()
    if not library:
        print(missing_faketime_message(), file=sys.stderr)
        return 2
    made = not os.path.lexists(a.data)
    try:
        shutil.copytree(a.seed, a.data)
    except OSError as e:
        if made:
            shutil.rmtree(a.data, ignore_errors=True)
        print(f"verify: couldn't copy the demo data into {a.data}: {e.strerror or e}", file=sys.stderr)
        return 2
    log = os.fdopen(os.open(os.path.join(a.data, "server.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
    server = subprocess.Popen(server_command(), cwd=ROOT, env=frozen_env(clean_env(a.data, port=a.port), library), stdout=log, stderr=subprocess.STDOUT)

    def ended(signum: int, _frame: object) -> None:
        server.terminate()
    signal.signal(signal.SIGTERM, ended)
    signal.signal(signal.SIGINT, ended)
    try:
        return server.wait()
    finally:
        log.close()
        if made:
            shutil.rmtree(a.data, ignore_errors=True)


def cpu_count() -> int:
    return os.process_cpu_count() or 1


def busy_now() -> float:
    return os.getloadavg()[0] / cpu_count()


def status() -> dict[str, Any]:
    ratio = busy_now()
    return {"libfaketime": find_faketime(), "libfaketime_install": FAKETIME_INSTALL, "exiftool": shutil.which("exiftool"),
            "frozen_now": FROZEN_NOW, "frozen_now_iso": FROZEN_NOW_ISO, "cpus": cpu_count(), "busy_per_cpu": round(ratio, 2), "busy_limit": BUSY,
            "busy": ratio > BUSY}


def main(argv: list[str]) -> int:
    if argv[:1] == ["serve"]:
        return serve(argv[1:])
    if argv[:1] == ["seed"]:
        return seed(argv[1:])
    if argv[:1] == ["status"]:
        print(json.dumps(status()))
        return 0
    print("usage: python tools/verify.py seed --data D, serve --parent PID --seed S --data D --port P, or status (frontend/verify/control.mjs runs those)",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
