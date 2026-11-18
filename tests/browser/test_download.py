import ast
import os
import re
import stat
import tempfile
import threading
import traceback
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import browser_jobs, downloads, exports
from darkroom.storage import db, paths
from darkroom.storage.models import Alert, BrowserJob, Export, ExportPart, GoogleAccount
from darkroom.storage.paths import Photos, remove_inbox_tree
from darkroom_browser.context import Cancelled, DownloadInterrupted, JobContext
from darkroom_browser.jobs import REGISTRY, download
from tests.browser.scripted import Clock
from tests.browser.test_classify import SIGNED_IN, VERIFY
from tests.browser.takeout_pages import COMPLETED_ID, LIVE_BASE, MADE_UP_USER, archive, archive_url, items_linked, part_item, part_names
from tests.shared import VIEWER_SECRET, add_person, own_database, settings_from

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_793_750_400.0
SIZES = (40 * 1024, 40 * 1024, 40 * 1024)
ARCHIVE = archive_url(COMPLETED_ID)
EXPORT_PAGE = archive(sizes=SIZES)


class DomainReporter:
    def __init__(self, engine, settings):
        self.engine = engine
        self.settings = settings
        self.events = []

    def session(self):
        return db.session(self.engine)

    def progress(self, job_id, message):
        self.events.append(("progress", message))
        with self.session() as opened:
            browser_jobs.progress(opened, job_id, message, NOW)

    def waiting(self, job_id, reason):
        self.events.append(("waiting", reason))
        with self.session() as opened:
            browser_jobs.waiting(opened, self.settings, job_id, reason, NOW)

    def download_plan(self, job_id):
        with self.session() as opened:
            return downloads.plan(opened, self.settings, job_id)

    def part(self, job_id, number, body):
        self.events.append((body["event"], number))
        with self.session() as opened:
            if body["event"] == "attempt":
                return downloads.attempt(opened, job_id, number, NOW)
            if body["event"] == "progress":
                return downloads.progress(opened, job_id, number, body["bytes"], NOW)
            if body["event"] == "done":
                return downloads.part_done(opened, self.settings, job_id, number, body["filename"], body["bytes"], NOW)
            return downloads.part_failed(opened, job_id, number, body["reason"], NOW)


class DownloadBrowser:
    def __init__(self, script, verify_reads=2):
        self.script = list(script)
        self.url = None
        self.verifying = 0
        self.verify_reads = verify_reads
        self.downloaded = []
        self.selectors = []
        self.visited = []
        self.closed = False
        self.read_links = []
        self.targets = self.as_chromium_resolves

    def goto(self, url):
        self.url = url
        self.visited.append(url)
        self.verifying = 0

    def current_url(self):
        return self.url or "about:blank"

    def content(self):
        if self.verifying:
            self.verifying -= 1
            return VERIFY
        return EXPORT_PAGE if self.url == ARCHIVE else SIGNED_IN

    def click(self, selector):
        raise AssertionError("the download job only clicks through download_to")

    def type_text(self, selector, text):
        raise AssertionError("the download job types nothing")

    def wait_for(self, selector, timeout_ms=30_000):
        return None

    @staticmethod
    def as_chromium_resolves(selector):
        return ["https://takeout.google.com/takeout/download?" + re.search(r'download\?([^"]*)"\]$', selector).group(1)]

    def link_targets(self, selector):
        self.read_links.append(selector)
        return self.targets(selector)

    def download_to(self, selector, folder, progress=None):
        self.selectors.append(selector)
        number = int(re.search(r"i=(\d+)", selector).group(1)) + 1
        self.downloaded.append(number)
        outcome = self.script.pop(0)
        if outcome == "verify":
            self.verifying = self.verify_reads
            raise TimeoutError("no download started")
        if outcome == "interrupted":
            raise DownloadInterrupted("network")
        if outcome == "lease lost":
            raise Cancelled("lost")
        name = "Takeout.zip?" if outcome == "bad name" else f"takeout-20261102T031000Z-00{number}.zip"
        path = folder / name
        if outcome == "corrupt":
            path.write_bytes(b"PK\x03\x04 not really a zip" * 2000)
        else:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr(f"Takeout/Google Photos/{number}.bin", bytes(40 * 1024))
        if progress is not None:
            progress(20 * 1024)
        return path

    def screenshot(self, path):
        path.write_bytes(b"\x89PNG")

    def close(self):
        self.closed = True


class DownloadWorld(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.settings = settings_from(DARKROOM_PHOTOS_ROOT=folder.name, DARKROOM_PUBLIC_URL="https://darkroom.example", viewer_secret=VIEWER_SECRET)
        with db.session(self.engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo@example.test", default_person_id=person, created=1.0, session_state="ok")
            opened.add(account)
            opened.flush()
            self.account = account.id
            listing = [exports.Listed(COMPLETED_ID, NOW - 600, "ready", NOW + 7 * 86400, tuple(exports.ListedPart(n, size) for n, size in enumerate(SIZES, start=1)))]
            exports.reconcile(opened, account, listing, NOW)
            self.export = opened.execute(select(Export)).scalar_one().id
        self.reporter = DomainReporter(self.engine, self.settings)
        self.clock = Clock()

    def claim(self):
        with db.session(self.engine) as opened:
            return browser_jobs.claim(opened, NOW).id

    def run_job(self, browser, job_id=None):
        job_id = job_id or self.claim()
        ctx = JobContext(job_id, "download", str(self.account), {"email": "jo@example.test", "export_id": self.export}, self.root, self.reporter,
                         lambda profile: browser, threading.Event(), None, self.clock.sleep, self.clock.monotonic, lambda: NOW)
        return job_id, download.download_export(ctx)

    def finish(self, job_id):
        with db.session(self.engine) as opened:
            downloads.finish(opened, self.settings, job_id, NOW)
            browser_jobs.done(opened, job_id, {}, NOW)

    def fail(self, job_id):
        with db.session(self.engine) as opened:
            job = browser_jobs.failed(opened, self.settings, job_id, "DownloadFailed", None, [], NOW)
            downloads.job_failed(opened, job, NOW)

    def parts(self):
        with db.session(self.engine) as opened:
            return [(part.status, part.downloads_used, part.attempts) for part in opened.execute(select(ExportPart).order_by(ExportPart.number)).scalars()]

    def status(self):
        with db.session(self.engine) as opened:
            return opened.get(Export, self.export).status

    def folder(self):
        return self.root / "_inbox" / "jo" / f"export-{self.export}"


class DownloadJobTests(DownloadWorld):
    def test_it_downloads_every_part_in_order_into_the_inbox(self):
        browser = DownloadBrowser(["ok", "ok", "ok"])
        job_id, result = self.run_job(browser)
        self.assertEqual(result, {"export_id": self.export, "parts": 3, "downloaded": 3})
        self.assertEqual(browser.downloaded, [1, 2, 3])
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)
        self.assertEqual(sorted(path.name for path in self.folder().iterdir()), [f"takeout-20261102T031000Z-00{n}.zip" for n in (1, 2, 3)])
        self.assertEqual({path.stat().st_mode & 0o7777 for path in self.folder().iterdir()}, {download.PART_MODE})
        self.assertIn(("progress", 1), self.reporter.events)
        self.finish(job_id)
        self.assertEqual(self.status(), "downloaded")

    def test_a_verification_during_part_2_pauses_alerts_once_and_resumes_part_2(self):
        browser = DownloadBrowser(["ok", "verify", "ok", "ok"])
        job_id, _ = self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2, 2, 3])
        self.assertEqual([event for event in self.reporter.events if event[0] == "waiting"], [("waiting", "verify")])
        self.assertIn(("progress", "verified; resuming part 2"), self.reporter.events)
        self.assertEqual(self.parts(), [("done", 1, 1), ("done", 2, 2), ("done", 1, 1)])
        self.assertEqual(self.clock.slept, [download.POLL_SECONDS])
        with db.session(self.engine) as opened:
            sign_in = opened.execute(select(Alert).where(Alert.kind == "sign_in_needed")).scalars().all()
            self.assertEqual(len(sign_in), 1)
            self.assertIsNotNone(sign_in[0].closed)
            self.assertEqual(opened.get(BrowserJob, job_id).status, "running")

    def test_a_corrupt_part_is_retried_twice_then_fails_with_every_attempt_counted(self):
        browser = DownloadBrowser(["ok", "corrupt", "corrupt", "corrupt", "ok"])
        with self.assertRaisesRegex(download.DownloadFailed, "part 2 failed after 3 attempts"):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2, 2, 2])
        self.assertEqual(self.parts(), [("done", 1, 1), ("failed", 3, 3), ("pending", 0, 0)])
        self.assertEqual(self.status(), "failed")

    def test_never_a_sixth_download_even_across_jobs(self):
        with db.session(self.engine) as opened:
            opened.execute(select(ExportPart).where(ExportPart.number == 1)).scalar_one().downloads_used = 4
        browser = DownloadBrowser(["interrupted", "ok"])
        with self.assertRaises(download.DownloadFailed):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [1])
        self.assertEqual(self.parts()[0], ("failed", 5, 1))
        with db.session(self.engine) as opened:
            job = browser_jobs.queue(opened, self.account, "download", {"export_id": self.export}, NOW)
            job.status, job.heartbeat = "running", NOW
            job_id = job.id
        with self.assertRaises(downloads.Refused):
            self.run_job(DownloadBrowser(["ok"]), job_id)

    def test_a_restart_downloads_only_the_remaining_parts_and_discards_partial_leftovers(self):
        first = DownloadBrowser(["ok", "lease lost"])
        job_id = self.claim()
        with self.assertRaises(Cancelled):
            self.run_job(first, job_id)
        (self.folder() / ".partial" / "takeout-left-over-002.zip").write_bytes(b"half")
        self.fail(job_id)
        with db.session(self.engine) as opened:
            self.assertEqual(exports.reconcile(opened, opened.get(GoogleAccount, self.account), [exports.Listed(COMPLETED_ID, NOW - 600, "ready", NOW + 7 * 86400, tuple(exports.ListedPart(n, size) for n, size in enumerate(SIZES, start=1)))], NOW).requeued, 1)
        second = DownloadBrowser(["ok", "ok"])
        job_id, result = self.run_job(second)
        self.assertEqual(second.downloaded, [2, 3])
        self.assertEqual(result["downloaded"], 2)
        self.assertFalse((self.folder() / ".partial").exists())
        self.assertEqual(self.parts(), [("done", 1, 1), ("done", 2, 2), ("done", 1, 1)])
        self.finish(job_id)
        self.assertEqual(self.status(), "downloaded")

    def test_a_download_with_a_bad_file_name_counts_as_a_failed_attempt(self):
        browser = DownloadBrowser(["bad name", "ok", "ok", "ok"])
        self.run_job(browser)
        self.assertIn(("failed", 1), self.reporter.events)
        self.assertEqual(self.parts()[0], ("done", 2, 2))
        self.assertFalse(any(path.name == "Takeout.zip?" for path in self.folder().rglob("*")))

    def test_a_part_recorded_as_done_but_missing_from_the_inbox_stops_the_job(self):
        self.run_job(DownloadBrowser(["ok", "ok", "ok"]))
        (self.folder() / "takeout-20261102T031000Z-001.zip").unlink()
        with db.session(self.engine) as opened:
            job = browser_jobs.queue(opened, self.account, "download", {"export_id": self.export}, NOW)
            job.status, job.heartbeat = "running", NOW
            job_id = job.id
        with self.assertRaisesRegex(download.DownloadFailed, "part 1 is recorded as downloaded"):
            self.run_job(DownloadBrowser([]), job_id)

    def test_an_export_takeout_no_longer_lists_stops_the_job(self):
        browser = DownloadBrowser(["ok"])
        browser.content = lambda: archive(status="Expired", items=[]) if browser.url == ARCHIVE else SIGNED_IN
        with self.assertRaisesRegex(download.DownloadFailed, "no longer lists this export"):
            self.run_job(browser)

    def test_the_links_come_from_the_exports_own_page_on_takeout(self):
        browser = DownloadBrowser(["ok", "ok", "ok"])
        self.run_job(browser)
        self.assertEqual(set(browser.visited), {"https://takeout.google.com/", ARCHIVE})

    def test_relative_part_links_are_checked_resolved_and_clicked_as_the_page_writes_them(self):
        browser = DownloadBrowser(["ok", "ok", "ok"])
        page = archive(sizes=SIZES, items=items_linked("takeout/download?{query}", sizes=SIZES), base=LIVE_BASE)
        browser.content = lambda: page if browser.url == ARCHIVE else SIGNED_IN
        _, result = self.run_job(browser)
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(browser.selectors, [f'a[href="takeout/download?j={COMPLETED_ID}&i={index}&user={MADE_UP_USER}"]' for index in range(3)])

    def test_each_part_is_clicked_only_after_the_browser_reads_its_link_as_the_address_checked(self):
        browser = DownloadBrowser(["ok", "ok", "ok"])
        self.run_job(browser)
        self.assertEqual(browser.read_links, browser.selectors)

    def assert_read_differently_downloads_nothing(self, reading):
        browser = DownloadBrowser(["ok"])
        browser.targets = reading
        with self.assertRaisesRegex(download.DownloadFailed, "could not confirm that the link to part 1 on Takeout leads to the address Darkroom checked") as raised:
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [])
        self.assertEqual(self.parts(), [("pending", 0, 0)] * 3)
        self.assertNotIn("user=", str(raised.exception))
        self.assertNotIn(COMPLETED_ID, str(raised.exception))

    def test_a_part_link_the_browser_resolves_to_another_path_downloads_nothing(self):
        self.assert_read_differently_downloads_nothing(lambda selector: [f"https://takeout.google.com/download?j={COMPLETED_ID}&i=0&user={MADE_UP_USER}"])

    def test_a_part_link_the_browser_resolves_to_another_host_downloads_nothing(self):
        self.assert_read_differently_downloads_nothing(lambda selector: [f"https://other.example/takeout/download?j={COMPLETED_ID}&i=0&user={MADE_UP_USER}"])

    def test_a_second_matching_link_that_leads_elsewhere_downloads_nothing(self):
        self.assert_read_differently_downloads_nothing(lambda selector: [*DownloadBrowser.as_chromium_resolves(selector), "https://other.example/"])

    def test_a_part_link_the_browser_cannot_find_downloads_nothing(self):
        self.assert_read_differently_downloads_nothing(lambda selector: [])

    def test_a_part_link_the_browser_cannot_read_downloads_nothing(self):
        self.assert_read_differently_downloads_nothing(lambda selector: [""])

    def test_an_error_reading_a_part_link_downloads_nothing_and_names_no_address(self):
        def unreadable(selector):
            raise RuntimeError(f"page closed while reading {selector}")

        self.assert_read_differently_downloads_nothing(unreadable)

    def test_part_links_that_resolve_off_takeouts_download_path_download_nothing(self):
        browser = DownloadBrowser(["ok"])
        page = archive(sizes=SIZES, items=items_linked("takeout/download?{query}", sizes=SIZES))
        browser.content = lambda: page if browser.url == ARCHIVE else SIGNED_IN
        with self.assertRaisesRegex(download.DownloadFailed, "not one Darkroom can use"):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [])

    def test_an_export_id_that_is_not_a_plain_id_opens_nothing(self):
        with db.session(self.engine) as opened:
            opened.get(Export, self.export).google_id = "../settings"
        browser = DownloadBrowser(["ok"])
        with self.assertRaisesRegex(download.DownloadFailed, "export id"):
            self.run_job(browser)
        self.assertNotIn("https://takeout.google.com/manage/archive/../settings", browser.visited)
        self.assertEqual(browser.downloaded, [])

    def page_marking_downloads(self, browser, unreadable=()):
        names = part_names(3)

        def page():
            if browser.url != ARCHIVE:
                return SIGNED_IN
            done = set(browser.downloaded)
            return archive(sizes=SIZES, items=[part_item(COMPLETED_ID, index, 3, size, names[index], downloaded=index + 1 in done,
                                                         label="" if index + 1 in unreadable else None, index_attribute=index + 1 not in unreadable)
                                               for index, size in enumerate(SIZES)])

        return page

    def test_parts_takeout_marks_as_downloaded_do_not_stop_the_next_part(self):
        browser = DownloadBrowser(["ok", "ok", "ok"])
        browser.content = self.page_marking_downloads(browser)
        _, result = self.run_job(browser)
        self.assertEqual((result["downloaded"], browser.downloaded), (3, [1, 2, 3]))
        self.assertEqual(self.parts(), [("done", 1, 1)] * 3)

    def test_a_part_takeout_marks_as_downloaded_is_downloaded_again_when_darkroom_has_not_got_it(self):
        browser = DownloadBrowser(["interrupted", "ok", "ok", "ok"])
        browser.content = self.page_marking_downloads(browser)
        self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 1, 2, 3])
        self.assertEqual(self.parts()[0], ("done", 2, 2))

    def test_another_parts_unreadable_entry_does_not_stop_the_part_being_fetched(self):
        browser = DownloadBrowser(["ok", "ok"])
        browser.content = self.page_marking_downloads(browser, unreadable=(3,))
        with self.assertRaisesRegex(download.DownloadFailed, "no readable part number: 3 part items") as raised:
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [1, 2])
        self.assertEqual(self.parts(), [("done", 1, 1), ("done", 1, 1), ("pending", 0, 0)])
        self.assertNotIn(COMPLETED_ID, str(raised.exception))
        self.assertNotIn("user=", str(raised.exception))

    def test_takeout_listing_fewer_parts_than_darkroom_expects_stops_the_job(self):
        browser = DownloadBrowser(["ok"])
        browser.content = lambda: archive(sizes=SIZES[:2]) if browser.url == ARCHIVE else SIGNED_IN
        with self.assertRaisesRegex(download.DownloadFailed, "Takeout lists 2 parts"):
            self.run_job(browser)
        self.assertEqual(browser.downloaded, [])


class DownloadsFolderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.profile = self.root / "_browser" / "7" / "profile"
        self.staging = self.root / "_inbox" / ".downloads"
        self.held = self.root / "_inbox" / "jo" / "review" / "export-1"
        self.held.mkdir(parents=True)
        (self.held / "IMG_1.JPG").write_bytes(b"held")

    def assert_held_copies_survive(self):
        self.assertEqual(sorted(each.name for each in self.held.iterdir()), ["IMG_1.JPG"])
        self.assertEqual((self.held / "IMG_1.JPG").read_bytes(), b"held")

    def test_the_folder_is_the_accounts_folder_under_the_inbox_and_is_made_when_missing(self):
        folder = download.downloads_folder(self.profile, self.root)
        self.assertEqual(folder, self.staging / "7")
        self.assertTrue(folder.is_dir())

    def test_a_profile_that_is_not_an_accounts_profile_folder_is_refused(self):
        for profile in (self.root / "_browser" / ".." / "profile", self.root / "_browser" / "7" / "other", self.root / "_browser" / "jo" / "profile"):
            with self.assertRaises(download.DownloadsFolderRefused, msg=profile):
                download.downloads_folder(profile, self.root)
        self.assertFalse(self.staging.exists())

    def test_a_link_at_the_inbox_the_downloads_folder_or_the_accounts_folder_is_refused_and_nothing_is_removed(self):
        for link, target in ((self.staging / "7", self.held), (self.staging, self.held.parent), (self.root / "_inbox", self.held.parent)):
            with self.subTest(link=str(link.relative_to(self.root))):
                moved = None
                if link == self.root / "_inbox":
                    moved = self.root / "inbox-real"
                    link.rename(moved)
                    target = moved / "jo" / "review"
                    self.held = target / "export-1"
                elif link != self.staging:
                    self.staging.mkdir(parents=True)
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaises(download.DownloadsFolderRefused):
                    download.downloads_folder(self.profile, self.root)
                self.assert_held_copies_survive()
                link.unlink()
                if link == self.staging / "7":
                    self.staging.rmdir()
                if moved is not None:
                    moved.rename(link)
                    self.held = link / "jo" / "review" / "export-1"

    def test_the_sweep_removes_regular_leftovers_and_only_the_link_of_a_linked_one(self):
        folder = download.downloads_folder(self.profile, self.root)
        (folder / "0b7c-guid").write_bytes(b"half a part")
        (folder / "held-link").symlink_to(self.held / "IMG_1.JPG")
        (folder / "folder-link").symlink_to(self.held, target_is_directory=True)
        (folder / "a-folder").mkdir()
        self.assertEqual(download.sweep_downloads(folder), 3)
        self.assertEqual([each.name for each in folder.iterdir()], ["a-folder"])
        self.assert_held_copies_survive()


def mode_of(path):
    return path.lstat().st_mode & 0o7777


DARKROOM_UID = 10001
WORKER_UID = 10002
SHARE_GID = 10010


def as_user(uid, work):
    child = os.fork()
    if child == 0:
        code = 1
        try:
            os.setgroups([])
            os.setgid(SHARE_GID)
            os.setuid(uid)
            os.umask(0o077 if uid == DARKROOM_UID else 0o007)
            result = work()
            code = result if isinstance(result, int) else 0
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(code)
    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status)


class SharedFolderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)

    def test_the_workers_modes_give_others_nothing(self):
        self.assertEqual((download.SHARED_FOLDER_MODE, download.PART_MODE), (0o2770, 0o660))
        self.assertEqual((download.SHARED_FOLDER_MODE & 0o007, download.PART_MODE & 0o007), (0, 0))
        self.assertEqual(download.SHARED_FOLDER_MODE, paths.SHARED_FOLDER_MODE)

    def test_the_workers_folders_are_shared_whatever_the_umask(self):
        downloads = download.downloads_folder(self.root / "_browser" / "7" / "profile", self.root)
        folder = download.inbox_folder(self.root, {"person": "jo", "folder": "export-1"})
        partial = download.fresh_partial(folder)
        for each in (self.root / "_inbox", downloads.parent, downloads, self.root / "_inbox" / "jo", folder, partial):
            self.assertEqual(mode_of(each), download.SHARED_FOLDER_MODE, each)

    def assert_a_group_member_can_read_and_remove(self, top, gid):
        for path in [top, *top.rglob("*")]:
            found = path.lstat()
            self.assertEqual(found.st_gid, gid, path)
            self.assertEqual(stat.S_IMODE(found.st_mode) & stat.S_IRWXO, 0, path)
            if stat.S_ISDIR(found.st_mode):
                self.assertTrue(found.st_mode & stat.S_ISGID, path)
                self.assertEqual(found.st_mode & stat.S_IRWXG, stat.S_IRWXG, path)
            else:
                self.assertTrue(stat.S_ISREG(found.st_mode), path)
                self.assertEqual(found.st_mode & (stat.S_IRGRP | stat.S_IWGRP), stat.S_IRGRP | stat.S_IWGRP, path)

    def test_everything_the_worker_makes_can_be_read_and_removed_by_another_member_of_the_group(self):
        photos = Photos(self.root)
        photos.share_inbox()
        photos.make_export_folder("jo", "export-1")
        gid = (self.root / "_inbox").lstat().st_gid
        downloads = download.downloads_folder(self.root / "_browser" / "7" / "profile", self.root)
        (downloads / "0b7c-guid").write_bytes(b"half")
        os.chmod(downloads / "0b7c-guid", download.PART_MODE)
        folder = download.inbox_folder(self.root, {"person": "jo", "folder": "export-1"})
        partial = download.fresh_partial(folder)
        (partial / "takeout-001.zip").write_bytes(b"part")
        os.replace(partial / "takeout-001.zip", folder / "takeout-001.zip")
        os.chmod(folder / "takeout-001.zip", download.PART_MODE)
        self.assert_a_group_member_can_read_and_remove(self.root / "_inbox", gid)

    def test_the_worker_never_widens_a_folder_darkroom_made_to_others(self):
        Photos(self.root).make_export_folder("jo", "export-1")
        download.inbox_folder(self.root, {"person": "jo", "folder": "export-1"})
        download.downloads_folder(self.root / "_browser" / "7" / "profile", self.root)
        for each in (self.root / "_inbox", self.root / "_inbox" / ".downloads", self.root / "_inbox" / "jo", self.root / "_inbox" / "jo" / "export-1"):
            self.assertEqual(mode_of(each) & stat.S_IRWXO, 0, each)

    def test_a_folder_darkroom_made_keeps_its_mode(self):
        Photos(self.root).make_export_folder("jo", "export-1")
        os.chmod(self.root / "_inbox" / "jo", 0o2750)
        download.inbox_folder(self.root, {"person": "jo", "folder": "export-1"})
        self.assertEqual(mode_of(self.root / "_inbox" / "jo"), 0o2750)

    @unittest.skipUnless(os.geteuid() == 0, "switching to Darkroom's and the worker's users needs root")
    def test_darkroom_reads_and_retires_what_the_worker_made_through_the_shared_group(self):
        os.chmod(self.root, 0o755)
        photos_root = self.root / "photos"
        photos_root.mkdir(mode=0o755)
        os.chmod(photos_root, 0o755)
        os.chown(photos_root, DARKROOM_UID, SHARE_GID)
        photos = Photos(photos_root)

        def darkroom_prepares():
            photos.share_inbox()
            photos.make_export_folder("jo", "export-1")

        def worker_downloads():
            downloads = download.downloads_folder(photos_root / "_browser" / "7" / "profile", photos_root)
            (downloads / "0b7c-guid").write_bytes(b"half")
            folder = download.inbox_folder(photos_root, {"person": "jo", "folder": "export-1"})
            partial = download.fresh_partial(folder)
            part = partial / "takeout-001.zip"
            part.write_bytes(b"part")
            os.replace(part, folder / part.name)
            os.chmod(folder / part.name, download.PART_MODE)
            download.fresh_partial(folder)

        def darkroom_retires():
            folder = photos_root / "_inbox" / "jo" / "export-1"
            assert (folder / "takeout-001.zip").read_bytes() == b"part"
            assert remove_inbox_tree(photos, folder)
            photos.retire_account_downloads("7")
            return None

        (photos_root / "_browser").mkdir()
        os.chown(photos_root / "_browser", WORKER_UID, SHARE_GID)
        self.assertEqual(as_user(DARKROOM_UID, darkroom_prepares), 0)
        self.assertEqual(as_user(WORKER_UID, worker_downloads), 0)
        self.assertEqual(as_user(DARKROOM_UID, darkroom_retires), 0)
        self.assertEqual(sorted(each.name for each in (photos_root / "_inbox").iterdir()), [".downloads", "jo"])
        self.assertEqual(list((photos_root / "_inbox" / ".downloads").iterdir()), [])
        self.assertEqual(list((photos_root / "_inbox" / "jo").iterdir()), [])

    @unittest.skipUnless(os.geteuid() == 0, "switching to Darkroom's and the worker's users needs root")
    def test_without_the_shared_group_darkroom_could_not_retire_the_workers_folder(self):
        os.chmod(self.root, 0o755)
        photos_root = self.root / "photos"
        photos_root.mkdir(mode=0o755)
        os.chmod(photos_root, 0o755)
        os.chown(photos_root, DARKROOM_UID, SHARE_GID)
        photos = Photos(photos_root)
        self.assertEqual(as_user(DARKROOM_UID, lambda: photos.make_export_folder("jo", "export-1")), 0)

        def worker_without_sharing():
            folder = photos_root / "_inbox" / "jo" / "export-1" / ".partial"
            folder.mkdir()
            os.chmod(folder, 0o755)
            (folder / "takeout-001.zip").write_bytes(b"part")

        self.assertEqual(as_user(WORKER_UID, worker_without_sharing), 0)
        def darkroom_cannot_remove():
            try:
                remove_inbox_tree(photos, photos_root / "_inbox" / "jo" / "export-1")
            except PermissionError:
                return 3
            return 0

        self.assertEqual(as_user(DARKROOM_UID, darkroom_cannot_remove), 3)

    def make_chromium_profile(self, profile):
        profile.mkdir(parents=True)
        os.chmod(profile, 0o700)
        for name in ("Default", "Default/Cache", "Crashpad"):
            (profile / name).mkdir(parents=True, exist_ok=True)
            os.chmod(profile / name, 0o700)
        (profile / "Default" / "Cookies").write_bytes(b"jar")
        os.chmod(profile / "Default" / "Cookies", 0o600)
        (profile / "Default" / "Cache" / "index").write_bytes(b"i")
        os.chmod(profile / "Default" / "Cache" / "index", 0o600)
        (profile / "chrome-helper").write_bytes(b"#!")
        os.chmod(profile / "chrome-helper", 0o700)
        (profile / "SingletonLock").symlink_to("host-1234")

    def test_sharing_a_profile_opens_its_folders_to_the_group_and_leaves_every_file_alone(self):
        profile = self.root / "_browser" / "7" / "profile"
        self.make_chromium_profile(profile)
        self.assertEqual(download.share_tree(profile.parent), 0)
        self.assertEqual(mode_of(profile), 0o770)
        self.assertEqual(mode_of(profile / "Default" / "Cache"), 0o770)
        self.assertEqual(mode_of(profile / "Default" / "Cookies"), 0o600)
        self.assertEqual(mode_of(profile / "Default" / "Cache" / "index"), 0o600)
        self.assertEqual(mode_of(profile / "chrome-helper"), 0o700)
        for each in [profile.parent, *profile.parent.rglob("*")]:
            if not each.is_symlink():
                self.assertEqual(mode_of(each) & stat.S_IRWXO, 0, each)

    def test_sharing_keeps_the_setgid_bit_and_changes_nothing_already_shared(self):
        folder = self.root / "_browser" / "7"
        folder.mkdir(parents=True)
        os.chmod(folder, 0o2770)
        (folder / "report").write_bytes(b"r")
        os.chmod(folder / "report", 0o660)
        self.assertEqual(download.share_tree(folder), 0)
        self.assertEqual((mode_of(folder), mode_of(folder / "report")), (0o2770, 0o660))

    def test_sharing_never_follows_or_changes_a_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        os.chmod(outside, 0o700)
        (outside / "secret").write_bytes(b"s")
        os.chmod(outside / "secret", 0o600)
        profile = self.root / "_browser" / "7" / "profile"
        profile.mkdir(parents=True)
        (profile / "folder-link").symlink_to(outside, target_is_directory=True)
        (profile / "file-link").symlink_to(outside / "secret")
        self.assertEqual(download.share_tree(profile.parent), 0)
        self.assertEqual((mode_of(outside), mode_of(outside / "secret")), (0o700, 0o600))

    def test_a_root_that_is_a_link_is_refused_untouched(self):
        outside = self.root / "outside"
        outside.mkdir()
        os.chmod(outside, 0o700)
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(download.share_tree(self.root / "link"), 1)
        self.assertEqual(mode_of(outside), 0o700)

    def test_a_path_the_worker_cannot_change_is_counted_not_raised(self):
        from unittest import mock

        profile = self.root / "_browser" / "7" / "profile"
        self.make_chromium_profile(profile)
        with mock.patch("os.chmod", side_effect=PermissionError("not yours")):
            self.assertGreater(download.share_tree(profile.parent), 0)

    def test_closing_the_browser_shares_the_account_folder_after_chromium_has_exited(self):
        from unittest import mock

        from darkroom_browser import live

        profile = self.root / "_browser" / "7" / "profile"
        self.make_chromium_profile(profile)
        seen = []
        context = mock.Mock()
        context.close.side_effect = lambda: seen.append(mode_of(profile))
        live.Live(mock.Mock(), context, mock.Mock(), None, profile).close()
        self.assertEqual(seen, [0o700])
        self.assertEqual(mode_of(profile), 0o770)

    def test_the_live_browser_reports_the_address_it_landed_on(self):
        from unittest import mock

        from darkroom_browser import live

        page = mock.Mock()
        page.url = "https://takeout.google.com/manage/"
        self.assertEqual(live.Live(page).current_url(), "https://takeout.google.com/manage/")

    def test_the_live_browser_reads_each_matching_links_resolved_address(self):
        from unittest import mock

        from darkroom_browser import live

        page = mock.Mock()
        page.eval_on_selector_all.return_value = ["https://takeout.google.com/takeout/download?j=x", {}, None]
        self.assertEqual(live.Live(page).link_targets('a[href="takeout/download?j=x"]'), ["https://takeout.google.com/takeout/download?j=x", "", ""])
        page.eval_on_selector_all.assert_called_once_with('a[href="takeout/download?j=x"]', "links => links.map(link => link.href)")

    def test_closing_still_reports_the_browsers_error_when_sharing_the_folder_fails(self):
        from unittest import mock

        from darkroom_browser import live

        profile = self.root / "_browser" / "7" / "profile"
        self.make_chromium_profile(profile)
        context = mock.Mock()
        context.close.side_effect = RuntimeError("chromium crashed")
        with mock.patch.object(live, "share_tree", side_effect=OSError("gone")):
            with self.assertRaisesRegex(RuntimeError, "chromium crashed"):
                live.Live(mock.Mock(), context, mock.Mock(), None, profile).close()

    @unittest.skipUnless(os.geteuid() == 0, "switching to Darkroom's and the worker's users needs root")
    def test_darkroom_retires_a_chromium_profile_only_once_the_worker_has_shared_it(self):
        os.chmod(self.root, 0o755)
        photos_root = self.root / "photos"
        photos_root.mkdir(mode=0o755)
        os.chmod(photos_root, 0o755)
        os.chown(photos_root, DARKROOM_UID, SHARE_GID)
        browser_folder = photos_root / "_browser"
        browser_folder.mkdir()
        os.chown(browser_folder, WORKER_UID, SHARE_GID)
        os.chmod(browser_folder, 0o2770)
        photos = Photos(photos_root)

        def worker_browses():
            self.make_chromium_profile(photos_root / "_browser" / "7" / "profile")
            os.chmod(photos_root / "_browser" / "7", 0o2770)

        def darkroom_retires():
            try:
                photos.retire_browser_account("7")
            except paths.ProfileNotRemoved:
                return 3
            return 0

        def worker_closes_chromium():
            return download.share_tree(photos_root / "_browser" / "7")

        self.assertEqual(as_user(WORKER_UID, worker_browses), 0)
        self.assertEqual(as_user(DARKROOM_UID, darkroom_retires), 3)
        self.assertTrue((photos_root / "_browser" / "7" / "profile").exists())
        self.assertEqual(as_user(WORKER_UID, worker_closes_chromium), 0)
        self.assertEqual(as_user(DARKROOM_UID, darkroom_retires), 0)
        self.assertFalse((photos_root / "_browser" / "7").exists())


class InboxFolderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_the_folder_must_be_a_person_and_an_export_folder_inside_the_inbox(self):
        for person, name in (("../jo", "export-1"), ("jo", "../originals"), ("Jo", "export-1"), ("jo", "export-x"), (None, "export-1")):
            with self.assertRaises(download.DownloadFailed, msg=(person, name)):
                download.inbox_folder(self.root, {"person": person, "folder": name})
        self.assertEqual(download.inbox_folder(self.root, {"person": "jo", "folder": "export-1"}), self.root / "_inbox" / "jo" / "export-1")

    def test_a_link_anywhere_on_the_way_is_refused(self):
        elsewhere = self.root / "jo" / "originals"
        elsewhere.mkdir(parents=True)
        (self.root / "_inbox").mkdir()
        (self.root / "_inbox" / "jo").symlink_to(self.root / "jo")
        with self.assertRaises(download.DownloadFailed):
            download.inbox_folder(self.root, {"person": "jo", "folder": "originals"})
        with self.assertRaises(download.DownloadFailed):
            download.inbox_folder(self.root, {"person": "jo", "folder": "export-1"})
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_plan_with_missing_or_gapped_parts_is_refused(self):
        for plan in ({"parts": [], "part_count": 0}, {"parts": [{"number": 1}, {"number": 3}], "part_count": 2}, {"part_count": 1}):
            with self.assertRaises(download.DownloadFailed, msg=plan):
                download.checked_parts(plan)

    def test_a_file_far_from_the_listed_size_is_wrong_size(self):
        path = self.root / "takeout-001.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a", b"x")
        self.assertEqual(download.problem_with(path, 100 * 1024 * 1024, set()), "wrong_size")
        self.assertEqual(download.problem_with(path, None, {"takeout-001.zip"}), "bad_name")
        self.assertIsNone(download.problem_with(path, None, set()))

    def test_a_part_link_escapes_quotes(self):
        self.assertEqual(download.part_link('https://takeout.google.com/takeout/download?j="x"\\'), 'a[href="https://takeout.google.com/takeout/download?j=\\"x\\"\\\\"]')


class TwoRootsDownloadTests(DownloadWorld):
    def setUp(self):
        super().setUp()
        appdata = tempfile.TemporaryDirectory()
        self.addCleanup(appdata.cleanup)
        self.appdata = Path(appdata.name)
        self.browser_root = self.appdata / "browser"
        self.opened = []

    def open_browser(self, browser):
        def opened(profile):
            self.opened.append((profile, download.downloads_folder(profile, self.root)))
            profile.mkdir(parents=True, exist_ok=True)
            return browser
        return opened

    def run_apart(self, browser):
        job_id = self.claim()
        ctx = JobContext(job_id, "download", str(self.account), {"email": "jo@example.test", "export_id": self.export}, self.root, self.reporter,
                         self.open_browser(browser), threading.Event(), None, self.clock.sleep, self.clock.monotonic, lambda: NOW, browser_root=self.browser_root)
        return job_id, download.download_export(ctx)

    def test_a_download_writes_its_parts_under_the_working_roots_inbox_and_never_under_appdata(self):
        job_id, result = self.run_apart(DownloadBrowser(["ok", "ok", "ok"]))
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(self.opened, [(self.browser_root / str(self.account) / "profile", self.root / "_inbox" / ".downloads" / str(self.account))])
        self.assertEqual(sorted(path.name for path in self.folder().iterdir()), [f"takeout-20261102T031000Z-00{n}.zip" for n in (1, 2, 3)])
        self.assertEqual([path for path in self.appdata.rglob("*") if not path.is_dir()], [])
        self.assertFalse((self.root / "_browser").exists())
        self.assertFalse((self.appdata / "_inbox").exists())
        self.finish(job_id)
        self.assertEqual(self.status(), "downloaded")


class TwoRootsDownloadsFolderTests(unittest.TestCase):
    def setUp(self):
        share = tempfile.TemporaryDirectory()
        self.addCleanup(share.cleanup)
        appdata = tempfile.TemporaryDirectory()
        self.addCleanup(appdata.cleanup)
        self.root = Path(share.name)
        self.appdata = Path(appdata.name)
        self.profile = self.appdata / "browser" / "7" / "profile"
        self.held = self.root / "_inbox" / "jo" / "review" / "export-1"
        self.held.mkdir(parents=True)
        (self.held / "IMG_1.JPG").write_bytes(b"held")

    def test_the_folder_is_under_the_working_roots_inbox_not_next_to_the_profile(self):
        folder = download.downloads_folder(self.profile, self.root)
        self.assertEqual(folder, self.root / "_inbox" / ".downloads" / "7")
        self.assertTrue(folder.is_dir())
        self.assertFalse((self.appdata / "_inbox").exists())

    def test_a_profile_that_is_not_an_accounts_profile_folder_is_refused(self):
        for profile in (self.appdata / "browser" / "7" / "other", self.appdata / "browser" / "jo" / "profile"):
            with self.assertRaises(download.DownloadsFolderRefused, msg=profile):
                download.downloads_folder(profile, self.root)
        self.assertFalse((self.root / "_inbox" / ".downloads").exists())

    def test_a_link_on_the_way_is_refused_and_the_held_copies_survive(self):
        staging = self.root / "_inbox" / ".downloads"
        staging.mkdir()
        (staging / "7").symlink_to(self.held, target_is_directory=True)
        with self.assertRaises(download.DownloadsFolderRefused):
            download.downloads_folder(self.profile, self.root)
        self.assertEqual((self.held / "IMG_1.JPG").read_bytes(), b"held")

    def test_the_folders_are_shared_whatever_the_umask(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        working = self.root / "working"
        working.mkdir()
        downloads = download.downloads_folder(self.profile, working)
        for each in (working / "_inbox", downloads.parent, downloads):
            self.assertEqual(mode_of(each), download.SHARED_FOLDER_MODE, each)


class RegistryTests(unittest.TestCase):
    def queued_kinds(self):
        kinds = set(browser_jobs.KINDS) | set(browser_jobs.GUARDED_KINDS)
        for path in (ROOT / "darkroom").rglob("*.py"):
            tree = ast.parse(path.read_text())
            constants = {node.targets[0].id: node.value.value for node in tree.body
                         if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("queue", "enqueue", "enqueue_once") and len(node.args) >= 3:
                    kind = node.args[2]
                    if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                        kinds.add(kind.value)
                    elif isinstance(kind, ast.Name) and kind.id in constants:
                        kinds.add(constants[kind.id])
                    elif isinstance(kind, ast.Attribute) and kind.attr.endswith("_KIND"):
                        kinds.add(getattr(exports, kind.attr, None) or getattr(browser_jobs, kind.attr))
        return kinds

    def test_every_kind_the_backend_can_queue_has_a_job_in_the_worker(self):
        kinds = self.queued_kinds()
        self.assertTrue({"noop", "connect", "schedule", "status", "download", "renew"} <= kinds)
        self.assertEqual(sorted(kinds - set(REGISTRY)), [])

    def test_the_download_job_is_registered(self):
        self.assertIs(REGISTRY["download"], download.download_export)


if __name__ == "__main__":
    unittest.main()
