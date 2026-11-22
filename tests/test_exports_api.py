import json
import sqlite3
import threading
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from darkroom.dates import utc_now
from darkroom.domain import browser_jobs, downloads, exports
from darkroom.storage import db
from darkroom.storage.models import PART_REFUSALS, BrowserJob, Event, Export, ExportPart, GoogleAccount, TakeoutEntry, TakeoutSidecar
from tests.api_world import ApiCase
from tests.privacy import no_leaks
from tests.shared import worker_fetch

TOKEN = "exports-api-worker-token-canary-0123456789abcdef"
DAY = 86400.0


class ExportsApiTests(ApiCase):
    extra_env = {"browser_token": TOKEN}

    def setUp(self):
        super().setUp()
        self.person_id = self.person()["id"]
        self.account_id = self.account(self.person_id)["id"]
        self.now = utc_now().timestamp()
        self.set_account(self.account_id, session_state="ok", schedule_created=self.now - DAY, schedule_end=self.now + 364 * DAY)

    def set_account(self, account_id, **values):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, account_id)
            for name, value in values.items():
                setattr(account, name, value)

    def worker(self, path, body=None):
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        status, _, reply = worker_fetch(self.base, "POST", path, headers, json.dumps(body or {}).encode())
        return status, json.loads(reply)

    def listing(self, *exports):
        return {"exports": list(exports)}

    def ready(self, google_id="g-1", parts=2):
        return {"google_id": google_id, "created_at": self.now - 3600, "state": "ready", "expires_at": self.now + 6 * DAY, "parts": [{"number": n + 1, "bytes": 1000 * (n + 1)} for n in range(parts)]}

    def check_and_finish(self, listing):
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertIn(status, (200, 201), reply)
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        while job["kind"] != "status":
            job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        return self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": listing})

    def test_nothing_is_listed_before_a_check(self):
        status, reply = self.call("GET", "/api/exports")
        self.assertEqual((status, reply["exports"]), (200, []))
        self.assertEqual([(each["account_id"], each["last_export_at"]) for each in reply["schedules"]], [(self.account_id, None)])
        self.assertGreater(reply["schedules"][0]["next_export_at"], self.now)

    def test_a_worker_listing_with_an_infinite_time_is_refused_and_nothing_is_stored(self):
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertIn(status, (200, 201), reply)
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        while job["kind"] != "status":
            job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        status, reply = self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": self.listing({**self.ready(), "created_at": float("inf")})})
        self.assertEqual(status, 400, reply)
        status, reply = self.call("GET", "/api/exports")
        self.assertEqual((status, reply["exports"]), (200, []))
        status, reply = self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": self.listing(self.ready())})
        self.assertEqual(status, 200, reply)
        self.assertEqual([each["created_at"] for each in self.call("GET", "/api/exports")[1]["exports"]], [self.now - 3600])

    def claimed_status_job(self):
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertIn(status, (200, 201), reply)
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        while job["kind"] != "status":
            job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        return job["id"]

    def test_the_largest_part_size_is_stored_exactly_and_a_larger_one_is_refused(self):
        job_id = self.claimed_status_job()
        too_big = self.listing({**self.ready(), "parts": [{"number": 1, "bytes": 2**63}]})
        status, reply = self.worker(f"/internal/browser/jobs/{job_id}/done", {"result": too_big})
        self.assertEqual(status, 400, reply)
        largest = self.listing({**self.ready(), "parts": [{"number": 1, "bytes": 2**63 - 1}, {"number": 2, "bytes": 2**53 + 1}]})
        status, reply = self.worker(f"/internal/browser/jobs/{job_id}/done", {"result": largest})
        self.assertEqual(status, 200, reply)
        with db.session(self.engine) as opened:
            self.assertEqual(sorted(part.bytes for part in opened.execute(select(ExportPart)).scalars()), [2**53 + 1, 2**63 - 1])

    def test_a_number_too_long_to_parse_is_refused_cleanly(self):
        job_id = self.claimed_status_job()
        body = json.dumps({"result": self.listing({**self.ready(), "parts": [{"number": 1, "bytes": 123456789}]})}).replace("123456789", "1" + "0" * 5000)
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        status, _, reply = worker_fetch(self.base, "POST", f"/internal/browser/jobs/{job_id}/done", headers, body.encode())
        self.assertEqual(status, 400, reply)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(ExportPart)).scalars().all(), [])

    def test_a_bad_time_already_stored_is_listed_as_unknown_and_the_page_still_loads(self):
        with db.session(self.engine) as opened:
            opened.add(Export(account_id=self.account_id, kind="manual", google_id="g-bad", created_at=float("inf"), ready_at=float("-inf"), expires_at=1e300, status="announced"))
            opened.add(Export(account_id=self.account_id, kind="manual", google_id="g-good", created_at=self.now - DAY, status="announced"))
        self.set_account(self.account_id, schedule_created=float("inf"))
        status, reply = self.call("GET", "/api/exports")
        self.assertEqual(status, 200, reply)
        self.assertNotIn(b"Infinity", self.replies[-1])
        self.assertCountEqual([(each["created_at"], each["ready_at"], each["expires_at"]) for each in reply["exports"]], [(None, None, None), (self.now - DAY, None, None)])
        self.assertEqual([(each["last_export_at"], each["next_export_at"]) for each in reply["schedules"]], [(self.now - DAY, None)])
        status, one = self.call("GET", f"/api/exports/{reply['exports'][0]['id']}")
        self.assertEqual(status, 200, one)
        self.assertNotIn(b"Infinity", self.replies[-1])

    def test_check_now_queues_a_status_job_for_each_connected_account_and_not_twice(self):
        other = self.account(self.person_id, "other@example.test")["id"]
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual(status, 201)
        self.assertEqual([(job["account_id"], job["kind"], job["status"]) for job in reply["jobs"]], [(self.account_id, "status", "queued")])
        self.assertEqual(reply["skipped"], 0)
        self.set_account(other, session_state="ok")
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, [job["account_id"] for job in reply["jobs"]], reply["skipped"]), (201, [other], 1))
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, reply["jobs"], reply["skipped"]), (200, [], 2))
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(BrowserJob)).scalars().all()), 2)

    def test_check_now_says_when_darkroom_is_busy_and_queues_nothing(self):
        with mock.patch.object(browser_jobs, "enqueue_once", side_effect=browser_jobs.Busy("Darkroom is busy with another change; try again in a moment")):
            status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, reply), (503, {"error": "Darkroom is busy with another change; try again in a moment"}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(BrowserJob)).scalars().all(), [])

    def test_check_now_with_no_connected_account_queues_nothing(self):
        self.set_account(self.account_id, session_state="needs_sign_in")
        status, reply = self.call("POST", "/api/exports/check-now")
        self.assertEqual((status, reply), (200, {"jobs": [], "skipped": 0}))

    def test_a_finished_status_job_announces_exports_and_the_page_can_list_them(self):
        status, reply = self.check_and_finish(self.listing(self.ready(), {"google_id": "g-2", "created_at": self.now - 60, "state": "in_progress", "expires_at": None, "parts": []}))
        self.assertEqual(status, 200, reply)
        status, reply = self.call("GET", "/api/exports")
        self.assertEqual(status, 200)
        by_id = {"ready" if each["parts"] else "waiting": each for each in reply["exports"]}
        ready = by_id["ready"]
        self.assertEqual((ready["kind"], ready["status"], ready["part_count"], ready["account_id"]), ("initial", "announced", 2, self.account_id))
        self.assertEqual([(part["number"], part["bytes"], part["progress"], part["downloads_used"], part["status"]) for part in ready["parts"]], [(1, 1000, 0, 0, "pending"), (2, 2000, 0, 0, "pending")])
        self.assertEqual((by_id["waiting"]["ready_at"], by_id["waiting"]["parts"]), (None, []))
        self.assertEqual(reply["schedules"][0]["last_export_at"], by_id["waiting"]["created_at"])
        status, one = self.call("GET", f"/api/exports/{ready['id']}")
        self.assertEqual((status, one["export"]), (200, ready))
        with db.session(self.engine) as opened:
            kinds = sorted(job.kind for job in opened.execute(select(BrowserJob)).scalars())
        self.assertEqual(kinds, ["download", "status"])

    def test_a_second_check_with_the_same_listing_changes_nothing(self):
        listing = self.listing(self.ready())
        self.check_and_finish(listing)
        first = self.call("GET", "/api/exports")[1]["exports"]
        self.check_and_finish(listing)
        self.assertEqual(self.call("GET", "/api/exports")[1]["exports"], first)
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(BrowserJob).where(BrowserJob.kind == "download")).scalars().all()), 1)

    def test_a_listing_that_is_not_valid_is_refused_and_the_job_stays_open(self):
        self.call("POST", "/api/exports/check-now")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        for result in ({}, {"exports": [{"google_id": "x"}]}, {"exports": [self.ready(parts=0)]}):
            status, reply = self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": result})
            self.assertEqual(status, 400, reply)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job["id"]).status, "running")
            self.assertEqual(opened.execute(select(Export)).scalars().all(), [])
        self.assertEqual(self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": self.listing()})[0], 200)

    def test_the_first_export_after_the_schedule_is_initial_and_later_ones_incremental(self):
        self.check_and_finish(self.listing(self.ready("g-1")))
        later = {**self.ready("g-2"), "created_at": self.now + 61 * DAY, "expires_at": self.now + 68 * DAY}
        self.check_and_finish(self.listing(later))
        kinds = [row["kind"] for row in self.call("GET", "/api/exports")[1]["exports"]]
        self.assertEqual(sorted(kinds), ["incremental", "initial"])

    def test_one_export_is_returned_by_id_and_unknown_ids_are_404(self):
        self.assertEqual(self.call("GET", "/api/exports/99")[0], 404)
        self.assertEqual(self.call("GET", "/api/exports/abc")[0], 404)
        self.assertEqual(self.call("GET", "/api/exports/" + "9" * 13)[0], 404)

    def test_the_session_is_marked_ok_by_a_finished_status_job(self):
        self.call("POST", "/api/exports/check-now")
        job = self.worker("/internal/browser/jobs/claim")[1]["job"]
        self.worker(f"/internal/browser/jobs/{job['id']}/waiting", {"reason": "sign_in"})
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account_id).session_state, "needs_sign_in")
        self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": self.listing()})
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account_id).session_state, "ok")

    def test_a_status_job_cannot_be_queued_through_the_generic_endpoint(self):
        status, reply = self.call("POST", "/api/browser/jobs", {"account_id": self.account_id, "kind": "status"})
        self.assertEqual(status, 400, reply)

    def test_an_export_that_expired_before_it_was_seen_is_listed_as_expired_with_no_parts(self):
        old = {"google_id": "g-old", "created_at": self.now - 10 * DAY, "state": "ready", "expires_at": self.now - 3 * DAY, "parts": [{"number": 1, "bytes": 5}]}
        self.check_and_finish(self.listing(old))
        (row,) = self.call("GET", "/api/exports")[1]["exports"]
        self.assertEqual((row["status"], row["parts"], row["ready_at"]), ("expired", [], None))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(BrowserJob).where(BrowserJob.kind == "download")).scalars().all(), [])


class RetryDownloadApiTests(ApiCase):
    extra_env = {"browser_token": TOKEN}

    def setUp(self):
        super().setUp()
        self.account_id = self.account(self.person()["id"])["id"]
        self.now = utc_now().timestamp()
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account_id)
            account.session_state, account.schedule_created, account.schedule_end = "ok", self.now - DAY, self.now + 364 * DAY

    def failed_export(self, **values):
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account_id)
            listed = exports.Listed("g-retry", self.now - 3600, "ready", self.now + 6 * DAY, (exports.ListedPart(1, 1000), exports.ListedPart(2, 2000)))
            exports.reconcile(opened, account, [listed], self.now)
            export = opened.execute(select(Export).where(Export.google_id == "g-retry")).scalar_one()
            for job in opened.execute(select(BrowserJob).where(BrowserJob.kind == "download")).scalars():
                job.status = "failed"
            export.status, export.download_tries = "failed", exports.MAX_DOWNLOAD_JOBS
            for part in exports.parts_of(opened, export.id):
                part.status, part.attempts, part.downloads_used = "failed", 3, 3
            for name, value in values.items():
                setattr(export, name, value)
            return export.id

    def queued_downloads(self):
        with db.session(self.engine) as opened:
            return len(opened.execute(select(BrowserJob).where(BrowserJob.kind == "download", BrowserJob.status == "queued")).scalars().all())

    def test_a_failed_export_is_retried_and_a_second_press_changes_nothing(self):
        export_id = self.failed_export()
        self.replies.clear()
        with no_leaks(self, TOKEN, "jo.photos@example.test", replies=self.replies):
            (listed,) = self.call("GET", "/api/exports")[1]["exports"]
            self.assertEqual((listed["retry_allowed"], listed["retry_refusal"]), (True, None))
            status, reply = self.call("POST", f"/api/exports/{export_id}/retry-download")
            self.assertEqual((status, reply["retried"], reply["export"]["status"]), (200, True, "downloading"), reply)
            self.assertEqual([(part["status"], part["attempts"], part["downloads_used"]) for part in reply["export"]["parts"]], [("pending", 0, 3)] * 2)
            status, again = self.call("POST", f"/api/exports/{export_id}/retry-download")
            self.assertEqual((status, again["retried"], again["export"]), (200, False, reply["export"]))
            self.assertEqual((again["export"]["retry_allowed"], again["export"]["retry_refusal"]), (False, None))
        self.assertEqual(self.queued_downloads(), 1)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(Event.detail).where(Event.type == "export_download_retried")).scalars().all(), ["2 parts reset"])

    def test_a_refusal_is_409_with_its_sentence_and_changes_nothing(self):
        export_id = self.failed_export(expires_at=self.now - 60)
        self.replies.clear()
        with no_leaks(self, TOKEN, "jo.photos@example.test", replies=self.replies):
            status, reply = self.call("POST", f"/api/exports/{export_id}/retry-download")
        self.assertEqual((status, reply), (409, {"error": "This export has expired in Takeout, so it can't be downloaded again."}))
        (listed,) = self.call("GET", "/api/exports")[1]["exports"]
        self.assertEqual((listed["retry_allowed"], listed["retry_refusal"]), (False, reply["error"]))
        self.assertEqual(self.call("GET", f"/api/exports/{export_id}")[1]["export"]["status"], "failed")
        self.assertEqual(self.queued_downloads(), 0)

    def test_a_part_out_of_googles_downloads_is_refused_with_what_to_do(self):
        export_id = self.failed_export()
        with db.session(self.engine) as opened:
            exports.parts_of(opened, export_id)[1].downloads_used = 5
        status, reply = self.call("POST", f"/api/exports/{export_id}/retry-download")
        self.assertEqual(status, 409, reply)
        self.assertIn("Ask Takeout for a new export", reply["error"])

    def test_unknown_exports_are_404(self):
        for path in ("99", "abc", "9" * 13):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", f"/api/exports/{path}/retry-download")[0], 404)

    def test_a_database_busy_while_saving_the_retry_is_503_and_another_error_is_not_hidden(self):
        export_id = self.failed_export()
        locked = OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
        with mock.patch.object(downloads, "retry_failed", side_effect=locked):
            status, reply = self.call("POST", f"/api/exports/{export_id}/retry-download")
        self.assertEqual((status, reply), (503, {"error": "Darkroom is busy with another change; try again in a moment"}))
        broken = OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error"))
        with mock.patch.object(downloads, "retry_failed", side_effect=broken):
            self.assertEqual(self.call("POST", f"/api/exports/{export_id}/retry-download")[0], 500)
        self.assertEqual(self.call("GET", f"/api/exports/{export_id}")[1]["export"]["status"], "failed")

    def test_two_presses_at_once_retry_once_and_queue_one_job(self):
        export_id = self.failed_export()
        gate, outcomes = threading.Barrier(2), []

        def press():
            gate.wait()
            outcomes.append(self.call("POST", f"/api/exports/{export_id}/retry-download"))

        threads = [threading.Thread(target=press) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.queued_downloads(), 1)
        retried = [reply["retried"] for status, reply in outcomes if status == 200]
        self.assertEqual(retried.count(True), 1)
        self.assertTrue(all(status in (200, 503) for status, _ in outcomes), outcomes)
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(Event.id).where(Event.type == "export_download_retried")).all()), 1)

    def test_a_busy_database_is_503_and_changes_nothing(self):
        export_id = self.failed_export()
        with mock.patch.object(db, "write_lock", side_effect=browser_jobs.Busy("Darkroom is busy with another change; try again in a moment")):
            status, reply = self.call("POST", f"/api/exports/{export_id}/retry-download")
        self.assertEqual(status, 503, reply)
        self.assertEqual(self.call("GET", f"/api/exports/{export_id}")[1]["export"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()


class ExportsApiIngestFieldTests(ApiCase):
    extra_env = {"browser_token": TOKEN}
    PART_DEFAULTS = {"ingest_state": "not_queued", "entries": 0, "entries_done": 0, "held": 0, "held_waiting_sidecar": 0, "held_waiting_export": 0,
                     "failed_items": 0, "kept_reason": None, "freed": False, "bytes_freed": 0}
    EXPORT_DEFAULTS = {"parts_ingested": 0, "held_waiting_sidecar": 0, "held_waiting_export": 0, "bytes_freed": 0}

    def setUp(self):
        super().setUp()
        self.account_id = self.account(self.person()["id"])["id"]
        self.now = utc_now().timestamp()

    def add_export(self, google_id, states):
        with db.session(self.engine) as opened:
            export = Export(account_id=self.account_id, kind="manual", google_id=google_id, created_at=self.now - DAY, status="downloading", part_count=len(states))
            opened.add(export)
            opened.flush()
            for number, values in enumerate(states, start=1):
                opened.add(ExportPart(export_id=export.id, number=number, filename=f"part-{number}.zip", bytes=100, status="done", **values))
            opened.flush()
            return export.id

    def entries(self, export_id, part_number, **outcomes):
        with db.session(self.engine) as opened:
            for outcome, count in outcomes.items():
                for index in range(count):
                    opened.add(TakeoutEntry(export_id=export_id, part_number=part_number, name=f"canary-{part_number}-{outcome}-{index}.jpg", kind="media", bytes=1,
                                            crc32=1, outcome=outcome, listed_at=self.now))

    def listed(self, export_id):
        status, reply = self.call("GET", f"/api/exports/{export_id}")
        self.assertEqual(status, 200, reply)
        status, every = self.call("GET", "/api/exports")
        self.assertEqual(status, 200, every)
        self.assertEqual([each for each in every["exports"] if each["id"] == export_id], [reply["export"]])
        return reply["export"]

    def test_an_export_that_never_went_per_part_reports_the_defaults(self):
        export_id = self.add_export("g-old", [{}, {}])
        export = self.listed(export_id)
        self.assertEqual({key: export[key] for key in self.EXPORT_DEFAULTS}, self.EXPORT_DEFAULTS)
        for part in export["parts"]:
            self.assertEqual({key: part[key] for key in self.PART_DEFAULTS}, self.PART_DEFAULTS)
            self.assertEqual(sorted(part), sorted([*self.PART_DEFAULTS, "number", "filename", "bytes", "progress", "downloads_used", "attempts", "status"]))
        self.assertEqual([(part["number"], part["filename"], part["bytes"], part["progress"], part["status"]) for part in export["parts"]],
                         [(1, "part-1.zip", 100, 100, "done"), (2, "part-2.zip", 100, 100, "done")])

    def test_a_part_by_part_export_reports_each_part_and_the_held_split_from_the_ledger(self):
        export_id = self.add_export("g-new", [
            {"ingest_state": "ingested", "entries": 5, "entries_done": 5, "held": 3},
            {"ingest_state": "ingested", "entries": 4, "entries_done": 4, "held": 1, "failed_items": 2},
            {"ingest_state": "ingesting", "entries": 6, "entries_done": 2},
            {"ingest_state": "queued"},
        ])
        self.entries(export_id, 1, waiting_sidecar=2, waiting_export=1, placed=2)
        self.entries(export_id, 2, waiting_export=1, failed=2, skipped=1)
        other = self.add_export("g-other", [{"ingest_state": "ingested", "entries": 1, "entries_done": 1, "held": 7}])
        self.entries(other, 1, waiting_sidecar=7)
        export = self.listed(export_id)
        self.assertEqual({key: export[key] for key in self.EXPORT_DEFAULTS}, {"parts_ingested": 2, "held_waiting_sidecar": 2, "held_waiting_export": 2, "bytes_freed": 0})
        self.assertEqual([(part["ingest_state"], part["entries"], part["entries_done"], part["held"], part["held_waiting_sidecar"], part["held_waiting_export"],
                           part["failed_items"], part["kept_reason"]) for part in export["parts"]], [
            ("ingested", 5, 5, 3, 2, 1, 0, None),
            ("ingested", 4, 4, 1, 0, 1, 2, "failed_item"),
            ("ingesting", 6, 2, 0, 0, 0, 0, None),
            ("queued", 0, 0, 0, 0, 0, 0, None),
        ])
        self.assertEqual(self.listed(other)["held_waiting_sidecar"], 7)

    def test_a_freed_part_and_its_export_report_the_bytes_freed_and_a_part_still_freeing_does_not(self):
        export_id = self.add_export("g-freed", [
            {"ingest_state": "ingested", "free_state": "freed", "bytes_freed": 1200, "freed_at": self.now},
            {"ingest_state": "ingested", "free_state": "freeing", "bytes_freed": 900},
            {"ingest_state": "ingested", "free_state": "freed", "bytes_freed": 34},
            {"ingest_state": "ingested"},
        ])
        export = self.listed(export_id)
        self.assertEqual([(part["ingest_state"], part["freed"], part["bytes_freed"]) for part in export["parts"]],
                         [("ingested", True, 1200), ("ingested", False, 0), ("ingested", True, 34), ("ingested", False, 0)])
        self.assertEqual((export["bytes_freed"], export["parts_ingested"]), (1234, 4))

    def test_a_refused_part_reports_its_reason_code_and_no_entry_name(self):
        export_id = self.add_export("g-refused", [{"ingest_state": "refused", "ingest_refusal": "zip_bomb", "entries": 3, "entries_done": 1, "failed_items": 1},
                                                  {"ingest_state": "failed"}])
        self.entries(export_id, 1, waiting_sidecar=1)
        export = self.listed(export_id)
        self.assertEqual([(part["ingest_state"], part["kept_reason"]) for part in export["parts"]], [("refused", "zip_bomb"), ("failed", None)])
        self.assertEqual(export["parts_ingested"], 0)

    def test_each_refusal_reason_is_reported_as_the_part_s_kept_reason(self):
        export_id = self.add_export("g-every-refusal", [{"ingest_state": "refused", "ingest_refusal": reason} for reason in PART_REFUSALS])
        export = self.listed(export_id)
        self.assertEqual([(part["ingest_state"], part["kept_reason"]) for part in export["parts"]], [("refused", reason) for reason in PART_REFUSALS])

    def test_a_part_that_was_never_refused_reports_no_refusal(self):
        export_id = self.add_export("g-never-refused", [{"ingest_state": "ingested", "entries": 2, "entries_done": 2},
                                                        {"ingest_state": "ingested", "entries": 2, "entries_done": 2, "failed_items": 1}, {"ingest_state": "queued"}])
        export = self.listed(export_id)
        self.assertEqual([(part["ingest_state"], part["kept_reason"]) for part in export["parts"]], [("ingested", None), ("ingested", "failed_item"), ("queued", None)])

    def test_a_refused_part_from_before_reasons_were_stored_reports_none(self):
        export_id = self.add_export("g-old-refusal", [{"ingest_state": "refused", "failed_items": 2}])
        self.assertEqual([part["kept_reason"] for part in self.listed(export_id)["parts"]], [None])

    def test_the_refusal_reply_carries_no_entry_name_path_or_sidecar_text(self):
        export_id = self.add_export("g-refusal-canary", [{"ingest_state": "refused", "ingest_refusal": "listing_differs", "entries": 2}])
        self.entries(export_id, 1, waiting_sidecar=1, failed=1)
        with db.session(self.engine) as opened:
            entry = TakeoutEntry(export_id=export_id, part_number=1, name="canary-folder-of-refused-part/canary-sidecar-name.json", kind="sidecar", bytes=40,
                                 crc32=1, outcome="recorded", listed_at=self.now)
            opened.add(entry)
            opened.flush()
            opened.add(TakeoutSidecar(export_id=export_id, part_number=1, entry_id=entry.id, folder="canary-folder-of-refused-part", name="canary-sidecar-name.json",
                                      text="canary sidecar text for the refused part"))
        status, one = self.call("GET", f"/api/exports/{export_id}")
        self.assertEqual(status, 200, one)
        status, every = self.call("GET", "/api/exports")
        self.assertEqual(status, 200, every)
        self.assertEqual(one["export"]["parts"][0]["kept_reason"], "listing_differs")
        with no_leaks(self, "canary-1-", "canary-folder-of-refused-part", "canary-sidecar-name", "canary sidecar text",
                      replies=[json.dumps(one).encode(), json.dumps(every).encode()]):
            pass

    def test_every_export_is_counted_in_one_ledger_query_not_one_per_part(self):
        export_id = self.add_export("g-queries", [{"ingest_state": "ingested", "entries": 1, "held": 1} for _ in range(6)])
        statements = []

        def count(connection, cursor, statement, *rest):
            if "takeout_entries" in statement:
                statements.append(statement)

        from sqlalchemy import event
        event.listen(self.engine, "before_cursor_execute", count)
        try:
            self.assertEqual(len(self.listed(export_id)["parts"]), 6)
        finally:
            event.remove(self.engine, "before_cursor_execute", count)
        self.assertEqual(len(statements), 2)

    def test_entry_names_never_appear_in_the_reply(self):
        export_id = self.add_export("g-private", [{"ingest_state": "ingested", "entries": 2, "held": 2, "failed_items": 1}])
        self.entries(export_id, 1, waiting_sidecar=1, waiting_export=1)
        with no_leaks(self, "canary-1-waiting_sidecar-0.jpg", "canary-1-waiting_export-0.jpg", replies=self.replies):
            self.listed(export_id)
