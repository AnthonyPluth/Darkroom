import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from darkroom import tls
from darkroom.dates import utc_now
from darkroom.domain import alerts, browser_jobs, demo
from darkroom.server import auth
from darkroom.storage import db
from darkroom.storage.models import Export, ImportRun, MediaItem
from darkroom.storage.paths import Photos
from tests.api_world import ApiCase
from tests.shared import fetch, settings_from, start_server


class StatusApiTests(ApiCase):
    def test_with_no_data_it_is_empty_and_the_worker_has_not_been_seen(self):
        self.assertEqual(self.call("GET", "/api/status"), (200, {"worker_last_seen": None, "people": [], "alerts": []}))
        self.assertEqual(self.call("GET", "/api/events"), (200, {"events": []}))

    def test_a_person_with_nothing_yet_has_zeros_and_nulls(self):
        jo = self.person("Jo")
        (card,) = self.call("GET", "/api/status")[1]["people"]
        self.assertEqual(card, {"id": jo["id"], "slug": "jo", "name": "Jo", "indexed": 0, "with_gps": 0, "unknown_time": 0, "accounts": [], "pixels": [],
                                "devices": [], "last_import": None, "unmatched": None, "alerts": []})

    def seed_media(self, person_id):
        rows = (("a.jpg", "exif", "exif_offset"), ("b.jpg", "none", "unknown"), ("c.jpg", "exif", "unknown"), ("d.jpg", "none", "gps_lookup"))
        with db.session(self.engine) as opened:
            for name, gps, time in rows:
                opened.add(MediaItem(person_id=person_id, source="phone", path=f"x/{name}", sha1=name * 8, bytes=1, gps_source=gps, time_source=time, indexed_at=1.0))
            opened.add(MediaItem(person_id=person_id, source="takeout", path="x/t.jpg", sha1="t" * 40, bytes=1, gps_source="google", time_source="unknown", indexed_at=1.0))

    def test_the_card_counts_originals_gps_and_unknown_times_for_phone_items_only(self):
        jo = self.person("Jo")
        self.person("Sam")
        self.seed_media(jo["id"])
        people = self.call("GET", "/api/status")[1]["people"]
        self.assertEqual([(p["slug"], p["indexed"], p["with_gps"], p["unknown_time"]) for p in people], [("jo", 4, 2, 2), ("sam", 0, 0, 0)])

    def test_accounts_pixels_and_devices_appear_on_every_person_they_serve(self):
        jo, sam = self.person("Jo"), self.person("Sam")
        account = self.account(jo["id"])
        pixel = self.pixel(account["id"], "Shared", people_ids=[jo["id"], sam["id"]])
        self.add_device(jo["id"], "Jo's iPhone", last_checkin=50.0, pending=7)
        revoked = self.add_device(jo["id"], "Old iPhone")
        self.call("DELETE", f"/api/devices/{revoked}")
        people = {p["slug"]: p for p in self.call("GET", "/api/status")[1]["people"]}
        for slug in ("jo", "sam"):
            self.assertEqual([a["email"] for a in people[slug]["accounts"]], ["jo.photos@example.test"], slug)
            self.assertEqual([(x["id"], x["name"]) for x in people[slug]["pixels"]], [(pixel["id"], "Shared")], slug)
        self.assertEqual(people["jo"]["accounts"][0]["session_state"], "unknown")
        self.assertEqual(people["jo"]["devices"], [{"id": people["jo"]["devices"][0]["id"], "name": "Jo's iPhone", "last_checkin": 50.0, "pending_uploads": 7, "eligible": None}])
        self.assertEqual(people["sam"]["devices"], [])

    def test_open_alerts_are_listed_overall_and_on_the_people_they_concern(self):
        jo, sam = self.person("Jo"), self.person("Sam")
        account = self.account(jo["id"])
        self.pixel(account["id"], people_ids=[jo["id"], sam["id"]])
        with db.session(self.engine) as opened:
            alerts.open_alert(opened, self.settings, "k1", "sign_in_needed", "jo.photos@example.test", alerts.sign_in_needed("jo.photos@example.test"), None, 10.0)
            alerts.open_alert(opened, self.settings, "k2", "originals_unmatched", "sam", alerts.originals_unmatched("Sam", "Pixel A", 4), None, 20.0)
            alerts.open_alert(opened, self.settings, "k3", "export_imported", "jo", alerts.export_imported("done"), None, 30.0)
        reply = self.call("GET", "/api/status")[1]
        self.assertEqual([a["kind"] for a in reply["alerts"]], ["sign_in_needed", "originals_unmatched"])
        by_person = {p["slug"]: [a["kind"] for a in p["alerts"]] for p in reply["people"]}
        self.assertEqual(by_person, {"jo": ["sign_in_needed"], "sam": ["sign_in_needed", "originals_unmatched"]})
        self.assertEqual(set(reply["alerts"][0]), {"id", "kind", "subject", "message", "link", "opened"})

    def test_last_import_is_the_newest_finished_run_of_an_account_that_serves_the_person(self):
        jo, sam = self.person("Jo"), self.person("Sam")
        account = self.account(jo["id"])
        other = self.account(sam["id"], "sam.photos@example.test")
        with db.session(self.engine) as opened:
            for account_id, finished in ((account["id"], 100.0), (account["id"], 300.0), (account["id"], None), (other["id"], 900.0)):
                export = Export(account_id=account_id, kind="manual")
                opened.add(export)
                opened.flush()
                opened.add(ImportRun(export_id=export.id, started=1.0, finished=finished))
        people = {p["slug"]: p["last_import"] for p in self.call("GET", "/api/status")[1]["people"]}
        self.assertEqual(people, {"jo": 300.0, "sam": 900.0})

    def test_the_worker_last_seen_is_reported(self):
        with db.session(self.engine) as opened:
            browser_jobs.worker_seen(opened, 1234.5)
        self.assertEqual(self.call("GET", "/api/status")[1]["worker_last_seen"], 1234.5)

    def test_events_are_newest_first_and_limited(self):
        self.person("Jo")
        self.person("Sam")
        status, reply = self.call("GET", "/api/events")
        self.assertEqual((status, [(e["type"], e["subject"]) for e in reply["events"]]), (200, [("person_created", "sam"), ("person_created", "jo")]))
        self.assertEqual(set(reply["events"][0]), {"id", "time", "type", "subject", "detail"})
        self.assertEqual(len(self.call("GET", "/api/events?limit=1")[1]["events"]), 1)
        self.assertEqual(len(self.call("GET", "/api/events?limit=")[1]["events"]), 2)

    def test_a_bad_limit_is_a_400(self):
        for limit in ("0", "-1", "abc", "201", "1.5", "9999999"):
            with self.subTest(limit=limit):
                status, reply = self.call("GET", f"/api/events?limit={limit}")
                self.assertEqual((status, reply), (400, {"error": "limit must be a whole number from 1 to 200"}))


class SeededDemoStatusTests(unittest.TestCase):
    def test_the_demo_data_fills_every_card(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = db.make_engine(f"sqlite:///{root / 'demo.db'}")
            self.addCleanup(engine.dispose)
            db.Base.metadata.create_all(engine)
            (root / "photos").mkdir()
            settings = settings_from(DARKROOM_DEMO="1", DARKROOM_PHOTOS_ROOT=str(root / "photos"))
            with db.session(engine) as opened, mock.patch.object(tls, "urlopen", side_effect=AssertionError):
                demo.seed(opened, Photos(root / "photos"), settings, date(2026, 6, 15))
                token = demo.sign_in(opened, settings, utc_now().timestamp())
            base, _ = start_server(self, settings, engine)
            status, _, body = fetch(base, "GET", "/api/status", {"Cookie": f"{auth.SESSION_COOKIE}={token}"})
            self.assertEqual(status, 200)
            reply = json.loads(body)
        people = {p["slug"]: p for p in reply["people"]}
        self.assertEqual(sorted(people), ["avery", "blair", "casey"])
        self.assertTrue(all(p["indexed"] > 0 and len(p["devices"]) == 1 for p in people.values()))
        self.assertEqual([a["email"] for a in people["blair"]["accounts"]], ["avery.photos@example.com"])
        self.assertEqual(people["blair"]["accounts"][0]["session_state"], "needs_sign_in")
        self.assertEqual([a["kind"] for a in reply["alerts"]], ["sign_in_needed"])
        self.assertEqual(len(people["avery"]["alerts"]), 1)
        self.assertEqual(people["casey"]["alerts"], [])
        self.assertEqual(people["casey"]["devices"][0]["pending_uploads"], 3)


if __name__ == "__main__":
    unittest.main()
