import io
import os
import shutil
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
from darkroom_browser.context import DownloadInterrupted, PartClick

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


class GoogleStandIn(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "")
        path, _, query = self.path.partition("?")
        if host == TAKEOUT and path == "/takeout/download":
            number = int(query.split("i=")[1].split("&")[0]) + 1
            if "late=1" in query:
                time.sleep(LATE_SECONDS)
            self.send_response(302)
            self.send_header("Location", f"https://{GOOGLES_DOWNLOAD_HOST}/download/takeout-20261102T031000Z-00{number}.zip?{query}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if host == GOOGLES_DOWNLOAD_HOST and path.startswith("/download/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{path.removeprefix("/download/")}"')
            self.send_header("Content-Length", str(len(ZIP)))
            self.end_headers()
            self.wfile.write(ZIP)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return None


class ChromiumDownloadTests(unittest.TestCase):
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
        server = ThreadingHTTPServer(("127.0.0.1", 0), GoogleStandIn)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
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
            if self.browser.download_started():
                return
            self.browser.page.wait_for_timeout(100)
        self.fail("no download event reached the worker")

    def click_that_times_out(self, selector):
        with self.assertRaises(NoDownloadInTime):
            self.browser.download_to(selector, self.target)
        self.wait_for_a_download()

    def test_a_click_on_a_takeout_link_that_redirects_to_googles_download_host_downloads(self):
        path = self.browser.download_to("#same", self.target)
        self.assertEqual((path.name, path.read_bytes()), ("takeout-20261102T031000Z-001.zip", ZIP))
        self.assertFalse(self.browser.download_started())

    def test_a_redirected_download_that_the_click_started_in_a_new_tab_is_recorded_and_kept(self):
        self.click_that_times_out("#popup")
        path = self.browser.collect_download(self.target, PartClick("#popup", PART_1, 1))
        self.assertEqual((path.name, path.read_bytes()), ("takeout-20261102T031000Z-001.zip", ZIP))
        self.assertFalse(self.browser.download_started())

    def assert_refused_and_swept(self, part):
        with self.assertRaisesRegex(DownloadInterrupted, "not this part's"):
            self.browser.collect_download(self.target, part)
        self.assertEqual(list((self.root / "_inbox" / ".downloads" / "3").iterdir()), [])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_redirected_download_of_another_part_is_refused_and_swept(self):
        self.click_that_times_out("#other")
        self.assert_refused_and_swept(PartClick("#other", PART_1, 1))

    def test_a_download_the_job_did_not_click_for_is_refused_and_swept(self):
        self.browser.page.evaluate("document.getElementById('popup').click()")
        self.wait_for_a_download()
        self.assert_refused_and_swept(PartClick("#popup", PART_1, 1))


if __name__ == "__main__":
    unittest.main()
