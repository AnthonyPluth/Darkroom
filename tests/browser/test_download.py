import ast
import re
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import browser_jobs, downloads, exports
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, Export, ExportPart, GoogleAccount
from darkroom_browser import selectors
from darkroom_browser.context import Cancelled, DownloadInterrupted, JobContext
from darkroom_browser.jobs import REGISTRY, download
from tests.browser.scripted import Clock
from tests.browser.test_classify import SIGNED_IN, VERIFY
from tests.browser.test_parse_exports import page, ready
from tests.shared import add_person, own_database, settings_from

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_793_750_400.0
SIZES = ((1, "40 KB"), (2, "40 KB"), (3, "40 KB"))
EXPORTS = page(ready(parts=SIZES))


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
        self.closed = False

    def goto(self, url):
        self.url = url
        self.verifying = 0

    def content(self):
        if self.verifying:
            self.verifying -= 1
            return VERIFY
        return EXPORTS if self.url == selectors.TAKEOUT_MANAGE_URL else SIGNED_IN

    def click(self, selector):
        raise AssertionError("the download job only clicks through download_to")

    def type_text(self, selector, text):
        raise AssertionError("the download job types nothing")

    def wait_for(self, selector, timeout_ms=30_000):
        return None

    def download_to(self, selector, folder, progress=None):
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
        self.settings = settings_from(DARKROOM_PHOTOS_ROOT=folder.name, VNC_URL="https://browser.example/vnc.html")
        with db.session(self.engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo@example.test", default_person_id=person, created=1.0, session_state="ok")
            opened.add(account)
            opened.flush()
            self.account = account.id
            listing = [exports.Listed("aaaa-1111", NOW - 600, "ready", NOW + 7 * 86400, tuple(exports.ListedPart(n, 40 * 1024) for n, _ in SIZES))]
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
            self.assertEqual(exports.reconcile(opened, opened.get(GoogleAccount, self.account), [exports.Listed("aaaa-1111", NOW - 600, "ready", NOW + 7 * 86400, tuple(exports.ListedPart(n, 40 * 1024) for n, _ in SIZES))], NOW).requeued, 1)
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
        browser.content = lambda: page(ready(export_id="other-9")) if browser.url == selectors.TAKEOUT_MANAGE_URL else SIGNED_IN
        with self.assertRaisesRegex(download.DownloadFailed, "no longer lists this export"):
            self.run_job(browser)

    def test_takeout_listing_fewer_parts_than_darkroom_expects_stops_the_job(self):
        browser = DownloadBrowser(["ok"])
        browser.content = lambda: page(ready(parts=SIZES[:2])) if browser.url == selectors.TAKEOUT_MANAGE_URL else SIGNED_IN
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
        folder = download.downloads_folder(self.profile)
        self.assertEqual(folder, self.staging / "7")
        self.assertTrue(folder.is_dir())

    def test_a_profile_that_is_not_an_accounts_profile_folder_is_refused(self):
        for profile in (self.root / "_browser" / ".." / "profile", self.root / "_browser" / "7" / "other", self.root / "_browser" / "jo" / "profile"):
            with self.assertRaises(download.DownloadsFolderRefused, msg=profile):
                download.downloads_folder(profile)
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
                    download.downloads_folder(self.profile)
                self.assert_held_copies_survive()
                link.unlink()
                if link == self.staging / "7":
                    self.staging.rmdir()
                if moved is not None:
                    moved.rename(link)
                    self.held = link / "jo" / "review" / "export-1"

    def test_the_sweep_removes_regular_leftovers_and_only_the_link_of_a_linked_one(self):
        folder = download.downloads_folder(self.profile)
        (folder / "0b7c-guid").write_bytes(b"half a part")
        (folder / "held-link").symlink_to(self.held / "IMG_1.JPG")
        (folder / "folder-link").symlink_to(self.held, target_is_directory=True)
        (folder / "a-folder").mkdir()
        self.assertEqual(download.sweep_downloads(folder), 3)
        self.assertEqual([each.name for each in folder.iterdir()], ["a-folder"])
        self.assert_held_copies_survive()


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
                        kinds.add(getattr(exports, kind.attr))
        return kinds

    def test_every_kind_the_backend_can_queue_has_a_job_in_the_worker(self):
        kinds = self.queued_kinds()
        self.assertTrue({"noop", "connect", "schedule", "status", "download"} <= kinds)
        self.assertEqual(sorted(kinds - set(REGISTRY)), [])

    def test_the_download_job_is_registered(self):
        self.assertIs(REGISTRY["download"], download.download_export)


if __name__ == "__main__":
    unittest.main()
