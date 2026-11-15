import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import select
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select as query

from darkroom import config, oidc
from darkroom.domain import browser_jobs
from darkroom.server import routes, viewer
from darkroom.server.common import CSP, VIEWER_CSP
from darkroom.storage import db
from darkroom.storage.models import Alert
from darkroom_init import plan, viewer_auth
from tests.browser.test_init import handed
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, settings_from, start_server
from tests.test_browser_jobs import add_account

SECRET = "viewer-relay-secret-canary-0123456789abcdef"
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEY = base64.b64encode(b"relay-test-key16").decode()
OIDC = {"OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom", "OIDC_ALLOWED_GROUPS": "parents", "DARKROOM_SECRET_KEY": "viewer-relay-key-0123456789abcdef"}
BINARY, CLOSE, PING, PONG = 0x2, 0x8, 0x9, 0xA


def frame(opcode, payload=b"", masked=True):
    size = len(payload)
    bit = 0x80 if masked else 0
    head = bytes([0x80 | opcode]) + (bytes([bit | size]) if size < 126 else bytes([bit | 126]) + size.to_bytes(2, "big") if size < 65536 else bytes([bit | 127]) + size.to_bytes(8, "big"))
    if not masked:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))


def exactly(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def read_frame(stream):
    first, second = exactly(stream, 2)
    size = second & 0x7F
    if size == 126:
        size = int.from_bytes(exactly(stream, 2), "big")
    elif size == 127:
        size = int.from_bytes(exactly(stream, 8), "big")
    key = exactly(stream, 4) if second & 0x80 else b""
    payload = exactly(stream, size)
    if key:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return first & 0x0F, payload


def read_head(stream):
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        chunk = stream.recv(1)
        if not chunk:
            break
        data += chunk
    return data


def ended(stream, seconds=3.0):
    stream.settimeout(seconds)
    try:
        while stream.recv(4096):
            pass
    except (ConnectionResetError, BrokenPipeError):
        return True
    except TimeoutError:
        return False
    return True


class FakeWebsockify:
    def __init__(self, test, close_after_hello=False, refuse=False):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.close_after_hello, self.refuse = close_after_hello, refuse
        self.heads, self.pongs, self.ends = [], [], []
        self.connections = 0
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        test.addCleanup(self.listener.close)

    def serve(self):
        while True:
            try:
                stream, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.talk, args=(stream,), daemon=True).start()

    def talk(self, stream):
        with stream:
            head = read_head(stream)
            self.heads.append(head.decode("latin-1"))
            lines = head.decode("latin-1").split("\r\n")
            fields = {name.lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:] if line)}
            expected = "Basic " + base64.b64encode(f"darkroom:{SECRET}".encode()).decode()
            if self.refuse or fields.get("authorization") != expected:
                stream.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                return
            accept = base64.b64encode(hashlib.sha1(fields["sec-websocket-key"].encode() + GUID).digest()).decode()
            stream.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\nX-Fake: upstream\r\n\r\n".encode())
            stream.sendall(frame(BINARY, b"RFB 003.008\n", masked=False))
            if self.close_after_hello:
                return
            self.echo(stream)

    def echo(self, stream):
        while True:
            try:
                opcode, payload = read_frame(stream)
            except (EOFError, OSError):
                self.ends.append("eof")
                return
            if opcode == PONG:
                self.pongs.append(payload)
            elif opcode == CLOSE:
                stream.sendall(frame(CLOSE, payload, masked=False))
                self.ends.append("close")
                return
            else:
                stream.sendall(frame(opcode, payload, masked=False))


class IdleCuttingProxy:
    def __init__(self, test, target_port, idle):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.target_port, self.idle = target_port, idle
        self.cut = False
        threading.Thread(target=self.serve, daemon=True).start()
        test.addCleanup(self.listener.close)

    def serve(self):
        with contextlib.suppress(OSError):
            client, _ = self.listener.accept()
            server = socket.create_connection(("127.0.0.1", self.target_port))
            heard = time.monotonic()
            with client, server:
                while True:
                    ready, _, _ = select.select([client, server], [], [], 0.05)
                    if server in ready:
                        data = server.recv(65536)
                        if not data:
                            return
                        heard = time.monotonic()
                        client.sendall(data)
                    if client in ready:
                        data = client.recv(65536)
                        if not data:
                            return
                        server.sendall(data)
                    if time.monotonic() - heard > self.idle:
                        self.cut = True
                        return


class RelayCase(unittest.TestCase):
    secret = SECRET

    def setUp(self):
        self.engine = own_database(self)
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name)
        (self.files / "app").mkdir()
        (self.files / "vnc.html").write_text('<!doctype html><script type="module" src="app/ui.js"></script>')
        (self.files / "app/ui.js").write_text("export {}")
        settings = dataclasses.replace(settings_from(**OIDC), viewer_secret=self.secret, viewer_files=self.files)
        self.base, self.settings = start_server(self, settings, self.engine)
        self.port = int(self.base.rsplit(":", 1)[1])
        self.origin = self.base
        moment = time.time()
        with db.session(self.engine, writing=True) as opened:
            oidc.remember_user(opened, oidc.Identity("u1", "jo@example.test", "Jo", ("parents",), True), moment)
            self.session = oidc.new_session(opened, self.settings, "u1", moment)
        self.fake = FakeWebsockify(self)
        patch = mock.patch.object(config, "VIEWER_PORT", self.fake.port)
        patch.start()
        self.addCleanup(patch.stop)

    def raw(self, target="/viewer/websockify", extra=None, cookie=True, origin=None, port=None):
        stream = socket.create_connection(("127.0.0.1", port or self.port), timeout=5)
        self.addCleanup(stream.close)
        fields = {"Host": f"127.0.0.1:{self.port}", "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": KEY, "Sec-WebSocket-Version": "13",
                  "Origin": self.origin if origin is None else origin}
        if cookie:
            fields["Cookie"] = f"darkroom_session={self.session}"
        fields = {name: value for name, value in (fields | (extra or {})).items() if value is not None}
        stream.sendall((f"GET {target} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in fields.items()) + "\r\n").encode())
        return stream

    def opened(self, **options):
        stream = self.raw(**options)
        head = read_head(stream)
        self.assertTrue(head.startswith(b"HTTP/1.1 101 Switching Protocols\r\n"), head)
        self.assertEqual(read_frame(stream), (BINARY, b"RFB 003.008\n"))
        return stream, head

    def status_of(self, stream):
        head = read_head(stream)
        return int(head.split(b" ", 2)[1]) if head else 0

    def get(self, path, cookie=True):
        return fetch(self.base, "GET", path, {"Cookie": f"darkroom_session={self.session}"} if cookie else {})


class RelayRefusalTests(RelayCase):
    def test_the_route_is_a_session_route_that_matches_any_depth_below_viewer(self):
        found = {f"{route.method} {route.path}": route.auth for route in routes.ROUTES}
        self.assertEqual(found["GET /viewer/{path*}"], "session")
        self.assertEqual(routes.match("GET", "/viewer/app/styles/base.css")[1], {"path": "app/styles/base.css"})
        self.assertEqual(routes.match("GET", "/viewer/vnc.html")[1], {"path": "vnc.html"})
        self.assertIsNone(routes.match("GET", "/viewer/"))
        self.assertIsNone(routes.match("GET", "/viewer"))

    def test_without_a_session_both_the_page_and_the_socket_are_401_and_nothing_reaches_the_upstream(self):
        self.assertEqual(self.get("/viewer/vnc.html", cookie=False)[0], 401)
        self.assertEqual(self.status_of(self.raw(cookie=False)), 401)
        self.assertEqual(self.status_of(self.raw(extra={"Cookie": "darkroom_session=not-a-session-token"})), 401)
        self.assertEqual(self.fake.connections, 0)

    def test_an_upgrade_from_another_origin_or_without_one_is_403(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": None}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.status_of(self.raw(extra=headers)), 403)
        self.assertEqual(self.fake.connections, 0)

    def test_a_request_cannot_name_its_own_target(self):
        decoy = FakeWebsockify(self)
        refused = [
            f"http://127.0.0.1:{decoy.port}/viewer/websockify",
            f"//127.0.0.1:{decoy.port}/viewer/websockify",
            "/viewer/../websockify",
            "/viewer/%2e%2e/websockify",
            "/viewer/.hidden",
            "/viewer/app//ui.js",
            "/viewer/vnc.html",
            f"/viewer/127.0.0.1:{decoy.port}",
        ]
        for target in refused:
            with self.subTest(target=target):
                self.assertEqual(self.status_of(self.raw(target=target)), 404)
        self.assertEqual((self.fake.connections, decoy.connections), (0, 0))
        named = {"Host": f"127.0.0.1:{decoy.port}", "X-Forwarded-Host": f"127.0.0.1:{decoy.port}", "Forwarded": f"host=127.0.0.1:{decoy.port}",
                 "Authorization": "Basic ZXZpbDpldmls", "X-Target": f"127.0.0.1:{decoy.port}"}
        stream, _ = self.opened(target=f"/viewer/websockify?host=127.0.0.1&port={decoy.port}&token=other", extra=named, origin=f"http://127.0.0.1:{decoy.port}")
        stream.close()
        self.assertEqual(decoy.connections, 0)
        (head,) = self.fake.heads
        lines = head.split("\r\n")
        self.assertEqual(lines[0], "GET /websockify HTTP/1.1")
        self.assertIn(f"Host: 127.0.0.1:{self.fake.port}", lines)
        self.assertNotIn("ZXZpbDpldmls", head)
        self.assertFalse({"cookie", "x-forwarded-host", "forwarded", "x-target", "origin"} & {line.partition(":")[0].lower() for line in lines[1:]})
        self.assertNotIn(self.session, head)

    def test_a_malformed_upgrade_is_400(self):
        for extra in ({"Sec-WebSocket-Key": "short"}, {"Sec-WebSocket-Version": "8"}, {"Connection": "keep-alive"}, {"Sec-WebSocket-Protocol": "binary;evil"},
                      {"Sec-WebSocket-Protocol": "bin ary"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.status_of(self.raw(extra=extra)), 400)
        self.assertEqual(self.fake.connections, 0)


class RelayFileTests(RelayCase):
    def test_the_viewer_page_is_served_from_the_fixed_folder_under_the_viewer_policy(self):
        status, headers, body = self.get("/viewer/vnc.html?autoconnect=1&host=evil.example&port=1")
        self.assertEqual((status, body), (200, (self.files / "vnc.html").read_bytes()))
        self.assertEqual(headers["Content-Security-Policy"], VIEWER_CSP)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        status, headers, _ = self.get("/viewer/app/ui.js")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(self.fake.connections, 0)

    def test_missing_files_links_out_of_the_folder_and_large_files_are_404(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        outside = Path(elsewhere.name) / "outside.js"
        outside.write_text("export {}")
        (self.files / "away.js").symlink_to(outside)
        (self.files / "big.js").write_bytes(b"0" * (viewer.MAX_FILE + 1))
        for path in ("/viewer/gone.js", "/viewer/away.js", "/viewer/big.js", "/viewer/app"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)


class LockedViewerTests(RelayCase):
    secret = ""

    def test_without_the_secret_the_viewer_is_not_part_of_this_setup(self):
        status, _, body = self.get("/viewer/vnc.html")
        self.assertEqual((status, json.loads(body)), (404, {"error": viewer.NOT_HERE}))
        self.assertEqual(self.status_of(self.raw()), 404)
        self.assertEqual(self.fake.connections, 0)


class RelayUpstreamTests(RelayCase):
    def test_the_upstreams_101_reply_is_forwarded_untouched_and_frames_flow_both_ways(self):
        with no_leaks(self, SECRET, self.session):
            stream, head = self.opened()
            self.assertIn(b"X-Fake: upstream\r\n", head)
            payload = os.urandom(70000)
            stream.sendall(frame(BINARY, payload))
            self.assertEqual(read_frame(stream), (BINARY, payload))
            stream.sendall(frame(CLOSE, b"\x03\xe8"))
            self.assertEqual(read_frame(stream), (CLOSE, b"\x03\xe8"))
            self.assertTrue(ended(stream))

    def test_a_reconnect_after_a_clean_close_works(self):
        first, _ = self.opened()
        first.sendall(frame(CLOSE, b"\x03\xe8"))
        self.assertEqual(read_frame(first), (CLOSE, b"\x03\xe8"))
        self.assertTrue(ended(first))
        second, _ = self.opened()
        second.sendall(frame(BINARY, b"again"))
        self.assertEqual(read_frame(second), (BINARY, b"again"))
        self.assertEqual((self.fake.connections, self.fake.ends[:1]), (2, ["close"]))

    def test_an_upstream_that_hangs_up_is_closed_cleanly_towards_the_viewer(self):
        self.fake.close_after_hello = True
        stream, _ = self.opened()
        self.assertEqual(read_frame(stream), (CLOSE, (1001).to_bytes(2, "big")))
        self.assertTrue(ended(stream))

    def test_a_viewer_that_goes_away_ends_the_upstream_too(self):
        stream, _ = self.opened()
        stream.close()
        deadline = time.monotonic() + 3
        while not self.fake.ends and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.fake.ends, ["eof"])

    def test_a_viewer_that_stops_answering_pings_is_dropped_on_both_sides(self):
        with mock.patch.object(viewer, "PING_SECONDS", 0.1):
            stream, _ = self.opened()
            started = time.monotonic()
            self.assertTrue(ended(stream, 3.0))
        self.assertGreater(time.monotonic() - started, 0.25)
        deadline = time.monotonic() + 3
        while not self.fake.ends and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.fake.ends, ["eof"])

    def test_an_upstream_that_is_not_running_or_refuses_is_502(self):
        with socket.create_server(("127.0.0.1", 0)) as closed:
            unused = closed.getsockname()[1]
        with mock.patch.object(config, "VIEWER_PORT", unused):
            self.assertEqual(self.status_of(self.raw()), 502)
        self.fake.refuse = True
        with no_leaks(self, SECRET, self.session) as watched:
            stream = self.raw()
            head = read_head(stream)
            watched.add("Unauthorized")
        self.assertTrue(head.startswith(b"HTTP/1.0 502"), head)


class IdleTests(RelayCase):
    def talk_through_proxy(self, ping_seconds, idle, hold):
        proxy = IdleCuttingProxy(self, self.port, idle)
        with mock.patch.object(viewer, "PING_SECONDS", ping_seconds):
            stream, _ = self.opened(port=proxy.port)
            stream.settimeout(idle * 4)
            until = time.monotonic() + hold
            pings = 0
            try:
                while time.monotonic() < until:
                    opcode, payload = read_frame(stream)
                    if opcode == PING:
                        pings += 1
                        stream.sendall(frame(PONG, payload))
                stream.sendall(frame(BINARY, b"still here"))
                while (reply := read_frame(stream))[0] == PING:
                    stream.sendall(frame(PONG, reply[1]))
            except (EOFError, OSError):
                return proxy, pings, None
        return proxy, pings, reply

    def test_an_idle_viewer_is_kept_alive_past_a_proxys_read_timeout(self):
        proxy, pings, reply = self.talk_through_proxy(ping_seconds=0.15, idle=0.5, hold=2.0)
        self.assertEqual(reply, (BINARY, b"still here"))
        self.assertFalse(proxy.cut)
        self.assertGreaterEqual(pings, 5)
        self.assertGreaterEqual(len(self.fake.pongs), 5)

    def test_without_the_pings_the_same_proxy_cuts_the_idle_viewer(self):
        proxy, pings, reply = self.talk_through_proxy(ping_seconds=60.0, idle=0.5, hold=2.0)
        self.assertEqual((reply, pings, proxy.cut), (None, 0, True))


class FramesTests(unittest.TestCase):
    def test_frame_boundaries_are_followed_across_chunks_and_lengths(self):
        stream = frame(BINARY, b"x" * 5, masked=False) + frame(BINARY, b"y" * 300, masked=False) + frame(BINARY, b"z" * 70000, masked=False) + frame(BINARY, b"m" * 9)
        for step in (1, 3, 7, 1000, len(stream)):
            with self.subTest(step=step):
                frames = viewer.Frames()
                boundaries = 0
                for start in range(0, len(stream), step):
                    frames.feed(stream[start:start + step])
                    boundaries += frames.at_boundary()
                self.assertTrue(frames.at_boundary())
                self.assertFalse(frames.closing)
                self.assertGreaterEqual(boundaries, 1)

    def test_a_close_frame_is_noticed_and_a_partial_frame_is_not_a_boundary(self):
        frames = viewer.Frames()
        frames.feed(frame(BINARY, b"abc", masked=False)[:3])
        self.assertFalse(frames.at_boundary())
        frames.feed(b"bc" + frame(CLOSE, b"\x03\xe8", masked=False))
        self.assertTrue(frames.at_boundary() and frames.closing)
        frames.feed(frame(0x0, b"", masked=False))
        self.assertTrue(frames.at_boundary())


class ViewerLinkTests(unittest.TestCase):
    def settings(self, **values):
        secret = values.pop("secret", SECRET)
        return dataclasses.replace(settings_from(**values), viewer_secret=secret)

    def test_vnc_url_is_used_unchanged_when_set(self):
        for given in ("https://browser.example/vnc.html?autoconnect=1", "javascript:alert(1)"):
            with self.subTest(given=given):
                settings = self.settings(VNC_URL=given, DARKROOM_PUBLIC_URL="https://darkroom.example")
                self.assertEqual(config.viewer_link(settings), given)

    def test_without_vnc_url_the_link_goes_through_the_relay(self):
        relayed = "https://darkroom.example/viewer/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=3000&path=viewer/websockify"
        self.assertEqual(config.viewer_link(self.settings(DARKROOM_PUBLIC_URL="https://darkroom.example/")), relayed)
        self.assertEqual(config.viewer_link(self.settings(DARKROOM_PUBLIC_URL="https://home.example/darkroom")),
                         "https://home.example/darkroom/viewer/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=3000&path=darkroom/viewer/websockify")

    def test_without_the_secret_or_a_public_address_there_is_no_link(self):
        self.assertIsNone(config.viewer_link(self.settings(DARKROOM_PUBLIC_URL="https://darkroom.example", secret="")))
        self.assertIsNone(config.viewer_link(self.settings(DARKROOM_PUBLIC_URL="https://darkroom.example", secret="short")))
        self.assertIsNone(config.viewer_link(self.settings()))

    def test_the_alert_links_through_the_relay_only_when_vnc_url_is_unset(self):
        engine = own_database(self)
        cases = ((self.settings(DARKROOM_PUBLIC_URL="https://darkroom.example"), "https://darkroom.example/viewer/vnc.html?"),
                 (self.settings(DARKROOM_PUBLIC_URL="https://darkroom.example", VNC_URL="https://browser.example/vnc.html"), "https://browser.example/vnc.html"),
                 (self.settings(DARKROOM_PUBLIC_URL="https://darkroom.example", secret=""), None))
        for number, (settings, link) in enumerate(cases):
            with self.subTest(link=link), db.session(engine, writing=True) as opened:
                account = add_account(opened, f"jo{number}.takeout@example.test", f"jo{number}")
                job = browser_jobs.enqueue_once(opened, account, "connect", 1.0)
                browser_jobs.claim(opened, 2.0)
                browser_jobs.waiting(opened, settings, job.id, "sign_in", 3.0)
                alert = opened.execute(query(Alert).where(Alert.account_id == account)).scalars().one()
                self.assertTrue(alert.link.startswith(link) if link else alert.link is None, alert.link)

    def test_the_view_offers_the_relay_and_the_app_page_may_frame_it(self):
        engine = own_database(self)
        with tempfile.TemporaryDirectory() as folder:
            settings = dataclasses.replace(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_PHOTOS_ROOT=folder), viewer_secret=SECRET)
            base, _ = start_server(self, settings, engine)
            status, _, body = fetch(base, "GET", "/api/browser/view")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["vnc_url"].startswith(f"{base}/viewer/vnc.html?"))


class ViewerSecretTests(unittest.TestCase):
    def setUp(self):
        config.handed_tokens.clear()
        self.addCleanup(config.handed_tokens.clear)

    def test_the_app_reads_the_viewer_secret_once_from_its_pipe(self):
        fd = handed(SECRET + "\n")
        with no_leaks(self, SECRET), mock.patch.dict(os.environ, {"DARKROOM_VIEWER_SECRET_FD": str(fd)}, clear=True):
            self.assertEqual(config.load().viewer_secret, SECRET)
            self.assertRaises(OSError, os.fstat, fd)
            self.assertEqual(config.load().viewer_secret, SECRET)
        for given in ({}, {"DARKROOM_VIEWER_SECRET_FD": "1"}, {"DARKROOM_VIEWER_SECRET_FD": "x"}, {"DARKROOM_VIEWER_SECRET": SECRET}):
            with self.subTest(given=given), mock.patch.dict(os.environ, given, clear=True):
                self.assertEqual(config.load().viewer_secret, "")

    def test_the_relay_and_the_init_agree_on_the_upstream(self):
        viewer_spec = next(spec for spec in plan.specs({}, bool, bool) if spec.name == "viewer")
        self.assertIn(f"127.0.0.1:{config.VIEWER_PORT}", viewer_spec.argv)
        self.assertIn(f"--web={config.VIEWER_FILES}", viewer_spec.argv)
        self.assertEqual(config.VIEWER_USER, viewer_auth.VIEWER_USER)
        self.assertEqual(VIEWER_CSP, CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'"))


if __name__ == "__main__":
    unittest.main()
