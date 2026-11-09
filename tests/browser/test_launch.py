import unittest
from pathlib import Path

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

    def test_the_sandbox_stays_on(self):
        options = launch_options(PROFILE, DOWNLOADS, "chrome")
        self.assertFalse([arg for arg in options["args"] if "sandbox" in arg])
        self.assertNotIn("chromium_sandbox", options)

    def test_nothing_is_shared_between_calls(self):
        first = launch_options(PROFILE, DOWNLOADS, None)
        first["args"].append("--x")
        first["viewport"]["width"] = 1
        second = launch_options(PROFILE, DOWNLOADS, None)
        self.assertEqual(second["args"], ["--disable-blink-features=AutomationControlled"])
        self.assertEqual(second["viewport"]["width"], 1280)


if __name__ == "__main__":
    unittest.main()
