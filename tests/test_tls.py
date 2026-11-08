import socket
import ssl
import unittest
from unittest import mock

from darkroom import tls

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
