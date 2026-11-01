import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from darkroom_browser import capture, monitoring, scrub
from tests.browser.test_runner import FakeBrowser
from tests.privacy import no_leaks

ROOT = Path(__file__).resolve().parent.parent.parent

SNAPSHOT = """<html><head>
<script>window.__token = "session-token-canary-abcdefghijklmnopqrstuvwxyz0123456789";</script>
<meta name="csrf-token" content="csrf-canary-value">
</head><body data-auth-token="data-token-canary-1234" data-authuser="0" authuser='3'>
<h1>Manage your exports</h1>
<form action="https://accounts.google.com/signin?continue=https://takeout.google.com&flowName=canary-flow">
<input type="hidden" name="at" value="hidden-at-canary-value">
<input name="identifier" value=visible-canary-value>
<a href="https://takeout.google.com/manage?authuser=0&token=link-token-canary">Downloads</a>
cookie: SID=sid-cookie-canary-1234; __Secure-1PSID=secure-sid-canary-5678
jwt eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl here
opaque AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAopaquecanary
</form></body></html>"""

CANARIES = ("session-token-canary", "csrf-canary-value", "data-token-canary", "hidden-at-canary", "visible-canary-value", "link-token-canary",
            "sid-cookie-canary", "secure-sid-canary", "c2lnbmF0dXJl", "opaquecanary", "canary-flow")


class ScrubTests(unittest.TestCase):
    def test_scripts_form_values_tokens_cookies_and_query_strings_are_removed(self):
        scrubbed = scrub.scrub_html(SNAPSHOT)
        for canary in CANARIES:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, scrubbed)
        self.assertIn("<h1>Manage your exports</h1>", scrubbed)
        self.assertIn("Downloads</a>", scrubbed)
        self.assertIn('<script>[removed]</script>', scrubbed)
        self.assertIn('https://accounts.google.com/signin?[removed]', scrubbed)
        self.assertTrue(scrubbed.startswith("<!-- scrubbed by Darkroom"))

    def test_ordinary_markup_is_kept(self):
        page = "<html><body><p class='note'>Export 2 of 3 is ready.</p><a href='https://takeout.google.com/manage'>Manage</a></body></html>"
        self.assertEqual(scrub.scrub_html(page), scrub.NOTICE + page)

    def test_the_tool_writes_a_scrubbed_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "page.html"
            source.write_text(SNAPSHOT)
            result = subprocess.run([sys.executable, str(ROOT / "tools/scrub_snapshot.py"), str(source)], capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            copied = (Path(folder) / "page.scrubbed.html").read_text()
            self.assertNotIn("session-token-canary", copied)
            self.assertIn("Manage your exports", copied)
            sys.path.insert(0, str(ROOT / "tools"))
            import scrub_snapshot

            target = Path(folder) / "out.html"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scrub_snapshot.main([str(source), str(target)]), 0)
            self.assertNotIn("csrf-canary-value", target.read_text())


class CaptureTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "_browser" / "1" / "failures" / "9"

    def test_both_files_are_written_and_the_page_is_scrubbed(self):
        with no_leaks(self, "session-token-canary"):
            captured = capture.capture(FakeBrowser(SNAPSHOT), self.folder)
        self.assertEqual(captured, capture.Captured(self.folder, True, True))
        self.assertEqual((self.folder / "screenshot.png").read_bytes(), b"\x89PNG")
        page = (self.folder / "page.html").read_text()
        for canary in CANARIES:
            self.assertNotIn(canary, page)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["page.html", "screenshot.png"])

    def test_a_broken_screenshot_still_saves_the_page(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            captured = capture.capture(FakeBrowser(broken=True), self.folder)
        self.assertEqual(captured, capture.Captured(self.folder, False, True))
        self.assertIn("screenshot failed", err.getvalue())
        self.assertFalse((self.folder / "screenshot.png").exists())

    def test_nothing_to_capture(self):
        self.assertIsNone(capture.capture(None, self.folder))
        self.assertFalse(self.folder.exists())

        class Dead(FakeBrowser):
            def content(self):
                raise RuntimeError("target closed")

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(capture.capture(Dead(broken=True), self.folder))


class WorkerMonitoringTests(unittest.TestCase):
    def test_log_scrubs_tokens_and_writes_one_line(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            monitoring.log("claimed", job=3, note="Authorization: Bearer worker-token-canary", url="https://d.example/x?token=t-canary", token="raw-canary")
        line = err.getvalue()
        self.assertEqual(line.count("\n"), 1)
        for canary in ("worker-token-canary", "t-canary", "raw-canary"):
            self.assertNotIn(canary, line)
        self.assertIn('"job": 3', line)
        self.assertEqual(monitoring.scrub("DARKROOM_BROWSER_TOKEN=abc-canary x"), "DARKROOM_BROWSER_TOKEN=[removed] x")
        self.assertEqual(monitoring.scrub("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"), "[removed]")


if __name__ == "__main__":
    unittest.main()
