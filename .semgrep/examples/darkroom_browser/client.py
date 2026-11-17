import http.client

# ok: darkroom-no-raw-sockets
import socket  # nosemgrep: darkroom-no-raw-sockets -- the worker's one socket, the app's private Unix socket that OnSocket.connect opens


# ok: darkroom-no-raw-sockets
class OnSocket(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        # ok: darkroom-no-raw-sockets
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        # ok: darkroom-no-raw-sockets
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # nosemgrep: darkroom-no-raw-sockets -- the one socket the client opens, to the app's private Unix socket
        stream.connect("/run/darkroom/worker-api.sock")
        self.sock = stream


def reach_the_app(path: str) -> object:
    # ok: darkroom-no-raw-sockets
    return OnSocket(path, 30.0)


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
