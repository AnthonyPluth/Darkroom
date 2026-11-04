import time
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.storage import db
from darkroom.domain import accounts, alerts, browser_jobs
from darkroom.storage.models import Alert, BrowserJob, Event, Export, Pixel, PixelPerson
from tests.api_world import ApiCase


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
        pixel = self.pixel(self.account["id"], syncthing_device_id="ABCD123-EFGH456")
        self.assertEqual((pixel["people_ids"], pixel["syncthing_device_id"], pixel["google_account_id"]), ([self.jo["id"]], "ABCD123-EFGH456", self.account["id"]))
        self.assertEqual(self.call("GET", "/api/pixels")[1], {"pixels": [pixel]})

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
        pixel = self.pixel(self.account["id"], "Old", syncthing_device_id="AAA-111")
        status, reply = self.call("PATCH", f"/api/pixels/{pixel['id']}", {"name": "New"})
        self.assertEqual((status, reply["pixel"]["name"], reply["pixel"]["syncthing_device_id"]), (200, "New", "AAA-111"))
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
            ({"name": "P", "google_account_id": account, "syncthing_device_id": "bad id!"}, "syncthing_device_id may hold only letters, numbers and dashes"),
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
            alerts.open_alert(opened, self.settings, "k1", "sign_in_needed", email, alerts.sign_in_needed(email), None, 10.0)

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
            alerts.open_alert(opened, self.settings, "k2", "sign_in_needed", "a@example.test", alerts.sign_in_needed("a@example.test"), None, 11.0)
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
                              alerts.sign_in_needed("gone@example.test"), None, 10.0)
            alerts.open_alert(opened, self.settings, browser_jobs.sign_in_key(other["id"]), "sign_in_needed", "stay@example.test",
                              alerts.sign_in_needed("stay@example.test"), None, 10.0)
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


if __name__ == "__main__":
    unittest.main()
