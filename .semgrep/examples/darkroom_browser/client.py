import http.client
import io
from collections.abc import Callable
from typing import final

# ok: darkroom-no-raw-sockets
import socket  # nosemgrep: darkroom-no-raw-sockets -- the worker's one socket, the app's private Unix socket that OnSocket opens when it is made


class Received(io.RawIOBase):
    def __init__(self, receive: Callable[[bytearray], int]) -> None:
        super().__init__()
        self._receive = receive

    def readinto(self, buffer: bytearray) -> int:
        return self._receive(buffer)

    def makefile(self, mode: str) -> io.BufferedReader:
        return io.BufferedReader(self)


# ok: darkroom-no-raw-sockets
@final
# ok: darkroom-no-raw-sockets
class OnSocket:
    __slots__ = ("_received",)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("OnSocket takes no subclass")

    def __init__(self, socket_path: str, timeout: float) -> None:
        # ok: darkroom-no-raw-sockets
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # nosemgrep: darkroom-no-raw-sockets -- the one socket the client opens, to the app's private Unix socket
        stream.settimeout(timeout)
        stream.connect(socket_path)

        def receive(buffer: bytearray) -> int:
            return stream.recv_into(buffer)

        self._received = Received(receive)

    def getresponse(self) -> http.client.HTTPResponse:
        # ok: darkroom-no-raw-sockets
        reply = http.client.HTTPResponse(self._received, method="POST")
        reply.begin()
        return reply


def reach_the_app(path: str) -> object:
    # ok: darkroom-no-raw-sockets
    return OnSocket(path, 30.0)


def ask_the_app(path: str, body: bytes) -> tuple[int, bytes]:
    # ok: darkroom-no-raw-sockets
    connection = OnSocket(path, 30.0)
    try:
        # ok: darkroom-no-raw-sockets
        connection.request("POST", "/internal/worker/lease", body=body)
        # ok: darkroom-no-raw-sockets
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        # ok: darkroom-no-raw-sockets
        connection.close()


def second_socket(host: str) -> object:
    # ruleid: darkroom-no-raw-sockets
    stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stream.connect((host, 443))
    return stream


def second_connection(host: str) -> object:
    # ruleid: darkroom-no-raw-sockets
    return socket.create_connection((host, 443))


# ruleid: darkroom-no-raw-sockets
from socket import socketpair


def raw_urlopen(url: str) -> object:
    import urllib.request
    # ruleid: darkroom-raw-urlopen
    return urllib.request.urlopen(url)


def connection_to_a_host(host: str) -> object:
    # ruleid: darkroom-no-raw-sockets
    return http.client.HTTPConnection(host, 80)


def tls_connection_to_a_host(host: str) -> object:
    from http.client import HTTPSConnection
    # ruleid: darkroom-no-raw-sockets
    return HTTPSConnection(host)


def connect_later(host: str) -> object:
    # ruleid: darkroom-no-raw-sockets
    opener = socket.create_connection
    return opener((host, 443))


# ruleid: darkroom-no-raw-sockets
class ElsewhereOnTcp(http.client.HTTPConnection):
    pass


def connection_taken_as_a_value(host: str) -> object:
    # ruleid: darkroom-no-raw-sockets
    make = http.client.HTTPConnection
    return make(host)


# ruleid: darkroom-no-raw-sockets
class OnSocket(http.client.HTTPConnection):
    pass


# ruleid: darkroom-no-raw-sockets
class TcpUnderOnSocket(OnSocket):
    def connect(self) -> None:
        # ruleid: darkroom-no-raw-sockets
        http.client.HTTPConnection.connect(self)


# ruleid: darkroom-no-raw-sockets
class Tcp(OnSocket):
    def connect(self) -> None:
        # ruleid: darkroom-no-raw-sockets
        super(OnSocket, self).connect()


# ruleid: darkroom-no-raw-sockets
class Child(OnSocket):
    pass


# ruleid: darkroom-no-raw-sockets
SameClass = OnSocket


class ChildOfTheAlias(SameClass):
    def __init__(self, host: str) -> None:
        super().__init__("/run/darkroom/worker-api.sock", 30.0)
        self.host = host


def made_on_the_fly() -> type:
    # ruleid: darkroom-no-raw-sockets
    return type("OnTcp", (OnSocket,), {})
