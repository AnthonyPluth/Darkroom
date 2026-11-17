import http.client

# ok: darkroom-no-raw-sockets
import socket  # nosemgrep: darkroom-no-raw-sockets -- the worker's one socket, the app's private Unix socket that OnSocket.connect opens


class OnSocket(http.client.HTTPConnection):
    def connect(self) -> None:
        # ok: darkroom-no-raw-sockets
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # nosemgrep: darkroom-no-raw-sockets -- the one socket the client opens, to the app's private Unix socket
        stream.connect("/run/darkroom/worker-api.sock")
        self.sock = stream


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
