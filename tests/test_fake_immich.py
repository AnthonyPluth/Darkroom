import http.client
import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson

ROOT = Path(__file__).resolve().parent.parent
PEOPLE = [
    FakePerson("jo", "u-jo", "lib-jo", "key-jo", "Jo", "jo@x.test", ("/photos/jo/originals", "/photos/jo/google-only"), ("**/.*",)),
    FakePerson("al", "u-al", "lib-al", "key-al"),
]


def call(fake, method, path, key=None, body=None):
    request = urllib.request.Request(f"{fake.url}{path}", method=method, data=None if body is None else json.dumps(body).encode())
    if key:
        request.add_header("x-api-key", key)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None


class FakeImmichTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich(PEOPLE, "admin-key").start()
        self.addCleanup(self.fake.stop)

    def test_ping_and_about_need_no_key(self):
        self.assertEqual(call(self.fake, "GET", "/api/server/ping"), (200, {"res": "pong"}))
        status, about = call(self.fake, "GET", "/api/server/about")
        self.assertEqual(status, 200)
        self.assertTrue(about["version"].startswith("v"))

    def test_users_me_names_the_key_owner(self):
        status, user = call(self.fake, "GET", "/api/users/me", "key-jo")
        self.assertEqual((status, user["id"], user["email"], user["isAdmin"]), (200, "u-jo", "jo@x.test", False))
        status, user = call(self.fake, "GET", "/api/users/me", "key-al")
        self.assertEqual((user["name"], user["email"]), ("al", "al@fake.invalid"))
        status, user = call(self.fake, "GET", "/api/users/me", "admin-key")
        self.assertTrue(user["isAdmin"])

    def test_a_person_key_lists_only_their_library_and_admin_lists_all(self):
        _, libraries = call(self.fake, "GET", "/api/libraries", "key-jo")
        self.assertEqual([lib["id"] for lib in libraries], ["lib-jo"])
        self.assertEqual(libraries[0]["ownerId"], "u-jo")
        self.assertEqual(libraries[0]["importPaths"], ["/photos/jo/originals", "/photos/jo/google-only"])
        self.assertEqual(libraries[0]["exclusionPatterns"], ["**/.*"])
        _, libraries = call(self.fake, "GET", "/api/libraries", "admin-key")
        self.assertEqual([lib["id"] for lib in libraries], ["lib-jo", "lib-al"])

    def test_wrong_or_missing_keys_get_401(self):
        for path in ("/api/users/me", "/api/libraries"):
            for key in (None, "nope"):
                with self.subTest(path=path, key=key):
                    self.assertEqual(call(self.fake, "GET", path, key)[0], 401)
        self.assertEqual(call(self.fake, "POST", "/api/libraries/lib-jo/scan", "nope")[0], 401)
        self.assertEqual(self.fake.scans(), [])

    def test_only_the_admin_key_can_scan_and_scans_are_recorded_in_order(self):
        self.assertEqual(call(self.fake, "POST", "/api/libraries/lib-jo/scan", "key-jo")[0], 403)
        self.assertEqual(self.fake.scans(), [])
        self.assertEqual(call(self.fake, "POST", "/api/libraries/lib-jo/scan", "admin-key")[0], 204)
        self.assertEqual(call(self.fake, "POST", "/api/libraries/lib-al/scan", "admin-key")[0], 204)
        self.assertEqual(call(self.fake, "POST", "/api/libraries/lib-jo/scan", "admin-key")[0], 204)
        found = self.fake.scans()
        self.assertEqual([s.library_id for s in found], ["lib-jo", "lib-al", "lib-jo"])
        self.assertEqual({s.key_role for s in found}, {"admin"})
        self.assertEqual([s.at for s in found], sorted(s.at for s in found))

    def test_scanning_an_unknown_library_is_404_and_unrecorded(self):
        self.assertEqual(call(self.fake, "POST", "/api/libraries/ghost/scan", "admin-key")[0], 404)
        self.assertEqual(self.fake.scans(), [])

    def test_unknown_routes_are_404(self):
        self.assertEqual(call(self.fake, "GET", "/api/assets", "admin-key")[0], 404)

    def test_a_missing_or_unreadable_asset_answers_400_like_immich_and_404_only_when_asked(self):
        denied = (400, {"message": "Not found or no asset.read access", "error": "Bad Request", "statusCode": 400})
        self.fake.add_asset(FakeAsset(id="als", owner_id="u-al", data=b"x"))
        self.assertEqual(call(self.fake, "GET", "/api/assets/ghost", "key-jo"), denied)
        self.assertEqual(call(self.fake, "GET", "/api/assets/als", "key-jo"), denied)
        self.assertEqual(call(self.fake, "GET", "/api/assets/als", "key-al")[0], 200)
        self.fake.state.missing_status = 404
        self.assertEqual(call(self.fake, "GET", "/api/assets/ghost", "key-jo")[0], 404)
        self.assertEqual(call(self.fake, "GET", "/api/assets/als", "key-jo"), denied)
        self.fake.state.shared.add("als")
        self.assertEqual(call(self.fake, "GET", "/api/assets/als", "key-jo")[1]["ownerId"], "u-al")

    def test_a_capture_time_search_filters_on_file_created_at_like_the_pinned_builder(self):
        self.fake.add_asset(FakeAsset("a-exif", "u-jo", b"one", file_created_at="2024-05-01T10:00:00.000Z",
                                      exif={"dateTimeOriginal": "2024-05-01T12:00:00.000Z"}, ready_at=float("inf")))
        self.fake.add_asset(FakeAsset("a-extracted", "u-jo", b"two", file_created_at="2024-05-01T10:00:00.000Z",
                                      exif={"dateTimeOriginal": "2024-05-01T12:00:00.500Z"}))
        self.fake.add_asset(FakeAsset("a-trashed", "u-jo", b"three", file_created_at="2024-05-01T12:00:01.000Z", trashed=True))
        self.fake.add_asset(FakeAsset("a-other", "u-al", b"four", file_created_at="2024-05-01T12:00:00.000Z"))
        window = {"filter": {"takenAt": {"gte": "2024-05-01T11:59:59Z", "lte": "2024-05-01T12:00:01Z"}}, "size": 10}
        status, found = call(self.fake, "POST", "/api/search/metadata", "key-jo", window)
        self.assertEqual((status, [a["id"] for a in found["assets"]["items"]]), (200, ["a-extracted", "a-trashed"]))
        self.assertEqual(found["assets"]["items"][0]["fileCreatedAt"], "2024-05-01T12:00:00.500Z")
        status, found = call(self.fake, "POST", "/api/search/metadata", "key-jo", window | {"filter": {"takenAt": {"gte": "2024-05-01T09:59:59Z", "lte": "2024-05-01T10:00:01Z"}}})
        self.assertEqual([a["id"] for a in found["assets"]["items"]], ["a-exif"])
        self.assertNotIn("exifInfo", found["assets"]["items"][0])

    def test_an_updated_at_listing_returns_trashed_assets_like_the_pinned_builder(self):
        self.fake.add_asset(FakeAsset("a-live", "u-jo", b"one", updated_at="2026-01-02T00:00:00.000Z"))
        self.fake.add_asset(FakeAsset("a-trashed", "u-jo", b"two", updated_at="2026-01-02T00:00:00.000Z", trashed=True))
        self.fake.add_asset(FakeAsset("a-outside", "u-jo", b"three", updated_at="2026-02-01T00:00:00.000Z", trashed=True))
        self.fake.add_asset(FakeAsset("a-other", "u-al", b"four", updated_at="2026-01-02T00:00:00.000Z", trashed=True))
        window = {"filter": {"updatedAt": {"gte": "2026-01-01T00:00:00.000Z", "lte": "2026-01-03T00:00:00.000Z"}}, "size": 10}
        status, found = call(self.fake, "POST", "/api/search/metadata", "key-jo", window)
        self.assertEqual((status, {a["id"]: a["isTrashed"] for a in found["assets"]["items"]}), (200, {"a-live": False, "a-trashed": True}))

    def test_a_search_mixing_the_new_shape_with_flat_fields_is_refused_like_immich(self):
        for flat in ({"takenAfter": "2024-05-01T00:00:00Z"}, {"withDeleted": True}, {"checksum": "x"}, {"page": 2}):
            status, found = call(self.fake, "POST", "/api/search/metadata", "key-jo", {"filter": {"takenAt": {"gte": "2024-05-01T00:00:00Z"}}} | flat)
            self.assertEqual((status, found["statusCode"]), (400, 400))

    def test_mode_500_and_401_apply_to_every_call(self):
        self.fake.mode("500")
        self.assertEqual(call(self.fake, "GET", "/api/server/ping")[0], 500)
        self.fake.mode("401")
        self.assertEqual(call(self.fake, "GET", "/api/libraries", "admin-key")[0], 401)
        self.assertEqual(call(self.fake, "GET", "/api/server/ping")[0], 401)
        self.fake.mode("ok")
        self.assertEqual(call(self.fake, "GET", "/api/server/ping")[0], 200)

    def test_mode_down_drops_the_connection(self):
        self.fake.mode("down")
        with self.assertRaises((urllib.error.URLError, http.client.HTTPException, ConnectionError)):
            call(self.fake, "GET", "/api/server/ping")

    def test_mode_slow_delays_the_answer(self):
        self.fake.state.slow_seconds = 0.3
        self.fake.mode("slow")
        started = time.monotonic()
        self.assertEqual(call(self.fake, "GET", "/api/server/ping")[0], 200)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_mode_can_be_set_over_http(self):
        self.assertEqual(call(self.fake, "POST", "/__fake/mode", body={"mode": "500"}), (200, {"mode": "500"}))
        self.assertEqual(call(self.fake, "GET", "/api/server/ping")[0], 500)
        self.assertEqual(call(self.fake, "POST", "/__fake/mode", body={"mode": "bogus"})[0], 400)
        self.fake.mode("ok")

    def test_an_unknown_mode_is_refused_in_code(self):
        with self.assertRaises(ValueError):
            self.fake.mode("bogus")

    def test_it_refuses_to_bind_anything_but_loopback(self):
        for host in ("0.0.0.0", "192.168.1.5", "localhost", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                FakeImmich(PEOPLE, host=host)

    def test_it_binds_to_loopback_on_a_free_port(self):
        self.assertEqual(self.fake.server.server_address[0], "127.0.0.1")
        self.assertGreater(self.fake.port, 0)

    def test_stopping_a_fake_that_never_started_or_twice_does_not_hang(self):
        never = FakeImmich(PEOPLE)
        never.stop()
        never.stop()
        self.fake.stop()
        self.fake.stop()

    def test_it_works_as_a_context_manager(self):
        with FakeImmich(PEOPLE) as other:
            self.assertEqual(call(other, "GET", "/api/server/ping")[0], 200)


class StandaloneTests(unittest.TestCase):
    def test_the_command_serves_the_state_files_libraries(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            state.write_text(json.dumps({
                "admin_key": "adm",
                "people": [{"slug": "mo", "user_id": "u-mo", "library_id": "lib-mo", "key": "k-mo", "import_paths": ["/p/mo"]}],
            }))
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.fakes.immich", "--state", str(state)], cwd=ROOT,
                stdout=subprocess.PIPE, text=True,
            )
            try:
                line = process.stdout.readline().strip()
                self.assertTrue(line.startswith("fake Immich on http://127.0.0.1:"), line)
                url = line.rsplit(" ", 1)[1]
                request = urllib.request.Request(f"{url}/api/libraries", headers={"x-api-key": "adm"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    libraries = json.loads(response.read())
                self.assertEqual([(lib["id"], lib["importPaths"]) for lib in libraries], [("lib-mo", ["/p/mo"])])
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
