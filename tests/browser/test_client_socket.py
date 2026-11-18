import http.client
import io
import socket
import socketserver
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from darkroom_browser import client as app_client
from darkroom_browser.client import Client, OnSocket, Unreachable
from tests.browser.fake_darkroom import TOKEN

REAL_SOCKET = socket.socket
ANSWER = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"


class Recorded:
    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.families: list[int] = []
        self.made: list[socket.socket] = []
        self.closes: list[socket.socket] = []

    def spy(self) -> type:
        families, made, closes = self.families, self.made, self.closes

        class Spy(REAL_SOCKET):
            def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
                families.append(family)
                super().__init__(family, type, proto, fileno)
                if fileno is None:
                    made.append(self)

            def close(self):
                if self in made:
                    closes.append(self)
                super().close()

        return Spy


def unix_app(test: unittest.TestCase, recorded: Recorded) -> Path:
    class Answer(socketserver.BaseRequestHandler):
        def handle(self):
            seen = b""
            while b"\r\n\r\n" not in seen:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                seen += chunk
            head, _, rest = seen.partition(b"\r\n\r\n")
            length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")), 0)
            while len(rest) < length:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                rest += chunk
            if seen:
                recorded.requests.append(head + b"\r\n\r\n" + rest)
                self.request.sendall(ANSWER)

    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    socket_path = Path(folder.name) / "worker-api.sock"
    server = socketserver.UnixStreamServer(str(socket_path), Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return socket_path


def tcp_listener(test: unittest.TestCase) -> tuple[str, int, list[object]]:
    accepted: list[object] = []
    listener = REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    test.addCleanup(listener.close)
    test.addCleanup(stop.set)
    return "127.0.0.1", listener.getsockname()[1], accepted


class TheClassIsClosedTests(unittest.TestCase):
    def test_no_subclass_can_be_made_by_a_class_statement_or_by_type(self):
        socket_path = unix_app(self, Recorded())
        instance = OnSocket(str(socket_path), 5.0)
        self.addCleanup(instance.close)
        with self.assertRaises(TypeError):
            class Tcp(OnSocket):
                pass
        with self.assertRaises(TypeError):
            class FromAnInstance(type(instance)):
                pass
        with self.assertRaises(TypeError):
            type("OnTcp", (OnSocket,), {})
        with self.assertRaises(TypeError):
            type("Mixed", (int, type(instance)), {})
        self.assertTrue(getattr(OnSocket, "__final__", False))

    def test_the_class_is_not_an_http_connection_so_no_tcp_connect_is_reachable_from_an_instance(self):
        socket_path = unix_app(self, Recorded())
        instance = OnSocket(str(socket_path), 5.0)
        self.addCleanup(instance.close)
        self.assertEqual(OnSocket.__mro__, (OnSocket, object))
        self.assertNotIsInstance(instance, http.client.HTTPConnection)
        for name in ("connect", "host", "port", "set_tunnel", "_create_connection", "auto_open"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(instance, name))
        with self.assertRaises(AttributeError):
            super(type(instance), instance).connect()

    def test_setting_host_and_port_on_an_instance_still_sends_only_over_the_unix_socket(self):
        recorded = Recorded()
        socket_path = unix_app(self, recorded)
        host, port, accepted = tcp_listener(self)
        with mock.patch.object(app_client.socket, "socket", recorded.spy()):
            connection = OnSocket(str(socket_path), 5.0)
            try:
                for name, value in (("host", host), ("port", port), ("sock", REAL_SOCKET)):
                    with self.subTest(name=name), self.assertRaises(AttributeError):
                        setattr(connection, name, value)
                connection.request("POST", "/internal/browser/jobs/claim", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}"})
                reply = connection.getresponse()
                self.assertEqual((reply.status, reply.read()), (200, b"{}"))
            finally:
                connection.close()
        self.assertEqual(len(recorded.requests), 1)
        self.assertEqual(accepted, [])
        self.assertNotIn(socket.AF_INET, recorded.families)
        self.assertNotIn(socket.AF_INET6, recorded.families)
        self.assertIn(socket.AF_UNIX, recorded.families)

    def test_the_client_opens_only_unix_sockets(self):
        recorded = Recorded()
        socket_path = unix_app(self, recorded)
        with mock.patch.object(app_client.socket, "socket", recorded.spy()):
            self.assertIsNone(Client(socket_path, TOKEN, folder=socket_path.parent).claim("w"))
        self.assertEqual(len(recorded.requests), 1)
        self.assertTrue(recorded.families)
        self.assertEqual(set(recorded.families), {socket.AF_UNIX})

    def test_a_socket_that_is_not_there_is_unreachable_and_nothing_is_left_open(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for number, attempt in enumerate((lambda: OnSocket(str(Path(folder.name) / "worker-api.sock"), 1.0),
                        lambda: Client(Path(folder.name) / "worker-api.sock", TOKEN, folder=Path(folder.name)).claim("w"))):
            recorded = Recorded()
            with self.subTest(attempt=number), mock.patch.object(app_client.socket, "socket", recorded.spy()):
                with self.assertRaises((FileNotFoundError, Unreachable)) as raised:
                    attempt()
                self.assertIn("FileNotFoundError", f"{type(raised.exception).__name__} {raised.exception}")
                self.assertEqual(len(recorded.made), 1)
                self.assertEqual(recorded.closes, recorded.made)
                self.assertEqual(recorded.made[0].fileno(), -1)

    def test_closing_after_an_answer_closes_the_one_stream(self):
        recorded = Recorded()
        socket_path = unix_app(self, recorded)
        with mock.patch.object(app_client.socket, "socket", recorded.spy()):
            connection = OnSocket(str(socket_path), 5.0)
            connection.request("POST", "/internal/browser/jobs/claim", body=b"{}", headers={})
            reply = connection.getresponse()
            self.assertEqual((reply.status, reply.read()), (200, b"{}"))
            self.assertGreaterEqual(recorded.made[0].fileno(), 0)
            connection.close()
        self.assertEqual(len(recorded.made), 1)
        self.assertEqual(recorded.made[0].fileno(), -1)


def reachable_by_attribute(start: object, depth: int) -> list[tuple[str, object]]:
    seen: dict[int, tuple[str, object]] = {id(start): ("instance", start)}
    frontier = [("instance", start)]
    for _ in range(depth):
        following = []
        for trail, found in frontier:
            for name in dir(found):
                if name.startswith("__") and name.endswith("__"):
                    continue
                try:
                    value = getattr(found, name)
                except Exception:
                    continue
                if id(value) not in seen:
                    seen[id(value)] = (f"{trail}.{name}", value)
                    following.append((f"{trail}.{name}", value))
        frontier = following
    return list(seen.values())


def opens_sockets(found: object) -> bool:
    if found is socket or isinstance(found, (socket.socket, socket.SocketIO)):
        return True
    return any(isinstance(kind, type) and issubclass(kind, (socket.socket, socket.SocketIO)) for kind in (found, type(found), found.__class__))


class NoSocketHandedOutTests(unittest.TestCase):
    def test_an_instance_and_its_answer_hand_out_no_socket_or_socket_class_by_attribute(self):
        recorded = Recorded()
        socket_path = unix_app(self, recorded)
        connection = OnSocket(str(socket_path), 5.0)
        self.addCleanup(connection.close)
        self.assertFalse(hasattr(connection, "sock"))
        self.assertFalse(hasattr(connection, "__dict__"))
        with self.assertRaises(TypeError):
            vars(connection)
        connection.request("POST", "/internal/browser/jobs/claim", body=b"{}", headers={})
        reply = connection.getresponse()
        for start in (connection, reply, reply.fp, connection.reply):
            for trail, found in reachable_by_attribute(start, 4):
                with self.subTest(trail=trail):
                    self.assertFalse(opens_sockets(found), trail)
        self.assertIsInstance(reply.fp, io.BufferedReader)
        self.assertEqual(reply.read(), b"{}")

    def test_the_check_would_see_a_socket_kept_on_an_instance(self):
        stream = REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(stream.close)
        holder = types.SimpleNamespace(conn=types.SimpleNamespace(sock=stream))
        self.assertTrue(any(opens_sockets(found) for _, found in reachable_by_attribute(holder, 4)))
        self.assertTrue(any(opens_sockets(found) for _, found in reachable_by_attribute(types.SimpleNamespace(kind=REAL_SOCKET), 4)))
        self.assertTrue(any(opens_sockets(found) for _, found in reachable_by_attribute(types.SimpleNamespace(module=socket), 4)))


class OnTheWireTests(unittest.TestCase):
    def test_the_request_is_byte_for_byte_what_http_client_sent(self):
        recorded = Recorded()
        socket_path = unix_app(self, recorded)

        class Reference(http.client.HTTPConnection):
            def connect(self):
                self.sock = REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(str(socket_path))

        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        body = b'{"worker": "w"}'
        for make in (lambda: Reference("localhost", timeout=5.0), lambda: OnSocket(str(socket_path), 5.0)):
            connection = make()
            try:
                connection.request("POST", "/internal/browser/jobs/claim", body=body, headers=headers)
                connection.getresponse().read()
            finally:
                connection.close()
        self.assertEqual(len(recorded.requests), 2)
        self.assertEqual(recorded.requests[1], recorded.requests[0])

    def test_a_path_or_header_that_would_split_the_request_is_refused_before_anything_is_sent(self):
        recorded = Recorded()
        socket_path = unix_app(self, recorded)
        for path, headers in (("/a b", {}), ("/a\r\nX: y", {}), ("relative", {}), ("/a", {"Authorization": "Bearer x\r\nX: y"}),
                              ("/a", {"Bad Name": "x"}), ("/a", {"X": "y\x00"})):
            with self.subTest(path=path, headers=headers):
                connection = OnSocket(str(socket_path), 5.0)
                try:
                    with self.assertRaises((http.client.InvalidURL, ValueError)):
                        connection.request("POST", path, body=b"{}", headers=headers)
                finally:
                    connection.close()
        self.assertEqual(recorded.requests, [])


if __name__ == "__main__":
    unittest.main()
