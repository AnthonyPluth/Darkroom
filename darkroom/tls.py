from __future__ import annotations

import socket
import ssl
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPException, HTTPResponse
from types import TracebackType
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.events
import h2.exceptions

DEFAULT_TIMEOUT = 15.0
MAX_HTTP2_REPLY = 64 * 1024
READ_SIZE = 65535


class Refused(Exception):
    pass


class StreamFailed(OSError):
    pass


CONNECTION_PROBLEMS: tuple[type[Exception], ...] = (OSError, HTTPException)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("redirects are not followed")


def urlopen(url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None,
            method: str | None = None, timeout: float = DEFAULT_TIMEOUT, allow_http: bool = False) -> HTTPResponse:
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and allow_http):
        raise Refused("only https addresses are allowed here")
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirects())
    response = opener.open(request, timeout=timeout)
    assert isinstance(response, HTTPResponse)
    return response


@dataclass(frozen=True)
class Http2Reply:
    status: int
    headers: dict[str, str]
    body: bytes


class Http2:
    def __init__(self, host: str, port: int = 443, *, timeout: float = DEFAULT_TIMEOUT, cafile: str | None = None) -> None:
        self.host, self.port, self.timeout, self.cafile = host, port, timeout, cafile
        self.sock: ssl.SSLSocket | None = None
        self.connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))

    def __enter__(self) -> Http2:
        context = ssl.create_default_context(cafile=self.cafile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["h2"])
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        if self.sock.selected_alpn_protocol() != "h2":
            self.close()
            raise StreamFailed("the server does not speak HTTP/2")
        self.connection.initiate_connection()
        self.flush()
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.connection.close_connection()
            self.flush()
        except (OSError, h2.exceptions.ProtocolError):
            pass
        self.sock.close()
        self.sock = None

    def flush(self) -> None:
        assert self.sock is not None
        self.sock.sendall(self.connection.data_to_send())

    def post(self, path: str, headers: Mapping[str, str], body: bytes) -> Http2Reply:
        if self.sock is None:
            raise StreamFailed("the connection is closed")
        try:
            stream = self.connection.get_next_available_stream_id()
            request = [(":method", "POST"), (":scheme", "https"), (":authority", self.host), (":path", path),
                       *((name.lower(), value) for name, value in headers.items()), ("content-length", str(len(body)))]
            self.connection.send_headers(stream, request)
            self.connection.send_data(stream, body, end_stream=True)
            self.flush()
            return self.reply_to(stream)
        except h2.exceptions.ProtocolError as error:
            raise StreamFailed(f"HTTP/2 protocol error ({type(error).__name__})") from None

    def reply_to(self, stream: int) -> Http2Reply:
        assert self.sock is not None
        status, got, body = 0, {}, bytearray()
        while True:
            data = self.sock.recv(READ_SIZE)
            if not data:
                raise StreamFailed("the server closed the connection")
            for event in self.connection.receive_data(data):
                if isinstance(event, h2.events.ConnectionTerminated):
                    raise StreamFailed("the server ended the connection")
                if getattr(event, "stream_id", None) != stream:
                    continue
                if isinstance(event, h2.events.ResponseReceived):
                    got = {str(name): str(value) for name, value in event.headers or []}
                    status = int(got.get(":status", "0"))
                elif isinstance(event, h2.events.DataReceived):
                    body += event.data or b""
                    self.connection.acknowledge_received_data(event.flow_controlled_length or 0, stream)
                    if len(body) > MAX_HTTP2_REPLY:
                        raise StreamFailed("the reply was too large")
                elif isinstance(event, h2.events.StreamReset):
                    raise StreamFailed("the server reset the request")
                elif isinstance(event, h2.events.StreamEnded):
                    self.flush()
                    return Http2Reply(status, got, bytes(body))
            self.flush()
