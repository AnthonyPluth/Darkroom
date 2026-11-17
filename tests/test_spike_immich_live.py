import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson
from tools.spikes import immich_live

KEY_A, KEY_B, KEY_Q = "canary-key-a-7f3e91", "canary-key-b-c41d02", "canary-key-q-9a8b7c"
USER_A, USER_B, USER_Q = "0b5e6a43-5b1f-4c7e-9d2a-1f0e2d3c4b5a", "1c6f7b54-6c2a-4d8f-8e3b-2a1f3e4d5c6b", "2d7a8c65-7d3b-4e9a-9f4c-3b2a4f5e6d7c"


class HarnessTests(unittest.TestCase):
    def setUp(self):
        people = [FakePerson("a", USER_A, "lib-a", KEY_A), FakePerson("b", USER_B, "lib-b", KEY_B), FakePerson("q", USER_Q, "lib-q", KEY_Q)]
        self.fake = FakeImmich(people).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.permissions = tuple(sorted(immich_live.GRANTS))
        self.fake.state.version = "v3.3.6"
        self.fake.state.extraction_delay = 0.2
        self.folder = Path(tempfile.mkdtemp())
        self.env = {"IMMICH_URL": self.fake.url, "IMMICH_LIVE_KEY_A": KEY_A}

    def run_harness(self, *argv, out="result.md", **env):
        printed = io.StringIO()
        code = immich_live.main(["--out", str(self.folder / out), "--extraction-timeout", "2", *argv], printed, {**self.env, **env}, poll_seconds=0.05)
        path = self.folder / out
        return code, printed.getvalue(), path.read_text() if path.exists() else ""

    def row(self, text, key):
        title = {k: t for k, t, _ in immich_live.ROWS}[key]
        return next(line for line in text.splitlines() if line.startswith(f"| {title} |")).split(" | ")[1]

    def assert_no_secret(self, *texts):
        for text in texts:
            for secret in (KEY_A, KEY_B, KEY_Q, "canary-revoked-5e4d", USER_A, USER_B):
                self.assertNotIn(secret, text)

    def assert_only_safe_requests(self):
        for method, path in self.fake.requests():
            self.assertIn(method, ("GET", "POST"))
            self.assertFalse(any(word in path for word in ("trash", "restore", "jobs", "libraries", "admin")), path)
            self.assertNotIn(path, ("/api/users", "/api/server/ping"))

    def test_every_request_carries_the_harness_user_agent(self):
        code, printed, text = self.run_harness()
        self.assertEqual(code, 0, printed + text)
        agents = self.fake.user_agents()
        self.assertTrue(agents)
        self.assertEqual(set(agents), {"immich-spike/1.0"})

    def test_the_happy_path_with_both_keys_passes_and_reports_what_it_created(self):
        self.fake.state.quotas[USER_Q] = 10
        code, printed, text = self.run_harness(IMMICH_LIVE_KEY_B=KEY_B, IMMICH_LIVE_QUOTA_KEY=KEY_Q, IMMICH_LIVE_REVOKED_KEY="canary-revoked-5e4d")
        self.assertEqual(code, 0, printed + text)
        for key in ("grants", "new", "extract", "dup_sidecar", "concurrent", "lost", "two_users", "binding", "live", "raw", "quota", "revoked", "no_delete"):
            self.assertEqual(self.row(text, key), "pass", key)
        for key in ("partner", "trashed", "offline", "edited", "enrich", "video"):
            self.assertEqual(self.row(text, key), "not run", key)
        self.assertIn("v3.3.6", text)
        self.assertIn("`asset.upload`", text)
        self.assertIn("XMP date won", text)
        self.assertIn("Quota has been exceeded", text)
        self.assertIn("no asset.read access", text)
        created = [line.split()[2] for line in printed.splitlines() if line.startswith("created ")]
        stored = {asset.id: asset for asset in self.fake.state.assets}
        self.assertEqual(sorted(created), sorted(stored))
        for asset_id in created:
            self.assertTrue(stored[asset_id].name.startswith("darkroom-spike-"))
            self.assertIn(asset_id, text)
        self.assert_no_secret(printed, text)
        self.assert_only_safe_requests()

    def test_one_key_marks_the_two_account_rows_not_run_and_a_second_run_merges_them(self):
        code, printed, text = self.run_harness()
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.row(text, "two_users"), "not run")
        self.assertIn("needs second key", text)
        code, printed, merged = self.run_harness("--phase", "second-account", "--append-results", str(self.folder / "result.md"),
                                                 out="merged.md", IMMICH_LIVE_KEY_A=KEY_B)
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.row(merged, "two_users"), "pass")
        self.assertEqual(self.row(merged, "binding"), "pass")
        self.assertEqual(self.row(merged, "new"), "pass")
        owners = {asset.owner_id for asset in self.fake.state.assets if asset.name.endswith("-shared.jpg")}
        self.assertEqual(owners, {USER_A, USER_B})
        self.assert_no_secret(printed, merged)
        self.fake.state.version = "v3.4.0"
        code, printed, _ = self.run_harness("--phase", "second-account", "--append-results", str(self.folder / "result.md"), out="m.md", IMMICH_LIVE_KEY_A=KEY_B)
        self.assertEqual(code, 2)
        self.assertIn("server version differs", printed)

    def test_the_same_user_behind_both_keys_is_not_counted_as_two_users(self):
        code, _, text = self.run_harness(IMMICH_LIVE_KEY_B=KEY_A)
        self.assertEqual(code, 0)
        self.assertIn("second key resolves to the same user", text)

    def test_an_over_privileged_or_admin_key_is_refused_before_any_upload(self):
        for grants, admins in ((("asset.delete",), ()), (("all",), ()), (("admin.user.read",), ()), (("asset.update",), ()), ((), ("a",))):
            with self.subTest(grants=grants, admins=admins):
                self.fake.state.permissions = tuple(sorted(immich_live.GRANTS)) + grants
                self.fake.state.admins = admins
                code, printed, text = self.run_harness()
                self.assertEqual(code, 2)
                self.assertIn("refused", printed)
                self.assertEqual(text, "")
                self.assertNotIn(("POST", "/api/assets"), self.fake.requests())
                self.assert_no_secret(printed)

    def test_a_key_missing_a_grant_or_rejected_is_refused(self):
        self.fake.state.permissions = ("user.read", "server.about", "asset.read", "asset.download")
        self.assertEqual(self.run_harness()[0], 2)
        code, printed, _ = self.run_harness(IMMICH_LIVE_KEY_A="placeholder-the-proxy-replaces")
        self.assertEqual(code, 2)
        self.assertNotIn("placeholder-the-proxy-replaces", printed)

    def test_a_user_with_assets_it_did_not_create_is_refused(self):
        self.fake.add_asset(FakeAsset("3e8b9d76-8e4c-4f0b-8a5d-4c3b5a6f7e8d", USER_A, b"someone's photo", name="IMG_0001.JPG"))
        code, printed, _ = self.run_harness()
        self.assertEqual(code, 2)
        self.assertIn("did not create", printed)
        self.assertNotIn(("POST", "/api/assets"), self.fake.requests())

    def test_a_public_address_is_refused_without_the_flag_and_needs_https_with_it(self):
        for url, argv, wanted in (("https://photos.example.com", (), "not a private"), ("http://photos.example.com", ("--allow-public-address",), "must use https"),
                                  ("https://u:p@192.168.1.20", (), "nothing else"), ("https://192.168.1.20/api", (), "nothing else"), ("ftp://10.0.0.2", (), "nothing else")):
            with self.subTest(url=url):
                code, printed, _ = self.run_harness(*argv, IMMICH_URL=url)
                self.assertEqual(code, 2)
                self.assertIn(wanted, printed)
                self.assertNotIn("photos.example.com", printed)
        self.assertEqual(self.fake.requests(), [])
        self.assertIn("public https", immich_live.check_address("https://photos.example.com", True))

    def test_weak_or_absent_evidence_never_passes(self):
        def server_error(method, path, slug):
            own = {asset.id for asset in self.fake.state.assets if asset.owner_id == USER_B}
            if slug == "b" and method == "GET" and path.startswith("/api/assets/") and path.split("/")[3] not in own:
                return 500
            return 500 if slug == "q" and (method, path) == ("POST", "/api/assets") else None
        self.fake.state.intercept = server_error
        code, printed, text = self.run_harness(IMMICH_LIVE_KEY_B=KEY_B, IMMICH_LIVE_QUOTA_KEY=KEY_Q)
        self.assertEqual(code, 1, printed)
        self.assertEqual(self.row(text, "binding"), "not verified")
        self.assertIn("500", text.split("| Owner binding")[1].splitlines()[0])
        self.assertEqual(self.row(text, "quota"), "not verified")
        self.assertEqual(self.row(text, "two_users"), "pass")

    def test_an_extraction_that_never_completes_fails_its_rows(self):
        self.fake.state.extraction_delay = 1e9
        code, printed, text = self.run_harness("--large-video-mb", "0.2", "--extraction-timeout", "0.3")
        self.assertEqual(code, 1, printed)
        for key in ("extract", "video"):
            self.assertEqual(self.row(text, key), "fail", key)
        self.assertIn("timed out after 0.3 s", text)

    def test_a_missing_field_fails_the_rows_that_need_it(self):
        self.fake.state.drop_fields = ("libraryId", "type", "livePhotoVideoId")
        code, printed, text = self.run_harness()
        self.assertEqual(code, 1, printed)
        for key in ("new", "raw", "live", "no_delete"):
            self.assertEqual(self.row(text, key), "fail", key)

    def test_only_the_harness_routes_can_be_sent(self):
        asset = "/api/assets/0b5e6a43-5b1f-4c7e-9d2a-1f0e2d3c4b5a"
        for method, path in (("DELETE", "/api/assets"), ("DELETE", asset), ("PUT", asset), ("PUT", "/api/assets"), ("POST", "/api/trash/restore"),
                             ("POST", "/api/trash/empty"), ("POST", "/api/trash/restore/assets"), ("GET", "/api/users"), ("POST", "/api/jobs/metadataExtraction"),
                             ("POST", "/api/libraries/x/scan"), ("GET", "/api/libraries"), ("PUT", f"{asset}/original"), ("GET", f"{asset}/original"), ("GET", "/api/assets/x")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(immich_live.Refused):
                    immich_live.allowed(method, path)
        immich_live.allowed("GET", f"{asset}/original?edited=false")

    def test_the_large_video_is_opt_in_and_capped(self):
        code, printed, text = self.run_harness("--large-video-mb", "1")
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.row(text, "video"), "pass")
        self.assertEqual(self.run_harness("--large-video-mb", "1000")[0], 2)
        self.assertEqual(self.run_harness("--phase", "after-trash")[0], 2)
        (self.folder / "broken.md").write_text("```json immich-live-data\n{not json\n```\n")
        self.assertEqual(self.run_harness("--phase", "after-trash", "--append-results", str(self.folder / "broken.md"))[0], 2)
        self.assertEqual(self.run_harness(IMMICH_LIVE_KEY_A="")[0], 2)

    def test_the_after_trash_phase_reports_the_trashed_duplicate_without_restoring_it(self):
        self.run_harness()
        trash_me = next(asset for asset in self.fake.state.assets if asset.name.endswith("-trash-me.jpg"))
        code, printed, _ = self.run_harness("--phase", "after-trash", "--append-results", str(self.folder / "result.md"), out="t.md")
        self.assertEqual(code, 1)
        self.assertIn("1 failed or not verified", printed)
        trash_me.trashed = True
        code, printed, text = self.run_harness("--phase", "after-trash", "--append-results", str(self.folder / "result.md"), out="t.md")
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.row(text, "trashed"), "pass")
        self.assertTrue(trash_me.trashed)
        self.assert_only_safe_requests()


if __name__ == "__main__":
    unittest.main()
