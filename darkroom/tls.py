from __future__ import annotations

import contextlib
import os
import socket
import ssl
import stat
import urllib.request
from collections.abc import Iterable, Mapping
from http.client import HTTPConnection, HTTPException, HTTPResponse
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 15.0
SOCKET_FOLDER = Path("/run/darkroom")
Stream = socket.socket


class Refused(Exception):
    pass


CONNECTION_PROBLEMS: tuple[type[Exception], ...] = (OSError, HTTPException)
NOT_HTTP_REPLIES: tuple[type[Exception], ...] = (HTTPException,)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("redirects are not followed")


def tls_failure(error: BaseException) -> bool:
    return isinstance(error, ssl.SSLError) or isinstance(getattr(error, "reason", None), ssl.SSLError)


def client_context(cafile: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def urlopen(url: str, *, data: bytes | Iterable[bytes] | None = None, headers: Mapping[str, str] | None = None,
            method: str | None = None, timeout: float = DEFAULT_TIMEOUT, allow_http: bool = False) -> HTTPResponse:
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and allow_http):
        raise Refused("only https addresses are allowed here")
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    context = client_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirects())
    response = opener.open(request, timeout=timeout)
    assert isinstance(response, HTTPResponse)
    return response


def socket_folder_private(folder: Path) -> bool:
    try:
        found = os.lstat(folder)
    except OSError:
        return False
    return stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() and stat.S_IMODE(found.st_mode) & 0o077 == 0


def private_socket(socket_path: str) -> str:
    folder = SOCKET_FOLDER
    if not socket_path.startswith("/") or "\x00" in socket_path or os.path.normpath(socket_path) != socket_path or Path(socket_path).parent != folder:
        raise Refused("an internal socket outside the private socket folder")
    if not socket_folder_private(folder):
        raise Refused("the internal socket folder is not private to this user")
    return socket_path


def unix_stream(socket_path: str, timeout: float | None) -> Stream:
    checked = private_socket(socket_path)
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.settimeout(timeout)
    try:
        stream.connect(checked)
    except OSError:
        stream.close()
        raise
    return stream


class UnixConnection(HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = unix_stream(self.socket_path, self.timeout)


def unix_request(socket_path: str, method: str, target: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT, limit: int) -> tuple[int, bytes]:
    connection = UnixConnection(private_socket(socket_path), timeout)
    try:
        connection.request(method, target, body=data, headers=dict(headers or {}))
        reply = connection.getresponse()
        return reply.status, reply.read(limit)
    finally:
        connection.close()


def hang_up(stream: Stream) -> None:
    with contextlib.suppress(OSError):
        stream.shutdown(socket.SHUT_RDWR)
    stream.close()
