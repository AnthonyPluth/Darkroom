import io
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from darkroom_browser import live
from darkroom_browser.context import DownloadInterrupted

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

REQUIRED = os.environ.get("DARKROOM_CHROMIUM_TESTS") == "1"
EXECUTABLE = os.environ.get("DARKROOM_CHROMIUM_EXECUTABLE")
PART_1 = "https://takeout.google.com/takeout/download?j=export-1&i=0&user=made-up"
PART_2 = "https://takeout.google.com/takeout/download?j=export-1&i=1&user=made-up"


def made_up_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Takeout/Google Photos/a.bin", b"x" * 2048)
    return buffer.getvalue()


ZIP = made_up_zip()
PAGE = f"""<html><body><a id="local" href="/part.zip">local</a>
<a id="popup" href="{PART_1}" target="_blank">part 1 in a new tab</a>
<a id="other" href="{PART_2}" target="_blank">part 2 in a new tab</a></body></html>"""


class LocalServer(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/part.zip":
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="takeout-20261102T031000Z-001.zip"')
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


def takeout_download(route):
    route.fulfill(status=200, body=ZIP, headers={"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="takeout-20261102T031000Z-001.zip"'})


class ChromiumDownloadTests(unittest.TestCase):
    def setUp(self):
        if sync_playwright is None:
            self.unavailable("Playwright is not installed here")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
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
        self.browser.context.route("https://takeout.google.com/takeout/download**", takeout_download)
        self.browser.goto(f"http://127.0.0.1:{self.server.server_address[1]}/page")
        self.target = self.root / "partial"

    def open(self, executable):
        original = live.launch_options

        def headless(profile_dir, downloads, channel):
            options = original(profile_dir, downloads, None)
            options.update(headless=True, chromium_sandbox=False)
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
        for _ in range(50):
            if self.browser.download_started():
                return
            self.browser.page.wait_for_timeout(100)
        self.fail("no download event reached the worker")

    def test_a_click_still_downloads_with_every_tab_watched(self):
        path = self.browser.download_to("#local", self.target)
        self.assertEqual(path.name, "takeout-20261102T031000Z-001.zip")
        self.assertEqual(path.read_bytes(), ZIP)
        self.assertFalse(self.browser.download_started())

    def test_a_download_that_starts_in_a_new_tab_without_the_click_waiting_is_recorded_and_kept(self):
        self.browser.page.evaluate("document.getElementById('popup').click()")
        self.wait_for_a_download()
        path = self.browser.collect_download(self.target, PART_1)
        self.assertEqual(path.read_bytes(), ZIP)

    def test_a_download_of_another_part_is_refused_and_swept(self):
        self.browser.page.evaluate("document.getElementById('other').click()")
        self.wait_for_a_download()
        with self.assertRaises(DownloadInterrupted):
            self.browser.collect_download(self.target, PART_1)
        self.assertEqual(list((self.root / "_inbox" / ".downloads" / "3").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
