import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from darkroom import tls
from darkroom.dates import utc_now
from darkroom.domain import alerts, browser_jobs, demo, events, pixel_check, share_check, syncthing_version, transport
from darkroom.providers import syncthing
from darkroom.server import auth
from darkroom.storage import db
from darkroom.storage.models import Export, ImportRun, IndexSkip, MediaItem, Pixel, PixelTransport
from tests.privacy import no_leaks
from darkroom.storage.paths import Photos
from tests.api_world import ApiCase
from tests.shared import fetch, settings_from, start_server


def made_up_device(index: int) -> str:
    return syncthing.device_id_of_certificate(f"made-up certificate of status test pixel {index}".encode())


class StatusApiTests(ApiCase):
    def test_with_no_data_it_is_empty_and_the_worker_has_not_been_seen(self):
        self.assertEqual(self.call("GET", "/api/status"), (200, {"worker_last_seen": None, "people": [], "alerts": [], "syncthing": None}))
        self.assertEqual(self.call("GET", "/api/events"), (200, {"events": []}))

    def test_a_person_with_nothing_yet_has_zeros_and_nulls(self):
        jo = self.person("Jo")
        (card,) = self.call("GET", "/api/status")[1]["people"]
        self.assertEqual(card, {"id": jo["id"], "slug": "jo", "name": "Jo", "indexed": 0, "with_gps": 0, "unknown_time": 0, "index_skipped": 0, "accounts": [], "pixels": [],
                                "last_import": None, "unmatched": None, "alerts": []})

    def test_the_card_counts_the_files_the_index_set_aside_for_each_person(self):
        jo = self.person("Jo")
        self.person("Sam")
        with db.session(self.engine) as opened:
            for name in ("a.jpg", "b.png"):
                opened.add(IndexSkip(person_id=jo["id"], path=f"jo/originals/{name}", bytes=1, mtime=1.0, reason="not a photo or video", skipped_at=2.0))
        people = {card["slug"]: card["index_skipped"] for card in self.call("GET", "/api/status")[1]["people"]}
        self.assertEqual(people, {"jo": 2, "sam": 0})
        self.assertNotIn(b"a.jpg", self.replies[-1])

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

    def test_accounts_and_pixels_appear_on_every_person_they_serve(self):
        jo, sam = self.person("Jo"), self.person("Sam")
        account = self.account(jo["id"])
        pixel = self.pixel(account["id"], "Shared", people_ids=[jo["id"], sam["id"]])
        people = {p["slug"]: p for p in self.call("GET", "/api/status")[1]["people"]}
        for slug in ("jo", "sam"):
            self.assertEqual([a["email"] for a in people[slug]["accounts"]], ["jo.photos@example.test"], slug)
            self.assertEqual([(x["id"], x["name"]) for x in people[slug]["pixels"]], [(pixel["id"], "Shared")], slug)
        self.assertEqual(people["jo"]["accounts"][0]["session_state"], "unknown")

    def test_open_alerts_are_listed_overall_and_on_the_people_they_concern(self):
        jo, sam = self.person("Jo"), self.person("Sam")
        account = self.account(jo["id"])
        self.pixel(account["id"], people_ids=[jo["id"], sam["id"]])
        with db.session(self.engine) as opened:
            alerts.open_alert(opened, self.settings, "k1", "sign_in_needed", "jo.photos@example.test", alerts.sign_in_needed("jo.photos@example.test"), None, 10.0, about=alerts.NOBODY)
            alerts.open_alert(opened, self.settings, "k2", "originals_unmatched", "sam", alerts.originals_unmatched("Sam", "Pixel A", 4), None, 20.0, about=alerts.NOBODY)
            alerts.open_alert(opened, self.settings, "k3", "export_imported", "jo", alerts.export_imported("done"), None, 30.0, about=alerts.NOBODY)
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
            settings = settings_from(DARKROOM_DEMO="1", DARKROOM_SYNCTHING="1", DARKROOM_PHOTOS_ROOT=str(root / "photos"))
            with db.session(engine) as opened, mock.patch.object(tls, "urlopen", side_effect=AssertionError):
                demo.seed(opened, Photos(root / "photos"), settings, date(2026, 6, 15))
                token = demo.sign_in(opened, settings, utc_now().timestamp())
            base, _ = start_server(self, settings, engine)
            status, _, body = fetch(base, "GET", "/api/status", {"Cookie": f"{auth.SESSION_COOKIE}={token}"})
            self.assertEqual(status, 200)
            reply = json.loads(body)
        people = {p["slug"]: p for p in reply["people"]}
        self.assertEqual(sorted(people), ["avery", "blair", "casey"])
        self.assertTrue(all(p["indexed"] > 0 for p in people.values()))
        self.assertEqual([a["email"] for a in people["blair"]["accounts"]], ["avery.photos@example.com", "blair.photos@example.com"])
        self.assertEqual(people["blair"]["accounts"][0]["session_state"], "needs_sign_in")
        self.assertEqual(sorted(a["kind"] for a in reply["alerts"]), ["pixel_held", "schedule_renewal_failed", "schedule_renewal_unconfirmed", "sign_in_needed"])
        self.assertEqual(sorted(a["kind"] for a in people["avery"]["alerts"]), ["schedule_renewal_unconfirmed", "sign_in_needed"])
        self.assertEqual(people["casey"]["alerts"], [])
        card = reply["syncthing"]
        self.assertEqual(card["state"], "running")
        self.assertEqual({pixel["name"]: pixel["state"] for pixel in card["pixels"]}, {"Family Pixel": "connected", "Casey's Pixel": "not_seen", "Blair's Pixel": "held",
                                                                                         "Avery's old Pixel": "device_id_invalid", "Casey's work Pixel": "device_id_shared",
                                                                                         "Casey's spare Pixel": "device_id_shared", "Blair's second Pixel": "folder_removed",
                                                                                         "Avery's travel Pixel": "folder_path_refused", "Casey's new Pixel": "not_paired"})
        others = {pixel["name"]: pixel["others"] for pixel in card["pixels"]}
        self.assertEqual((others["Casey's work Pixel"], others["Casey's spare Pixel"], others["Family Pixel"]), (["Casey's spare Pixel"], ["Casey's work Pixel"], []))


class SyncthingStatusApiTests(ApiCase):
    extra_env = {"DARKROOM_SYNCTHING": "1"}

    def setUp(self):
        super().setUp()
        jo = self.person()
        account = self.account(jo["id"])
        names = ("Unpaired", "Held", "Checking", "Waiting", "Away", "Sending", "Deleting", "Connected", "Recent")
        self.pixels = {name: self.pixel(account["id"], name, syncthing_device_id=None if name == "Unpaired" else made_up_device(index))["id"]
                       for index, name in enumerate(names)}
        self.now = utc_now().timestamp()

    def record(self, kind, subject, detail=None, at=None):
        with db.session(self.engine) as opened:
            events.record(opened, self.now if at is None else at, kind, subject, detail)

    def rows(self):
        now = self.now
        values = {
            "Held": {"error": transport.HELD}, "Checking": {"error": transport.CHECKING}, "Waiting": {},
            "Away": {"last_seen": now - 4 * 86_400 - 60}, "Sending": {"connected": True, "completion": 82.5, "last_seen": now},
            "Deleting": {"connected": True, "completion": 100.0, "synced": False, "last_seen": now},
            "Connected": {"connected": True, "completion": 100.0, "synced": True, "last_seen": now}, "Recent": {"last_seen": now - 5 * 3600},
        }
        with db.session(self.engine) as opened:
            for name, fields in values.items():
                opened.add(PixelTransport(pixel_id=self.pixels[name], updated=now, **fields))
            events.record(opened, now, pixel_check.UNACCOUNTED, f"pixel {self.pixels['Held']}", "files 3")

    def states(self, card):
        return {pixel["name"]: (pixel["state"], pixel["files"], pixel["days"]) for pixel in card["pixels"]}

    def test_before_the_first_reconcile_the_card_is_starting_and_no_pixel_syncs(self):
        status, reply = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        card = reply["syncthing"]
        self.assertEqual((card["state"], card["reason"], card["since"]), ("starting", None, None))
        self.assertEqual({state for state, _, _ in self.states(card).values()}, {"not_syncing"})

    def test_running_gives_each_pixel_its_state_in_the_signed_off_order(self):
        self.record(transport.RECOVERED, transport.SUBJECT)
        self.rows()
        _, reply = self.call("GET", "/api/status")
        self.assertEqual(reply["syncthing"]["state"], "running")
        self.assertEqual(self.states(reply["syncthing"]), {
            "Unpaired": ("not_paired", 0, 0), "Held": ("held", 3, 0), "Checking": ("checking", 0, 0), "Waiting": ("waiting", 0, 0), "Away": ("not_seen", 0, 4),
            "Sending": ("sending", 0, 0), "Deleting": ("sending", 0, 0), "Connected": ("connected", 0, 0), "Recent": ("not_connected", 0, 0)})
        person = reply["people"][0]
        self.assertEqual([(pixel["name"], pixel["syncthing"]["state"]) for pixel in person["pixels"]][:2], [("Unpaired", "not_paired"), ("Held", "held")])
        _, listed = self.call("GET", "/api/pixels")
        self.assertEqual({pixel["name"]: pixel["state"] for pixel in listed["syncthing"]}["Sending"], "sending")

    def stored(self, **devices):
        with db.session(self.engine, writing=True) as opened:
            for name, device in devices.items():
                opened.get(Pixel, self.pixels[name]).syncthing_device_id = device
            opened.commit()

    def errors(self, **codes):
        with db.session(self.engine, writing=True) as opened:
            for name, code in codes.items():
                opened.get(PixelTransport, self.pixels[name]).error = code
            opened.commit()

    def test_a_pixel_with_no_usable_folder_shows_its_own_state_ahead_of_held_and_checking(self):
        self.record(transport.RECOVERED, transport.SUBJECT)
        self.rows()
        shared = made_up_device(99)
        self.stored(Held=shared, Checking=shared.lower().replace("-", ""), Waiting=shared, Away="MADEUP-NOT-A-DEVICE-ID")
        self.errors(Held=transport.SHARED_DEVICE, Checking=transport.SHARED_DEVICE, Waiting=transport.SHARED_DEVICE, Away=transport.INVALID_DEVICE,
                    Sending=transport.REMOVED, Connected=transport.PATH_REFUSED)
        self.record(transport.FOLDER_REMOVED, f"pixel {self.pixels['Sending']}")
        _, reply = self.call("GET", "/api/status")
        card = reply["syncthing"]
        self.assertEqual({pixel["name"]: pixel["state"] for pixel in card["pixels"]}, {
            "Unpaired": "not_paired", "Held": "device_id_shared", "Checking": "device_id_shared", "Waiting": "device_id_shared", "Away": "device_id_invalid",
            "Sending": "folder_removed", "Deleting": "sending", "Connected": "folder_path_refused", "Recent": "not_connected"})
        others = {pixel["name"]: pixel["others"] for pixel in card["pixels"]}
        self.assertEqual((others["Held"], others["Checking"], others["Waiting"]), (["Checking", "Waiting"], ["Held", "Waiting"], ["Held", "Checking"]))
        self.assertEqual({name for name, listed in others.items() if listed}, {"Held", "Checking", "Waiting"})
        person = {pixel["name"]: pixel["syncthing"]["state"] for pixel in reply["people"][0]["pixels"]}
        self.assertEqual((person["Away"], person["Sending"]), ("device_id_invalid", "folder_removed"))
        _, listed = self.call("GET", "/api/pixels")
        self.assertEqual({pixel["name"]: pixel["others"] for pixel in listed["syncthing"]}["Held"], ["Checking", "Waiting"])
        self.replies.clear()
        with no_leaks(self, shared, shared.replace("-", ""), "MADEUP-NOT-A-DEVICE-ID", replies=self.replies):
            self.call("GET", "/api/status")

    def test_an_id_no_longer_shared_or_a_released_folder_shows_checking_until_the_next_reconcile(self):
        self.record(transport.RECOVERED, transport.SUBJECT)
        self.rows()
        self.errors(Held=transport.INVALID_DEVICE, Checking=transport.SHARED_DEVICE, Waiting=transport.REMOVED)
        self.record(transport.FOLDER_REMOVED, f"pixel {self.pixels['Waiting']}")
        states = lambda reply: {pixel["name"]: pixel["state"] for pixel in reply["syncthing"]["pixels"]}
        self.assertEqual(states(self.call("GET", "/api/status")[1])["Waiting"], "folder_removed")
        self.assertEqual(self.call("POST", f"/api/pixels/{self.pixels['Waiting']}/release-folder", {"confirmed": True}), (200, {"ok": True}))
        found = states(self.call("GET", "/api/status")[1])
        self.assertEqual((found["Held"], found["Checking"], found["Waiting"]), ("device_id_invalid", "checking", "checking"))

    def test_each_failure_gives_its_card_state_and_fixed_reason(self):
        self.rows()
        cases = (
            (transport.FAILED, transport.SUBJECT, transport.UNREACHABLE, ("down", None)),
            (syncthing_version.EVENT, syncthing_version.SUBJECT, syncthing_version.NOT_ANSWERING, ("down", None)),
            (transport.FAILED, transport.SUBJECT, transport.NOT_KEPT, ("settings_failed", transport.NOT_KEPT)),
            (syncthing_version.EVENT, syncthing_version.SUBJECT, "Syncthing reported v2.2.0, outside the tested range v2.1.7",
             ("version", "Syncthing reported v2.2.0, outside the tested range v2.1.7")),
            (syncthing_version.EVENT, syncthing_version.SUBJECT, syncthing_version.KEY_REFUSED, ("key", syncthing_version.KEY_REFUSED)),
            (transport.PAUSED, transport.SUBJECT, share_check.REBOUND, ("share_check_failed", share_check.REBOUND)),
            (share_check.FAILED, share_check.SUBJECT, share_check.MISSING, ("share_check_failed", share_check.MISSING)),
        )
        for at, (kind, subject, detail, expected) in enumerate(cases):
            with self.subTest(kind=kind, detail=detail):
                self.record(kind, subject, detail, at=self.now - 7200 + at)
                _, reply = self.call("GET", "/api/status")
                card = reply["syncthing"]
                self.assertEqual((card["state"], card["reason"]), expected)
                self.assertEqual(card["since"], self.now - 7200 + at)
                self.assertEqual({state for state, _, _ in self.states(card).values()}, {"not_syncing"})

    def test_a_share_check_wins_over_a_syncthing_that_is_down(self):
        self.record(share_check.FAILED, share_check.SUBJECT, share_check.MISSING, at=self.now - 10)
        self.record(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE)
        _, reply = self.call("GET", "/api/status")
        self.assertEqual(reply["syncthing"]["state"], "share_check_failed")

    def test_no_pixel_is_listed_on_the_card_until_one_is_paired(self):
        for name, pixel_id in self.pixels.items():
            if name != "Unpaired":
                self.assertEqual(self.call("DELETE", f"/api/pixels/{pixel_id}")[0], 200)
        self.record(transport.RECOVERED, transport.SUBJECT)
        _, reply = self.call("GET", "/api/status")
        self.assertEqual(reply["syncthing"]["pixels"], [])
        self.assertEqual(reply["people"][0]["pixels"][0]["syncthing"]["state"], "not_paired")

    def test_no_reply_names_a_device_id_or_a_file(self):
        canary = "CANARY9-DEVICEI-D000000"
        with db.session(self.engine) as opened:
            opened.get(Pixel, self.pixels["Held"]).syncthing_device_id = canary
        self.record(transport.RECOVERED, transport.SUBJECT)
        self.rows()
        with no_leaks(self, canary, replies=self.replies):
            self.call("GET", "/api/status")


class SyncthingOffStatusApiTests(ApiCase):
    def test_with_darkrooms_syncthing_off_nothing_about_it_is_sent(self):
        jo = self.person()
        self.pixel(self.account(jo["id"])["id"], "Pixel A", syncthing_device_id=made_up_device(1))
        _, reply = self.call("GET", "/api/status")
        self.assertIsNone(reply["syncthing"])
        self.assertIsNone(reply["people"][0]["pixels"][0]["syncthing"])
        self.assertIsNone(self.call("GET", "/api/pixels")[1]["syncthing"])


class DemoSyncthingRouteTests(ApiCase):
    def test_a_server_that_is_not_a_demo_answers_404_and_changes_nothing(self):
        status, reply = self.call("POST", "/api/demo/syncthing", {"state": "down"})
        self.assertEqual((status, reply), (404, {"error": "not found"}))
        with db.session(self.engine) as opened:
            self.assertEqual(events.recent(opened), [])


class DemoSyncthingSwitchTests(unittest.TestCase):
    def test_a_demo_server_switches_the_state_and_refuses_one_it_does_not_know(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = db.make_engine(f"sqlite:///{root / 'demo.db'}")
            self.addCleanup(engine.dispose)
            db.Base.metadata.create_all(engine)
            (root / "photos").mkdir()
            settings = settings_from(DARKROOM_DEMO="1", DARKROOM_SYNCTHING="1", DARKROOM_PHOTOS_ROOT=str(root / "photos"))
            with db.session(engine) as opened, mock.patch.object(tls, "urlopen", side_effect=AssertionError):
                demo.seed(opened, Photos(root / "photos"), settings, date(2026, 6, 15))
                token = demo.sign_in(opened, settings, utc_now().timestamp())
            base, _ = start_server(self, settings, engine)
            headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}", "Content-Type": "application/json"}
            self.assertEqual(fetch(base, "POST", "/api/demo/syncthing", headers, b'{"state": "sideways"}')[0], 400)
            self.assertEqual(fetch(base, "POST", "/api/demo/syncthing", headers, b'{}')[0], 400)
            status, _, body = fetch(base, "POST", "/api/demo/syncthing", headers, b'{"state": "down"}')
            self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
            reply = json.loads(fetch(base, "GET", "/api/status", headers)[2])
            self.assertEqual(reply["syncthing"]["state"], "down")
            self.assertIn("syncthing_down", [alert["kind"] for alert in reply["alerts"]])


if __name__ == "__main__":
    unittest.main()
