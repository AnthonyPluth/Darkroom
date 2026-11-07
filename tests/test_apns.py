import contextlib
import io
import json
import os
import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import run
from darkroom import config, tls
from darkroom.providers import apns, exiftool
from tests.fakes.apns import HOST, FakeApns, new_key, pem_of
from tests.privacy import no_leaks
from tests.shared import settings_from

TOKEN_A = "a1" * 32
TOKEN_B = "b2" * 32
TOKEN_C = "c3" * 32
KEY_ID = "ABC123DEFG"
TEAM_ID = "TEAM456XYZ"
BUNDLE_ID = "com.example.darkroom"
PUSH = apns.Push("sign_in_needed", 42, "Google sign-in needed")


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def write_key(folder: Path, key: object | None = None, mode: int = 0o600, name: str = "AuthKey.p8") -> Path:
    path = folder / name
    path.write_bytes(pem_of(key) if isinstance(key, ec.EllipticCurvePrivateKey) else (key if isinstance(key, bytes) else pem_of(new_key())))
    path.chmod(mode)
    return path


class ApnsWorld(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.key = new_key()
        self.key_file = write_key(self.folder, self.key)
        self.fake = FakeApns(self.folder, self.key.public_key())
        self.addCleanup(self.fake.listener.close)
        self.clock = Clock()

    def start(self) -> FakeApns:
        return self.fake.__enter__()

    def client(self, port: int | None = None) -> apns.Apns:
        credentials = apns.Credentials(KEY_ID, TEAM_ID, BUNDLE_ID, self.key_file)
        return apns.Apns(credentials, self.clock, host=HOST, port=port or self.fake.port, cafile=str(self.fake.cafile), timeout=5.0)


class SendTests(ApnsWorld):
    def test_pushes_go_only_to_apples_production_host(self):
        self.assertEqual(apns.PRODUCTION_HOST, "api.push.apple.com")
        source = Path(apns.__file__).read_text()
        self.assertNotIn("sandbox", source)
        self.assertNotIn("development.push", source)
        credentials = apns.Credentials(KEY_ID, TEAM_ID, BUNDLE_ID, self.key_file)
        client = apns.Apns(credentials, self.clock)
        self.assertEqual((client.host, client.port), ("api.push.apple.com", 443))

    def test_a_push_reaches_apns_over_http2_with_a_signed_token_and_only_the_kind_and_id(self):
        fake = self.start()
        results = self.client().send([TOKEN_A, TOKEN_B], PUSH)
        self.assertEqual(results, [apns.Result("sent", ""), apns.Result("sent", "")])
        self.assertEqual(fake.state.connections, 1)
        first = fake.state.received[0]
        self.assertEqual(first.path, f"/3/device/{TOKEN_A}")
        self.assertEqual(first.headers[":method"], "POST")
        self.assertEqual(first.headers["apns-topic"], BUNDLE_ID)
        self.assertEqual(first.headers["apns-push-type"], "alert")
        self.assertEqual(first.headers["apns-priority"], "10")
        self.assertEqual(first.headers["apns-collapse-id"], "alert-42")
        self.assertEqual(int(first.headers["apns-expiration"]), int(self.clock.now) + 24 * 3600)
        self.assertEqual(first.claims, {"iss": TEAM_ID, "iat": int(self.clock.now)})
        self.assertEqual(first.key_id, KEY_ID)
        self.assertEqual(json.loads(first.body), {"aps": {"alert": {"title": "Google sign-in needed"}, "sound": "default", "thread-id": "sign_in_needed"},
                                                  "kind": "sign_in_needed", "alert_id": 42})
        self.assertEqual(fake.state.received[1].path, f"/3/device/{TOKEN_B}")

    def test_tokens_apns_reports_invalid_are_told_apart_from_other_failures(self):
        fake = self.start()
        fake.state.replies = {TOKEN_A: (410, "Unregistered"), TOKEN_B: (400, "BadDeviceToken"), TOKEN_C: (429, "TooManyRequests")}
        token_d, token_e, token_f = "d4" * 32, "e5" * 32, "f6" * 32
        fake.state.replies |= {token_d: (400, "DeviceTokenNotForTopic"), token_e: (400, "TopicDisallowed"), token_f: (500, "")}
        results = self.client().send([TOKEN_A, TOKEN_B, TOKEN_C, token_d, token_e, token_f], PUSH)
        self.assertEqual(results, [apns.Result("invalid_token", "Unregistered"), apns.Result("invalid_token", "BadDeviceToken"),
                                   apns.Result("failed", "TooManyRequests"), apns.Result("invalid_token", "DeviceTokenNotForTopic"),
                                   apns.Result("failed", "TopicDisallowed"), apns.Result("failed", "HTTP 500")])

    def test_an_expired_provider_token_is_signed_again_and_the_push_retried_once(self):
        fake = self.start()
        fake.state.expire_tokens = 1
        self.assertEqual(self.client().send([TOKEN_A], PUSH), [apns.Result("sent", "")])
        self.assertEqual(len(fake.state.received), 2)
        self.assertNotEqual(fake.state.received[0].headers["authorization"], fake.state.received[1].headers["authorization"])
        fake.state.expire_tokens = 2
        self.assertEqual(self.client().send([TOKEN_A], PUSH), [apns.Result("failed", "ExpiredProviderToken")])

    def test_a_token_signed_with_another_key_is_refused(self):
        fake = self.start()
        self.key_file = write_key(self.folder, new_key(), name="Other.p8")
        self.assertEqual(self.client().send([TOKEN_A], PUSH), [apns.Result("failed", "InvalidProviderToken")])
        self.assertIsNone(fake.state.received[0].claims)

    def test_the_signed_token_is_reused_for_45_minutes_then_signed_again(self):
        fake = self.start()
        client = self.client()
        client.send([TOKEN_A], PUSH)
        self.clock.now += 44 * 60
        client.send([TOKEN_A], PUSH)
        self.clock.now += 2 * 60
        client.send([TOKEN_A], PUSH)
        bearers = [received.headers["authorization"] for received in fake.state.received]
        self.assertEqual(bearers[0], bearers[1])
        self.assertNotEqual(bearers[1], bearers[2])
        self.assertEqual(fake.state.received[2].claims, {"iss": TEAM_ID, "iat": int(self.clock.now)})

    def test_a_token_that_is_not_hex_is_never_sent(self):
        fake = self.start()
        self.assertEqual(self.client().send(["../../3/device/x", TOKEN_A.upper(), "ab"], PUSH), [apns.Result("invalid_token", "BadDeviceToken")] * 3)
        self.assertEqual(fake.state.connections, 0)
        results = self.client().send(["zz", TOKEN_A], PUSH)
        self.assertEqual(results, [apns.Result("invalid_token", "BadDeviceToken"), apns.Result("sent", "")])
        self.assertEqual([received.path for received in fake.state.received], [f"/3/device/{TOKEN_A}"])

    def test_apns_unreachable_fails_every_push_and_logs_only_the_error_type(self):
        with socket.socket() as unused:
            unused.bind((HOST, 0))
            port = unused.getsockname()[1]
        with no_leaks(self, TOKEN_A, TOKEN_B) as watched, contextlib.redirect_stderr(io.StringIO()) as err:
            watched.add(pem_of(self.key).decode().splitlines()[1])
            results = self.client(port).send([TOKEN_A, TOKEN_B], PUSH)
        self.assertEqual(results, [apns.Result("failed", "Unreachable")] * 2)
        self.assertEqual(json.loads(err.getvalue()), {"event": "apns_unreachable", "error": "ConnectionRefusedError"})

    def test_a_server_without_http2_is_refused(self):
        self.fake.state.alpn = "http/1.1"
        self.start()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.client().send([TOKEN_A], PUSH), [apns.Result("failed", "Unreachable")])
        self.assertIn("StreamFailed", err.getvalue())

    def test_a_certificate_apns_cannot_prove_is_refused(self):
        self.start()
        client = self.client()
        client.cafile = None
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(client.send([TOKEN_A], PUSH), [apns.Result("failed", "Unreachable")])
        self.assertIn("SSLCertVerificationError", err.getvalue())
        self.assertEqual(self.fake.state.received, [])

    def test_a_connection_dropped_part_way_fails_only_the_pushes_not_yet_answered(self):
        fake = self.start()
        fake.state.drop_after = 1
        with contextlib.redirect_stderr(io.StringIO()):
            results = self.client().send([TOKEN_A, TOKEN_B, TOKEN_C], PUSH)
        self.assertEqual(results, [apns.Result("sent", ""), apns.Result("failed", "Unreachable"), apns.Result("failed", "Unreachable")])

    def test_an_unusable_key_fails_the_pushes_without_saying_why_in_the_log(self):
        self.start()
        self.key_file.chmod(0o644)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.client().send([TOKEN_A], PUSH), [apns.Result("failed", "KeyUnusable")])
        self.assertEqual(json.loads(err.getvalue()), {"event": "apns_key_unusable"})

    def test_nothing_private_is_printed_logged_or_raised_while_pushing(self):
        fake = self.start()
        fake.state.replies = {TOKEN_B: (410, "Unregistered")}
        fake.state.expire_tokens = 1
        key_text = pem_of(self.key).decode().splitlines()[1]
        with no_leaks(self, TOKEN_A, TOKEN_B, key_text) as watched:
            client = self.client()
            results = client.send([TOKEN_A, TOKEN_B], PUSH)
            watched.add(client.signer.token)
            fake.state.drop_after = 0
            results += client.send([TOKEN_A], PUSH)
            print(results, repr(client))
        self.assertEqual([result.outcome for result in results], ["sent", "invalid_token", "failed"])

    def test_a_payload_over_apples_limit_is_refused(self):
        with self.assertRaises(ValueError):
            apns.payload_of(apns.Push("failure", 1, "x" * 5000))

    def test_reasons_and_timestamps_are_read_only_when_they_are_well_formed(self):
        self.assertEqual(apns.reply_of(b'{"reason":"BadDeviceToken"}'), ("BadDeviceToken", None))
        self.assertEqual(apns.reply_of(b'{"reason":"Unregistered","timestamp":1800000000123}'), ("Unregistered", 1800000000.123))
        for body in (b"", b"not json", b"[]", b'{"reason": 5}', b'{"reason":"has spaces in it"}', b'{"reason":"' + b"x" * 65 + b'"}'):
            self.assertEqual(apns.reply_of(body), ("", None), body)
        for stamp in (b'"1800000000123"', b"true", b"-1", b"0", b"1.5", b"100000000000000"):
            self.assertEqual(apns.reply_of(b'{"reason":"Unregistered","timestamp":' + stamp + b"}"), ("Unregistered", None), stamp)

    def test_a_410_carries_when_apns_last_knew_the_token_valid(self):
        fake = self.start()
        fake.state.replies = {TOKEN_A: (410, "Unregistered"), TOKEN_B: (410, "ExpiredToken"), TOKEN_C: (400, "BadDeviceToken")}
        fake.state.unregistered_at = {TOKEN_A: 1_799_999_000_500, TOKEN_C: 1_799_999_000_500}
        results = self.client().send([TOKEN_A, TOKEN_B, TOKEN_C], PUSH)
        self.assertEqual(results, [apns.Result("invalid_token", "Unregistered", 1_799_999_000.5), apns.Result("invalid_token", "ExpiredToken", None),
                                   apns.Result("invalid_token", "BadDeviceToken", None)])


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def values(self, **changes: str) -> dict[str, str]:
        found = {"APNS_KEY_ID": KEY_ID, "APNS_TEAM_ID": TEAM_ID, "APNS_BUNDLE_ID": BUNDLE_ID, "APNS_KEY_FILE": str(self.folder / "AuthKey.p8")}
        return {name: value for name, value in (found | changes).items() if value}

    def test_apns_is_optional_and_off_when_nothing_is_set(self):
        settings = settings_from()
        self.assertFalse(config.apns_configured(settings))
        self.assertEqual(config.apns_problems(settings), [])
        self.assertEqual(apns.key_problems(settings), [])
        self.assertIsNone(apns.of(settings, Clock()))

    def test_the_four_settings_go_together(self):
        for missing in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID", "APNS_KEY_FILE"):
            settings = settings_from(**self.values(**{missing: ""}))
            self.assertEqual(len(config.apns_problems(settings)), 1, missing)
            self.assertIn("go together", config.apns_problems(settings)[0])
            self.assertIsNone(apns.of(settings, Clock()))
            self.assertEqual(apns.key_problems(settings), [])

    def test_ids_and_the_key_path_must_look_like_apples(self):
        cases = {"APNS_KEY_ID": ("abc123defg", "ABC123", "ABC123DEFGH"), "APNS_TEAM_ID": ("team456xyz", "TEAM 456XY"),
                 "APNS_BUNDLE_ID": ("darkroom", "com.example.", "com example.app", "com." + "a" * 160), "APNS_KEY_FILE": ("AuthKey.p8",)}
        for name, bad in cases.items():
            for value in bad:
                found = config.apns_problems(settings_from(**self.values(**{name: value})))
                self.assertEqual(len(found), 1, value)
                self.assertIn(name, found[0])

    def test_a_complete_setup_gives_a_client(self):
        write_key(self.folder)
        settings = settings_from(**self.values())
        self.assertEqual(config.apns_problems(settings), [])
        self.assertEqual(apns.key_problems(settings), [])
        client = apns.of(settings, Clock())
        assert client is not None
        self.assertEqual(client.credentials, apns.Credentials(KEY_ID, TEAM_ID, BUNDLE_ID, self.folder / "AuthKey.p8"))
        self.assertEqual((client.host, client.port), ("api.push.apple.com", 443))
        self.assertIsNone(client.cafile)

    def test_the_key_file_must_be_a_private_unencrypted_p256_key(self):
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        p384 = ec.generate_private_key(ec.SECP384R1())
        encrypted = new_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"pass-phrase"))
        cases = [
            (None, "cannot be read" if False else "is not a file"),
            (b"not a key at all", "not an unencrypted APNs auth key"),
            (encrypted, "not an unencrypted APNs auth key"),
            (rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), "P-256"),
            (pem_of(p384), "P-256"),
            (b"x" * (17 * 1024), "too large"),
        ]
        for content, expected in cases:
            path = self.folder / "AuthKey.p8"
            path.unlink(missing_ok=True)
            if content is not None:
                write_key(self.folder, content)
            found = apns.key_problems(settings_from(**self.values()))
            self.assertEqual(len(found), 1, expected)
            self.assertIn(expected, found[0])

    def test_a_key_others_can_read_a_folder_or_a_link_is_refused(self):
        key_file = write_key(self.folder, mode=0o640)
        self.assertIn("chmod 600", apns.key_problems(settings_from(**self.values()))[0])
        key_file.chmod(0o600)
        link = self.folder / "link.p8"
        link.symlink_to(key_file)
        self.assertIn("is not a file", apns.key_problems(settings_from(**self.values(APNS_KEY_FILE=str(link))))[0])
        self.assertIn("is not a file", apns.key_problems(settings_from(**self.values(APNS_KEY_FILE=str(self.folder))))[0])
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertEqual(apns.key_problems(settings_from(**self.values())), ["APNS_KEY_FILE cannot be read"])

    def test_check_config_reports_apns_problems_without_showing_the_key(self):
        canary = "canary-key-material-0451"
        write_key(self.folder, f"-----BEGIN PRIVATE KEY-----\n{canary}\n-----END PRIVATE KEY-----\n".encode())
        base = {"DARKROOM_PHOTOS_ROOT": str(self.folder), "DARKROOM_DATA_DIR": str(self.folder), "DARKROOM_SECRET_KEY": "k",
                "DARKROOM_PUBLIC_URL": "https://d.example", "DARKROOM_ALLOW_NO_AUTH": "1"}
        with no_leaks(self, canary), contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.dict(os.environ, base | self.values(), clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                self.assertEqual(run.main(["check-config"]), 1)
        self.assertEqual(err.getvalue(), "config: APNS_KEY_FILE is not an unencrypted APNs auth key (.p8)\n")
        write_key(self.folder)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with mock.patch.dict(os.environ, base | self.values(), clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                self.assertEqual(run.main(["check-config"]), 0)
        self.assertIn("Configuration looks complete.", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.dict(os.environ, base | self.values(APNS_TEAM_ID=""), clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                self.assertEqual(run.main(["check-config"]), 1)
        self.assertIn("go together", err.getvalue())


class Http2Tests(ApnsWorld):
    def test_posting_on_a_closed_connection_is_refused(self):
        self.start()
        connection = tls.Http2(HOST, self.fake.port, cafile=str(self.fake.cafile))
        with self.assertRaises(tls.StreamFailed):
            connection.post("/", {}, b"")
        with connection:
            reply = connection.post(f"/3/device/{TOKEN_A}", {}, b"{}")
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body), {"reason": "InvalidProviderToken"})
        connection.close()

    def test_the_socket_is_closed_when_the_http2_preface_cannot_be_sent(self):
        fake = self.start()
        fake.state.close_after_handshake = True
        connection = tls.Http2(HOST, fake.port, cafile=str(fake.cafile))
        opened: list[socket.socket] = []
        real_flush = connection.flush

        def flush_after_the_server_hung_up() -> None:
            if connection.sock is not None and not opened:
                opened.append(connection.sock)
                self.assertTrue(fake.state.closed_after_handshake.wait(5))
                time.sleep(0.2)
            real_flush()

        with mock.patch.object(connection, "flush", side_effect=flush_after_the_server_hung_up), self.assertRaises(OSError):
            connection.__enter__()
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].fileno(), -1)
        self.assertIsNone(connection.sock)


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
            with self.assertRaises(OSError), tls.Http2(HOST, port, timeout=2):
                pass
        self.assertEqual(len(made), 2)
        for context in made:
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)


if __name__ == "__main__":
    unittest.main()
