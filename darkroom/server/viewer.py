from __future__ import annotations

import base64
import re
import select
import time
from io import BufferedIOBase
from pathlib import Path

from darkroom import config, monitoring, tls
from darkroom.server import static
from darkroom.server.common import VIEWER_CSP, Request, Response, asks_for_websocket, error_response, header

PREFIX = "/viewer/"
SOCKET_PATH = "websockify"
SAFE_PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")
WEBSOCKET_KEY = re.compile(r"[A-Za-z0-9+/]{22}==")
TOKEN = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
PROTOCOLS = re.compile(rf"{TOKEN}(?: *, *{TOKEN})*")
STATUS = re.compile(rb"HTTP/1\.[01] ([0-9]{3})[ \r]")
MAX_FILE = 4 * 1024 * 1024
HEAD_LIMIT = 16 * 1024
CONNECT_SECONDS = 5.0
SEND_SECONDS = 60.0
PING_SECONDS = 20.0
SILENT_PINGS = 3
POLL_SECONDS = 0.25
CHUNK = 64 * 1024
PING = b"\x89\x00"
GOING_AWAY = b"\x88\x02\x03\xe9"
CLOSE = 0x8
NOT_HERE = "the remote browser view is not part of this setup"
NOT_RUNNING = "the remote browser view is not running"


def relay(request: Request) -> Response:
    settings = request.settings
    if not config.browser_token_usable(settings.viewer_secret):
        return error_response(NOT_HERE, 404)
    rest = request.params.get("path", "")
    if not request.target.startswith(PREFIX) or not SAFE_PATH.fullmatch(rest):
        return error_response("not found", 404)
    if asks_for_websocket(request):
        return upgrade(request, rest)
    return viewer_file(settings.viewer_files, rest)


def viewer_file(root: Path, rest: str) -> Response:
    base = root.resolve()
    candidate = (base / rest).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file() or candidate.stat().st_size > MAX_FILE:
        return error_response("not found", 404)
    return Response(200, candidate.read_bytes(), static.content_type_of(candidate.name), {"Content-Security-Policy": VIEWER_CSP, "X-Content-Type-Options": "nosniff"})


def asks_to_upgrade(request: Request) -> bool:
    tokens = {part.strip().lower() for part in (header(request, "Connection") or "").split(",")}
    return "upgrade" in tokens


def upstream_request(request: Request) -> bytes | None:
    key = (header(request, "Sec-WebSocket-Key") or "").strip()
    version = (header(request, "Sec-WebSocket-Version") or "").strip()
    protocols = (header(request, "Sec-WebSocket-Protocol") or "").strip()
    if not asks_to_upgrade(request) or not WEBSOCKET_KEY.fullmatch(key) or version != "13" or (protocols and not PROTOCOLS.fullmatch(protocols)):
        return None
    credentials = base64.b64encode(f"{config.VIEWER_USER}:{request.settings.viewer_secret}".encode("ascii")).decode("ascii")
    lines = [f"GET /{SOCKET_PATH} HTTP/1.1", f"Host: {tls.LOOPBACK}:{config.VIEWER_PORT}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", *([f"Sec-WebSocket-Protocol: {protocols}"] if protocols else []),
             f"Authorization: Basic {credentials}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def upgrade(request: Request, rest: str) -> Response:
    if rest != SOCKET_PATH:
        return error_response("not found", 404)
    asked = upstream_request(request)
    if asked is None:
        return error_response("bad request", 400)
    try:
        upstream = tls.loopback(config.VIEWER_PORT, CONNECT_SECONDS)
    except OSError:
        monitoring.log("remote browser view not reachable")
        return error_response(NOT_RUNNING, 502)
    try:
        upstream.sendall(asked)
        reply = read_head(upstream)
    except OSError:
        reply = b""
    status = STATUS.match(reply)
    if status is None or status.group(1) != b"101":
        tls.hang_up(upstream)
        monitoring.log("remote browser view refused the relay", status=int(status.group(1)) if status else 0)
        return error_response(NOT_RUNNING, 502)
    upstream.settimeout(SEND_SECONDS)
    return Response(101, b"", takeover=lambda client, buffered: carry(client, buffered, upstream, reply))


def read_head(upstream: tls.Stream) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > HEAD_LIMIT:
            return b""
        chunk = upstream.recv(4096)
        if not chunk:
            return b""
        data += chunk
    return data


class Frames:
    def __init__(self) -> None:
        self.head = bytearray()
        self.left = 0
        self.closing = False

    def at_boundary(self) -> bool:
        return not self.head and self.left == 0

    def feed(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.left:
                step = min(self.left, len(view))
                self.left -= step
                view = view[step:]
                continue
            self.head.append(view[0])
            view = view[1:]
            if len(self.head) == head_size(self.head):
                self.left = payload_size(self.head)
                self.closing = self.closing or self.head[0] & 0x0F == CLOSE
                self.head.clear()


def head_size(head: bytearray) -> int:
    if len(head) < 2:
        return 2
    length = head[1] & 0x7F
    return 2 + (2 if length == 126 else 8 if length == 127 else 0) + (4 if head[1] & 0x80 else 0)


def payload_size(head: bytearray) -> int:
    length = head[1] & 0x7F
    if length == 126:
        return int.from_bytes(head[2:4], "big")
    if length == 127:
        return int.from_bytes(head[2:10], "big")
    return length


def carry(client: tls.Stream, buffered: BufferedIOBase, upstream: tls.Stream, reply: bytes) -> None:
    started = time.monotonic()
    ended = "error"
    end = reply.index(b"\r\n\r\n") + 4
    frames = Frames()
    try:
        client.settimeout(SEND_SECONDS)
        client.sendall(reply[:end])
        if reply[end:]:
            frames.feed(reply[end:])
            client.sendall(reply[end:])
        client.setblocking(False)
        early = buffered.read1(CHUNK)
        client.settimeout(SEND_SECONDS)
        if early:
            upstream.sendall(early)
        ended = pump(client, upstream, frames)
    except OSError:
        pass
    finally:
        tls.hang_up(upstream)
        tls.hang_up(client)
        monitoring.log("remote browser view closed", ended=ended, seconds=round(time.monotonic() - started))


def pump(client: tls.Stream, upstream: tls.Stream, frames: Frames) -> str:
    heard = pinged = time.monotonic()
    while True:
        now = time.monotonic()
        silent_limit = SILENT_PINGS * PING_SECONDS
        wait = max(0.0, min(max(POLL_SECONDS, pinged + PING_SECONDS - now), heard + silent_limit - now))
        ready, _, _ = select.select([client, upstream], [], [], wait)
        if upstream in ready:
            data = upstream.recv(CHUNK)
            if not data:
                if frames.at_boundary() and not frames.closing:
                    client.sendall(GOING_AWAY)
                return "upstream"
            frames.feed(data)
            client.sendall(data)
        if client in ready:
            data = client.recv(CHUNK)
            if not data:
                return "client"
            heard = time.monotonic()
            upstream.sendall(data)
        now = time.monotonic()
        if now - heard >= silent_limit:
            return "silent"
        if now - pinged >= PING_SECONDS and frames.at_boundary() and not frames.closing:
            client.sendall(PING)
            pinged = now
