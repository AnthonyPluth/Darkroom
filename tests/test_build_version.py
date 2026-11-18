import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run
from darkroom import config
from tests.privacy import no_leaks
from tests.shared import fetch, own_database, settings_from, start_server

SECRET_KEY = "build-version-secret-key-0123456789"
MALFORMED_CANARY = "malformed-build-canary-7f3a"


class BuildOfTests(unittest.TestCase):
    def test_a_release_and_a_commit_are_read(self):
        self.assertEqual(config.build_of("v4.7.1\n0123456789abcdef0123456789abcdef01234567\n"), config.Build("v4.7.1", "0123456"))

    def test_a_short_commit_is_kept_as_it_is(self):
        self.assertEqual(config.build_of("v1.0.0\nabc1234\n"), config.Build("v1.0.0", "abc1234"))

    def test_no_version_is_dev_with_the_commit(self):
        self.assertEqual(config.build_of("\nabc1234def\n"), config.Build("dev", "abc1234"))

    def test_nothing_is_dev_without_a_commit(self):
        for text in ("", "\n", "\n\n", "   \n  \n"):
            with self.subTest(text=text):
                self.assertEqual(config.build_of(text), config.Build("dev", ""))

    def test_surrounding_spaces_are_ignored(self):
        self.assertEqual(config.build_of("  v2.3.4  \n  abc1234  \n"), config.Build("v2.3.4", "abc1234"))

    def test_a_malformed_version_is_dev(self):
        for version in ("4.7.1", "v4.7", "v4.7.1.2", "v4.7.1-rc1", "V4.7.1", "latest", "v4.7.1<script>", f"v4.7.1 {MALFORMED_CANARY}", "v" + "1" * 20 + ".0.0", "v４.7.1"):
            with self.subTest(version=version):
                self.assertEqual(config.build_of(f"{version}\nabc1234\n"), config.Build("dev", "abc1234"))

    def test_a_malformed_commit_is_left_out(self):
        for commit in ("abc12", "ABC1234", "abc123g", "abc1234 x", "a" * 41, MALFORMED_CANARY, "<b>abc1234</b>"):
            with self.subTest(commit=commit):
                self.assertEqual(config.build_of(f"v4.7.1\n{commit}\n"), config.Build("v4.7.1", ""))

    def test_extra_lines_make_it_dev(self):
        self.assertEqual(config.build_of("v4.7.1\nabc1234\nmore\n"), config.Build("dev", ""))

    def test_shown_is_the_version_then_the_short_commit(self):
        self.assertEqual(config.Build("v4.7.1", "abc1234").shown, "v4.7.1 (abc1234)")
        self.assertEqual(config.Build("dev", "abc1234").shown, "dev (abc1234)")
        self.assertEqual(config.Build("dev", "").shown, "dev")
        self.assertEqual(config.Build("v4.7.1", "").shown, "v4.7.1")


class ReadBuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "build-info"

    def test_no_file_is_dev(self):
        self.assertEqual(config.read_build(self.path), config.Build("dev", ""))

    def test_the_file_is_read(self):
        self.path.write_text("v4.7.1\nabc1234def567\n", encoding="ascii")
        self.assertEqual(config.read_build(self.path), config.Build("v4.7.1", "abc1234"))

    def test_a_file_that_is_not_ascii_is_dev(self):
        self.path.write_bytes(b"v4.7.1\n\xff\xfe\n")
        self.assertEqual(config.read_build(self.path), config.Build("dev", ""))

    def test_an_oversized_file_is_dev(self):
        self.path.write_text("v4.7.1\nabc1234\n" + " " * 1000, encoding="ascii")
        self.assertEqual(config.read_build(self.path), config.Build("dev", ""))

    def test_a_folder_where_the_file_goes_is_dev(self):
        self.path.mkdir()
        self.assertEqual(config.read_build(self.path), config.Build("dev", ""))

    def test_settings_carry_the_build_from_the_file(self):
        self.path.write_text("v9.8.7\nfedcba9\n", encoding="ascii")
        with mock.patch.object(config, "BUILD_FILE", self.path):
            self.assertEqual(settings_from().build, config.Build("v9.8.7", "fedcba9"))

    def test_settings_without_the_file_are_dev(self):
        with mock.patch.object(config, "BUILD_FILE", self.path):
            self.assertEqual(settings_from().build, config.Build("dev", ""))

    def test_the_build_file_is_beside_the_package(self):
        self.assertEqual(config.BUILD_FILE, Path(config.__file__).resolve().parent / "build-info")


class VersionApiTests(unittest.TestCase):
    def serve(self, build, **env):
        values = {"DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_SECRET_KEY": SECRET_KEY, "DARKROOM_PUBLIC_URL": "http://127.0.0.1", **env}
        engine = own_database(self)
        base, _ = start_server(self, dataclasses.replace(settings_from(**values), build=build), engine)
        return base

    def get(self, base, headers=None):
        status, _, body = fetch(base, "GET", "/api/version", headers)
        return status, body

    def test_a_release_build_answers_its_version_and_commit(self):
        status, body = self.get(self.serve(config.Build("v4.7.1", "abc1234")))
        self.assertEqual((status, json.loads(body)), (200, {"version": "v4.7.1", "commit": "abc1234"}))

    def test_a_dev_build_answers_dev_and_no_commit(self):
        status, body = self.get(self.serve(config.Build("dev", "")))
        self.assertEqual((status, json.loads(body)), (200, {"version": "dev", "commit": None}))

    def test_a_dev_build_with_a_commit_answers_both(self):
        status, body = self.get(self.serve(config.Build("dev", "abc1234")))
        self.assertEqual((status, json.loads(body)), (200, {"version": "dev", "commit": "abc1234"}))

    def test_without_sign_in_it_answers_401_and_no_version(self):
        base = self.serve(config.Build("v4.7.1", "abc1234"), DARKROOM_ALLOW_NO_AUTH="", OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom",
                          DARKROOM_PUBLIC_URL="https://darkroom.example")
        status, body = self.get(base)
        self.assertEqual(status, 401)
        self.assertNotIn(b"v4.7.1", body)
        self.assertNotIn(b"abc1234", body)

    def test_healthz_still_shows_no_build(self):
        base = self.serve(config.Build("v4.7.1", "abc1234"))
        status, _, body = fetch(base, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertNotIn(b"v4.7.1", body)
        self.assertNotIn(b"abc1234", body)

    def test_a_malformed_build_file_never_reaches_the_reply(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "build-info"
        path.write_text(f"v4.7.1-{MALFORMED_CANARY}\n{MALFORMED_CANARY}\n", encoding="ascii")
        replies = []
        with mock.patch.object(config, "BUILD_FILE", path), no_leaks(self, MALFORMED_CANARY, SECRET_KEY, replies=replies):
            build = config.read_build()
            status, body = self.get(self.serve(build))
            replies.append(body)
        self.assertEqual((status, json.loads(body)), (200, {"version": "dev", "commit": None}))


class StartedLogTests(unittest.TestCase):
    def logged(self, settings):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run.log_started(settings)
        return [json.loads(line) for line in err.getvalue().splitlines()]

    def test_it_logs_the_version_and_commit_once(self):
        settings = dataclasses.replace(settings_from(), build=config.Build("v4.7.1", "abc1234"))
        self.assertEqual(self.logged(settings), [{"event": "darkroom started", "version": "v4.7.1", "commit": "abc1234"}])

    def test_a_dev_build_logs_dev_and_an_empty_commit(self):
        settings = dataclasses.replace(settings_from(), build=config.Build("dev", ""))
        self.assertEqual(self.logged(settings), [{"event": "darkroom started", "version": "dev", "commit": ""}])

    def test_the_line_carries_nothing_private(self):
        canaries = ("started-log-secret-key-0123456789", "started-log-client-secret-0123", "started-log-immich-key-0123", "private-host.example")
        settings = dataclasses.replace(settings_from(DARKROOM_SECRET_KEY=canaries[0], OIDC_CLIENT_SECRET=canaries[1], IMMICH_ADMIN_KEY=canaries[2],
                                                     DARKROOM_PUBLIC_URL=f"https://{canaries[3]}"), build=config.Build("v4.7.1", "abc1234"))
        with no_leaks(self, *canaries):
            run.log_started(settings)


if __name__ == "__main__":
    unittest.main()
