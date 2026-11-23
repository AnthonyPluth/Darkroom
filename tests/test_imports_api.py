import dataclasses
import json
import unittest

from sqlalchemy import select, update

from darkroom.domain.ingest import run
from darkroom.storage.models import Export, ExportPart, ImportRun
from tests.import_world import ADMIN_KEY, DAY, NOW, World
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
        self.assertEqual((status, body), (202, {"export_id": self.export_id, "status": "downloaded", "stage": "queued", "per_part": False, "parts_requeued": 0}))
        self.assertEqual(self.call("GET", "/api/imports"), (200, {"imports": []}))
        self.import_now()
        status, body = self.call("GET", "/api/imports")
        self.assertEqual(status, 200)
        (listed,) = body["imports"]
        self.assertEqual({key: listed[key] for key in ("export_id", "account_id", "status", "stage", "added", "skipped_t1", "skipped_t2",
                                                       "skipped_t3", "review", "missing_gps", "failed", "sidecars_unreadable", "sidecars_repaired",
                                                       "unmatched_by_person", "error")},
                         {"export_id": self.export_id, "account_id": self.world.account, "status": "done", "stage": "done", "added": 2,
                          "skipped_t1": 3, "skipped_t2": 1, "skipped_t3": 1, "review": 1, "missing_gps": 1, "failed": 0,
                          "sidecars_unreadable": 0, "sidecars_repaired": 0, "unmatched_by_person": {"jo": 2, "sam": 0}, "error": None})
        self.assertEqual((listed["not_imported"], listed["orphan_sidecars"], listed["unpaired_media"]), ({}, 0, 0))
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
        self.assertEqual((status, body), (202, {"export_id": self.export_id, "status": "importing", "stage": "unpacked", "per_part": False, "parts_requeued": 0}))
        self.import_now()
        runs = sorted(self.call("GET", "/api/imports")[1]["imports"], key=lambda r: r["id"])
        self.assertEqual([(r["status"], r["failed"], r["added"]) for r in runs], [("done", 1, 1), ("done", 0, 2)])
        self.assertEqual(self.call("POST", f"/api/exports/{self.export_id}/import"), (409, {"error": "this export is already imported"}))

    def test_files_left_out_of_an_import_are_counted_by_kind(self):
        self.call("POST", f"/api/exports/{self.export_id}/import")
        self.import_now()
        opened = self.world.reopen()
        row = opened.query(ImportRun).one()
        for stored, shown in (('{".txt": 2, "none": 1, "other": 1}', {".txt": 2, "none": 1, "other": 1}), ("not json", {}), ('[".txt"]', {}),
                              ('{".txt": "two"}', {})):
            with self.subTest(stored=stored):
                row.not_imported, row.orphan_sidecars, row.unpaired_media = stored, 3, 4
                opened.commit()
                (listed,) = self.call("GET", "/api/imports")[1]["imports"]
                self.assertEqual((listed["not_imported"], listed["orphan_sidecars"], listed["unpaired_media"]), (shown, 3, 4))

    def test_a_run_that_never_counted_what_was_left_out_lists_null_counts_not_zero(self):
        self.call("POST", f"/api/exports/{self.export_id}/import")
        self.import_now()
        opened = self.world.reopen()
        row = opened.query(ImportRun).one()
        row.not_imported = row.orphan_sidecars = row.unpaired_media = None
        opened.commit()
        (listed,) = self.call("GET", "/api/imports")[1]["imports"]
        self.assertEqual((listed["not_imported"], listed["orphan_sidecars"], listed["unpaired_media"]), (None, None, None))
        self.assertEqual(self.call("GET", f"/api/imports/{listed['id']}")[1]["run"]["not_imported"], None)

    def test_a_retry_whose_staging_is_gone_is_refused(self):
        import shutil

        self.failing_import()
        shutil.rmtree(self.world.root / "_inbox/jo" / f"export-{self.export_id}")
        status, body = self.call("POST", f"/api/exports/{self.export_id}/import")
        self.assertEqual(status, 409)
        self.assertIn("staging folder", body["error"])


class PerPartImportsApiTests(unittest.TestCase):
    def setUp(self):
        from tests.test_finish_export import FinishWorld

        self.world = FinishWorld(self)
        settings = dataclasses.replace(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_PUBLIC_URL="http://127.0.0.1"),
                                       photos_root=self.world.settings.photos_root, data_dir=self.world.settings.data_dir,
                                       immich_url=self.world.settings.immich_url, immich_admin_key=self.world.settings.immich_admin_key)
        self.base, self.settings = start_server(self, settings, self.world.engine)
        self.replies = []

    call = ImportsApiTests.call

    def imported_with_a_failed_item(self):
        from tests.test_finish_export import failing_place

        export = self.world.create(self.world.scenario)
        with failing_place(PRIVATE_NAME):
            self.world.ingest_all(export)
            self.world.finish(export)
        return export

    def runs(self):
        return [(row["status"], row["failed"]) for row in sorted(self.call("GET", "/api/imports")[1]["imports"], key=lambda row: row["id"])]

    def test_a_per_part_export_with_a_failed_item_is_retried_and_then_finishes(self):
        from darkroom.domain.ingest import finish_export

        export = self.imported_with_a_failed_item()
        with no_leaks(self, PRIVATE_NAME, replies=self.replies):
            self.assertEqual(self.call("POST", f"/api/exports/{export.id}/import"), (202, {"export_id": export.id, "status": "importing", "stage": "unpacked",
                                                                                              "per_part": True, "parts_requeued": 0}))
            self.assertEqual(finish_export.finish_ready(self.world.reopen(), self.world.settings, NOW + DAY, self.world.tools), [export.id])
            self.assertEqual(self.runs(), [("done", 1), ("done", 0)])
        self.assertEqual(self.call("POST", f"/api/exports/{export.id}/import"), (409, {"error": "this export is already imported"}))

    def test_a_per_part_retry_whose_part_was_freed_is_refused_plainly(self):
        export = self.imported_with_a_failed_item()
        opened = self.world.reopen()
        opened.execute(update(ExportPart).where(ExportPart.export_id == export.id, ExportPart.number == 1).values(free_state="freed"))
        opened.commit()
        self.assertEqual(self.call("POST", f"/api/exports/{export.id}/import"),
                         (409, {"error": "part 1 of this export was deleted to free space, so its failed items cannot be retried from it"}))
        self.assertEqual(self.runs(), [("done", 1)])

    def test_a_refused_part_is_not_requeued_and_keeps_its_refusal(self):
        export = self.world.create(self.world.scenario, status="downloaded")
        opened = self.world.reopen()
        opened.execute(update(ExportPart).where(ExportPart.export_id == export.id, ExportPart.number == 2).values(ingest_state="refused", ingest_refusal="not_a_zip"))
        opened.commit()
        self.assertEqual(self.call("POST", f"/api/exports/{export.id}/import"),
                         (202, {"export_id": export.id, "status": "downloaded", "stage": None, "per_part": True, "parts_requeued": 0}))
        opened = self.world.reopen()
        states = opened.execute(select(ExportPart.ingest_state, ExportPart.ingest_refusal).where(ExportPart.export_id == export.id)
                                .order_by(ExportPart.number)).all()
        self.assertEqual([tuple(row) for row in states], [("queued", None), ("refused", "not_a_zip"), ("queued", None)])


class PerPartRetryApiTests(unittest.TestCase):
    def setUp(self):
        from tests.test_part_ingest import PartWorld

        self.world = PartWorld(self)
        settings = dataclasses.replace(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_PUBLIC_URL="http://127.0.0.1"),
                                       photos_root=self.world.settings.photos_root, data_dir=self.world.settings.data_dir,
                                       immich_url=self.world.settings.immich_url, immich_admin_key=self.world.settings.immich_admin_key)
        self.base, self.settings = start_server(self, settings, self.world.engine)
        self.export = self.world.create(self.world.scenario, status="downloading")

    def call(self, method, path):
        headers = {"Content-Type": "application/json", "Origin": self.base} if method != "GET" else {}
        status, _, body = fetch(self.base, method, path, headers, b"{}" if method != "GET" else None)
        return status, json.loads(body)

    def states(self):
        self.world.opened.expire_all()
        return [(part.ingest_state, part.ingest_failure) for part in self.world.opened.execute(
            select(ExportPart).where(ExportPart.export_id == self.export.id).order_by(ExportPart.number)).scalars()]

    def test_a_retry_requeues_only_the_failed_parts_and_says_how_many(self):
        self.world.ingest(self.export, 1)
        self.world.opened.execute(update(ExportPart).where(ExportPart.export_id == self.export.id, ExportPart.number == 2)
                                  .values(ingest_state="failed", ingest_failure="files", ingest_failure_type="OSError"))
        self.world.opened.execute(update(ExportPart).where(ExportPart.export_id == self.export.id, ExportPart.number == 3)
                                  .values(ingest_state="refused", ingest_refusal="not_a_zip"))
        self.world.opened.commit()
        exported = self.call("GET", f"/api/exports/{self.export.id}")[1]["export"]
        self.assertTrue(exported["import_retry_allowed"])
        self.assertEqual([part["import_failure"] for part in exported["parts"]], [None, {"code": "files", "error": "OSError"}, None])
        status, body = self.call("POST", f"/api/exports/{self.export.id}/import")
        self.assertEqual((status, body), (202, {"export_id": self.export.id, "status": "downloading", "stage": None, "per_part": True, "parts_requeued": 1}))
        self.assertEqual(self.states(), [("ingested", None), ("queued", None), ("refused", None)])
        self.world.opened.expire_all()
        self.assertEqual(self.world.part(self.export, 3).ingest_refusal, "not_a_zip")
        self.assertFalse(self.call("GET", f"/api/exports/{self.export.id}")[1]["export"]["import_retry_allowed"])

    def test_a_refused_part_alone_offers_no_retry_and_a_request_leaves_it_refused(self):
        self.world.opened.execute(update(ExportPart).where(ExportPart.export_id == self.export.id, ExportPart.number == 2)
                                  .values(ingest_state="refused", ingest_refusal="not_a_zip"))
        self.world.opened.commit()
        self.assertFalse(self.call("GET", f"/api/exports/{self.export.id}")[1]["export"]["import_retry_allowed"])
        before = self.states()
        self.assertEqual(self.call("POST", f"/api/exports/{self.export.id}/import"), (409, {"error": "this export has not been downloaded yet"}))
        self.assertEqual(self.states(), before)

    def test_a_retry_with_nothing_failed_is_refused_and_changes_nothing(self):
        self.world.ingest(self.export, 1)
        before = self.states()
        self.assertEqual(self.call("POST", f"/api/exports/{self.export.id}/import"), (409, {"error": "this export has not been downloaded yet"}))
        self.assertEqual(self.states(), before)


if __name__ == "__main__":
    unittest.main()
