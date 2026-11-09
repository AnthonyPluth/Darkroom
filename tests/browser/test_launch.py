import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from darkroom_browser.launch import choose_channel, launch_options

PROFILE = Path("/photos/_browser/3/profile")
DOWNLOADS = Path("/photos/_inbox/.downloads/3")


class ChannelTests(unittest.TestCase):
    def test_chrome_is_used_when_installed_on_amd64(self):
        for machine in ("x86_64", "AMD64", "amd64"):
            with self.subTest(machine=machine):
                self.assertEqual(choose_channel("chrome", machine, True), "chrome")

    def test_the_channel_defaults_are_case_and_space_tolerant(self):
        self.assertEqual(choose_channel(" Chrome ", "x86_64", True), "chrome")

    def test_bundled_chromium_when_chrome_is_not_installed(self):
        self.assertIsNone(choose_channel("chrome", "x86_64", False))

    def test_bundled_chromium_on_other_architectures(self):
        for machine in ("aarch64", "arm64", "armv7l"):
            with self.subTest(machine=machine):
                self.assertIsNone(choose_channel("chrome", machine, True))

    def test_bundled_chromium_when_asked_for(self):
        for requested in ("", "chromium", "bundled", "msedge"):
            with self.subTest(requested=requested):
                self.assertIsNone(choose_channel(requested, "x86_64", True))

    def test_the_machine_and_the_install_are_looked_up_when_not_given(self):
        self.assertIn(choose_channel("chrome"), (None, "chrome"))


class OptionTests(unittest.TestCase):
    def test_the_options_with_chrome(self):
        options = launch_options(PROFILE, DOWNLOADS, "chrome")
        self.assertEqual(options, {
            "user_data_dir": str(PROFILE),
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
            "accept_downloads": True,
            "downloads_path": str(DOWNLOADS),
            "ignore_default_args": ["--enable-automation"],
            "args": ["--disable-blink-features=AutomationControlled"],
            "channel": "chrome",
        })

    def test_no_channel_key_for_bundled_chromium(self):
        options = launch_options(PROFILE, DOWNLOADS, None)
        self.assertNotIn("channel", options)
        self.assertEqual(options["ignore_default_args"], ["--enable-automation"])
        self.assertEqual(options["args"], ["--disable-blink-features=AutomationControlled"])

    def test_the_worker_adds_no_sandbox_switch_of_its_own(self):
        options = launch_options(PROFILE, DOWNLOADS, "chrome")
        self.assertFalse([arg for arg in options["args"] if "sandbox" in arg])

    def test_nothing_is_shared_between_calls(self):
        first = launch_options(PROFILE, DOWNLOADS, None)
        first["args"].append("--x")
        first["viewport"]["width"] = 1
        second = launch_options(PROFILE, DOWNLOADS, None)
        self.assertEqual(second["args"], ["--disable-blink-features=AutomationControlled"])
        self.assertEqual(second["viewport"]["width"], 1280)


class LaunchError(Exception):
    pass


class FakePlaywright:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = []
        self.stopped = 0
        self.chromium = self
        self.page = mock.Mock()
        self.context = mock.Mock(pages=[self.page])

    def launch_persistent_context(self, **options):
        self.calls.append(options)
        failure = self.failures.pop(0)
        if failure is not None:
            raise failure
        return self.context

    def start(self):
        return self

    def stop(self):
        self.stopped += 1


class OpenContextTests(unittest.TestCase):
    def open(self, fake, requested="chrome"):
        from darkroom_browser import live

        module = types.SimpleNamespace(sync_playwright=lambda: fake)
        stderr = io.StringIO()
        with (tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {"playwright.sync_api": module, "playwright": types.SimpleNamespace(sync_api=module)}),
              mock.patch.dict("os.environ", {"BROWSER_CHANNEL": requested}), mock.patch.object(live, "choose_channel", lambda r: "chrome" if r == "chrome" else None),
              contextlib.redirect_stderr(stderr)):
            try:
                return live.open_context(Path(directory) / "_browser" / "3" / "profile"), stderr.getvalue()
            except Exception as error:
                return error, stderr.getvalue()

    def test_chrome_launches_without_a_retry(self):
        fake = FakePlaywright([None])
        result, log = self.open(fake)
        self.assertEqual([call.get("channel") for call in fake.calls], ["chrome"])
        self.assertEqual(result.page, fake.page)
        self.assertEqual(fake.stopped, 0)
        self.assertEqual(log, "")

    def test_a_failed_chrome_is_logged_and_chromium_is_used(self):
        fake = FakePlaywright([LaunchError("secret path /photos/_browser/3"), None])
        result, log = self.open(fake)
        self.assertEqual([call.get("channel") for call in fake.calls], ["chrome", None])
        self.assertEqual(result.page, fake.page)
        self.assertEqual(fake.stopped, 0)
        self.assertEqual(json.loads(log), {"event": "chrome_launch_failed", "error": "LaunchError", "channel": "chrome"})
        self.assertNotIn("secret", log)

    def test_both_failing_stops_playwright_and_raises_the_last_error(self):
        fake = FakePlaywright([LaunchError("chrome"), ValueError("chromium")])
        result, _ = self.open(fake)
        self.assertIsInstance(result, ValueError)
        self.assertEqual(fake.stopped, 1)

    def test_bundled_chromium_failing_is_not_retried(self):
        fake = FakePlaywright([LaunchError("x")])
        result, log = self.open(fake, requested="chromium")
        self.assertIsInstance(result, LaunchError)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.stopped, 1)
        self.assertEqual(log, "")


if __name__ == "__main__":
    unittest.main()
