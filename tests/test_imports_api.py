import dataclasses
import json
import unittest

from darkroom.domain.ingest import run
from darkroom.storage.models import Export, ImportRun
from tests.import_world import ADMIN_KEY, NOW, World
from tests.privacy import no_leaks
from tests.shared import fetch, settings_from, start_server

PRIVATE_NAME = "IMG_2001.JPG"
ROUTES = (("POST", "/api/exports/{id}/import"), ("GET", "/api/imports"), ("GET", "/api/imports/{id}"))


class ImportsApiTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        settings = dataclasses.replace(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_PUBLIC_URL="http://127.0.0.1"),
                                       photos_root=self.world.settings.photos_root, data_dir=self.world.settings.data_dir,
                                       immich_url=self.world.settings.immich_url, immich_admin_key=self.world.settings.immich_admin_key)
        self.base, self.settings = start_server(self, settings, self.world.engine)
        self.export_id = self.world.hand_dropped().id
        self.replies = []

    def call(self, method, path):
        headers = {"Content-Type": "application/json", "Origin": self.base} if method != "GET" else {}
        status, _, body = fetch(self.base, method, path, headers, b"{}" if method != "GET" else None)
        self.replies.append(body)
        return status, json.loads(body)

    def import_now(self):
        opened = self.world.reopen()
        run.run_waiting(opened, self.settings, NOW, self.world.tools)

    def test_the_routes_need_a_signed_in_session(self):
        from darkroom.server import routes

        for method, path in ROUTES:
            with self.subTest(route=path):
                self.assertEqual(routes.find(method, path.replace("{id}", "1")).auth, "session")

    def test_requesting_an_import_queues_it_for_the_scheduler(self):
        status, body = self.call("POST", f"/api/exports/{self.export_id}/import")
        self.assertEqual((status, body), (202, {"export_id": self.export_id, "status": "downloaded", "stage": "queued"}))
        self.assertEqual(self.call("GET", "/api/imports"), (200, {"imports": []}))
        self.import_now()
        status, body = self.call("GET", "/api/imports")
        self.assertEqual(status, 200)
        (listed,) = body["imports"]
        self.assertEqual({key: listed[key] for key in ("export_id", "account_id", "status", "stage", "added", "skipped_t1", "skipped_t2",
                                                       "skipped_t3", "review", "missing_gps", "failed", "unmatched_by_person", "error")},
                         {"export_id": self.export_id, "account_id": self.world.account, "status": "done", "stage": "done", "added": 2,
                          "skipped_t1": 3, "skipped_t2": 1, "skipped_t3": 1, "review": 1, "missing_gps": 1, "failed": 0,
                          "unmatched_by_person": {"jo": 2, "sam": 0}, "error": None})
        self.assertEqual(self.call("GET", f"/api/imports/{listed['id']}"), (200, {"run": listed}))

    def test_an_imported_export_cannot_be_requested_again(self):
        self.call("POST", f"/api/exports/{self.export_id}/import")
        self.import_now()
        self.assertEqual(self.call("POST", f"/api/exports/{self.export_id}/import"), (409, {"error": "this export is already imported"}))

    def test_an_export_not_downloaded_yet_cannot_be_requested(self):
        self.world.opened.get(Export, self.export_id).status = "announced"
        self.world.opened.commit()
        self.assertEqual(self.call("POST", f"/api/exports/{self.export_id}/import"), (409, {"error": "this export has not been downloaded yet"}))

    def test_missing_exports_and_imports_are_404(self):
        for path in ("/api/exports/999/import", "/api/exports/x/import", "/api/exports/1234567890123/import"):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path), (404, {"error": "no such export"}))
        for path in ("/api/imports/999", "/api/imports/one", "/api/imports/-1"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path), (404, {"error": "no such import"}))

    def test_a_failed_run_shows_its_public_error(self):
        part = sorted(self.world.photos.manual_inbox("jo").glob("*.zip"))[0]
        part.write_bytes(b"junk")
        self.call("POST", f"/api/exports/{self.export_id}/import")
        self.import_now()
        (listed,) = self.call("GET", "/api/imports")[1]["imports"]
        self.assertEqual(listed["status"], "failed")
        self.assertEqual(listed["error"], "the export could not be unpacked: part 1 is not a valid zip")

    def test_status_shows_unmatched_originals_per_person(self):
        status, body = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual([(p["slug"], p["unmatched"]) for p in body["people"]], [("jo", None), ("sam", None)])
        self.call("POST", f"/api/exports/{self.export_id}/import")
        self.import_now()
        body = self.call("GET", "/api/status")[1]
        self.assertEqual([(p["slug"], p["indexed"], p["unmatched"]) for p in body["people"]], [("jo", 4, 2), ("sam", 2, 0)])

    def test_replies_logs_and_the_database_carry_no_file_names_or_keys(self):
        with no_leaks(self, ADMIN_KEY, replies=self.replies):
            self.call("POST", f"/api/exports/{self.export_id}/import")
            self.import_now()
            run_id = self.world.opened.query(ImportRun).one().id
            self.call("GET", "/api/imports")
            self.call("GET", f"/api/imports/{run_id}")
            self.call("GET", "/api/status")
        self.assertFalse(any(PRIVATE_NAME.encode() in reply for reply in self.replies))

    def failing_import(self):
        from unittest import mock

        from darkroom.domain.ingest import place

        real = place.copy_into

        def failing(folder, source, sha1, **options):
            if source.name == "IMG_2001.JPG":
                raise place.PlaceFailed("taken")
            return real(folder, source, sha1, **options)

        self.call("POST", f"/api/exports/{self.export_id}/import")
        opened = self.world.reopen()
        with mock.patch.object(place, "copy_into", failing):
            run.run_waiting(opened, self.settings, NOW, self.world.tools)
        self.assertEqual(self.call("GET", "/api/imports")[1]["imports"][0]["failed"], 1)

    def test_an_export_with_failed_items_can_be_requested_again_and_then_finishes(self):
        self.failing_import()
        status, body = self.call("POST", f"/api/exports/{self.export_id}/import")
        self.assertEqual((status, body), (202, {"export_id": self.export_id, "status": "importing", "stage": "unpacked"}))
        self.import_now()
        runs = sorted(self.call("GET", "/api/imports")[1]["imports"], key=lambda r: r["id"])
        self.assertEqual([(r["status"], r["failed"], r["added"]) for r in runs], [("done", 1, 1), ("done", 0, 2)])
        self.assertEqual(self.call("POST", f"/api/exports/{self.export_id}/import"), (409, {"error": "this export is already imported"}))

    def test_a_retry_whose_staging_is_gone_is_refused(self):
        import shutil

        self.failing_import()
        shutil.rmtree(self.world.root / "_inbox/jo" / f"export-{self.export_id}")
        status, body = self.call("POST", f"/api/exports/{self.export_id}/import")
        self.assertEqual(status, 409)
        self.assertIn("staging folder", body["error"])


if __name__ == "__main__":
    unittest.main()
