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
KEY_AALL, KEY_BALL, KEY_READ, KEY_REVOKED = "canary-key-aall-31c7d4", "canary-key-ball-8e2f60", "canary-key-read-5b9a13", "canary-revoked-5e4d"
USER_A, USER_B, USER_Q = "0b5e6a43-5b1f-4c7e-9d2a-1f0e2d3c4b5a", "1c6f7b54-6c2a-4d8f-8e3b-2a1f3e4d5c6b", "2d7a8c65-7d3b-4e9a-9f4c-3b2a4f5e6d7c"


class HarnessTests(unittest.TestCase):
    def setUp(self):
        people = [FakePerson("a", USER_A, "lib-a", KEY_A), FakePerson("b", USER_B, "lib-b", KEY_B), FakePerson("q", USER_Q, "lib-q", KEY_Q),
                  FakePerson("a-all", USER_A, "lib-a", KEY_AALL), FakePerson("b-all", USER_B, "lib-b", KEY_BALL), FakePerson("a-read", USER_A, "lib-a", KEY_READ)]
        self.fake = FakeImmich(people).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.permissions = tuple(sorted(immich_live.GRANTS))
        self.fake.state.version = "v3.3.6"
        self.fake.state.key_permissions = {KEY_AALL: ("all",), KEY_BALL: ("all",), KEY_READ: tuple(sorted(immich_live.GRANTS))}
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
            for secret in (KEY_A, KEY_B, KEY_Q, KEY_AALL, KEY_BALL, KEY_READ, KEY_REVOKED, USER_A, USER_B):
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
        code, printed, text = self.run_harness(IMMICH_LIVE_KEY_B=KEY_B, IMMICH_LIVE_QUOTA_KEY=KEY_Q, IMMICH_LIVE_REVOKED_KEY=KEY_REVOKED)
        self.assertEqual(code, 0, printed + text)
        for key in ("grants", "new", "extract", "dup_sidecar", "concurrent", "lost", "two_users", "binding", "live", "raw", "quota", "revoked", "no_delete"):
            self.assertEqual(self.row(text, key), "pass", key)
        for key in ("partner", "trashed", "offline", "edited", "e1", "e2", "e18", "video"):
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

    ENRICH_ENV = {"IMMICH_LIVE_ENRICH_KEY": KEY_AALL, "IMMICH_LIVE_ENRICH_KEY_2": KEY_BALL, "IMMICH_LIVE_ENRICH_READ_KEY": KEY_READ, "IMMICH_LIVE_REVOKED_KEY": KEY_REVOKED}

    def run_enrich(self, *argv, **env):
        return self.run_harness("--enrich", "--settle-seconds", "0.1", *argv, **{**self.ENRICH_ENV, **env})

    def test_the_enrich_phase_runs_only_the_e_rows_and_passes_them(self):
        updates = []
        self.fake.state.intercept = lambda method, path, slug: updates.append((slug, method)) if method == "PUT" else None
        code, printed, text = self.run_enrich()
        self.assertEqual(code, 0, printed + text)
        for key in ("grants", "e1", "e2", "e3", "e4", "e5", "e6", "e9", "e10", "e13", "e14", "e15", "e16", "e18"):
            self.assertEqual(self.row(text, key), "pass", key)
        for key in ("e7", "e8", "e11", "e12", "e17", "new", "extract", "concurrent", "live", "raw", "no_delete"):
            self.assertEqual(self.row(text, key), "not run", key)
        self.assertEqual({slug for slug, _ in updates}, {"a-all", "b-all", "a-read"})
        self.assertIn("`all`", text)
        self.assertIn("Missing required permission", text)
        self.assertIn("Not found or no asset.update access", text)
        self.assertIn("is the stored instant 12:30Z", text)
        self.assertIn("overwrites a present value without an error", text)
        self.assert_no_secret(printed, text)

    def test_the_enrich_phase_sends_nothing_but_reads_searches_fixture_uploads_and_the_one_update_route(self):
        code, printed, _ = self.run_enrich()
        self.assertEqual(code, 0, printed)
        requests = self.fake.requests()
        self.assertIn(("PUT", next(path for method, path in requests if method == "PUT")), requests)
        for method, path in requests:
            self.assertIn(method, ("GET", "POST", "PUT"))
            if method == "PUT":
                self.assertRegex(path, rf"^{immich_live.UPDATE_ROUTE.pattern}$")
            if method == "POST":
                self.assertIn(path, ("/api/assets", "/api/assets/bulk-upload-check", "/api/search/metadata"))
            if "api-keys" in path:
                self.assertEqual((method, path), ("GET", "/api/api-keys/me"))
            self.assertFalse(any(word in path for word in ("trash", "restore", "jobs", "libraries", "admin", "metadata/", "copy")), path)
            if method != "POST":
                self.assertNotIn(path, ("/api/assets", "/api/users"))

    def test_only_the_enrich_accounts_may_send_the_update_route(self):
        asset = "/api/assets/0b5e6a43-5b1f-4c7e-9d2a-1f0e2d3c4b5a"
        immich_live.allowed("PUT", asset, enriching=True)
        immich_live.allowed("GET", f"{asset}/thumbnail", enriching=True)
        immich_live.allowed("GET", f"/api/albums?assetId={asset.rsplit('/', 1)[1]}", enriching=True)
        for path in (f"{asset}/thumbnail", f"/api/albums?assetId={asset.rsplit('/', 1)[1]}"):
            with self.assertRaises(immich_live.Refused):
                immich_live.allowed("GET", path)
        with self.assertRaises(immich_live.Refused):
            immich_live.allowed("PUT", asset)
        for method, path in (("DELETE", asset), ("DELETE", "/api/assets"), ("PUT", "/api/assets"), ("PATCH", asset), ("PUT", f"{asset}/original"), ("PUT", f"{asset}/metadata"),
                             ("PUT", "/api/assets/copy"), ("POST", "/api/assets/jobs"), ("POST", "/api/trash/restore"), ("DELETE", "/api/api-keys/x"), ("POST", "/api/api-keys"),
                             ("GET", "/api/albums"), ("POST", f"{asset}/thumbnail"), ("PUT", "/api/albums/x")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(immich_live.Refused):
                    immich_live.allowed(method, path, enriching=True)

    def test_the_import_key_never_sends_an_update(self):
        api = immich_live.Api(self.fake.url, KEY_A)
        with self.assertRaises(immich_live.Refused):
            api.call("PUT", "/api/assets/0b5e6a43-5b1f-4c7e-9d2a-1f0e2d3c4b5a", {"description": "x"})
        self.assertEqual(self.fake.requests(), [])

    def test_a_refused_enrich_key_sends_nothing(self):
        cases = (("an admin user's key", {"IMMICH_LIVE_ENRICH_KEY": KEY_AALL}, ("a-all",), "admin user"),
                 ("a key of another user", {"IMMICH_LIVE_ENRICH_KEY": KEY_BALL}, (), "same user"),
                 ("a second key of the same user", {"IMMICH_LIVE_ENRICH_KEY_2": KEY_AALL}, (), "second user"),
                 ("a read-only key that holds all", {"IMMICH_LIVE_ENRICH_READ_KEY": KEY_AALL}, (), "neither `all` nor `asset.update`"),
                 ("no enrich key", {"IMMICH_LIVE_ENRICH_KEY": ""}, (), "IMMICH_LIVE_ENRICH_KEY"),
                 ("a key a header cannot carry", {"IMMICH_LIVE_ENRICH_KEY": "bad key"}, (), "header"))
        for name, env, admins, wanted in cases:
            with self.subTest(name):
                self.fake.state.admins = admins
                code, printed, text = self.run_enrich(**env)
                self.assertEqual(code, 2, printed)
                self.assertIn(wanted, printed)
                self.assertEqual(text, "")
                self.assertNotIn("PUT", [method for method, _ in self.fake.requests()])
                self.assert_no_secret(printed)

    def test_the_enrich_flag_runs_alone(self):
        for argv in (("--phase", "after-trash"), ("--large-video-mb", "1"), ("--settle-seconds", "100000")):
            with self.subTest(argv=argv):
                code, printed, _ = self.run_harness("--enrich", *argv, **self.ENRICH_ENV)
                self.assertEqual(code, 2, printed)
        self.assertEqual(self.fake.requests(), [])

    def test_an_update_the_server_ignores_or_refuses_never_passes(self):
        self.fake.state.intercept = lambda method, path, slug: 500 if method == "PUT" and slug == "a-all" else None
        code, printed, text = self.run_enrich()
        self.assertEqual(code, 1, printed)
        for key in ("e2", "e3", "e4", "e5", "e6", "e15", "e16", "e18"):
            self.assertEqual(self.row(text, key), "fail", key)
        self.assertEqual(self.row(text, "e10"), "pass")
        self.assertIn("answered 500", text)

    def test_repeat_and_album_rows_need_the_update_to_apply(self):
        for name in ("refused", "ignored"):
            with self.subTest(name):
                self.fake.state.ignore_updates = name == "ignored"
                self.fake.state.intercept = (lambda m, p, slug: 500 if m == "PUT" and slug == "a-all" else None) if name == "refused" else None
                code, printed, text = self.run_enrich()
                self.assertEqual(code, 1, printed)
                self.assertEqual(self.row(text, "e15"), "fail")
                self.assertEqual(self.row(text, "e18"), "fail")

    def test_the_offsetless_edit_of_e4_must_apply(self):
        for mode in ("refuse", "ignore"):
            with self.subTest(mode):
                self.fake.state.naive_dates = mode
                code, printed, text = self.run_enrich()
                self.assertEqual(code, 1, printed)
                self.assertEqual(self.row(text, "e4"), "fail")
                self.assertEqual(self.row(text, "e5"), "pass")

    def test_ignored_updates_fail_the_null_row_and_every_fill_row(self):
        self.fake.state.ignore_updates = True
        code, printed, text = self.run_enrich()
        self.assertEqual(code, 1, printed)
        for key in ("e2", "e3", "e4", "e5", "e6", "e15", "e16", "e18"):
            self.assertEqual(self.row(text, key), "fail", key)

    def test_a_key_without_the_update_grant_fails_the_rows_instead_of_passing(self):
        self.fake.state.key_permissions[KEY_AALL] = tuple(sorted(immich_live.GRANTS))
        code, _, text = self.run_enrich()
        self.assertEqual(code, 1)
        self.assertEqual(self.row(text, "e2"), "fail")
        self.assertIn("403", text)

    def test_the_enrich_rows_that_need_a_key_are_not_run_without_it_and_merge_into_an_earlier_result(self):
        code, printed, _ = self.run_harness()
        self.assertEqual(code, 0, printed)
        code, printed, text = self.run_harness("--enrich", "--settle-seconds", "0.1", "--append-results", str(self.folder / "result.md"), out="e.md", IMMICH_LIVE_ENRICH_KEY=KEY_AALL)
        self.assertEqual(code, 0, printed + text)
        self.assertEqual(self.row(text, "e2"), "pass")
        self.assertEqual(self.row(text, "new"), "pass")
        for key in ("e10", "e13", "e14"):
            self.assertEqual(self.row(text, key), "not run", key)


if __name__ == "__main__":
    unittest.main()
