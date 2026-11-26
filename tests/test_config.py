import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom import config
from tests.shared import settings_from

ROOT = Path(__file__).resolve().parent.parent


def check_config(case: unittest.TestCase, extra: dict[str, str], *canaries: str) -> tuple[int, str, str]:
    import contextlib
    import io

    import run
    from darkroom.providers import exiftool
    from tests.privacy import no_leaks

    with tempfile.TemporaryDirectory() as folder:
        values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_PUBLIC_URL": "https://d.example", "DARKROOM_ALLOW_NO_AUTH": "1"} | extra
        with no_leaks(case, *canaries) if canaries else contextlib.nullcontext(), contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
            code = run.main(["check-config"])
    return code, out.getvalue(), err.getvalue()


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
        self.assertFalse(settings.managed_release)

    def test_managed_release_needs_an_explicit_opt_in(self):
        for value in ("", "0", "unexpected", "1"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DARKROOM_MANAGED_RELEASE": value}, clear=True):
                self.assertEqual(config.load().managed_release, value == "1")

    def test_the_browser_root_is_unset_unless_given_and_must_be_absolute(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.load().browser_root)
        with mock.patch.dict(os.environ, {"DARKROOM_BROWSER_ROOT": "/data/browser"}, clear=True):
            self.assertEqual(config.load().browser_root, Path("/data/browser"))
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_BROWSER_ROOT": "browser"}
            with mock.patch.dict(os.environ, values, clear=True):
                self.assertIn("DARKROOM_BROWSER_ROOT must be an absolute path: browser", config.problems(config.load()))

    def test_a_browser_root_inside_the_photo_share_is_a_problem_except_its_browser_folder(self):
        inside = "DARKROOM_BROWSER_ROOT must not be DARKROOM_PHOTOS_ROOT, a folder inside it other than its _browser folder, or a folder that holds it"
        with tempfile.TemporaryDirectory() as top, tempfile.TemporaryDirectory() as appdata:
            share = f"{top}/photos"
            os.mkdir(share)
            os.symlink(share, f"{appdata}/linked-share")
            os.symlink(f"{share}/jo", f"{appdata}/linked-person")
            for root, refused in ((share, True), (f"{share}/jo/originals", True), (f"{share}/_browser/x", True), (f"{share}/_inbox", True), (f"{share}/1", True),
                                  (top, True), ("/", True), (f"{appdata}/linked-share", True), (f"{appdata}/linked-person", True),
                                  (f"{share}/_browser", False), (f"{appdata}/browser", False)):
                values = {"DARKROOM_PHOTOS_ROOT": share, "DARKROOM_DATA_DIR": appdata, "DARKROOM_BROWSER_ROOT": root}
                with self.subTest(root=root), mock.patch.dict(os.environ, values, clear=True):
                    settings = config.load()
                    self.assertEqual(inside in config.problems(settings), refused)
                    self.assertEqual(inside in config.refuses_to_start(settings), refused)

    def test_lists_are_split_and_trimmed(self):
        with mock.patch.dict(os.environ, {"OIDC_ALLOWED_GROUPS": " a , b ,,c"}, clear=True):
            self.assertEqual(config.load().oidc_allowed_groups, ("a", "b", "c"))

    def test_an_incomplete_setup_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "OIDC_ISSUER": "https://id.example"}
            with mock.patch.dict(os.environ, values, clear=True):
                found = config.problems(config.load())
        self.assertEqual(len(found), 3)
        self.assertFalse(any("SECRET_KEY" in line for line in found))
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


class NoAuthHostTests(unittest.TestCase):
    def test_the_default_hosts_are_addresses_localhost_and_the_configured_ones(self):
        settings = settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_HOST="192.168.1.4", DARKROOM_PUBLIC_URL="http://Tower.Local:8000/")
        self.assertEqual(config.no_auth_hosts(settings), {"localhost", "192.168.1.4", "tower.local"})
        for host in ("localhost", "localhost:8000", "LOCALHOST.", "127.0.0.1", "127.0.0.1:8000", "[::1]:8000", "::1", "10.1.2.3", "tower.local:8000", "TOWER.LOCAL."):
            self.assertTrue(config.no_auth_host_allowed(settings, host), host)
        for host in ("", " ", "evil.example", "evil.example:8000", "tower.local.evil.example", "localhost.evil.example", "127.0.0.1.evil.example",
                     "evil.example@127.0.0.1", "127.0.0.1/evil", "127.0.0.1\\evil", "127.0.0.1 evil", "nas", "darkroom.example"):
            self.assertFalse(config.no_auth_host_allowed(settings, host), host)

    def test_the_bind_host_counts_as_a_name(self):
        self.assertTrue(config.no_auth_host_allowed(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_HOST="nas"), "NAS:8000"))
        self.assertEqual(config.no_auth_hosts(settings_from(DARKROOM_HOST="::1")), {"localhost", "::1"})

    def test_more_host_names_are_configurable(self):
        settings = settings_from(DARKROOM_ALLOWED_HOSTS=" nas , Photos.Home.Arpa:8000,,")
        self.assertEqual(settings.allowed_hosts, ("nas", "Photos.Home.Arpa:8000"))
        self.assertEqual(config.no_auth_hosts(settings), {"localhost", "127.0.0.1", "nas", "photos.home.arpa"})
        for host in ("nas", "nas:8000", "photos.home.arpa", "PHOTOS.home.arpa:9000"):
            self.assertTrue(config.no_auth_host_allowed(settings, host), host)
        for host in ("nas.evil.example", "home.arpa", "evil.example"):
            self.assertFalse(config.no_auth_host_allowed(settings, host), host)
        self.assertEqual(config.allowed_hosts_problems(settings), [])

    def test_an_unusable_allowed_host_is_reported(self):
        for value in ("https://nas", "*.lan", "nas/photos", "jo@nas", "nas, ,https://tower.local"):
            with self.subTest(value=value):
                settings = settings_from(DARKROOM_ALLOWED_HOSTS=value)
                found = config.allowed_hosts_problems(settings)
                self.assertEqual(len(found), 1)
                self.assertIn("DARKROOM_ALLOWED_HOSTS", found[0])
                self.assertIn(found[0], config.problems(settings))
                self.assertFalse(config.no_auth_host_allowed(settings, value))
                self.assertNotIn(value.strip(), config.no_auth_hosts(settings))
        wildcard = settings_from(DARKROOM_ALLOWED_HOSTS="*.lan")
        self.assertEqual(config.no_auth_hosts(wildcard), {"localhost", "127.0.0.1"})
        for host in ("*.lan", "*.lan:8000", "nas.lan", "lan"):
            self.assertFalse(config.no_auth_host_allowed(wildcard, host), host)


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

    def test_usable_viewer_url_offers_only_an_http_address_with_a_valid_port_and_no_credentials(self):
        for value in ("https://[::1", "javascript:alert(1)", "//browser.example/vnc", "https://user:secret@browser.example/",
                      "https://browser.example:99999/vnc.html", "https://browser.example:port-canary/", "http://browser.example:-1/"):
            with self.subTest(value=value):
                self.assertIsNone(config.usable_viewer_url(value))
        for value in ("https://darkroom.example/viewer/vnc.html?autoconnect=1", "http://127.0.0.1:8000/"):
            with self.subTest(value=value):
                self.assertEqual(config.usable_viewer_url(value), value)

    def test_check_config_names_each_removed_browser_variable_as_ignored_without_its_value(self):
        for name in config.REMOVED_BROWSER_VARIABLES:
            canary = f"{name.lower()}-canary-0123456789abcdef0123456789abcdef"
            with self.subTest(name=name):
                code, out, err = check_config(self, {name: canary}, canary)
                self.assertEqual(code, 0, err)
                warning = [line for line in err.splitlines() if "set and ignored" in line]
                self.assertEqual(len(warning), 1, err)
                self.assertIn(f"config: warning: {name} is set and ignored", warning[0])
                for other in set(config.REMOVED_BROWSER_VARIABLES) - {name}:
                    self.assertNotIn(other, warning[0])
                self.assertNotIn(canary, err + out)
        code, _, err = check_config(self, {})
        self.assertEqual(code, 0, err)
        self.assertNotIn("set and ignored", err)

    def test_check_config_names_every_removed_browser_variable_set_together_once(self):
        values = {name: f"{name.lower()}-canary-0123456789abcdef0123456789abcdef" for name in config.REMOVED_BROWSER_VARIABLES}
        code, out, err = check_config(self, values, *values.values())
        self.assertEqual(code, 0, err)
        self.assertIn("config: warning: DARKROOM_BROWSER_TOKEN, DARKROOM_URL, VNC_URL, DARKROOM_VNC_SECRET are set and ignored", err)
        self.assertIn("remove these lines from .env", err)
        self.assertNotIn("_canary", err + out)

    def test_the_removed_browser_variables_change_no_setting(self):
        token = "an-environment-token-canary-0123456789abcdef0123"
        values = {"DARKROOM_BROWSER_TOKEN": token, "VNC_URL": "https://browser.example/vnc.html", "DARKROOM_URL": "http://darkroom:8000",
                  "DARKROOM_VNC_SECRET": "a-viewer-secret-canary-0123456789abcdef", "DARKROOM_PUBLIC_URL": "https://darkroom.example"}
        with mock.patch.dict(os.environ, values, clear=True):
            settings = config.load()
            self.assertEqual(config.ignored_browser_variables(), config.REMOVED_BROWSER_VARIABLES)
        self.assertEqual(settings.browser_token, "")
        self.assertFalse(hasattr(settings, "vnc_url"))
        self.assertIsNone(config.viewer_link(settings))
        self.assertNotIn(token, repr(settings))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.ignored_browser_variables(), ())
        for name in config.REMOVED_BROWSER_VARIABLES:
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "   "}, clear=True):
                self.assertEqual(config.ignored_browser_variables(), ())

    def test_serve_logs_the_removed_browser_variables_as_ignored_at_start_up_without_their_values(self):
        import contextlib
        import io

        import run
        from tests.privacy import no_leaks

        canary = "a-start-up-token-canary-0123456789abcdef0123"
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_BROWSER_TOKEN": canary, "VNC_URL": "https://vnc.canary.example"}
            with no_leaks(self, canary, "vnc.canary.example"), contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.dict(os.environ, values, clear=True), \
                    mock.patch.object(run, "migrate", side_effect=SystemExit(7)):
                with self.assertRaises(SystemExit):
                    run.main(["serve"])
        line = next(line for line in err.getvalue().splitlines() if "set and ignored" in line)
        self.assertIn('"level": "warning"', line)
        self.assertIn("DARKROOM_BROWSER_TOKEN, VNC_URL are set and ignored", line)
        self.assertNotIn(canary, err.getvalue())
        self.assertNotIn("vnc.canary.example", err.getvalue())

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


SWITCH_CANARY = "a-switch-value-canary-0123456789abcdef0123"


class RemovedSyncthingSwitchTests(unittest.TestCase):
    def test_only_a_value_that_used_to_turn_syncthing_off_counts_as_set_off(self):
        for value in ("0", "false", "no", "off", "yes", "true", " 0 ", SWITCH_CANARY):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DARKROOM_SYNCTHING": value}, clear=True):
                self.assertTrue(config.syncthing_switch_set_off())
        for given in ({}, {"DARKROOM_SYNCTHING": ""}, {"DARKROOM_SYNCTHING": "   "}, {"DARKROOM_SYNCTHING": "1"}, {"DARKROOM_SYNCTHING": " 1 "}):
            with self.subTest(given=given), mock.patch.dict(os.environ, given, clear=True):
                self.assertFalse(config.syncthing_switch_set_off())

    def test_the_warning_is_one_fixed_line_that_says_syncthing_is_always_on(self):
        self.assertEqual(config.SYNCTHING_SWITCH_IGNORED,
                         "DARKROOM_SYNCTHING is no longer supported and is ignored: Darkroom's Syncthing is always on in the one container, so remove it "
                         "(docs/deploy/one-container.md)")
        self.assertNotIn("\n", config.SYNCTHING_SWITCH_IGNORED)

    def test_check_config_warns_once_without_the_value_and_still_passes(self):
        code, out, err = check_config(self, {"DARKROOM_SYNCTHING": SWITCH_CANARY}, SWITCH_CANARY)
        self.assertEqual(code, 0, err)
        self.assertEqual([line for line in err.splitlines() if "DARKROOM_SYNCTHING" in line], [f"config: warning: {config.SYNCTHING_SWITCH_IGNORED}"])
        self.assertNotIn(SWITCH_CANARY, err + out)
        for given in ({}, {"DARKROOM_SYNCTHING": "1"}, {"DARKROOM_SYNCTHING": ""}):
            with self.subTest(given=given):
                code, out, err = check_config(self, given)
                self.assertEqual(code, 0, err)
                self.assertNotIn("DARKROOM_SYNCTHING", err + out)

    def test_the_switch_changes_no_setting(self):
        with mock.patch.dict(os.environ, {"DARKROOM_SYNCTHING": "0"}, clear=True):
            off = config.load()
        with mock.patch.dict(os.environ, {}, clear=True):
            unset = config.load()
        self.assertEqual(off, unset)
        self.assertFalse(hasattr(off, "syncthing"))

    def test_serve_logs_the_warning_once_at_start_up_without_the_value_and_carries_on(self):
        import contextlib
        import io

        import run
        from tests.privacy import no_leaks

        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SYNCTHING": SWITCH_CANARY}
            with no_leaks(self, SWITCH_CANARY), contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.dict(os.environ, values, clear=True), \
                    mock.patch.object(run, "migrate", side_effect=SystemExit(7)) as migrate:
                with self.assertRaises(SystemExit):
                    run.main(["serve"])
        migrate.assert_called_once()
        lines = [line for line in err.getvalue().splitlines() if "DARKROOM_SYNCTHING" in line]
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertEqual(json.loads(lines[0]), {"event": config.SYNCTHING_SWITCH_IGNORED, "level": "warning"})
        self.assertNotIn(SWITCH_CANARY, err.getvalue())


if __name__ == "__main__":
    unittest.main()
