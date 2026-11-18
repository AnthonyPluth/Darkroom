from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import socket  # nosemgrep: darkroom-no-raw-sockets -- the worker's one socket, the app's private Unix socket that OnSocket opens when it is made (#393)
import stat
from collections.abc import Buffer, Callable
from pathlib import Path
from typing import Any, final

from darkroom_browser.context import LeaseLost, Refused, Unreachable

TIMEOUT = 30.0
MAX_REPLY = 1024 * 1024
SOCKET_FOLDER = Path("/run/darkroom")
APP_SOCKET = SOCKET_FOLDER / "worker-api.sock"


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


HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*")
REQUEST_PATH = re.compile(r"/[\x21-\x7e]*")


@final
class Received(io.RawIOBase):
    __slots__ = ("_receive",)

    def __init__(self, receive: Callable[[Buffer], int]) -> None:
        super().__init__()
        self._receive = receive

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        return self._receive(buffer)

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("the answer is read only as bytes")
        return io.BufferedReader(self)


@final
class OnSocket:
    __slots__ = ("_close", "_received", "_send", "method", "reply")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("OnSocket is the worker's one connection to the app's private socket and takes no subclass")

    def __init__(self, socket_path: str, timeout: float) -> None:
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # nosemgrep: darkroom-no-raw-sockets -- the one socket the client opens, to the app's private Unix socket (#393)
        try:
            stream.settimeout(timeout)
            stream.connect(socket_path)
        except OSError:
            stream.close()
            raise

        def send(data: bytes) -> None:
            stream.sendall(data)

        def receive(buffer: Buffer) -> int:
            return stream.recv_into(buffer)

        def close() -> None:
            stream.close()

        self._send = send
        self._received = Received(receive)
        self._close = close
        self.method = "POST"
        self.reply: http.client.HTTPResponse | None = None

    def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        if not HEADER_NAME.fullmatch(method):
            raise ValueError("the request's method is not a token")
        if not REQUEST_PATH.fullmatch(path):
            raise http.client.InvalidURL("the request's path is not an absolute path of visible ASCII")
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost", "Accept-Encoding: identity", f"Content-Length: {len(body)}"]
        for name, value in headers.items():
            if not HEADER_NAME.fullmatch(name) or not HEADER_VALUE.fullmatch(value):
                raise ValueError("a request header is not a token and a line of visible ASCII")
            lines.append(f"{name}: {value}")
        self.method = method
        self._send("\r\n".join(lines).encode("ascii") + b"\r\n\r\n" + body)

    def getresponse(self) -> http.client.HTTPResponse:
        self.reply = http.client.HTTPResponse(self._received, method=self.method)  # type: ignore[arg-type]
        self.reply.begin()
        return self.reply

    def close(self) -> None:
        if self.reply is not None:
            self.reply.close()
        self._close()


class Client:
    def __init__(self, socket_path: Path, token: str, timeout: float = TIMEOUT, folder: Path | None = None) -> None:
        self.socket_path = socket_path
        self.folder = SOCKET_FOLDER if folder is None else folder
        self.token = token
        self.timeout = timeout

    def exchange(self, path: str, data: bytes) -> tuple[int, bytes]:
        socket_path = checked_socket(self.socket_path, self.folder)
        try:
            connection = OnSocket(socket_path, self.timeout)
        except OSError as error:
            raise Unreachable(type(error).__name__) from None
        try:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
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
