import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom import config

ROOT = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    def test_every_variable_config_reads_is_in_env_example(self):
        read = set(re.findall(r'env(?:_list)?\("([A-Z0-9_]+)"', (ROOT / "darkroom/config.py").read_text()))
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.MULTILINE))
        self.assertTrue(read)
        self.assertEqual(sorted(read - documented), [])

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = config.load()
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.oidc_allowed_groups, ("parents",))
        self.assertEqual(settings.oidc_device_groups, ("family", "parents"))

    def test_lists_are_split_and_trimmed(self):
        with mock.patch.dict(os.environ, {"OIDC_ALLOWED_GROUPS": " a , b ,,c"}, clear=True):
            self.assertEqual(config.load().oidc_allowed_groups, ("a", "b", "c"))

    def test_an_incomplete_setup_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "OIDC_ISSUER": "https://id.example", "HA_URL": "https://ha.example"}
            with mock.patch.dict(os.environ, values, clear=True):
                found = config.problems(config.load())
        self.assertEqual(len(found), 4)
        self.assertTrue(any("OIDC_ISSUER" in line for line in found))
        self.assertTrue(any("HA_URL" in line for line in found))

    def test_home_assistant_needs_all_three_settings_or_none(self):
        with tempfile.TemporaryDirectory() as folder:
            base = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example"}
            partial = {**base, "HA_URL": "https://ha.example", "HA_TOKEN": "t"}
            with mock.patch.dict(os.environ, partial, clear=True):
                self.assertTrue(any("HA_NOTIFY_SERVICE" in line for line in config.problems(config.load())))
            with mock.patch.dict(os.environ, {**partial, "HA_NOTIFY_SERVICE": "mobile_app_x"}, clear=True):
                self.assertEqual(config.problems(config.load()), [])

    def test_a_complete_setup_has_no_problems(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example/"}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(config.problems(config.load()), [])


if __name__ == "__main__":
    unittest.main()
