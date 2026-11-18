import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import accounts, browser_jobs, downloads, exports
from darkroom.domain.ingest import run
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Event, Export, ExportPart, GoogleAccount
from darkroom.storage.paths import Photos
from tests.shared import add_person, own_database, settings_from

NOW = 1_800_000_000.0
DAY = 86400.0
MB = 1024 * 1024


def listed(google_id="g-1", parts=((1, 4 * MB), (2, 2 * MB)), expires=NOW + 7 * DAY):
    return exports.Listed(google_id, NOW - 600, "ready", expires, tuple(exports.ListedPart(number, size) for number, size in parts))


class DownloadCase(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.settings = settings_from(DARKROOM_PHOTOS_ROOT=folder.name, DARKROOM_PUBLIC_URL="https://darkroom.example")
        with db.session(self.engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo.takeout@example.test", default_person_id=person, created=1.0, session_state="ok", schedule_created=NOW - DAY,
                                    schedule_end=NOW + 365 * DAY)
            opened.add(account)
            opened.flush()
            self.account = account.id
            exports.reconcile(opened, account, [listed()], NOW)
            self.export = opened.execute(select(Export)).scalar_one().id

    def reconcile(self, now):
        with db.session(self.engine) as opened:
            return exports.reconcile(opened, opened.get(GoogleAccount, self.account), [listed()], now)

    def claim(self, now=NOW):
        with db.session(self.engine) as opened:
            job = browser_jobs.claim(opened, now)
            self.assertEqual(job.kind, "download")
            return job.id

    def call(self, work, *args):
        with db.session(self.engine) as opened:
            return work(opened, *args)

    def attempt(self, job, number, now=NOW):
        return self.call(downloads.attempt, job, number, now)

    def fail_part(self, job, number, reason="corrupt", now=NOW):
        return self.call(downloads.part_failed, job, number, reason, now)

    def folder(self):
        return Photos(self.root).export_tree("jo", f"export-{self.export}")

    def write_zip(self, name, size):
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("Takeout/Google Photos/a.bin", b"x" * size)
        return path

    def part_done(self, job, number, name, now=NOW):
        path = self.write_zip(name, [4 * MB, 2 * MB][number - 1])
        return self.call(lambda opened: downloads.part_done(opened, self.settings, job, number, name, path.stat().st_size, now))

    def part(self, number):
        with db.session(self.engine) as opened:
            row = opened.execute(select(ExportPart).where(ExportPart.export_id == self.export, ExportPart.number == number)).scalar_one()
            return row.status, row.downloads_used, row.attempts, row.bytes_done

    def export_row(self):
        with db.session(self.engine) as opened:
            row = opened.get(Export, self.export)
            return row.status, row.download_tries, row.stage, row.inbox_dir

    def fail_job(self, job, now=NOW):
        with db.session(self.engine) as opened:
            failed = browser_jobs.failed(opened, self.settings, job, "UnexpectedPage: the page changed", None, [], now)
            downloads.job_failed(opened, failed, now)

    def download_jobs(self):
        with db.session(self.engine) as opened:
            return [(json.loads(job.payload)["export_id"], job.status) for job in opened.execute(select(BrowserJob).where(BrowserJob.kind == "download").order_by(BrowserJob.id)).scalars()]


class AttemptTests(DownloadCase):
    def test_every_attempt_counts_a_download_and_an_attempt_before_the_click(self):
        job = self.claim()
        reply = self.attempt(job, 1)
        self.assertEqual((reply["allowed"], reply["gave_up"]), (True, False))
        self.assertEqual(self.part(1), ("downloading", 1, 1, 0))
        self.assertEqual(self.export_row()[0], "downloading")
        self.fail_part(job, 1, "interrupted")
        self.assertEqual(self.part(1), ("pending", 1, 1, None))
        self.attempt(job, 1)
        self.assertEqual(self.part(1), ("downloading", 2, 2, 0))

    def test_a_corrupt_download_is_retried_twice_then_the_part_and_the_export_fail(self):
        job = self.claim()
        for tries in (1, 2):
            self.assertTrue(self.attempt(job, 2)["allowed"])
            reply = self.fail_part(job, 2)
            self.assertEqual((reply["allowed"], reply["gave_up"]), (True, False))
            self.assertEqual(self.part(2), ("corrupt", tries, tries, None))
        self.assertTrue(self.attempt(job, 2)["allowed"])
        reply = self.fail_part(job, 2)
        self.assertEqual((reply["allowed"], reply["gave_up"]), (False, True))
        self.assertEqual(self.part(2), ("failed", 3, 3, None))
        self.assertEqual(self.export_row()[0], "failed")
        with self.assertRaises(downloads.Refused):
            self.attempt(job, 2)
        with db.session(self.engine) as opened, self.assertRaises(run.NotReady):
            run.request_import(opened, self.export)
        self.assertEqual(self.export_row(), ("failed", 0, None, None))

    def test_never_a_sixth_download_even_when_earlier_ones_were_used_outside_this_job(self):
        with db.session(self.engine) as opened:
            row = opened.execute(select(ExportPart).where(ExportPart.export_id == self.export, ExportPart.number == 1)).scalar_one()
            row.downloads_used = 5
        job = self.claim()
        reply = self.attempt(job, 1)
        self.assertEqual((reply["allowed"], reply["gave_up"]), (False, True))
        self.assertEqual(self.part(1), ("failed", 5, 0, None))
        self.assertEqual(self.export_row()[0], "failed")

    def test_the_counters_survive_a_restart_so_a_new_job_cannot_reset_them(self):
        first = self.claim()
        self.attempt(first, 1)
        self.fail_part(first, 1)
        self.attempt(first, 1)
        self.fail_job(first)
        self.assertEqual(self.part(1), ("pending", 2, 2, None))
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 1)
        second = self.claim(NOW + DAY)
        self.assertTrue(self.attempt(second, 1, NOW + DAY)["allowed"])
        self.assertEqual(self.part(1), ("downloading", 3, 3, 0))
        self.assertTrue(self.fail_part(second, 1, now=NOW + DAY)["gave_up"])
        self.assertEqual(self.part(1)[:3], ("failed", 3, 3))

    def test_progress_is_kept_and_shown_as_a_share_of_the_listed_size(self):
        job = self.claim()
        self.attempt(job, 1)
        self.call(downloads.progress, job, 1, MB, NOW)
        self.assertEqual(self.part(1)[3], MB)
        with db.session(self.engine) as opened:
            self.assertEqual([part["progress"] for part in exports.as_dict(opened, opened.get(Export, self.export), NOW)["parts"]], [25, 0])
        with self.assertRaises(downloads.Refused):
            self.call(downloads.progress, job, 2, MB, NOW)
        with self.assertRaises(downloads.Refused):
            self.call(downloads.progress, job, 1, -1, NOW)

class PartDoneTests(DownloadCase):
    def test_a_finished_part_is_recorded_with_its_file_name(self):
        job = self.claim()
        self.attempt(job, 1)
        self.part_done(job, 1, "takeout-20270115T030000Z-001.zip")
        status, used, attempts, done = self.part(1)
        self.assertEqual((status, used, attempts), ("done", 1, 1))
        self.assertGreater(done, 4 * MB)
        with self.assertRaises(downloads.Refused):
            self.attempt(job, 1)

    def test_a_file_name_that_is_not_a_takeout_zip_is_refused(self):
        job = self.claim()
        self.attempt(job, 1)
        for name in ("../originals/x.zip", ".hidden.zip", "part.tar", "a/b.zip", "x" * 300 + ".zip"):
            with self.assertRaises(downloads.Refused, msg=name):
                self.call(lambda opened, name=name: downloads.part_done(opened, self.settings, job, 1, name, 10, NOW))
        self.assertEqual(self.part(1)[0], "downloading")

    def test_a_file_that_is_missing_a_link_or_the_wrong_size_is_refused(self):
        job = self.claim()
        self.attempt(job, 1)
        small, real = self.write_zip("small.zip", MB).stat().st_size, self.write_zip("real.zip", 4 * MB).stat().st_size
        (self.folder() / "link.zip").symlink_to(self.folder() / "real.zip")
        for name, size in (("missing.zip", 4 * MB), ("real.zip", real + 1), ("small.zip", small), ("link.zip", real)):
            with self.assertRaises(downloads.Refused, msg=name):
                self.call(lambda opened, name=name, size=size: downloads.part_done(opened, self.settings, job, 1, name, size, NOW))
        self.assertEqual(self.part(1)[0], "downloading")

    def test_two_parts_cannot_share_a_file_name(self):
        job = self.claim()
        self.attempt(job, 1)
        self.part_done(job, 1, "takeout-001.zip")
        self.attempt(job, 2)
        with self.assertRaises(downloads.Refused):
            self.call(lambda opened: downloads.part_done(opened, self.settings, job, 2, "takeout-001.zip", (self.folder() / "takeout-001.zip").stat().st_size, NOW))


class FinishTests(DownloadCase):
    def test_the_last_part_marks_the_export_downloaded_and_queues_the_import(self):
        job = self.claim()
        for number in (1, 2):
            self.attempt(job, number)
            self.part_done(job, number, f"takeout-00{number}.zip")
        with db.session(self.engine) as opened:
            downloads.finish(opened, self.settings, job, NOW)
            browser_jobs.done(opened, job, {}, NOW)
        status, tries, stage, inbox = self.export_row()
        self.assertEqual((status, tries, stage), ("downloaded", 0, "queued"))
        self.assertEqual(Path(inbox), self.folder())
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 0)
        self.assertEqual(self.download_jobs(), [(self.export, "done")])

    def test_an_export_with_a_part_left_is_not_finished(self):
        job = self.claim()
        self.attempt(job, 1)
        self.part_done(job, 1, "takeout-001.zip")
        with self.assertRaises(downloads.Refused):
            self.call(lambda opened: downloads.finish(opened, self.settings, job, NOW))
        self.assertEqual(self.export_row()[0], "downloading")


class RetryTests(DownloadCase):
    def test_a_failed_download_job_is_queued_again_at_the_next_check(self):
        job = self.claim()
        self.attempt(job, 1)
        self.fail_job(job)
        self.assertEqual(self.part(1), ("pending", 1, 1, None))
        self.assertEqual(self.export_row()[:2], ("downloading", 1))
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 1)
        self.assertEqual(self.download_jobs(), [(self.export, "failed"), (self.export, "queued")])
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 0)
        self.assertEqual(len(self.download_jobs()), 2)

    def test_after_three_failed_download_jobs_the_export_fails_and_is_not_queued_again(self):
        for day in range(3):
            job = self.claim(NOW + day * DAY)
            self.attempt(job, 1, NOW + day * DAY)
            self.fail_job(job, NOW + day * DAY)
            if day < 2:
                self.assertEqual(self.reconcile(NOW + (day + 1) * DAY).requeued, 1)
        self.assertEqual(self.export_row()[:2], ("failed", 3))
        self.assertEqual(self.reconcile(NOW + 4 * DAY).requeued, 0)
        self.assertEqual([status for _, status in self.download_jobs()], ["failed", "failed", "failed"])
        with db.session(self.engine) as opened:
            self.assertIn("export_download_failed", opened.execute(select(Event.type)).scalars().all())
            self.assertEqual(len(opened.execute(select(Alert).where(Alert.kind == "failure")).scalars().all()), 3)

    def test_a_job_that_fails_after_the_last_part_is_done_leaves_the_export_to_be_finished(self):
        first = self.claim()
        self.attempt(first, 1)
        self.part_done(first, 1, "takeout-001.zip")
        self.fail_job(first)
        self.reconcile(NOW + DAY)
        second = self.claim(NOW + DAY)
        self.attempt(second, 2, NOW + DAY)
        self.fail_job(second, NOW + DAY)
        self.reconcile(NOW + 2 * DAY)
        third = self.claim(NOW + 2 * DAY)
        self.attempt(third, 2, NOW + 2 * DAY)
        self.part_done(third, 2, "takeout-002.zip", NOW + 2 * DAY)
        self.fail_job(third, NOW + 2 * DAY)
        self.assertEqual(self.export_row()[:2], ("downloading", 2))
        self.assertEqual(self.reconcile(NOW + 3 * DAY).requeued, 1)
        last = self.claim(NOW + 3 * DAY)
        with db.session(self.engine) as opened:
            downloads.finish(opened, self.settings, last, NOW + 3 * DAY)
            browser_jobs.done(opened, last, {}, NOW + 3 * DAY)
        self.assertEqual(self.export_row()[:3], ("downloaded", 2, "queued"))

    def test_a_complete_export_whose_job_failed_is_finished_even_after_it_expires(self):
        late = {"past its expiry": listed(), "listed as expired": exports.Listed("g-1", NOW - 600, "expired", None, ())}
        for name, item in late.items():
            with self.subTest(name):
                self.setUp()
                job = self.claim()
                for number in (1, 2):
                    self.attempt(job, number)
                    self.part_done(job, number, f"takeout-00{number}.zip")
                self.fail_job(job)
                with db.session(self.engine) as opened:
                    self.assertEqual(exports.reconcile(opened, opened.get(GoogleAccount, self.account), [item], NOW + 8 * DAY).requeued, 1)
                self.assertEqual(self.export_row()[0], "downloading")
                last = self.claim(NOW + 8 * DAY)
                with db.session(self.engine) as opened:
                    downloads.finish(opened, self.settings, last, NOW + 8 * DAY)
                self.assertEqual(self.export_row()[:3], ("downloaded", 0, "queued"))

    def test_jobs_that_fail_before_any_attempt_use_no_tries_and_are_retried(self):
        for day in range(4):
            self.fail_job(self.claim(NOW + day * DAY), NOW + day * DAY)
            self.assertEqual(self.reconcile(NOW + (day + 1) * DAY).requeued, 1)
        self.assertEqual(self.export_row()[:2], ("announced", 0))
        self.assertEqual(self.part(1), ("pending", 0, 0, None))
        self.assertEqual([status for _, status in self.download_jobs()], ["failed"] * 4 + ["queued"])

    def test_a_check_that_finds_the_tries_used_up_marks_the_export_failed(self):
        job = self.claim()
        with db.session(self.engine) as opened:
            opened.get(Export, self.export).download_tries = exports.MAX_DOWNLOAD_JOBS
            browser_jobs.failed(opened, self.settings, job, "lost", None, [], NOW)
        self.assertEqual(self.export_row()[0], "announced")
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 0)
        self.assertEqual(self.export_row()[0], "failed")

    def test_an_export_that_expired_while_its_download_failed_is_expired_not_queued(self):
        job = self.claim()
        self.fail_job(job)
        self.assertEqual(self.reconcile(NOW + 8 * DAY).expired, 1)
        self.assertEqual(self.export_row()[0], "expired")
        self.assertEqual(len(self.download_jobs()), 1)

    def test_an_export_whose_download_job_is_still_open_is_left_alone(self):
        self.claim()
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 0)
        self.assertEqual(len(self.download_jobs()), 1)

    def test_a_download_job_that_lost_its_lease_puts_its_part_back_without_using_a_try(self):
        job = self.claim()
        self.attempt(job, 1)
        jobs.build(self.settings, self.engine, NOW).run_one("browser.leases", NOW + browser_jobs.LEASE_SECONDS + 1)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job).status, "failed")
        self.assertEqual(self.part(1), ("pending", 1, 0, None))
        self.assertEqual(self.export_row()[:2], ("downloading", 0))

    def test_a_complete_export_whose_finish_is_refused_every_time_ends_up_failed_and_frees_the_account(self):
        job = self.claim()
        for number in (1, 2):
            self.attempt(job, number)
            self.part_done(job, number, f"takeout-00{number}.zip")
        self.fail_job(job)
        late = NOW + 8 * DAY
        with mock.patch.object(downloads, "export_folder", side_effect=downloads.Refused("the folder became a link")):
            for round_ in range(exports.MAX_DOWNLOAD_JOBS):
                self.assertEqual(self.export_row()[:2], ("downloading", round_))
                self.assertEqual(self.reconcile(late + round_ * DAY).requeued, 1)
                retried = self.claim(late + round_ * DAY)
                with db.session(self.engine) as opened, self.assertRaises(downloads.Refused):
                    downloads.finish(opened, self.settings, retried, late + round_ * DAY)
                self.fail_job(retried, late + round_ * DAY)
            self.assertEqual(self.reconcile(late + 9 * DAY).requeued, 0)
        self.assertEqual(self.export_row()[:2], ("failed", exports.MAX_DOWNLOAD_JOBS))
        self.assertEqual(len(self.download_jobs()), 1 + exports.MAX_DOWNLOAD_JOBS)
        with db.session(self.engine) as opened:
            self.assertFalse(exports.download_underway(opened, self.account))

    def test_a_failed_job_that_is_not_a_download_changes_no_export(self):
        with db.session(self.engine) as opened:
            job = browser_jobs.queue(opened, self.account, "status", None, NOW)
            job.status = "failed"
            self.assertIsNone(downloads.job_failed(opened, job, NOW))
            gone = browser_jobs.queue(opened, self.account, "download", {"export_id": 999}, NOW)
            self.assertIsNone(downloads.job_failed(opened, gone, NOW))


class LeaseExpiryTests(DownloadCase):
    def lose_lease(self, now):
        jobs.build(self.settings, self.engine, now).run_one("browser.leases", now + browser_jobs.LEASE_SECONDS + 1)

    def interruptions(self):
        with db.session(self.engine) as opened:
            return opened.get(Export, self.export).download_interruptions

    def events(self, kind):
        with db.session(self.engine) as opened:
            return opened.execute(select(Event.detail).where(Event.type == kind).order_by(Event.id)).scalars().all()

    def test_a_lost_lease_is_recorded_as_an_interruption_not_a_failed_try(self):
        job = self.claim()
        self.attempt(job, 1)
        self.lose_lease(NOW)
        self.assertEqual((self.export_row()[:2], self.interruptions()), (("downloading", 0), 1))
        self.assertEqual(self.events("export_download_interrupted"), [f"1 of {downloads.MAX_INTERRUPTIONS} interruptions used"])
        self.assertEqual(self.events("export_download_failed"), [])
        self.assertEqual(self.reconcile(NOW + DAY).requeued, 1)

    def test_a_lost_lease_before_any_attempt_changes_nothing(self):
        self.claim()
        self.lose_lease(NOW)
        self.assertEqual((self.export_row()[:2], self.interruptions()), (("announced", 0), 0))
        self.assertEqual(self.part(1), ("pending", 0, 0, None))

    def test_once_the_interruptions_are_used_up_a_lost_lease_counts_as_a_try(self):
        with db.session(self.engine) as opened:
            opened.get(Export, self.export).download_interruptions = downloads.MAX_INTERRUPTIONS
        job = self.claim()
        self.attempt(job, 1)
        self.lose_lease(NOW)
        self.assertEqual((self.export_row()[:2], self.interruptions()), (("downloading", 1), downloads.MAX_INTERRUPTIONS))
        self.assertEqual(self.part(1), ("pending", 1, 1, None))
        self.assertEqual(self.events("export_download_interrupted"), [])

    def test_a_worker_that_keeps_losing_its_lease_still_ends_the_export_failed(self):
        rounds = downloads.MAX_INTERRUPTIONS + exports.MAX_DOWNLOAD_JOBS
        for day in range(rounds):
            at = NOW + day * DAY
            if day:
                self.assertEqual(self.reconcile(at).requeued, 1)
            self.attempt(self.claim(at), 1 + day % 2, at)
            self.lose_lease(at)
        self.assertEqual((self.export_row()[:2], self.interruptions()), (("failed", exports.MAX_DOWNLOAD_JOBS), downloads.MAX_INTERRUPTIONS))
        self.assertEqual(self.reconcile(NOW + rounds * DAY).requeued, 0)
        self.assertEqual([status for _, status in self.download_jobs()], ["failed"] * rounds)
        with db.session(self.engine) as opened:
            self.assertFalse(exports.download_underway(opened, self.account))

    def test_a_worker_that_reports_the_lease_message_itself_still_uses_a_try(self):
        job = self.claim()
        self.attempt(job, 1)
        with db.session(self.engine) as opened:
            failed = browser_jobs.failed(opened, self.settings, job, browser_jobs.EXPIRED_MESSAGE, None, [], NOW)
            downloads.job_failed(opened, failed, NOW)
        self.assertEqual((self.export_row()[:2], self.interruptions()), (("downloading", 1), 0))
        self.assertEqual(self.part(1), ("pending", 1, 1, None))


class RetryFailedTests(DownloadCase):
    def retry(self, now=NOW + DAY):
        with db.session(self.engine, writing=True) as opened:
            export, retried = downloads.retry_failed(opened, self.export, now)
            return export.status, retried

    def refused(self, now=NOW + DAY):
        with db.session(self.engine, writing=True) as opened, self.assertRaises(downloads.CannotRetry) as caught:
            downloads.retry_failed(opened, self.export, now)
        return str(caught.exception)

    def set_export(self, **values):
        with db.session(self.engine) as opened:
            export = opened.get(Export, self.export)
            for name, value in values.items():
                setattr(export, name, value)

    def set_part(self, number, **values):
        with db.session(self.engine) as opened:
            row = opened.execute(select(ExportPart).where(ExportPart.export_id == self.export, ExportPart.number == number)).scalar_one()
            for name, value in values.items():
                setattr(row, name, value)

    def snapshot(self):
        with db.session(self.engine) as opened:
            export = opened.get(Export, self.export)
            parts = [(part.number, part.status, part.downloads_used, part.attempts, part.bytes_done, part.filename) for part in exports.parts_of(opened, self.export)]
            return export.status, export.download_tries, export.download_interruptions, export.inbox_dir, parts, len(self.download_jobs()), self.events()

    def events(self):
        with db.session(self.engine) as opened:
            return opened.execute(select(Event.type, Event.detail).where(Event.type == "export_download_retried")).all()

    def fail_part_two(self):
        job = self.claim()
        self.attempt(job, 1)
        self.part_done(job, 1, "takeout-001.zip")
        for _ in range(downloads.MAX_ATTEMPTS):
            self.attempt(job, 2)
            self.fail_part(job, 2)
        self.fail_job(job)
        return job

    def test_a_failed_export_goes_back_to_downloading_and_only_its_unfinished_part_is_reset(self):
        self.fail_part_two()
        kept = (self.folder() / "takeout-001.zip").read_bytes()
        self.set_export(download_tries=exports.MAX_DOWNLOAD_JOBS, download_interruptions=downloads.MAX_INTERRUPTIONS)
        done_before = self.snapshot()[4][0]
        self.assertEqual(done_before[:4] + done_before[5:], (1, "done", 1, 1, "takeout-001.zip"))
        self.assertEqual(self.export_row()[0], "failed")
        self.assertEqual(self.retry(), ("downloading", True))
        status, tries, interruptions, inbox, parts, queued, events = self.snapshot()
        self.assertEqual((status, tries, interruptions, inbox), ("downloading", 0, 0, None))
        self.assertEqual(parts, [done_before, (2, "pending", 3, 0, None, None)])
        self.assertEqual((self.folder() / "takeout-001.zip").read_bytes(), kept)
        self.assertEqual(sorted(path.name for path in self.folder().iterdir()), ["takeout-001.zip"])
        self.assertEqual(self.download_jobs()[-1], (self.export, "queued"))
        self.assertEqual(queued, 2)
        self.assertEqual(events, [("export_download_retried", "1 part reset")])

    def test_the_retried_download_runs_and_keeps_counting_googles_downloads(self):
        self.fail_part_two()
        self.retry()
        job = self.claim(NOW + DAY)
        reply = self.attempt(job, 2, NOW + DAY)
        self.assertEqual((reply["allowed"], reply["part"]["downloads_used"], reply["part"]["attempts"]), (True, 4, 1))
        self.part_done(job, 2, "takeout-002.zip", NOW + DAY)
        with db.session(self.engine) as opened:
            downloads.finish(opened, self.settings, job, NOW + DAY)
        self.assertEqual(self.export_row()[:2], ("downloaded", 0))

    def test_every_part_left_to_download_waits_again_with_its_attempts_back_and_its_downloads_kept(self):
        for status, attempts in (("pending", 3), ("corrupt", 2), ("downloading", 1), ("failed", 3)):
            with self.subTest(status=status):
                self.set_export(status="failed", download_tries=2)
                self.set_part(1, status=status, attempts=attempts, downloads_used=attempts, bytes_done=None if status != "downloading" else 1024)
                self.set_part(2, status="failed", attempts=3, downloads_used=3)
                self.assertEqual(self.retry()[1], True)
                self.assertEqual(self.part(1), ("pending", attempts, 0, None))
                self.assertEqual(self.part(2), ("pending", 3, 0, None))

    def retried_part_two_downloads(self, at):
        self.assertEqual(self.part(2), ("pending", 3, downloads.MAX_ATTEMPTS, None))
        self.assertEqual(self.export_row()[0], "failed")
        self.assertEqual(self.retry(at), ("downloading", True))
        job = self.claim(at)
        reply = self.attempt(job, 2, at)
        self.assertEqual((reply["allowed"], reply["gave_up"], reply["part"]["downloads_used"], reply["part"]["attempts"]), (True, False, 4, 1))
        self.assertEqual((self.export_row()[0], self.part(2)[0]), ("downloading", "downloading"))

    def test_a_part_left_waiting_with_its_attempts_used_by_three_failed_jobs_downloads_after_a_retry(self):
        for day in range(exports.MAX_DOWNLOAD_JOBS):
            at = NOW + day * DAY
            if day:
                self.assertEqual(self.reconcile(at).requeued, 1)
            job = self.claim(at)
            self.attempt(job, 2, at)
            self.fail_job(job, at)
        self.retried_part_two_downloads(NOW + 3 * DAY)

    def test_a_part_left_waiting_with_its_attempts_used_by_counted_lost_leases_downloads_after_a_retry(self):
        self.set_export(download_interruptions=downloads.MAX_INTERRUPTIONS)
        for day in range(exports.MAX_DOWNLOAD_JOBS):
            at = NOW + day * DAY
            if day:
                self.assertEqual(self.reconcile(at).requeued, 1)
            self.attempt(self.claim(at), 2, at)
            jobs.build(self.settings, self.engine, at).run_one("browser.leases", at + browser_jobs.LEASE_SECONDS + 1)
        self.retried_part_two_downloads(NOW + 3 * DAY)

    def test_uncounted_lost_leases_leave_no_part_with_its_attempts_used(self):
        for day in range(downloads.MAX_INTERRUPTIONS):
            at = NOW + day * DAY
            if day:
                self.assertEqual(self.reconcile(at).requeued, 1)
            self.attempt(self.claim(at), 2, at)
            jobs.build(self.settings, self.engine, at).run_one("browser.leases", at + browser_jobs.LEASE_SECONDS + 1)
        self.assertEqual((self.export_row()[:2], self.part(2)), (("downloading", 0), ("pending", 3, 0, None)))

    def test_a_second_press_changes_nothing_and_queues_no_second_job(self):
        self.fail_part_two()
        self.assertEqual(self.retry(), ("downloading", True))
        once = self.snapshot()
        self.assertEqual(self.retry(NOW + DAY + 1), ("downloading", False))
        self.assertEqual(self.snapshot(), once)

    def test_an_expired_export_is_refused_and_left_as_it_is(self):
        self.fail_part_two()
        before = self.snapshot()
        self.assertEqual(self.refused(NOW + 7 * DAY), "This export has expired in Takeout, so it can't be downloaded again.")
        self.set_export(status="expired")
        self.assertEqual(self.refused(), "This export has expired in Takeout, so it can't be downloaded again.")
        self.set_export(status="failed")
        self.assertEqual(self.snapshot(), before)

    def test_a_downloaded_imported_or_failed_import_export_is_refused(self):
        self.fail_part_two()
        cases = {
            ("imported", None): "This export is already downloaded, so there is nothing to retry.",
            ("downloaded", None): "This export is already downloaded, so there is nothing to retry.",
            ("importing", None): "This export is already downloaded, so there is nothing to retry.",
            ("failed", "/photos/_inbox/jo/export-1"): "This export was downloaded and its import failed; retry it on the Imports page.",
        }
        for (status, inbox), message in cases.items():
            with self.subTest(status=status):
                self.set_export(status=status, inbox_dir=inbox)
                before = self.snapshot()
                self.assertEqual(self.refused(), message)
                self.assertEqual(self.snapshot(), before)

    def test_an_export_with_no_part_left_to_download_is_refused(self):
        self.set_export(status="failed")
        for number in (1, 2):
            self.set_part(number, status="done", filename=f"takeout-00{number}.zip")
        before = self.snapshot()
        self.assertEqual(self.refused(), "Every part of this export is already downloaded, so there is nothing left to download.")
        self.assertEqual(self.snapshot(), before)

    def test_an_export_with_no_parts_listed_is_refused(self):
        with db.session(self.engine) as opened:
            for part in exports.parts_of(opened, self.export):
                opened.delete(part)
            opened.get(Export, self.export).status = "failed"
        self.assertEqual(self.refused(), "Every part of this export is already downloaded, so there is nothing left to download.")

    def test_a_part_that_used_all_of_googles_downloads_is_refused_and_nothing_changes(self):
        self.fail_part_two()
        self.set_part(2, downloads_used=downloads.MAX_DOWNLOADS)
        before = self.snapshot()
        self.assertEqual(self.refused(), "Part 2 has used all 5 of Google's downloads, so Takeout won't let Darkroom download it again. "
                                         "Ask Takeout for a new export of Google Photos instead.")
        self.assertEqual(self.snapshot(), before)

    def test_an_export_still_waiting_to_download_is_left_as_it_is(self):
        before = self.snapshot()
        self.assertEqual(self.retry(), ("announced", False))
        self.assertEqual(self.snapshot(), before)

    def test_an_export_google_is_still_preparing_is_refused(self):
        self.set_export(ready_at=None)
        self.assertEqual(self.refused(), "Google is still preparing this export, so there is nothing to retry yet.")

    def shown(self, now=NOW + DAY):
        with db.session(self.engine) as opened:
            found = exports.as_dict(opened, opened.get(Export, self.export), now)
            return found["retry_allowed"], found["retry_refusal"]

    def test_the_export_says_whether_a_retry_would_be_allowed_and_why_not(self):
        self.assertEqual(self.shown(), (False, None))
        self.fail_part_two()
        self.assertEqual(self.shown(), (True, None))
        self.assertEqual(self.shown(NOW + 7 * DAY), (False, "This export has expired in Takeout, so it can't be downloaded again."))
        self.set_part(2, downloads_used=downloads.MAX_DOWNLOADS)
        self.assertEqual(self.shown(), (False, self.refused()))
        self.set_export(inbox_dir="/photos/_inbox/jo/export-1")
        self.assertEqual(self.shown(), (False, "This export was downloaded and its import failed; retry it on the Imports page."))
        self.set_export(status="imported")
        self.assertEqual(self.shown(), (False, None))

    def test_an_unknown_export_is_not_found(self):
        with db.session(self.engine, writing=True) as opened, self.assertRaises(exports.NoSuchExport):
            downloads.retry_failed(opened, self.export + 99, NOW)


class LinkedFolderTests(DownloadCase):
    def tree(self, folder):
        return {str(path.relative_to(folder)): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file()}

    def lay_out(self, link, target):
        for child in self.root.iterdir():
            child.unlink() if child.is_symlink() else shutil.rmtree(child)
        target.mkdir(parents=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
        inside = self.root / "_inbox" / "jo" / f"export-{self.export}"
        inside.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(inside / "takeout-001.zip", "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("Takeout/Google Photos/a.bin", b"x" * 4 * MB)
        return (inside / "takeout-001.zip").stat().st_size

    def test_a_linked_export_or_inbox_folder_is_refused_for_the_plan_and_the_part(self):
        job = self.claim()
        self.attempt(job, 1)
        originals = self.root / "jo" / "originals"
        inbox = self.root / "_inbox"
        layouts = {"export folder to originals": (inbox / "jo" / f"export-{self.export}", originals),
                   "export folder elsewhere": (inbox / "jo" / f"export-{self.export}", self.root / "elsewhere"),
                   "person's inbox to originals": (inbox / "jo", originals),
                   "inbox to originals": (inbox, originals)}
        for name, (link, target) in layouts.items():
            with self.subTest(name):
                size = self.lay_out(link, target)
                before = self.tree(originals) if originals.exists() else {}
                with self.assertRaises(downloads.Refused):
                    self.call(downloads.plan, self.settings, job)
                with self.assertRaises(downloads.Refused):
                    self.call(lambda opened, size=size: downloads.part_done(opened, self.settings, job, 1, "takeout-001.zip", size, NOW))
                self.assertEqual(self.part(1)[0], "downloading")
                self.assertEqual(self.tree(originals) if originals.exists() else {}, before)

    def test_finishing_into_a_folder_that_became_a_link_is_refused(self):
        job = self.claim()
        for number in (1, 2):
            self.attempt(job, number)
            self.part_done(job, number, f"takeout-00{number}.zip")
        originals = self.root / "jo" / "originals"
        originals.parent.mkdir(parents=True)
        self.folder().rename(originals)
        self.folder().symlink_to(originals, target_is_directory=True)
        with self.assertRaises(downloads.Refused):
            self.call(lambda opened: downloads.finish(opened, self.settings, job, NOW))
        self.assertEqual(self.export_row(), ("downloading", 0, None, None))


class DefaultPersonTests(DownloadCase):
    def move_to(self, slug):
        with db.session(self.engine) as opened:
            other = add_person(opened, slug)
            accounts.update(opened, self.account, None, other, NOW)
            return other

    def held(self):
        with self.assertRaises(accounts.JobOpen):
            self.move_to("ann")
        with db.session(self.engine) as opened, self.assertRaises(accounts.JobOpen):
            accounts.update(opened, self.account, "jo.other@example.test", None, NOW)

    def test_the_default_person_and_email_stay_while_a_download_is_queued_running_or_between_tries(self):
        job = self.claim()
        self.held()
        self.attempt(job, 1)
        self.part_done(job, 1, "takeout-001.zip")
        self.fail_job(job)
        self.held()
        self.reconcile(NOW + DAY)
        self.held()
        with db.session(self.engine) as opened:
            account = opened.get(GoogleAccount, self.account)
            self.assertEqual((downloads.person_of(opened, opened.get(Export, self.export)).slug, account.email), ("jo", "jo.takeout@example.test"))
            accounts.update(opened, self.account, account.email, account.default_person_id, NOW)

    def test_the_default_person_can_change_once_the_export_is_downloaded(self):
        job = self.claim()
        for number in (1, 2):
            self.attempt(job, number)
            self.part_done(job, number, f"takeout-00{number}.zip")
        with db.session(self.engine) as opened:
            downloads.finish(opened, self.settings, job, NOW)
            browser_jobs.done(opened, job, {}, NOW)
        other = self.move_to("ann")
        with db.session(self.engine) as opened:
            account = accounts.update(opened, self.account, "jo.other@example.test", None, NOW)
            self.assertEqual((account.email, account.default_person_id), ("jo.other@example.test", other))
            self.assertEqual(Path(opened.get(Export, self.export).inbox_dir), self.folder())


if __name__ == "__main__":
    unittest.main()
