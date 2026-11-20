import io
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from darkroom_browser import live
from darkroom_browser.context import PartClick
from darkroom_browser.stall import IN_PROGRESS_SUFFIX, StallWatch, Verdict

try:
    from playwright.sync_api import TimeoutError as NoDownloadInTime
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    NoDownloadInTime = TimeoutError

REQUIRED = os.environ.get("DARKROOM_CHROMIUM_TESTS") == "1"
EXECUTABLE = os.environ.get("DARKROOM_CHROMIUM_EXECUTABLE")
TAKEOUT = "takeout.google.com"
GOOGLES_DOWNLOAD_HOST = "takeout-download.usercontent.google.com"
PART_1 = f"https://{TAKEOUT}/takeout/download?j=export-1&i=0&user=made-up"
PART_2 = f"https://{TAKEOUT}/takeout/download?j=export-1&i=1&user=made-up"
PAGE_URL = f"https://{TAKEOUT}/manage/archive/export-1"


def made_up_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Takeout/Google Photos/a.bin", b"x" * 2048)
    return buffer.getvalue()


ZIP = made_up_zip()
CLICK_WAIT_MS = 3000
LATE_SECONDS = 5.0
PAGE = f"""<html><body><a id="same" href="{PART_1}">part 1 in this tab</a>
<a id="popup" href="{PART_1}&amp;late=1" target="_blank">part 1 in a new tab, answered late</a>
<a id="other" href="{PART_2}&amp;late=1" target="_blank">part 2 in a new tab, answered late</a></body></html>"""


NAMES = {1: "takeout-20261102T031000Z-001.zip", 2: "takeout-20261102T031000Z-002.zip"}
DOWNLOADS = {f"/download/{name}": f'attachment; filename="{name}"' for name in NAMES.values()}
LINKS = {PART_1: (1, False), f"{PART_1}&late=1": (1, True), f"{PART_2}&late=1": (2, True)}
REDIRECTS = {link.removeprefix(f"https://{TAKEOUT}"): (f"https://{GOOGLES_DOWNLOAD_HOST}/download/{NAMES[number]}?{link.partition('?')[2]}", late)
             for link, (number, late) in LINKS.items()}


class GoogleStandIn(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "")
        if host == TAKEOUT and self.path in REDIRECTS:
            location, late = REDIRECTS[self.path]
            if late:
                time.sleep(LATE_SECONDS)
            self.answer(302, {"Location": location, "Content-Length": "0"}, b"")
        elif host == GOOGLES_DOWNLOAD_HOST and self.path.partition("?")[0] in DOWNLOADS:
            disposition = DOWNLOADS[self.path.partition("?")[0]]
            self.answer(200, {"Content-Type": "application/zip", "Content-Disposition": disposition, "Content-Length": str(len(ZIP))}, ZIP)
        elif host == TAKEOUT and self.path == PAGE_URL.removeprefix(f"https://{TAKEOUT}"):
            body = PAGE.encode()
            self.answer(200, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))}, body)
        else:
            self.answer(404, {"Content-Length": "0"}, b"")

    def answer(self, status, headers, body):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return None


class ChromiumWorld(unittest.TestCase):
    handler = GoogleStandIn

    def setUp(self):
        if sync_playwright is None:
            self.unavailable("Playwright is not installed here")
        if shutil.which("openssl") is None:
            self.unavailable("there is no openssl to make the stand-in's certificate")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.port = self.serve_tls(self.root / "tls")
        self.browser = None
        failure = None
        for executable in (None, EXECUTABLE or shutil.which("google-chrome")):
            try:
                self.browser = self.open(executable)
                break
            except Exception as error:
                failure = error
        if self.browser is None:
            self.unavailable(f"Chromium did not start: {str(failure).splitlines()[0][:200]}")
        self.addCleanup(self.browser.close)
        self.short_wait = mock.patch.object(live, "DEFAULT_TIMEOUT_MS", CLICK_WAIT_MS)
        self.short_wait.start()
        self.addCleanup(self.short_wait.stop)
        self.browser.goto(PAGE_URL)
        self.target = self.root / "partial"

    def serve_tls(self, folder):
        folder.mkdir()
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=stand-in",
                        "-addext", f"subjectAltName=DNS:{TAKEOUT},DNS:{GOOGLES_DOWNLOAD_HOST}",
                        "-keyout", str(folder / "key.pem"), "-out", str(folder / "cert.pem")], check=True, capture_output=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(folder / "cert.pem", folder / "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def open(self, executable):
        original = live.launch_options
        rules = f"MAP {TAKEOUT} 127.0.0.1:{self.port}, MAP {GOOGLES_DOWNLOAD_HOST} 127.0.0.1:{self.port}"

        def headless(profile_dir, downloads, channel):
            options = original(profile_dir, downloads, None)
            options.update(headless=True, chromium_sandbox=False, ignore_https_errors=True)
            options["args"] = [*options["args"], f"--host-resolver-rules={rules}", "--no-proxy-server"]
            if executable is not None:
                options["executable_path"] = executable
            return options

        with mock.patch.object(live, "launch_options", headless), mock.patch.object(live, "choose_channel", lambda requested: None):
            return live.open_context(self.root / "_browser" / "3" / "profile", self.root)

    def unavailable(self, reason):
        if REQUIRED:
            raise AssertionError(f"DARKROOM_CHROMIUM_TESTS=1 but {reason}")
        raise unittest.SkipTest(reason)

    def wait_for_a_download(self):
        for _ in range(150):
            if self.browser.arrived:
                return
            self.browser.page.wait_for_timeout(100)
        self.fail("no download event reached the worker")

    def click_that_times_out(self, selector):
        with self.assertRaises(NoDownloadInTime):
            self.browser.start_download(selector, PartClick(selector, PART_1, 1))
        self.wait_for_a_download()

    def until_finished(self, handles):
        watches = {handle: StallWatch(time.monotonic(), stall_seconds=30.0) for handle in handles}
        overlapped = False
        verdicts = {}
        deadline = time.monotonic() + 60
        while len(verdicts) < len(handles) and time.monotonic() < deadline:
            states = {handle: self.browser.download_state(handle) for handle in handles if handle not in verdicts}
            overlapped = overlapped or sum(1 for state in states.values() if state.in_progress) == 2
            for handle, state in states.items():
                verdict = watches[handle].judge(time.monotonic(), state)
                if verdict is not Verdict.WAIT:
                    verdicts[handle] = verdict
            self.browser.page.wait_for_timeout(200)
        return verdicts, overlapped

    def landed(self, handle):
        self.assertEqual(self.until_finished([handle])[0], {handle: Verdict.FINISHED})
        return self.browser.keep_download(handle, self.target)


class ChromiumDownloadTests(ChromiumWorld):
    def test_a_click_on_a_takeout_link_that_redirects_to_googles_download_host_downloads(self):
        path = self.landed(self.browser.start_download("#same", PartClick("#same", PART_1, 1)))
        self.assertEqual((path.name, path.read_bytes()), ("takeout-20261102T031000Z-001.zip", ZIP))
        self.assertEqual(self.browser.arrived, [])

    def test_a_redirected_download_that_the_click_started_in_a_new_tab_is_recorded_and_kept(self):
        self.click_that_times_out("#popup")
        handle = self.browser.late_download(PartClick("#popup", PART_1, 1))
        self.assertIsNotNone(handle)
        path = self.landed(handle)
        self.assertEqual((path.name, path.read_bytes()), ("takeout-20261102T031000Z-001.zip", ZIP))
        self.assertEqual(self.browser.arrived, [])

    def assert_refused_and_cancelled(self, part):
        self.assertIsNone(self.browser.late_download(part))
        self.assertEqual(self.browser.drop_strays(), 1)
        self.assertEqual(self.browser.arrived, [])
        self.assertIsNone(self.browser.late_download(part))
        self.assertFalse(self.target.exists())

    def test_a_redirected_download_of_another_part_is_refused_and_cancelled(self):
        self.click_that_times_out("#other")
        self.assert_refused_and_cancelled(PartClick("#other", PART_1, 1))

    def test_a_download_the_job_did_not_click_for_is_refused_and_cancelled(self):
        self.browser.page.evaluate("document.getElementById('popup').click()")
        self.wait_for_a_download()
        self.assert_refused_and_cancelled(PartClick("#popup", PART_1, 1))


STAGGER_LINKS = {number: f"https://{TAKEOUT}/takeout/download?j=export-1&i={number - 1}&user=made-up" for number in (1, 2)}
STAGGER_NAMES = {number: f"takeout-20261102T031000Z-00{number}.zip" for number in (1, 2)}
APART_SECONDS = 1.5
CHUNKS = 24
CHUNK_SECONDS = 0.15


def part_zip(number):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(f"Takeout/Google Photos/{number}.bin", bytes([number]) * (CHUNKS * 16 * 1024))
    return buffer.getvalue()


STAGGER_ZIPS = {number: part_zip(number) for number in (1, 2)}
STAGGER_REDIRECTS = {link.removeprefix(f"https://{TAKEOUT}"): f"https://{GOOGLES_DOWNLOAD_HOST}/download/{STAGGER_NAMES[number]}?{link.partition('?')[2]}"
                     for number, link in STAGGER_LINKS.items()}
STAGGER_PAGE = "<html><body>" + "".join(f'<a id="part{n}" href="{link}">part {n}</a>' for n, link in STAGGER_LINKS.items()) + "</body></html>"


class SlowGoogle(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "")
        path = self.path.partition("?")[0]
        if host == TAKEOUT and self.path in STAGGER_REDIRECTS:
            self.answer(302, {"Location": STAGGER_REDIRECTS[self.path], "Content-Length": "0"})
        elif host == GOOGLES_DOWNLOAD_HOST and path.startswith("/download/"):
            number = next(n for n, name in STAGGER_NAMES.items() if path.endswith(name))
            body = STAGGER_ZIPS[number]
            self.answer(200, {"Content-Type": "application/zip", "Content-Disposition": f'attachment; filename="{STAGGER_NAMES[number]}"', "Content-Length": str(len(body))})
            step = len(body) // CHUNKS + 1
            for start in range(0, len(body), step):
                try:
                    self.wfile.write(body[start:start + step])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(CHUNK_SECONDS)
        elif host == TAKEOUT and path == "/manage/archive/export-1":
            body = STAGGER_PAGE.encode()
            self.answer(200, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))})
            self.wfile.write(body)
        else:
            self.answer(404, {"Content-Length": "0"})

    def answer(self, status, headers):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def log_message(self, *args):
        return None


class SlowGoogleHeaderTests(unittest.TestCase):
    def setUp(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowGoogle)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def ask(self, path):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.sendall(f"GET {path} HTTP/1.0\r\nHost: {TAKEOUT}\r\n\r\n".encode())
            answer = b""
            while chunk := connection.recv(4096):
                answer += chunk
        head = answer.partition(b"\r\n\r\n")[0].decode()
        return head.split("\r\n")

    def test_the_redirect_is_one_of_the_fixed_addresses(self):
        lines = self.ask(STAGGER_LINKS[1].removeprefix(f"https://{TAKEOUT}"))
        self.assertIn(" 302 ", lines[0])
        self.assertIn(f"Location: {STAGGER_REDIRECTS[STAGGER_LINKS[1].removeprefix(f'https://{TAKEOUT}')]}", lines)

    def test_a_request_carrying_a_line_break_cannot_add_a_header(self):
        for injected in ("%0D%0ASet-Cookie:%20split=1", "%0ASet-Cookie:%20split=1", "&x=%0D%0A%0D%0Abody"):
            with self.subTest(injected=injected):
                lines = self.ask(STAGGER_LINKS[1].removeprefix(f"https://{TAKEOUT}") + injected)
                self.assertIn(" 404 ", lines[0])
                self.assertFalse([line for line in lines if line.lower().startswith(("set-cookie", "location"))])


class ChromiumStaggerTests(ChromiumWorld):
    handler = SlowGoogle

    def test_two_downloads_one_after_the_other_never_overlap_and_each_lands_as_its_own_part(self):
        first = self.browser.start_download("#part1", PartClick("#part1", STAGGER_LINKS[1], 1))
        self.browser.page.wait_for_timeout(int(APART_SECONDS * 1000))
        self.assertTrue(self.browser.download_state(first).in_progress)
        verdicts, _ = self.until_finished([first])
        self.assertEqual(verdicts, {first: Verdict.FINISHED})
        path = self.browser.keep_download(first, self.target / "1")
        self.assertEqual((path.name, path.read_bytes()), (STAGGER_NAMES[1], STAGGER_ZIPS[1]))
        self.browser.goto(PAGE_URL)
        second = self.browser.start_download("#part2", PartClick("#part2", STAGGER_LINKS[2], 2))
        self.assertNotEqual(first, second)
        self.browser.page.wait_for_timeout(int(APART_SECONDS * 1000))
        self.assertTrue(self.browser.download_state(second).in_progress)
        self.assertFalse(self.browser.download_state(first).in_progress)
        in_progress = [entry.name for entry in (self.root / "_inbox" / ".downloads" / "3").iterdir() if entry.name.endswith(IN_PROGRESS_SUFFIX)]
        self.assertEqual(in_progress, [second + IN_PROGRESS_SUFFIX])
        verdicts, _ = self.until_finished([second])
        self.assertEqual(verdicts, {second: Verdict.FINISHED})
        path = self.browser.keep_download(second, self.target / "2")
        self.assertEqual((path.name, path.read_bytes()), (STAGGER_NAMES[2], STAGGER_ZIPS[2]))
        self.assertEqual(list((self.root / "_inbox" / ".downloads" / "3").iterdir()), [])

    def test_dropping_strays_while_a_part_downloads_leaves_it_running_to_the_end(self):
        first = self.browser.start_download("#part1", PartClick("#part1", STAGGER_LINKS[1], 1))
        self.browser.page.wait_for_timeout(int(APART_SECONDS * 1000))
        self.assertTrue(self.browser.download_state(first).in_progress)
        self.assertEqual(self.browser.drop_strays(), 0)
        verdicts, _ = self.until_finished([first])
        self.assertEqual(verdicts, {first: Verdict.FINISHED})
        self.assertEqual(self.browser.keep_download(first, self.target).read_bytes(), STAGGER_ZIPS[1])

    def test_cancelling_one_download_leaves_the_other_running_to_the_end(self):
        first = self.browser.start_download("#part1", PartClick("#part1", STAGGER_LINKS[1], 1))
        self.browser.page.wait_for_timeout(int(APART_SECONDS * 1000))
        second = self.browser.start_download("#part2", PartClick("#part2", STAGGER_LINKS[2], 2))
        self.browser.cancel_download(first)
        verdicts, _ = self.until_finished([second])
        self.assertEqual(verdicts, {second: Verdict.FINISHED})
        path = self.browser.keep_download(second, self.target)
        self.assertEqual(path.read_bytes(), STAGGER_ZIPS[2])
        self.assertEqual(list((self.root / "_inbox" / ".downloads" / "3").iterdir()), [])
        with self.assertRaises(KeyError):
            self.browser.keep_download(first, self.target)


if __name__ == "__main__":
    unittest.main()
