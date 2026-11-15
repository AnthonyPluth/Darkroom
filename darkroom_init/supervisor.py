from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from darkroom_init.plan import PIPE, Secret, Spec

PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
BASE_SECONDS = 1.0
CAP_SECONDS = 60.0
STABLE_SECONDS = 60.0
STOP_SECONDS = 5.0
READY_SECONDS = 60.0
KILL_SECONDS = 1.0
TICK_SECONDS = 0.2
LISTEN = "0A"


def log(event: str, **fields: str | int | float) -> None:
    print(json.dumps({"event": event} | fields, sort_keys=True), file=sys.stderr, flush=True)


def backoff(failures: int, base: float = BASE_SECONDS, cap: float = CAP_SECONDS) -> float:
    return min(cap, base * float(2 ** min(failures, 32)))


def prctl(option: int, value: int) -> bool:
    return bool(ctypes.CDLL(None, use_errno=True).prctl(option, value, 0, 0, 0) == 0)


def processes(proc: Path = Path("/proc")) -> list[tuple[int, str, int, int]]:
    found = []
    for stat in proc.glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            found.append((int(stat.parent.name), fields[0], int(fields[1]), int(fields[2])))
        except (OSError, IndexError, ValueError):
            continue
    return found


def adopted_by(parent: int, proc: Path = Path("/proc")) -> list[int]:
    return [pid for pid, _, ppid, _ in processes(proc) if ppid == parent]


def group_alive(pgid: int, proc: Path = Path("/proc")) -> bool:
    return any(group == pgid and state != "Z" for _, state, _, group in processes(proc))


def listening(port: int, proc: Path = Path("/proc")) -> bool:
    wanted = f":{port:04X}"
    for table in ("net/tcp", "net/tcp6"):
        with contextlib.suppress(OSError):
            if any(parts[1].endswith(wanted) and parts[3] == LISTEN for parts in (row.split() for row in (proc / table).read_text().splitlines()[1:])):
                return True
    return False


def signal_group(pgid: int, signum: int) -> bool:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return False
    return True


@dataclass
class Child:
    spec: Spec
    process: subprocess.Popen[bytes] | None = None
    started: float = 0.0
    failures: int = 0
    due: float = 0.0


class Supervisor:
    def __init__(self, specs: list[Spec], secrets: Mapping[Secret, str], adopt_orphans: bool = False, base: float = BASE_SECONDS, cap: float = CAP_SECONDS,
                 stable: float = STABLE_SECONDS, stop_seconds: float = STOP_SECONDS, ready_seconds: float = READY_SECONDS, tick: float = TICK_SECONDS) -> None:
        self.children = [Child(spec) for spec in specs]
        self.secrets = dict(secrets)
        self.adopt_orphans = adopt_orphans
        self.base, self.cap, self.stable = base, cap, stable
        self.stop_seconds, self.ready_seconds, self.tick = stop_seconds, ready_seconds, tick
        self.stopping = False
        self.ended: list[Child] = []

    def stop(self, *_: object) -> None:
        self.stopping = True

    def start(self, child: Child) -> None:
        spec = child.spec
        pipes = {name: os.pipe() for name in spec.secrets}
        argv, env = list(spec.argv), dict(spec.env)
        for name, (read_end, _) in pipes.items():
            argv = [part.replace(PIPE[name], str(read_end)) for part in argv]
            env = {key: value.replace(PIPE[name], str(read_end)) for key, value in env.items()}
        output = subprocess.DEVNULL if spec.quiet else None
        child.started = time.monotonic()
        try:
            child.process = subprocess.Popen(argv, env=env, cwd=spec.cwd, pass_fds=tuple(read_end for read_end, _ in pipes.values()), start_new_session=True,
                                             stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        except OSError as error:
            child.process = None
            child.failures += 1
            child.due = child.started + backoff(child.failures, self.base, self.cap)
            log("child did not start", child=spec.name, error=type(error).__name__)
        else:
            log("child started", child=spec.name)
        finally:
            for name, (read_end, write_end) in pipes.items():
                os.close(read_end)
                with contextlib.suppress(OSError):
                    os.write(write_end, self.secrets[name].encode() if child.process is not None else b"")
                os.close(write_end)

    def collected(self) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        if self.adopt_orphans:
            with contextlib.suppress(ChildProcessError):
                while (done := os.waitpid(-1, os.WNOHANG))[0]:
                    found.append(done)
            return found
        for child in self.children:
            if child.process is not None and child.process.returncode is None:
                with contextlib.suppress(ChildProcessError):
                    pid, status = os.waitpid(child.process.pid, os.WNOHANG)
                    if pid:
                        found.append((pid, status))
        return found

    def reap(self) -> None:
        running = {child.process.pid: child for child in self.children if child.process is not None and child.process.returncode is None}
        for pid, status in self.collected():
            child = running.get(pid)
            if child is not None and child.process is not None:
                child.process.returncode = os.waitstatus_to_exitcode(status)
                self.ended.append(child)

    def stop_group(self, pgid: int) -> None:
        for signum, seconds in ((signal.SIGTERM, self.stop_seconds), (signal.SIGKILL, KILL_SECONDS)):
            if not signal_group(pgid, signum):
                return
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                alive = group_alive(pgid)
                self.reap()
                if not alive:
                    return
                time.sleep(min(self.tick, 0.05))

    def sweep_orphans(self) -> None:
        if not self.adopt_orphans:
            return
        known = {child.process.pid for child in self.children if child.process is not None}
        for pid in adopted_by(os.getpid()):
            if pid in known:
                continue
            with contextlib.suppress(ProcessLookupError):
                pgid = os.getpgid(pid)
                log("stopping a process its parent left behind")
                if pgid == os.getpgrp():
                    os.kill(pid, signal.SIGKILL)
                else:
                    self.stop_group(pgid)
        self.reap()

    def after_exit(self, child: Child) -> None:
        process = child.process
        if process is None:
            return
        self.stop_group(process.pid)
        if child.spec.strays:
            self.sweep_orphans()
        child.failures = 0 if time.monotonic() - child.started >= self.stable else child.failures + 1
        delay = backoff(child.failures, self.base, self.cap)
        child.due = time.monotonic() + delay
        child.process = None
        log("child exited; restarting it", child=child.spec.name, code=process.returncode or 0, seconds=round(delay, 1))

    def handle_exits(self) -> None:
        self.reap()
        while self.ended and not self.stopping:
            self.after_exit(self.ended.pop(0))

    def wait_ready(self, child: Child) -> None:
        deadline = time.monotonic() + self.ready_seconds
        while child.spec.ready is not None and not self.stopping and child.process is not None and time.monotonic() < deadline:
            if child.spec.ready():
                return
            self.handle_exits()
            time.sleep(self.tick)
        if child.spec.ready is not None and child.process is not None and not self.stopping:
            log("child not ready in time; starting the next one", child=child.spec.name)

    def run(self) -> int:
        for child in self.children:
            if self.stopping:
                break
            self.start(child)
            self.wait_ready(child)
        while not self.stopping:
            self.handle_exits()
            for child in self.children:
                if child.process is None and child.due <= time.monotonic() and not self.stopping:
                    self.start(child)
            time.sleep(self.tick)
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        log("stopping")
        for child in reversed(self.children):
            if child.process is not None and child.process.returncode is None:
                self.stop_group(child.process.pid)
                log("child stopped", child=child.spec.name)
        self.sweep_orphans()
