import os
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from darkroom import tls
from tests.shared import private_socket_folder

HOST = "127.0.0.1"


class TlsFloorTests(unittest.TestCase):
    def test_every_client_context_refuses_anything_older_than_tls_1_2(self):
        self.assertGreaterEqual(tls.client_context().minimum_version, ssl.TLSVersion.TLSv1_2)
        made: list[ssl.SSLContext] = []

        def recorded(cafile: str | None = None) -> ssl.SSLContext:
            made.append(real(cafile))
            return made[-1]

        real = tls.client_context
        with socket.socket() as unused:
            unused.bind((HOST, 0))
            port = unused.getsockname()[1]
        with mock.patch.object(tls, "client_context", side_effect=recorded):
            with self.assertRaises(OSError):
                tls.urlopen(f"https://{HOST}:{port}/", timeout=2)
        self.assertEqual(len(made), 1)
        for context in made:
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)


if __name__ == "__main__":
    unittest.main()


class SocketFolderTests(unittest.TestCase):
    def test_the_folder_is_the_one_the_init_uses_and_the_image_makes_private(self):
        from darkroom_init import plan

        self.assertEqual(tls.SOCKET_FOLDER, Path("/run/darkroom"))
        self.assertEqual(tls.SOCKET_FOLDER, plan.SOCKET_FOLDER)
        image = (Path(__file__).resolve().parent.parent / "Dockerfile.single").read_text()
        self.assertIn("&& chmod 0700 /run/darkroom", image)


class PrivateSocketTests(unittest.TestCase):
    def setUp(self):
        self.folder = private_socket_folder(self)
        self.accepted = 0

    def listen(self, path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen()
        self.addCleanup(listener.close)

        def accept():
            try:
                stream, _ = listener.accept()
            except OSError:
                return
            self.accepted += 1
            stream.close()
        threading.Thread(target=accept, daemon=True).start()
        return path

    def test_a_socket_directly_in_the_private_folder_is_connected_to(self):
        path = self.listen(self.folder / "viewer.sock")
        tls.unix_stream(str(path), 2).close()
        self.assertEqual(tls.private_socket(str(path)), str(path))

    def test_a_socket_path_outside_the_folder_is_refused_before_connecting(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        (self.folder / "nested").mkdir(mode=0o700)
        outside = [self.listen(Path(elsewhere.name) / "viewer.sock"), self.listen(self.folder / "nested" / "viewer.sock")]
        for path in (*map(str, outside), f"{self.folder}/nested/../viewer.sock", f"{self.folder}//viewer.sock", f"{self.folder}/./viewer.sock", "viewer.sock",
                     f"{self.folder}/viewer.sock\x00", str(self.folder), f"{self.folder}/"):
            with self.subTest(path=path):
                self.assertRaisesRegex(tls.Refused, "outside the private socket folder", tls.unix_stream, path, 2)
                self.assertRaises(tls.Refused, tls.unix_request, path, "GET", "/", limit=10)
        self.assertEqual(self.accepted, 0)

    def test_a_folder_others_can_enter_or_read_or_that_is_a_link_is_refused(self):
        path = self.listen(self.folder / "viewer.sock")
        for mode in (0o701, 0o710, 0o740, 0o755, 0o777):
            with self.subTest(mode=oct(mode)):
                self.folder.chmod(mode)
                self.assertRaisesRegex(tls.Refused, "not private", tls.unix_stream, str(path), 2)
        self.folder.chmod(0o700)
        linked = Path(tempfile.mkdtemp())
        self.addCleanup(os.rmdir, linked)
        link = linked / "run"
        link.symlink_to(self.folder)
        self.addCleanup(link.unlink)
        with mock.patch.object(tls, "SOCKET_FOLDER", link):
            self.assertRaisesRegex(tls.Refused, "not private", tls.unix_stream, str(link / "viewer.sock"), 2)
        with mock.patch.object(tls, "SOCKET_FOLDER", self.folder / "missing"):
            self.assertRaisesRegex(tls.Refused, "not private", tls.unix_stream, str(self.folder / "missing" / "viewer.sock"), 2)
        self.assertFalse(tls.socket_folder_private(path))
        self.assertEqual(self.accepted, 0)

    def test_a_folder_owned_by_another_user_is_refused(self):
        real = os.lstat(self.folder)
        with mock.patch.object(os, "getuid", return_value=real.st_uid + 1):
            self.assertFalse(tls.socket_folder_private(self.folder))
            self.assertRaisesRegex(tls.Refused, "not private", tls.unix_stream, str(self.listen(self.folder / "viewer.sock")), 2)
        self.assertEqual(self.accepted, 0)

