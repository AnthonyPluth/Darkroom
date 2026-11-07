import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom import config
from tests.shared import settings_from

ROOT = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    def test_every_variable_config_reads_is_in_env_example(self):
        read = set(re.findall(r'env(?:_list|_flag)?\("([A-Z0-9_]+)"', (ROOT / "darkroom/config.py").read_text()))
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.MULTILINE))
        self.assertTrue(read)
        self.assertEqual(sorted(read - documented), [])

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = config.load()
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.oidc_allowed_groups, ("parents",))
        self.assertFalse(settings.allow_no_auth)

    def test_lists_are_split_and_trimmed(self):
        with mock.patch.dict(os.environ, {"OIDC_ALLOWED_GROUPS": " a , b ,,c"}, clear=True):
            self.assertEqual(config.load().oidc_allowed_groups, ("a", "b", "c"))

    def test_an_incomplete_setup_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "OIDC_ISSUER": "https://id.example"}
            with mock.patch.dict(os.environ, values, clear=True):
                found = config.problems(config.load())
        self.assertEqual(len(found), 4)
        self.assertTrue(any("OIDC_ISSUER" in line for line in found))
        self.assertTrue(any("sign-in is not set up" in line for line in found))

    def test_a_complete_setup_has_no_problems(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "https://d.example/",
                      "OIDC_ISSUER": "https://id.example/", "OIDC_CLIENT_ID": "darkroom"}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(config.problems(config.load()), [])
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": "k", "DARKROOM_PUBLIC_URL": "http://localhost:8000",
                      "DARKROOM_ALLOW_NO_AUTH": "1"}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(config.problems(config.load()), [])


class LocalHostTests(unittest.TestCase):
    def test_local_addresses(self):
        for host in ("localhost", "127.0.0.1", "::1", "[::1]", "192.168.1.50", "10.0.0.9", "100.101.102.103", "nas.local", "homeserver", "box.tail1234.ts.net", "LOCALHOST"):
            self.assertTrue(config.local_host(host), host)

    def test_public_addresses(self):
        for host in ("darkroom.example", "8.8.8.8", "2001:4860:4860::8888", "", "evil.com"):
            self.assertFalse(config.local_host(host), host)


class AuthProblemTests(unittest.TestCase):
    def test_oidc_needs_a_public_url_and_someone_allowed(self):
        found = config.auth_problems(settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="x", OIDC_ALLOWED_GROUPS="", DARKROOM_PUBLIC_URL="darkroom.example"))
        self.assertEqual(len(found), 2)
        self.assertTrue(any("DARKROOM_PUBLIC_URL" in line for line in found))
        self.assertTrue(any("OIDC_ALLOWED_GROUPS" in line for line in found))

    def test_http_is_only_for_local_addresses(self):
        remote = settings_from(OIDC_ISSUER="http://id.example", OIDC_CLIENT_ID="x", DARKROOM_PUBLIC_URL="http://darkroom.example")
        self.assertEqual(len(config.auth_problems(remote)), 2)
        local = settings_from(OIDC_ISSUER="http://192.168.1.2:1411", OIDC_CLIENT_ID="x", DARKROOM_PUBLIC_URL="http://192.168.1.3:8000")
        self.assertEqual(config.auth_problems(local), [])

    def test_no_auth_only_without_oidc_and_on_a_local_address(self):
        self.assertEqual(config.auth_problems(settings_from(DARKROOM_ALLOW_NO_AUTH="1")), [])
        self.assertTrue(config.no_auth_allowed(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_HOST="192.168.1.4")))
        with_oidc = settings_from(DARKROOM_ALLOW_NO_AUTH="1", OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="x", DARKROOM_PUBLIC_URL="https://d.example")
        self.assertEqual(len(config.refuses_to_start(with_oidc)), 1)
        self.assertFalse(config.no_auth_allowed(with_oidc))
        everywhere = settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_HOST="0.0.0.0")
        self.assertEqual(len(config.refuses_to_start(everywhere)), 1)
        self.assertIn("0.0.0.0", config.auth_problems(everywhere)[0])
        self.assertFalse(config.no_auth_allowed(everywhere))
        self.assertEqual(config.refuses_to_start(settings_from(DARKROOM_HOST="0.0.0.0")), [])
        self.assertEqual(config.auth_problems(settings_from(DARKROOM_HOST="0.0.0.0")), ["sign-in is not set up: set OIDC_ISSUER and OIDC_CLIENT_ID (or DARKROOM_ALLOW_NO_AUTH=1 on a local address) so someone can use the web UI"])

    def test_check_config_reports_a_vnc_url_the_viewer_cannot_use(self):
        for value in ("https://[::1", "javascript:alert(1)", "//browser.example/vnc", "https://user:secret@browser.example/"):
            with self.subTest(value=value):
                found = config.vnc_url_problems(settings_from(VNC_URL=value))
                self.assertEqual(len(found), 1)
                self.assertIn("VNC_URL", found[0])
                self.assertNotIn("secret", found[0])
                self.assertIn("VNC_URL", " ".join(config.problems(settings_from(VNC_URL=value))))
                self.assertEqual(config.refuses_to_start(settings_from(VNC_URL=value)), [])
        for value in ("", "https://browser.example:6080/vnc.html?autoconnect=1", "http://127.0.0.1:6080/"):
            with self.subTest(value=value):
                self.assertEqual(config.vnc_url_problems(settings_from(VNC_URL=value)), [])

    def test_the_pixel_folder_cap_is_whole_gigabytes_and_at_least_one_or_darkroom_refuses_to_start(self):
        self.assertEqual(settings_from().pixel_folder_cap_gb, 50)
        self.assertEqual(settings_from(PIXEL_FOLDER_CAP_GB="20").pixel_folder_cap_gb, 20)
        self.assertEqual(config.refuses_to_start(settings_from(PIXEL_FOLDER_CAP_GB="20")), [])
        for value in ("0", "-5", "50GB", "1.5", "\u00b2"):
            with self.subTest(value=value):
                bad = settings_from(PIXEL_FOLDER_CAP_GB=value)
                self.assertEqual(bad.pixel_folder_cap_gb, 0)
                self.assertIn("PIXEL_FOLDER_CAP_GB", " ".join(config.refuses_to_start(bad)))
                self.assertIn("PIXEL_FOLDER_CAP_GB", " ".join(config.problems(bad)))


if __name__ == "__main__":
    unittest.main()
