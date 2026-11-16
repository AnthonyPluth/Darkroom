import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select, update

from darkroom import config, oidc
from darkroom.providers import syncthing
from darkroom.storage import db
from darkroom.domain import accounts, alerts, browser_jobs, dedupe, pixels, transport
from darkroom.storage.models import Alert, BrowserJob, Event, Export, GoogleAccount, MediaItem, Pixel, PixelCopy, PixelPerson
from darkroom.storage.paths import Photos
from tests.api_world import ApiCase
from tests.fakes.syncthing import FakeSyncthing
from tests.privacy import no_leaks
from tests.shared import add_person, fetch, own_database, settings_from, start_server

FIRST_DEVICE = syncthing.device_id_of_certificate(b"made-up certificate of the first pixel in the pixel api tests")
SECOND_DEVICE = syncthing.device_id_of_certificate(b"made-up certificate of the second pixel in the pixel api tests")


class PixelsApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.jo = self.person("Jo")
        self.sam = self.person("Sam")
        self.kai = self.person("Kai")
        self.account = self.account(self.jo["id"])

    def served(self):
        status, reply = self.call("GET", "/api/accounts")
        self.assertEqual(status, 200)
        (found,) = [a for a in reply["accounts"] if a["id"] == self.account["id"]]
        return found["served_people"]

    def test_a_pixel_serves_the_accounts_default_person_unless_told_otherwise(self):
        pixel = self.pixel(self.account["id"], syncthing_device_id=FIRST_DEVICE)
        self.assertEqual((pixel["people_ids"], pixel["syncthing_device_id"], pixel["google_account_id"]), ([self.jo["id"]], FIRST_DEVICE, self.account["id"]))
        self.assertEqual(self.call("GET", "/api/pixels")[1], {"pixels": [pixel], "syncthing": None})

    def test_only_a_pixel_that_starts_serving_someone_new_scans_for_items_to_confirm(self):
        with mock.patch.object(dedupe, "confirm_served_matches", wraps=dedupe.confirm_served_matches) as scan:
            pixel = self.pixel(self.account["id"], "First")
            scan.assert_not_called()
            self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"name": "Renamed", "syncthing_device_id": FIRST_DEVICE})[0], 200)
            self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [self.jo["id"]]})[0], 200)
            scan.assert_not_called()
            self.pixel(self.account["id"], "Second", people_ids=[self.jo["id"]])
            scan.assert_not_called()
            self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [self.jo["id"], self.sam["id"]]})[0], 200)
            self.assertEqual(scan.call_count, 1)
            self.pixel(self.account["id"], "Third", people_ids=[self.kai["id"]])
            self.assertEqual(scan.call_count, 2)

    def test_a_pixel_with_one_person_and_a_pixel_with_three_round_trip(self):
        one = self.pixel(self.account["id"], "Solo", people_ids=[self.sam["id"]])
        three = self.pixel(self.account["id"], "Shared", people_ids=[self.kai["id"], self.jo["id"], self.sam["id"]])
        listed = self.call("GET", "/api/pixels")[1]["pixels"]
        self.assertEqual([(p["name"], p["people_ids"]) for p in listed], [("Solo", [self.sam["id"]]), ("Shared", sorted([self.jo["id"], self.sam["id"], self.kai["id"]]))])
        self.assertEqual(listed[1], three)
        self.assertEqual(listed[0], one)

    def test_adding_a_person_to_a_pixel_changes_the_served_people_at_once(self):
        pixel = self.pixel(self.account["id"])
        self.assertEqual(self.served(), [self.jo["id"]])
        status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [self.jo["id"], self.sam["id"]]})
        self.assertEqual((status, reply["pixel"]["people_ids"]), (200, [self.jo["id"], self.sam["id"]]))
        self.assertEqual(self.served(), [self.jo["id"], self.sam["id"]])
        self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [self.kai["id"]]})
        self.assertEqual(self.served(), [self.jo["id"], self.kai["id"]])
        self.call("DELETE", f"/api/pixels/{pixel['id']}")
        self.assertEqual(self.served(), [self.jo["id"]])

    def test_moving_a_pixel_to_another_account_moves_who_that_account_serves(self):
        other = self.account_for("kai.photos@example.test", self.kai["id"])
        pixel = self.pixel(self.account["id"], people_ids=[self.sam["id"]])
        self.call("PATCH", f"/api/pixels/{pixel['id']}", {"google_account_id": other["id"]})
        self.assertEqual(self.served(), [self.jo["id"]])
        (found,) = [a for a in self.call("GET", "/api/accounts")[1]["accounts"] if a["id"] == other["id"]]
        self.assertEqual(found["served_people"], sorted([self.kai["id"], self.sam["id"]]))

    def account_for(self, email, person_id):
        return self.call("POST", "/api/accounts", {"email": email, "default_person_id": person_id})[1]["account"]

    def test_update_changes_only_what_is_sent(self):
        pixel = self.pixel(self.account["id"], "Old", syncthing_device_id=SECOND_DEVICE)
        status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", {"name": "New"})
        self.assertEqual((status, reply["pixel"]["name"], reply["pixel"]["syncthing_device_id"]), (200, "New", SECOND_DEVICE))
        status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", {"syncthing_device_id": None})
        self.assertEqual(reply["pixel"]["syncthing_device_id"], None)
        self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {})[0], 200)

    def test_validation_errors_and_404s(self):
        account = self.account["id"]
        bad = (
            ({}, "name is required"),
            ({"name": "P"}, "google_account_id must be a whole number"),
            ({"name": "P", "google_account_id": 0}, "google_account_id must be between 1 and 2147483647"),
            ({"name": "P", "google_account_id": account, "people_ids": []}, "a Pixel serves at least one person"),
            ({"name": "P", "google_account_id": account, "people_ids": "all"}, "people_ids must be a list of whole numbers"),
            ({"name": "P", "google_account_id": account, "people_ids": [1, 1]}, "people_ids lists the same number twice"),
            ({"name": "P", "google_account_id": account, "people_ids": [True]}, "people_ids must be a list of whole numbers"),
            ({"name": "P", "google_account_id": account, "people_ids": list(range(1, 60))}, "people_ids has too many entries"),
            ({"name": "P", "google_account_id": account, "syncthing_device_id": "bad id!"}, "syncthing_device_id: not a Syncthing device ID"),
        )
        for body, message in bad:
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/pixels", body), (400, {"error": message}))
        self.assertEqual(self.call("POST", "/api/pixels", {"name": "P", "google_account_id": 999}), (404, {"error": "no such Google account"}))
        self.assertEqual(self.call("POST", "/api/pixels", {"name": "P", "google_account_id": account, "people_ids": [999]}), (404, {"error": "no such person"}))
        pixel = self.pixel(account)
        path = f"/api/pixels/{pixel['id']}"
        self.assertEqual(self.call("PATCH", path, {"people_ids": []}), (400, {"error": "a Pixel serves at least one person"}))
        self.assertEqual(self.call("PATCH", path, {"name": ""}), (400, {"error": "name is required"}))
        self.assertEqual(self.call("PATCH", path, {"google_account_id": 999}), (404, {"error": "no such Google account"}))
        self.assertEqual(self.call("PATCH", path, {"people_ids": [999]}), (404, {"error": "no such person"}))
        self.assertEqual(self.call("PATCH", "/api/pixels/999", {"name": "x"}), (404, {"error": "no such Pixel"}))
        self.assertEqual(self.call("PATCH", "/api/pixels/abc", {"name": "x"}), (404, {"error": "no such Pixel"}))
        self.assertEqual(self.call("DELETE", "/api/pixels/999"), (404, {"error": "no such Pixel"}))
        self.assertEqual(self.call("DELETE", "/api/pixels/abc"), (404, {"error": "no such Pixel"}))
        self.assertEqual(self.call("GET", "/api/pixels")[1]["pixels"][0]["people_ids"], [self.jo["id"]])

    def test_last_sync_is_null_until_something_records_it_and_is_returned_when_set(self):
        pixel = self.pixel(self.account["id"])
        self.assertIsNone(pixel["last_sync"])
        self.assertIsNone(self.call("GET", "/api/pixels")[1]["pixels"][0]["last_sync"])
        with db.session(self.engine) as opened:
            opened.get(Pixel, pixel["id"]).last_sync = 1234.5
        self.assertEqual(self.call("GET", "/api/pixels")[1]["pixels"][0]["last_sync"], 1234.5)
        status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", {"name": "Renamed", "last_sync": 1.0})
        self.assertEqual((status, reply["pixel"]["last_sync"]), (200, 1234.5))

    def test_deleting_a_pixel_removes_its_links_and_nothing_else(self):
        pixel = self.pixel(self.account["id"], people_ids=[self.jo["id"], self.sam["id"]])
        self.assertEqual(self.call("DELETE", f"/api/pixels/{pixel['id']}"), (200, {"ok": True}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(PixelPerson)).scalars().all(), [])
        self.assertEqual([p["slug"] for p in self.call("GET", "/api/people")[1]["people"]], ["jo", "sam", "kai"])

    def test_a_deleted_pixels_id_is_never_given_to_a_new_pixel(self):
        kept = self.pixel(self.account["id"], "Kept")
        gone = self.pixel(self.account["id"], "Gone")
        self.assertEqual(self.call("DELETE", f"/api/pixels/{gone['id']}"), (200, {"ok": True}))
        new = self.pixel(self.account["id"], "New")
        self.assertNotIn(new["id"], (kept["id"], gone["id"]))
        self.assertGreater(new["id"], gone["id"])

    def test_a_pixel_with_copies_queued_or_in_its_folder_is_not_deleted_until_they_are_pruned(self):
        pixel = self.pixel(self.account["id"])
        with db.session(self.engine) as opened:
            item = MediaItem(person_id=self.jo["id"], source="phone", path="jo/originals/a.jpg", sha1="a" * 40, bytes=1, indexed_at=1.0)
            opened.add(item)
            opened.flush()
            opened.add(PixelCopy(pixel_id=pixel["id"], item_id=item.id, person_id=self.jo["id"], state="placed", bytes=1, sha1="a" * 40, queued_at=1.0))
        for state in ("placed", "queued"):
            with self.subTest(state=state):
                with db.session(self.engine) as opened:
                    opened.execute(update(PixelCopy).values(state=state))
                status, reply = self.call("DELETE", f"/api/pixels/{pixel['id']}")
                self.assertEqual(status, 409)
                self.assertIn("still has photos", reply["error"])
                self.assertEqual([p["id"] for p in self.call("GET", "/api/pixels")[1]["pixels"]], [pixel["id"]])
        with db.session(self.engine) as opened:
            opened.execute(update(PixelCopy).values(state="pruned"))
        self.assertEqual(self.call("DELETE", f"/api/pixels/{pixel['id']}"), (200, {"ok": True}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(PixelCopy)).scalars().all(), [])

    def copy_of(self, pixel_id, person_id, state, name="a.jpg"):
        with db.session(self.engine) as opened:
            item = MediaItem(person_id=person_id, source="phone", path=f"p{person_id}/originals/{name}", sha1="a" * 40, bytes=1, indexed_at=1.0)
            opened.add(item)
            opened.flush()
            opened.add(PixelCopy(pixel_id=pixel_id, item_id=item.id, person_id=person_id, state=state, bytes=1, sha1="a" * 40, queued_at=1.0))

    def states(self):
        with db.session(self.engine) as opened:
            return sorted((row.person_id, row.state) for row in opened.execute(select(PixelCopy)).scalars())

    def test_taking_someone_off_a_pixel_drops_their_queued_photos_so_it_can_then_be_deleted(self):
        pixel = self.pixel(self.account["id"], people_ids=[self.jo["id"], self.sam["id"]])
        self.copy_of(pixel["id"], self.sam["id"], "queued", "q.jpg")
        self.copy_of(pixel["id"], self.jo["id"], "pruned", "p.jpg")
        self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [self.jo["id"]]})[0], 200)
        self.assertEqual(self.states(), [(self.jo["id"], "pruned")])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(Event.detail).where(Event.type == "pixel_copies_dropped")).scalars().all(),
                             ["1 queued photos of people it no longer serves"])
        self.assertEqual(self.call("DELETE", f"/api/pixels/{pixel['id']}"), (200, {"ok": True}))

    def test_a_pixel_keeps_its_account_and_people_while_their_photos_are_in_its_folder(self):
        status, reply = self.call("POST", "/api/accounts", {"email": "other@example.test", "default_person_id": self.sam["id"]})
        self.assertEqual(status, 201, reply)
        other = reply["account"]
        pixel = self.pixel(self.account["id"], people_ids=[self.jo["id"], self.sam["id"]])
        self.copy_of(pixel["id"], self.sam["id"], "placed", "s.jpg")
        self.copy_of(pixel["id"], self.jo["id"], "queued", "j.jpg")
        for change in ({"people_ids": [self.jo["id"]]}, {"google_account_id": other["id"]}):
            with self.subTest(change=change):
                status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", change)
                self.assertEqual(status, 409)
                self.assertIn("still has photos in its folder", reply["error"])
        self.assertEqual(self.states(), [(self.jo["id"], "queued"), (self.sam["id"], "placed")])
        (found,) = self.call("GET", "/api/pixels")[1]["pixels"]
        self.assertEqual((found["google_account_id"], found["people_ids"]), (self.account["id"], sorted([self.jo["id"], self.sam["id"]])))
        self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [self.sam["id"], self.kai["id"]]})[0], 200)
        self.assertEqual(self.states(), [(self.sam["id"], "placed")])

    def test_a_busy_pixel_answers_503_to_try_again(self):
        pixel = self.pixel(self.account["id"])
        from darkroom.domain import pixels as pixel_domain

        with mock.patch.object(pixel_domain, "lock", side_effect=accounts.Busy("Darkroom is busy; try again in a moment")):
            self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"name": "Renamed"})[0], 503)
            self.assertEqual(self.call("DELETE", f"/api/pixels/{pixel['id']}")[0], 503)
        self.assertEqual(self.call("GET", "/api/pixels")[1]["pixels"][0]["name"], "Pixel A")

    def test_a_typed_device_id_is_checked_with_syncthings_format_and_stored_in_its_canonical_form(self):
        loose = FIRST_DEVICE.lower().replace("-", "")
        pixel = self.pixel(self.account["id"], syncthing_device_id=f"  {loose}  ")
        self.assertEqual(pixel["syncthing_device_id"], FIRST_DEVICE)
        wrong_check = SECOND_DEVICE[:13] + ("A" if SECOND_DEVICE[13] != "A" else "B") + SECOND_DEVICE[14:]
        path = f"/api/pixels/{pixel['id']}"
        refused = (
            (SECOND_DEVICE[:-1], "syncthing_device_id: not a Syncthing device ID"),
            (SECOND_DEVICE.replace(SECOND_DEVICE[0], "1", 1), "syncthing_device_id: not a Syncthing device ID"),
            ("MADE-UP-DEVICE", "syncthing_device_id: not a Syncthing device ID"),
            (wrong_check, "syncthing_device_id: a check character of the Syncthing device ID is wrong"),
        )
        for typed, message in refused:
            with self.subTest(typed=typed):
                self.assertEqual(self.call("PATCH", path, {"syncthing_device_id": typed}), (400, {"error": message}))
                self.assertEqual(self.call("POST", "/api/pixels", {"name": "P", "google_account_id": self.account["id"], "syncthing_device_id": typed}),
                                 (400, {"error": message}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Pixel, pixel["id"]).syncthing_device_id, FIRST_DEVICE)
            self.assertEqual(opened.query(Pixel).count(), 1)

    def test_a_typed_device_id_already_on_another_pixel_is_refused(self):
        first = self.pixel(self.account["id"], "First", syncthing_device_id=FIRST_DEVICE)
        second = self.pixel(self.account["id"], "Second")
        taken = (409, {"error": pixels.DEVICE_TAKEN})
        self.assertEqual(self.call("PATCH", f"/api/pixels/{second['id']}", {"syncthing_device_id": FIRST_DEVICE.lower()}), taken)
        self.assertEqual(self.call("POST", "/api/pixels", {"name": "Third", "google_account_id": self.account["id"], "syncthing_device_id": FIRST_DEVICE}), taken)
        self.assertEqual(self.call("PATCH", f"/api/pixels/{first['id']}", {"name": "Still first", "syncthing_device_id": FIRST_DEVICE})[0], 200)
        with db.session(self.engine) as opened:
            self.assertEqual([pixel.syncthing_device_id for pixel in opened.query(Pixel).order_by(Pixel.id)], [FIRST_DEVICE, None])

    def test_a_stored_device_id_sent_back_unchanged_is_kept_as_it_was(self):
        pixel = self.pixel(self.account["id"])
        with db.session(self.engine) as opened:
            opened.get(Pixel, pixel["id"]).syncthing_device_id = "DEMO-FAMILY-PIXEL"
        status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", {"name": "Renamed", "syncthing_device_id": "DEMO-FAMILY-PIXEL"})
        self.assertEqual((status, reply["pixel"]["syncthing_device_id"]), (200, "DEMO-FAMILY-PIXEL"))
        self.assertEqual(self.call("PATCH", f"/api/pixels/{pixel['id']}", {"syncthing_device_id": "DEMO-FAMILY-PIXEL-2"}),
                         (400, {"error": "syncthing_device_id: not a Syncthing device ID"}))


class AccountsApiTests(ApiCase):
    def test_create_list_update_delete(self):
        jo, sam = self.person("Jo"), self.person("Sam")
        account = self.account(jo["id"], "Jo.Photos@Example.test")
        self.assertEqual((account["email"], account["default_person_id"], account["session_state"], account["served_people"]), ("Jo.Photos@Example.test", jo["id"], "unknown", [jo["id"]]))
        self.assertEqual(self.call("GET", "/api/accounts")[1], {"accounts": [account]})
        status, reply = self.call("PATCH", f"/api/accounts/{account['id']}", {"default_person_id": sam["id"]})
        self.assertEqual((status, reply["account"]["default_person_id"], reply["account"]["served_people"]), (200, sam["id"], [sam["id"]]))
        status, reply = self.call("PATCH", f"/api/accounts/{account['id']}", {"email": "new@example.test"})
        self.assertEqual((reply["account"]["email"], reply["account"]["default_person_id"]), ("new@example.test", sam["id"]))
        self.assertEqual(self.call("PATCH", f"/api/accounts/{account['id']}", {})[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/accounts/{account['id']}"), (200, {"ok": True}))
        self.assertEqual(self.call("GET", "/api/accounts")[1], {"accounts": []})

    def test_a_duplicate_email_is_a_conflict_whatever_its_case(self):
        jo = self.person("Jo")
        first = self.account(jo["id"], "a@example.test")
        second = self.account(jo["id"], "b@example.test")
        self.assertEqual(self.call("POST", "/api/accounts", {"email": "A@EXAMPLE.test", "default_person_id": jo["id"]})[0], 409)
        self.assertEqual(self.call("PATCH", f"/api/accounts/{second['id']}", {"email": "a@example.test"})[0], 409)
        self.assertEqual(self.call("PATCH", f"/api/accounts/{first['id']}", {"email": "a@example.test"})[0], 200)

    def test_a_non_ascii_duplicate_email_is_a_conflict_on_create_and_update(self):
        jo = self.person("Jo")
        first = self.account(jo["id"], "\u00c4@example.test")
        self.assertEqual(first["email"], "\u00c4@example.test")
        self.assertEqual(self.call("POST", "/api/accounts", {"email": "\u00e4@example.test", "default_person_id": jo["id"]})[0], 409)
        second = self.account(jo["id"], "other@example.test")
        self.assertEqual(self.call("PATCH", f"/api/accounts/{second['id']}", {"email": "\u00e4@EXAMPLE.test"})[0], 409)
        self.assertEqual(self.call("PATCH", f"/api/accounts/{first['id']}", {"email": "\u00e4@example.test"})[0], 200)
        self.assertEqual(self.call("GET", "/api/accounts")[1]["accounts"][0]["email"], "\u00e4@example.test")

    def sign_in_alert(self, email):
        with db.session(self.engine) as opened:
            alerts.open_alert(opened, self.settings, "k1", "sign_in_needed", email, alerts.sign_in_needed(email), None, 10.0, about=alerts.NOBODY)

    def alerts_of_jo(self):
        status, reply = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        (card,) = reply["people"]
        return [(a["subject"], a["message"]) for a in card["alerts"]], [(a["subject"]) for a in reply["alerts"]]

    def test_editing_an_accounts_email_keeps_its_open_sign_in_alert_on_the_people_it_serves(self):
        jo = self.person("Jo")
        account = self.account(jo["id"], "old@example.test")
        self.sign_in_alert("old@example.test")
        self.assertEqual(self.call("PATCH", f"/api/accounts/{account['id']}", {"email": "new@example.test"})[0], 200)
        shown, overall = self.alerts_of_jo()
        self.assertEqual([subject for subject, _ in shown], ["new@example.test"])
        self.assertIn("new@example.test", shown[0][1])
        self.assertNotIn("old@example.test", shown[0][1])
        self.assertEqual(overall, ["new@example.test"])

    def test_a_case_only_email_edit_keeps_the_alert_too(self):
        jo = self.person("Jo")
        account = self.account(jo["id"], "jo@example.test")
        self.sign_in_alert("jo@example.test")
        self.assertEqual(self.call("PATCH", f"/api/accounts/{account['id']}", {"email": "Jo@Example.test"})[0], 200)
        self.assertEqual([subject for subject, _ in self.alerts_of_jo()[0]], ["Jo@Example.test"])

    def test_editing_an_email_leaves_other_accounts_and_closed_alerts_alone(self):
        jo = self.person("Jo")
        account = self.account(jo["id"], "a@example.test")
        other = self.account(jo["id"], "b@example.test")
        self.sign_in_alert("b@example.test")
        with db.session(self.engine) as opened:
            alerts.open_alert(opened, self.settings, "k2", "sign_in_needed", "a@example.test", alerts.sign_in_needed("a@example.test"), None, 11.0, about=alerts.NOBODY)
            alerts.close(opened, "k2", 12.0)
        self.call("PATCH", f"/api/accounts/{account['id']}", {"email": "c@example.test"})
        self.assertEqual([subject for subject, _ in self.alerts_of_jo()[0]], ["b@example.test"])
        with db.session(self.engine) as opened:
            self.assertEqual(sorted(a.subject for a in opened.execute(select(Alert)).scalars()), ["a@example.test", "b@example.test"])
        self.assertEqual(other["email"], "b@example.test")

    def test_validation_errors_and_404s(self):
        jo = self.person("Jo")
        for body, message in (({}, "email is required"), ({"email": "nope"}, "email must be an email address"), ({"email": "a b@example.test"}, "email must be an email address"),
                              ({"email": "a@b@example.test"}, "email must be an email address"), ({"email": "a@example."}, "email must be an email address"),
                              ({"email": "a@example.test"}, "default_person_id must be a whole number"), ({"email": "a@example.test", "default_person_id": "1"}, "default_person_id must be a whole number")):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/accounts", body), (400, {"error": message}))
        self.assertEqual(self.call("POST", "/api/accounts", {"email": "a@example.test", "default_person_id": 999}), (404, {"error": "no such person"}))
        account = self.account(jo["id"])
        path = f"/api/accounts/{account['id']}"
        self.assertEqual(self.call("PATCH", path, {"email": ""}), (400, {"error": "email is required"}))
        self.assertEqual(self.call("PATCH", path, {"default_person_id": 999}), (404, {"error": "no such person"}))
        self.assertEqual(self.call("PATCH", "/api/accounts/999", {"email": "x@example.test"}), (404, {"error": "no such Google account"}))
        self.assertEqual(self.call("PATCH", "/api/accounts/abc", {"email": "x@example.test"}), (404, {"error": "no such Google account"}))
        self.assertEqual(self.call("DELETE", "/api/accounts/999"), (404, {"error": "no such Google account"}))
        self.assertEqual(self.call("DELETE", "/api/accounts/abc"), (404, {"error": "no such Google account"}))

    def make_profile(self, account_id):
        profile = self.photos / "_browser" / str(account_id) / "profile"
        profile.mkdir(parents=True)
        (profile / "Cookies").write_bytes(b"signed-in session")
        failures = self.photos / "_browser" / str(account_id) / "failures" / "1"
        failures.mkdir(parents=True)
        (failures / "page.html").write_bytes(b"snapshot")
        return profile

    def bystanders(self):
        files = {self.photos / "_browser" / "99" / "profile" / "Cookies": b"other", self.photos / "jo" / "originals" / "a.jpg": b"photo",
                 self.photos / "jo" / "google-only" / "b.jpg": b"g", self.photos / "_inbox" / "jo" / "c.zip": b"zip"}
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return files

    def test_deleting_an_account_retires_its_browser_profile_so_a_new_account_inherits_nothing(self):
        jo = self.person("Jo")
        keep = self.bystanders()
        account = self.account(jo["id"], "gone@example.test")
        profile = self.make_profile(account["id"])
        self.assertEqual(self.call("DELETE", f"/api/accounts/{account['id']}"), (200, {"ok": True}))
        self.assertFalse(profile.exists())
        self.assertFalse((self.photos / "_browser" / str(account["id"])).exists())
        fresh = self.account(jo["id"], "fresh@example.test")
        self.assertFalse((self.photos / "_browser" / str(fresh["id"]) / "profile").exists())
        for path, content in keep.items():
            self.assertEqual(path.read_bytes(), content)

    def test_an_account_without_a_browser_folder_deletes_cleanly(self):
        jo = self.person("Jo")
        account = self.account(jo["id"])
        self.assertEqual(self.call("DELETE", f"/api/accounts/{account['id']}")[0], 200)

    def test_a_browser_folder_that_is_a_link_is_never_followed_and_the_account_stays(self):
        jo = self.person("Jo")
        keep = self.bystanders()
        account = self.account(jo["id"])
        (self.photos / "_browser").mkdir(exist_ok=True)
        (self.photos / "_browser" / str(account["id"])).symlink_to(self.photos / "jo")
        status, reply = self.call("DELETE", f"/api/accounts/{account['id']}")
        self.assertEqual(status, 500)
        self.assertIn("is a link", reply["error"])
        self.assertIn("so the account was not deleted", reply["error"])
        self.assertEqual(len(self.call("GET", "/api/accounts")[1]["accounts"]), 1)
        for path, content in keep.items():
            self.assertEqual(path.read_bytes(), content)

    def test_a_profile_that_cannot_be_removed_keeps_the_account(self):
        from unittest import mock

        jo = self.person("Jo")
        account = self.account(jo["id"])
        profile = self.make_profile(account["id"])
        with mock.patch("shutil.rmtree", side_effect=PermissionError("no")):
            status, reply = self.call("DELETE", f"/api/accounts/{account['id']}")
        self.assertEqual((status, reply), (500, {"error": "the account's browser folder could not be removed, so the account was not deleted"}))
        self.assertEqual(len(self.call("GET", "/api/accounts")[1]["accounts"]), 1)
        self.assertTrue(profile.exists())
        self.assertEqual(self.call("DELETE", f"/api/accounts/{account['id']}")[0], 200)

    def test_deleting_an_account_closes_its_alerts_so_status_and_the_ticker_drop_them(self):
        jo = self.person("Jo")
        account = self.account(jo["id"], "gone@example.test")
        other = self.account(jo["id"], "stay@example.test")
        with db.session(self.engine) as opened:
            alerts.open_alert(opened, self.settings, browser_jobs.sign_in_key(account["id"]), "sign_in_needed", "gone@example.test",
                              alerts.sign_in_needed("gone@example.test"), None, 10.0, about=alerts.account(account["id"]))
            alerts.open_alert(opened, self.settings, browser_jobs.sign_in_key(other["id"]), "sign_in_needed", "stay@example.test",
                              alerts.sign_in_needed("stay@example.test"), None, 10.0, about=alerts.account(other["id"]))
        self.assertEqual(self.call("DELETE", f"/api/accounts/{account['id']}")[0], 200)
        self.assertEqual([a["subject"] for a in self.call("GET", "/api/status")[1]["alerts"]], ["stay@example.test"])
        with db.session(self.engine) as opened:
            sent = alerts.tick(opened, self.settings, 10.0 + 10 * 86_400, notifier=lambda *args: "sent")
            subjects = [e.detail for e in opened.execute(select(Event).where(Event.type == "alert_sent")).scalars()]
        self.assertEqual(sent, 1)
        self.assertNotIn("gone@example.test", subjects)

    def test_an_account_with_pixels_or_exports_is_not_deleted(self):
        jo = self.person("Jo")
        account = self.account(jo["id"])
        pixel = self.pixel(account["id"])
        status, reply = self.call("DELETE", f"/api/accounts/{account['id']}")
        self.assertEqual(status, 409)
        self.assertIn("Pixels", reply["error"])
        self.call("DELETE", f"/api/pixels/{pixel['id']}")
        with db.session(self.engine) as opened:
            opened.add(Export(account_id=account["id"], kind="manual"))
        self.assertIn("exports", self.call("DELETE", f"/api/accounts/{account['id']}")[1]["error"])
        self.assertEqual(len(self.call("GET", "/api/accounts")[1]["accounts"]), 1)

    def test_an_account_with_a_running_or_waiting_browser_job_is_not_deleted_until_it_finishes(self):
        jo = self.person("Jo")
        account = self.account(jo["id"])
        profile = self.make_profile(account["id"])
        for status in browser_jobs.OPEN_STATUSES:
            with db.session(self.engine) as opened:
                job = browser_jobs.enqueue(opened, account["id"], "noop", None, 1.0)
                job.status = status
                job_id = job.id
            status_code, reply = self.call("DELETE", f"/api/accounts/{account['id']}")
            self.assertEqual(status_code, 409, status)
            self.assertIn("browser job that has not finished", reply["error"])
            self.assertTrue(profile.exists())
            with db.session(self.engine) as opened:
                opened.get(BrowserJob, job_id).status = "done"
        self.assertEqual(len(self.call("GET", "/api/accounts")[1]["accounts"]), 1)

    def test_a_job_the_lease_just_expired_blocks_the_delete_for_the_lease_window(self):
        jo = self.person("Jo")
        account = self.account(jo["id"])
        profile = self.make_profile(account["id"])
        with db.session(self.engine) as opened:
            job = browser_jobs.enqueue(opened, account["id"], "noop", None, 1.0)
            job.status = "failed"
            job.message = browser_jobs.EXPIRED_MESSAGE
            job.finished = time.time()
        status_code, reply = self.call("DELETE", f"/api/accounts/{account['id']}")
        self.assertEqual(status_code, 409)
        self.assertIn("stopped reporting less than 3 minutes ago", reply["error"])
        self.assertNotIn("has not finished", reply["error"])
        self.assertTrue(profile.exists())

    def test_a_delete_that_cannot_get_the_write_lock_answers_503_to_try_again(self):
        jo = self.person("Jo")
        account = self.account(jo["id"])
        with mock.patch.object(accounts, "delete", side_effect=accounts.Busy("Darkroom is busy with another change; try deleting the account again in a moment")):
            status_code, reply = self.call("DELETE", f"/api/accounts/{account['id']}")
        self.assertEqual((status_code, reply), (503, {"error": "Darkroom is busy with another change; try deleting the account again in a moment"}))
        self.assertEqual(len(self.call("GET", "/api/accounts")[1]["accounts"]), 1)

    def test_finished_and_queued_browser_jobs_do_not_block_deleting_an_account(self):
        jo = self.person("Jo")
        account = self.account(jo["id"])
        for status in ("done", "failed", "queued"):
            with db.session(self.engine) as opened:
                browser_jobs.enqueue(opened, account["id"], "noop", None, 1.0).status = status
        self.assertEqual(self.call("DELETE", f"/api/accounts/{account['id']}"), (200, {"ok": True}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(BrowserJob).count(), 0)


KEY = "pairing-api-syncthing-key-canary-5e2a"
OWN = syncthing.device_id_of_certificate(b"made-up certificate of darkroom in the pairing api tests")
WAITING = syncthing.device_id_of_certificate(b"made-up certificate of a pixel waiting to pair")
STRANGER = syncthing.device_id_of_certificate(b"made-up certificate of a device that never tried to connect")
ADDRESS = "192.0.2.77:22000"
OIDC = {"OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "darkroom", "OIDC_ALLOWED_GROUPS": "parents", "DARKROOM_SECRET_KEY": "pairing-api-key-0123456789abcdef"}


class PairingApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        socket = self.root / "st.sock"
        self.fake = FakeSyncthing(self, KEY, OWN, str(socket))
        self.fake.pending = {WAITING: {"name": "Jo's Pixel", "address": ADDRESS, "time": "2026-11-16T10:00:00Z"}}
        patched = mock.patch.object(config, "SYNCTHING_SOCKET", socket)
        patched.start()
        self.addCleanup(patched.stop)
        self.settings = dataclasses.replace(settings_from(DARKROOM_PHOTOS_ROOT=str(self.root), **OIDC), syncthing=True, syncthing_key=KEY)
        self.base, self.settings = start_server(self, self.settings, self.engine)
        moment = time.time()
        with db.session(self.engine, writing=True) as opened:
            oidc.remember_user(opened, oidc.Identity("u1", "jo@example.test", "Jo", ("parents",), True), moment)
            self.session = oidc.new_session(opened, self.settings, "u1", moment)
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo.photos@example.test", default_person_id=person, created=1.0)
            opened.add(account)
            opened.flush()
            for name in ("Jo's Pixel", "Other Pixel"):
                pixel = Pixel(name=name, google_account_id=account.id, created=1.0)
                opened.add(pixel)
                opened.flush()
                opened.add(PixelPerson(pixel_id=pixel.id, person_id=person))
            self.pixel_id, self.other_id = [pixel.id for pixel in opened.query(Pixel).order_by(Pixel.id)]
        self.replies: list[bytes] = []

    def call(self, method, path, body=None, base=None, cookie=True, origin=None):
        headers = {"Cookie": f"darkroom_session={self.session}"} if cookie else {}
        if method != "GET":
            headers |= {"Content-Type": "application/json", "Origin": origin or self.base}
        status, _, reply = fetch(base or self.base, method, path, headers, None if method == "GET" else json.dumps(body if body is not None else {}).encode())
        self.replies.append(reply)
        return status, json.loads(reply)

    def approve(self, device=WAITING, confirmed=True, pixel_id=None, **options):
        return self.call("POST", f"/api/pixels/{pixel_id or self.pixel_id}/pair", {"device_id": device, "confirmed": confirmed}, **options)

    def devices(self):
        with db.session(self.engine) as opened:
            return [pixel.syncthing_device_id for pixel in opened.query(Pixel).order_by(Pixel.id)]

    def writes_to_syncthing(self):
        return [(received.method, received.path) for received in self.fake.requests if received.method != "GET"]

    def test_the_routes_are_session_routes(self):
        from darkroom.server import routes

        found = {f"{route.method} {route.path}": route.auth for route in routes.ROUTES}
        for key in ("GET /api/pairing/device", "GET /api/pairing/waiting", "POST /api/pixels/{id}/pair", "POST /api/pixels/{id}/release-folder"):
            self.assertEqual(found[key], "session", key)

    def test_darkrooms_own_device_id_is_shown_to_a_signed_in_person(self):
        self.assertEqual(self.call("GET", "/api/pairing/device"), (200, {"device_id": OWN}))
        self.assertEqual(self.call("GET", "/api/pairing/device", cookie=False), (401, {"error": "sign in required"}))

    def test_the_waiting_devices_show_their_id_and_a_cleaned_name_and_nothing_else(self):
        self.fake.pending[STRANGER] = {"name": "Odd\x07\nname" + "x" * 300, "address": ADDRESS}
        self.fake.pending["not-a-device-id"] = {"name": "ignored"}
        with no_leaks(self, ADDRESS, KEY, self.session, replies=self.replies):
            status, reply = self.call("GET", "/api/pairing/waiting")
        self.assertEqual(status, 200)
        expected = sorted([{"device_id": WAITING, "name": "Jo's Pixel"}, {"device_id": STRANGER, "name": "Oddname" + "x" * 93}], key=lambda found: found["device_id"])
        self.assertEqual(reply, {"devices": expected})
        self.assertEqual(self.call("GET", "/api/pairing/waiting", cookie=False), (401, {"error": "sign in required"}))

    def test_looking_at_waiting_devices_pairs_nothing(self):
        for _ in range(3):
            self.assertEqual(self.call("GET", "/api/pairing/waiting")[0], 200)
            self.assertEqual(self.call("GET", "/api/pairing/device")[0], 200)
        self.assertEqual(self.devices(), [None, None])
        self.assertEqual(self.writes_to_syncthing(), [])
        self.assertEqual(set(self.fake.devices), {OWN})

    def test_approving_a_waiting_device_saves_its_id_for_that_pixel_and_the_reconciler_then_wants_it(self):
        with no_leaks(self, ADDRESS, KEY, self.session, replies=self.replies, database=self.engine):
            status, reply = self.approve(WAITING.lower().replace("-", ""))
        self.assertEqual((status, reply["pixel"]["id"], reply["pixel"]["syncthing_device_id"]), (200, self.pixel_id, WAITING))
        self.assertEqual(self.devices(), [WAITING, None])
        self.assertEqual(self.writes_to_syncthing(), [])
        with db.session(self.engine) as opened:
            wanted = transport.plan(opened, Photos(self.root), lambda pixel_id: False, None).wanted
            self.assertEqual((wanted[self.pixel_id].device, wanted[self.pixel_id].code), (WAITING, None))
            self.assertEqual(opened.query(Event).filter(Event.type == "pixel_updated").count(), 1)

    def test_nothing_is_paired_without_a_session_from_another_origin_or_without_explicit_confirmation(self):
        refused = (
            (self.approve(cookie=False), (401, {"error": "sign in required"})),
            (self.approve(origin="https://evil.example"), (403, {"error": "cross-site requests are refused"})),
            (self.approve(confirmed=False), (400, {"error": "confirmed must be true: compare the device ID with the one the Pixel's Syncthing app shows first"})),
            (self.approve(confirmed="yes"), (400, {"error": "confirmed must be true or false"})),
            (self.call("POST", f"/api/pixels/{self.pixel_id}/pair", {"device_id": WAITING}), (400, {"error": "confirmed must be true or false"})),
            (self.call("POST", f"/api/pixels/{self.pixel_id}/pair", {"confirmed": True}), (400, {"error": "device_id is required"})),
            (self.approve(STRANGER), (409, {"error": "that device is not waiting to connect to Darkroom; check the ID, or type it in the Pixel's details instead"})),
            (self.approve(OWN), (409, {"error": "that device is not waiting to connect to Darkroom; check the ID, or type it in the Pixel's details instead"})),
            (self.approve(WAITING[:-1]), (400, {"error": "syncthing_device_id: not a Syncthing device ID"})),
            (self.approve(pixel_id=999), (404, {"error": "no such Pixel"})),
            (self.call("POST", "/api/pixels/abc/pair", {"device_id": WAITING, "confirmed": True}), (404, {"error": "no such Pixel"})),
        )
        for answer, expected in refused:
            with self.subTest(expected=expected):
                self.assertEqual(answer, expected)
        self.assertEqual(self.devices(), [None, None])
        self.assertEqual(self.writes_to_syncthing(), [])

    def test_a_device_id_already_paired_with_another_pixel_is_refused(self):
        with db.session(self.engine) as opened:
            opened.get(Pixel, self.other_id).syncthing_device_id = WAITING.lower()
        self.assertEqual(self.approve(), (409, {"error": pixels.DEVICE_TAKEN}))
        self.assertEqual(self.devices(), [None, WAITING.lower()])
        self.assertEqual(self.approve(pixel_id=self.other_id)[0], 200)
        self.assertEqual(self.devices(), [None, WAITING])

    def test_a_reset_phone_is_paired_again_with_its_new_id(self):
        with db.session(self.engine) as opened:
            opened.get(Pixel, self.pixel_id).syncthing_device_id = STRANGER
        self.assertEqual(self.approve()[0], 200)
        self.assertEqual(self.devices(), [WAITING, None])

    def test_when_syncthing_cannot_be_reached_nothing_is_paired(self):
        unreachable = (503, {"error": "Darkroom's Syncthing could not be reached; try again in a moment, or type the Pixel's device ID instead"})
        self.fake.status = 500
        self.assertEqual(self.approve(), unreachable)
        self.assertEqual(self.call("GET", "/api/pairing/waiting"), unreachable)
        self.assertEqual(self.call("GET", "/api/pairing/device"), unreachable)
        self.fake.status = 0
        with no_leaks(self, KEY, "pairing-api-wrong-key-canary-0001", replies=self.replies):
            for key in ("", "pairing-api-wrong-key-canary-0001"):
                base, _ = start_server(self, dataclasses.replace(self.settings, public_url="", syncthing_key=key), self.engine)
                self.assertEqual(self.call("GET", "/api/pairing/device", base=base), unreachable)
                self.assertEqual(self.approve(base=base, origin=base), unreachable)
        self.assertEqual(self.devices(), [None, None])

    def test_with_the_transport_off_every_pairing_route_says_so_and_typing_still_works(self):
        base, _ = start_server(self, dataclasses.replace(self.settings, public_url="", syncthing=False), self.engine)
        off = (409, {"error": "the Syncthing transport is off; type the Pixel's device ID in the Pixel's details instead"})
        self.assertEqual(self.call("GET", "/api/pairing/device", base=base), off)
        self.assertEqual(self.call("GET", "/api/pairing/waiting", base=base), off)
        self.assertEqual(self.approve(base=base, origin=base), off)
        self.assertEqual(self.call("POST", f"/api/pixels/{self.pixel_id}/release-folder", {"confirmed": True}, base=base, origin=base), off)
        self.assertEqual(self.fake.requests, [])
        status, reply = self.call("PATCH", f"/api/pixels/{self.pixel_id}", {"syncthing_device_id": WAITING}, base=base, origin=base)
        self.assertEqual((status, reply["pixel"]["syncthing_device_id"]), (200, WAITING))

    def removed(self, pixel_id):
        with db.session(self.engine) as opened:
            return transport.removed_before(opened, pixel_id)

    def test_a_removed_folder_is_released_only_on_explicit_confirmation_by_a_signed_in_person(self):
        path = f"/api/pixels/{self.pixel_id}/release-folder"
        nothing = (409, {"error": transport.NOTHING_TO_RELEASE})
        self.assertEqual(self.call("POST", path, {"confirmed": True}), nothing)
        with db.session(self.engine, writing=True) as opened:
            transport.hold_removed(opened, self.pixel_id, 5.0)
        refused = (
            (self.call("POST", path, {"confirmed": True}, cookie=False), (401, {"error": "sign in required"})),
            (self.call("POST", path, {"confirmed": True}, origin="https://evil.example"), (403, {"error": "cross-site requests are refused"})),
            (self.call("POST", path, {}), (400, {"error": "confirmed must be true or false"})),
            (self.call("POST", path, {"confirmed": False}), (400, {"error": "confirmed must be true to let Darkroom share this Pixel's folder again"})),
            (self.call("POST", "/api/pixels/999/release-folder", {"confirmed": True}), (404, {"error": "no such Pixel"})),
            (self.call("POST", "/api/pixels/abc/release-folder", {"confirmed": True}), (404, {"error": "no such Pixel"})),
            (self.call("POST", f"/api/pixels/{self.other_id}/release-folder", {"confirmed": True}), nothing),
        )
        for answer, expected in refused:
            with self.subTest(expected=expected):
                self.assertEqual(answer, expected)
        self.assertTrue(self.removed(self.pixel_id))
        with no_leaks(self, KEY, str(self.root), self.session, replies=self.replies):
            self.assertEqual(self.call("POST", path, {"confirmed": True}), (200, {"ok": True}))
        self.assertFalse(self.removed(self.pixel_id))
        self.assertEqual(self.call("POST", path, {"confirmed": True}), nothing)
        self.assertEqual(self.writes_to_syncthing(), [])
        with db.session(self.engine) as opened:
            found = opened.query(Event.type, Event.subject, Event.detail).filter(Event.type.in_((transport.FOLDER_REMOVED, transport.FOLDER_RELEASED))).order_by(Event.id).all()
        self.assertEqual([tuple(row) for row in found], [(transport.FOLDER_REMOVED, f"pixel {self.pixel_id}", None), (transport.FOLDER_RELEASED, f"pixel {self.pixel_id}", None)])


if __name__ == "__main__":
    unittest.main()
