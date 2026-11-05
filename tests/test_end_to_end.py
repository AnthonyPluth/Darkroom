import json
import shutil
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.domain import alerts, browser_jobs, exports
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, BrowserJob, DedupeDecision, Event, Export, GoogleAccount, ImportRun, MediaItem
from tests.import_world import ADMIN_KEY, DAY, NOW, World
from tests.shared import fetch, settings_from, start_server
from tests.takeout_fixtures import TakeoutBuilder

TOKEN = "end-to-end-worker-token-0123456789abcdef0123456789"
HOUR = 3600.0
CANAL = "A made-up canal"
DAWN = "The canal at dawn"
OLD_PLACE = (45.4, 12.3)
NEW_PLACE = (45.5, 12.4)
SUMMARY = "Takeout import finished"


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def to(self, moment):
        self.now = moment


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self, people=("jo", "sam"), pixel_people=("jo", "sam"))
        self.clock = Clock(NOW)
        for patch in (mock.patch("darkroom.server.api.browser_internal.now", new=self.clock),
                      mock.patch.object(alerts, "notifier_for", return_value=self.world.recorder)):
            patch.start()
            self.addCleanup(patch.stop)
        env = {"DARKROOM_PHOTOS_ROOT": str(self.world.root), "DARKROOM_DATA_DIR": str(self.world.base / "data"), "IMMICH_URL": self.world.fake.url,
               "IMMICH_ADMIN_KEY": ADMIN_KEY, "DARKROOM_BROWSER_TOKEN": TOKEN, "DARKROOM_ALLOW_NO_AUTH": "1", "TZ": "UTC"}
        self.base, self.settings = start_server(self, settings_from(**env), self.world.engine)
        self.phone = self.bytes_of(1)
        self.canal = self.bytes_of(2)
        self.sam = self.bytes_of(3)
        self.world.original("jo", "IMG_1.JPG", self.phone)
        self.world.original("sam", "IMG_3.JPG", self.sam)
        self.world.index_all()
        with db.session(self.world.engine) as opened:
            opened.get(GoogleAccount, self.world.account).session_state = "ok"
        self.scheduler = jobs.build(self.settings, self.world.engine, NOW)
        self.takeouts = 0

    def bytes_of(self, seed):
        return TakeoutBuilder(self.world.base / f"scratch-{seed}", seed=seed).image_bytes(".JPG")

    def worker(self, path, body=None):
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        status, _, reply = fetch(self.base, "POST", path, headers, json.dumps(body or {}).encode())
        self.assertEqual(status, 200, (path, reply))
        return json.loads(reply)

    def claim(self, kind):
        job = self.worker("/internal/browser/jobs/claim")["job"]
        while job is not None and job["kind"] == "status" and kind != "status":
            self.worker(f"/internal/browser/jobs/{job['id']}/done", {"result": {"exports": []}})
            job = self.worker("/internal/browser/jobs/claim")["job"]
        self.assertEqual(job["kind"], kind)
        return job["id"]

    def tick(self, moment):
        self.clock.to(moment)
        return self.scheduler.run_due(moment)

    def query(self, work):
        with db.session(self.world.engine) as opened:
            return work(opened)

    def takeout_zip(self, place, description, new=()):
        self.takeouts += 1
        builder = TakeoutBuilder(self.world.base / f"takeout-{self.takeouts}", seed=50 + self.takeouts, stamp=f"2027010{self.takeouts}T000000Z")
        builder.photo("IMG_1.JPG", data=self.phone, gps=None)
        builder.photo("IMG_2.JPG", data=self.canal, gps=place, description=description)
        builder.photo("IMG_3.JPG", data=self.sam, gps=None)
        for name in new:
            builder.photo(name, gps=None)
        return builder.build().parts[0]

    def schedule_created(self):
        with db.session(self.world.engine) as opened:
            browser_jobs.queue(opened, self.world.account, "schedule", {}, self.clock())
        job = self.claim("schedule")
        self.worker(f"/internal/browser/jobs/{job}/done", {"result": {"schedule_created": self.clock(), "schedule_end": self.clock() + 365 * DAY}})

    def download(self, google_id, part):
        with db.session(self.world.engine) as opened:
            exports.check_now(opened, self.clock())
        status = self.claim("status")
        listing = [{"google_id": google_id, "created_at": self.clock() - 600, "state": "ready", "expires_at": self.clock() + 7 * DAY,
                    "parts": [{"number": 1, "bytes": part.stat().st_size}]}]
        self.worker(f"/internal/browser/jobs/{status}/done", {"result": {"exports": listing}})
        export = self.query(lambda opened: opened.execute(select(Export).where(Export.google_id == google_id)).scalar_one())
        folder = self.world.photos.export_tree("jo", f"export-{export.id}")
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(part, folder / part.name)
        job = self.claim("download")
        self.assertEqual(self.worker(f"/internal/browser/jobs/{job}/download")["part_count"], 1)
        self.assertTrue(self.worker(f"/internal/browser/jobs/{job}/parts/1", {"event": "attempt"})["allowed"])
        self.worker(f"/internal/browser/jobs/{job}/parts/1", {"event": "done", "filename": part.name, "bytes": part.stat().st_size})
        self.worker(f"/internal/browser/jobs/{job}/done", {"result": {"parts": 1}})
        return export.id

    def titles(self):
        return [title for title, _ in self.world.recorder.sent]

    def test_a_year_of_exports_from_the_first_import_to_the_renewal_and_the_incremental_one(self):
        self.schedule_created()
        end = self.query(lambda opened: opened.get(GoogleAccount, self.world.account).schedule_end)
        self.assertEqual(end, NOW + 365 * DAY)

        self.clock.to(NOW + DAY)
        first = self.download("g-1", self.takeout_zip(OLD_PLACE, CANAL))
        self.assertEqual(self.query(lambda opened: (opened.get(Export, first).status, opened.get(Export, first).kind)), ("downloaded", "initial"))
        self.assertIn("imports", self.tick(NOW + DAY + 2 * HOUR))
        run_one = self.query(lambda opened: opened.execute(select(ImportRun).order_by(ImportRun.id)).scalars().all()[0])
        self.assertEqual((run_one.status, run_one.added, run_one.skipped_t1, run_one.failed), ("done", 1, 2, 0))
        self.assertEqual(self.titles().count(SUMMARY), 1)
        self.assertIn("Export 1: 1 added, 2 skipped as duplicates, 0 in review, 0 missing GPS", [message for _, message in self.world.recorder.sent])
        self.assertEqual(sorted(set(self.world.scanned())), ["lib-jo"])
        self.assertTrue((self.world.root / "jo/google-only/2017/07/IMG_2.JPG.xmp").is_file())
        self.assertEqual(self.titles().count("Takeout schedule ending"), 0)

        notice = end - 21 * DAY
        self.assertIn("schedules.watch", self.tick(notice - 3 * HOUR))
        self.assertEqual(self.titles().count("Takeout schedule ending"), 0)
        self.assertIn("schedules.watch", self.tick(notice + DAY))
        self.assertEqual(self.titles().count("Takeout schedule ending"), 1)
        renew = self.claim("renew")
        renewed_at = self.clock()
        self.worker(f"/internal/browser/jobs/{renew}/done", {"result": {"schedule_created": renewed_at, "schedule_end": renewed_at + 365 * DAY}})
        self.assertEqual(self.titles().count("Takeout schedule renewed"), 1)
        alerts_open = self.query(lambda opened: sorted(a.kind for a in opened.execute(select(Alert).where(Alert.closed.is_(None))).scalars()))
        self.assertNotIn("schedule_ending", alerts_open)
        self.tick(notice + 2 * DAY)
        renewals = self.query(lambda opened: opened.execute(select(BrowserJob.id).where(BrowserJob.kind == "renew")).scalars().all())
        self.assertEqual(len(renewals), 1)

        self.clock.to(notice + 3 * DAY)
        second = self.download("g-2", self.takeout_zip(NEW_PLACE, DAWN, new=("IMG_4.JPG",)))
        self.assertEqual(self.query(lambda opened: opened.get(Export, second).kind), "initial")
        scans_before = len(self.world.scanned())
        self.assertIn("imports", self.tick(notice + 3 * DAY + 2 * HOUR))
        run_two = self.query(lambda opened: opened.execute(select(ImportRun).where(ImportRun.export_id == second)).scalar_one())
        self.assertEqual((run_two.status, run_two.added, run_two.skipped_t1, run_two.failed, run_two.review), ("done", 1, 1, 0, 0))
        self.assertIn("Export 2: 1 added, 1 skipped as duplicates, 0 in review, 1 missing GPS", [message for _, message in self.world.recorder.sent])
        self.assertEqual(self.titles().count(SUMMARY), 2)
        self.assertGreater(len(self.world.scanned()), scans_before)

        text = (self.world.root / "jo/google-only/2017/07/IMG_2.JPG.xmp").read_text()
        self.assertIn(DAWN, text)
        self.assertNotIn(CANAL, text)
        events = self.query(lambda opened: opened.execute(select(Event.type).order_by(Event.id)).scalars().all())
        self.assertEqual(events.count("metadata_refreshed"), 1)
        phone_ids = self.query(lambda opened: opened.execute(select(MediaItem.id).where(MediaItem.source == "phone")).scalars().all())
        phone_matches = self.query(lambda opened: [(d.export_id, d.source_path) for d in opened.execute(select(DedupeDecision)).scalars() if d.matched_item_id in phone_ids])
        self.assertEqual({export_id for export_id, _ in phone_matches}, {first})
        takeout = self.query(lambda opened: opened.execute(select(MediaItem.path).where(MediaItem.source == "takeout").order_by(MediaItem.path)).scalars().all())
        self.assertEqual(len(takeout), 2)

        self.assertEqual(self.tick(notice + 3 * DAY + 4 * HOUR).count("imports"), 1)
        self.assertEqual(self.titles().count(SUMMARY), 2)


if __name__ == "__main__":
    unittest.main()
