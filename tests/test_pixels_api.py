import unittest

from sqlalchemy import select

from darkroom.storage import db
from darkroom.storage.models import Export, Pixel, PixelPerson
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


if __name__ == "__main__":
    unittest.main()
