from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

TIMEOUT = 30.0
MAX_REPLY = 1024 * 1024
SOCKET_FOLDER = Path("/run/darkroom")
APP_SOCKET = SOCKET_FOLDER / "worker-api.sock"


class Unreachable(Exception):
    pass


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"darkroom answered {status}: {message}")
        self.status = status


class LeaseLost(Refused):
    pass


def folder_is_private(folder: Path) -> bool:
    try:
        found = os.lstat(folder)
    except OSError:
        return False
    return stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() and stat.S_IMODE(found.st_mode) & 0o077 == 0


def checked_socket(socket_path: Path, folder: Path) -> str:
    text = str(socket_path)
    if not socket_path.is_absolute() or "\x00" in text or os.path.normpath(text) != text or socket_path.parent != folder:
        raise Unreachable("the app's socket is not directly in the private socket folder")
    if not folder_is_private(folder):
        raise Unreachable("the private socket folder is not private to this user")
    return text


class OnSocket(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.settimeout(self.timeout)
        try:
            stream.connect(self.socket_path)
        except OSError:
            stream.close()
            raise
        self.sock = stream


class Client:
    def __init__(self, socket_path: Path, token: str, timeout: float = TIMEOUT, folder: Path | None = None) -> None:
        self.socket_path = socket_path
        self.folder = SOCKET_FOLDER if folder is None else folder
        self.token = token
        self.timeout = timeout

    def exchange(self, path: str, data: bytes) -> tuple[int, bytes]:
        connection = OnSocket(checked_socket(self.socket_path, self.folder), self.timeout)
        try:
            connection.request("POST", path, body=data, headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            reply = connection.getresponse()
            return reply.status, reply.read(MAX_REPLY)
        except (OSError, http.client.HTTPException) as error:
            raise Unreachable(type(error).__name__) from None
        finally:
            connection.close()

    def post(self, path: str, body: dict[str, object]) -> dict[str, Any]:
        status, raw = self.exchange(path, json.dumps(body).encode())
        if 300 <= status < 400:
            raise Unreachable("darkroom redirected; the worker follows no redirects")
        if not 200 <= status < 300:
            message = self.error_text(raw)
            if status in (404, 409):
                raise LeaseLost(status, message)
            raise Refused(status, message)
        parsed = json.loads(raw or b"{}")
        if not isinstance(parsed, dict):
            raise Unreachable("darkroom answered something other than a JSON object")
        return parsed

    @staticmethod
    def error_text(raw: bytes) -> str:
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            return "unreadable error"
        return str(parsed.get("error", "unknown error")) if isinstance(parsed, dict) else "unknown error"

    def claim(self, worker: str) -> dict[str, Any] | None:
        job = self.post("/internal/browser/jobs/claim", {"worker": worker}).get("job")
        return job if isinstance(job, dict) else None

    def heartbeat(self, job_id: int) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/heartbeat", {})

    def progress(self, job_id: int, message: str) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/progress", {"message": message})

    def waiting(self, job_id: int, reason: str) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/waiting", {"reason": reason})

    def download_plan(self, job_id: int) -> dict[str, Any]:
        return self.post(f"/internal/browser/jobs/{job_id}/download", {})

    def part(self, job_id: int, number: int, body: dict[str, object]) -> dict[str, Any]:
        return self.post(f"/internal/browser/jobs/{job_id}/parts/{number}", body)

    def done(self, job_id: int, result: dict[str, object]) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/done", {"result": result})

    def failed(self, job_id: int, message: str, failure_dir: str | None, files: list[str] | None = None) -> None:
        body: dict[str, object] = {"message": message}
        if failure_dir is not None:
            body["failure_dir"] = failure_dir
            body["files"] = list(files or [])
        self.post(f"/internal/browser/jobs/{job_id}/failed", body)
