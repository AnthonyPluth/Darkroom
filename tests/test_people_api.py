import unittest

from sqlalchemy import select

from darkroom.storage import db, secretbox
from darkroom.storage.models import Device, Event, ImmichAsset, MediaItem, Person
from tests.api_world import SECRET_KEY, ApiCase
from tests.privacy import no_leaks

IMMICH_KEY = "immich-api-key-canary-0123456789abcdef"


class PeopleApiTests(ApiCase):
    def test_a_new_person_gets_a_slug_and_its_folders(self):
        person = self.person("Jo Example")
        self.assertEqual((person["slug"], person["name"], person["has_immich_key"]), ("jo-example", "Jo Example", False))
        for folder in ("originals", "google-only"):
            self.assertTrue((self.photos / "jo-example" / folder).is_dir())
        self.assertTrue((self.photos / "_inbox" / "jo-example" / "manual").is_dir())
        status, listed = self.call("GET", "/api/people")
        self.assertEqual((status, [p["id"] for p in listed["people"]]), (200, [person["id"]]))

    def test_a_chosen_slug_is_used_and_must_be_a_folder_name(self):
        self.assertEqual(self.person("Sam", slug="sam")["slug"], "sam")
        for slug in ("Sam", "../x", "-a", "a b"):
            with self.subTest(slug=slug):
                status, reply = self.call("POST", "/api/people", {"name": "Other", "slug": slug})
                self.assertEqual(status, 400)
                self.assertIn("slug", reply["error"])
        status, reply = self.call("POST", "/api/people", {"name": "!!!"})
        self.assertEqual(status, 400)

    def test_a_taken_slug_is_a_conflict_and_creates_nothing(self):
        self.person("Sam", slug="sam")
        status, reply = self.call("POST", "/api/people", {"name": "Other Sam", "slug": "sam"})
        self.assertEqual(status, 409)
        self.assertIn("sam", reply["error"])
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(Person)).scalars().all()), 1)

    def test_validation_errors_are_400s_with_a_message(self):
        cases = (({}, "name is required"), ({"name": "  "}, "name is required"), ({"name": 5}, "name must be text"), ({"name": "x" * 101}, "name is too long"),
                 ({"name": "Jo", "email": "nope"}, "email must be an email address"), ({"name": "Jo", "immich_user_id": 4}, "immich_user_id must be text"))
        for body, message in cases:
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/people", body), (400, {"error": message}))
        self.assertEqual(self.call("POST", "/api/people", raw=b"not json"), (400, {"error": "the request is not valid JSON"}))
        self.assertEqual(self.call("POST", "/api/people", raw=b"[]"), (400, {"error": "the request must be a JSON object"}))

    def test_a_new_immich_user_restarts_the_reconcile_window_and_drops_unfinished_rows_only(self):
        person = self.person("Jo", immich_user_id="u-1")
        with db.session(self.engine) as opened:
            opened.get(Person, person["id"]).immich_reconciled_until = 99.0
            for name, status in (("a", "stored"), ("b", "waiting"), ("c", "failed"), ("d", "ignored")):
                opened.add(ImmichAsset(person_id=person["id"], asset_id=name, kind="photo", status=status, sha1="ab", seen_at=1.0))
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"name": "Jo", "immich_user_id": "u-1"})[0], 200)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Person, person["id"]).immich_reconciled_until, 99.0)
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"immich_user_id": "u-2"})[0], 200)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Person, person["id"]).immich_reconciled_until)
            self.assertEqual([row.asset_id for row in opened.execute(select(ImmichAsset)).scalars()], ["a"])

    def test_update_changes_only_what_is_sent_and_null_clears(self):
        person = self.person("Jo", email="jo@example.test", immich_user_id="u-1", immich_library_id="l-1")
        status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"name": "Joanna", "immich_user_id": None})
        self.assertEqual(status, 200)
        self.assertEqual((reply["person"]["name"], reply["person"]["immich_user_id"], reply["person"]["email"], reply["person"]["immich_library_id"]),
                         ("Joanna", None, "jo@example.test", "l-1"))
        self.assertEqual(reply["person"]["slug"], "jo")
        status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"email": None})
        self.assertEqual(reply["person"]["email"], None)
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {})[0], 200)

    def test_update_validation_and_404s(self):
        person = self.person("Jo")
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"name": ""}), (400, {"error": "name is required"}))
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"email": "bad"})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/people/999", {"name": "X"}), (404, {"error": "no such person"}))
        self.assertEqual(self.call("PATCH", "/api/people/abc", {"name": "X"}), (404, {"error": "no such person"}))
        self.assertEqual(self.call("DELETE", "/api/people/999"), (404, {"error": "no such person"}))
        self.assertEqual(self.call("DELETE", "/api/people/abc"), (404, {"error": "no such person"}))
        self.assertEqual(self.call("GET", "/api/people/999/devices"), (404, {"error": "no such person"}))
        self.assertEqual(self.call("GET", "/api/people/x/devices"), (404, {"error": "no such person"}))

    def test_the_immich_key_is_sealed_and_never_returned_logged_or_stored_plain(self):
        with no_leaks(self, IMMICH_KEY, database=self.engine, replies=self.replies):
            person = self.person("Jo", immich_api_key=IMMICH_KEY)
            self.assertTrue(person["has_immich_key"])
            self.assertNotIn("immich_api_key", person)
            reply = self.call("PATCH", f"/api/people/{person['id']}", {"name": "Joanna"})[1]
            self.assertTrue(reply["person"]["has_immich_key"])
            listed = self.call("GET", "/api/people")[1]
            self.assertTrue(listed["people"][0]["has_immich_key"])
            with db.session(self.engine) as opened:
                sealed = opened.get(Person, person["id"]).immich_api_key_sealed
                self.assertTrue(secretbox.is_sealed(sealed))
                self.assertEqual(secretbox.unseal(sealed, SECRET_KEY), IMMICH_KEY)
                events = [(e.type, e.subject, e.detail) for e in opened.execute(select(Event)).scalars()]
            self.assertIn(("immich_key_set", "jo", None), events)
            self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": IMMICH_KEY + "-2"})
            self.assertEqual(self.call("GET", "/api/events")[0], 200)

    def test_a_rejected_key_is_not_echoed(self):
        with no_leaks(self, IMMICH_KEY * 20, database=self.engine, replies=self.replies):
            status, reply = self.call("POST", "/api/people", {"name": "Jo", "immich_api_key": IMMICH_KEY * 20})
            self.assertEqual(status, 400)
            self.assertEqual(reply, {"error": "immich_api_key is too long"})
            self.assertEqual(self.call("POST", "/api/people", {"name": "Jo", "immich_api_key": 12345678})[0], 400)

    def test_null_removes_the_key_and_the_event_says_so(self):
        person = self.person("Jo", immich_api_key=IMMICH_KEY)
        status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": None})
        self.assertEqual((status, reply["person"]["has_immich_key"]), (200, False))
        self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": None})
        with db.session(self.engine) as opened:
            types = [e.type for e in opened.execute(select(Event).order_by(Event.id)).scalars()]
        self.assertEqual(types.count("immich_key_removed"), 1)

    def test_deleting_is_refused_while_the_person_has_media_devices_accounts_or_pixels_and_files_stay(self):
        person = self.person("Jo")
        pid = person["id"]
        with db.session(self.engine) as opened:
            opened.add(MediaItem(person_id=pid, source="phone", path="jo/originals/a.jpg", sha1="a" * 40, bytes=1, indexed_at=1.0))
        status, reply = self.call("DELETE", f"/api/people/{pid}")
        self.assertEqual(status, 409)
        self.assertIn("media items", reply["error"])
        with db.session(self.engine) as opened:
            opened.query(MediaItem).delete()
        self.add_device(pid)
        self.assertIn("devices", self.call("DELETE", f"/api/people/{pid}")[1]["error"])
        self.account(pid)
        self.assertIn("Google accounts", self.call("DELETE", f"/api/people/{pid}")[1]["error"])
        self.assertEqual(self.call("GET", "/api/people")[1]["people"][0]["id"], pid)

    def test_deleting_an_unused_person_removes_the_record_but_never_the_folders(self):
        person = self.person("Jo")
        original = self.photos / "jo" / "originals" / "keep.jpg"
        original.write_bytes(b"photo bytes")
        other = self.person("Sam")
        account = self.account(other["id"])
        pixel = self.pixel(account["id"], people_ids=[other["id"], person["id"]])
        self.assertIn("Pixels", self.call("DELETE", f"/api/people/{person['id']}")[1]["error"])
        self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [other["id"]]})
        self.assertEqual(self.call("DELETE", f"/api/people/{person['id']}"), (200, {"ok": True}))
        self.assertEqual([p["slug"] for p in self.call("GET", "/api/people")[1]["people"]], ["sam"])
        self.assertEqual(original.read_bytes(), b"photo bytes")

    def test_a_deleted_persons_folder_name_is_never_reused_and_its_files_are_not_indexed(self):
        old = self.person("Jo")
        original = self.photos / "jo" / "originals" / "keep.jpg"
        original.write_bytes(b"photo bytes")
        self.assertEqual(self.call("DELETE", f"/api/people/{old['id']}"), (200, {"ok": True}))
        status, reply = self.call("POST", "/api/people", {"name": "Someone Else", "slug": "jo"})
        self.assertEqual(status, 409)
        self.assertIn("already in use on disk", reply["error"])
        self.assertIn("never reused or deleted automatically", reply["error"])
        self.assertEqual(self.call("POST", "/api/people", {"name": "Jo"})[0], 409)
        self.assertEqual(original.read_bytes(), b"photo bytes")
        self.assertEqual(self.call("GET", "/api/people"), (200, {"people": []}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(MediaItem)).scalars().all(), [])
        self.assertEqual(self.person("Kai")["slug"], "kai")

    def test_a_leftover_inbox_folder_also_blocks_the_name(self):
        (self.photos / "_inbox" / "kai").mkdir(parents=True)
        self.assertEqual(self.call("POST", "/api/people", {"name": "Kai"})[0], 409)
        self.assertFalse((self.photos / "kai").exists())

    def test_a_folder_that_cannot_be_made_is_a_clear_500_and_no_person_and_a_retry_works(self):
        (self.photos / "_inbox").write_bytes(b"in the way")
        before = sorted(str(p.relative_to(self.photos)) for p in self.photos.rglob("*"))
        status, reply = self.call("POST", "/api/people", {"name": "Jo"})
        self.assertEqual((status, reply), (500, {"error": "Darkroom could not create the person's folders"}))
        self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})
        self.assertEqual(sorted(str(p.relative_to(self.photos)) for p in self.photos.rglob("*")), before)
        self.assertFalse((self.photos / "jo").exists())
        (self.photos / "_inbox").unlink()
        self.assertEqual(self.call("POST", "/api/people", {"name": "Jo"})[0], 201)
        self.assertTrue((self.photos / "jo" / "originals").is_dir())

    def test_devices_of_a_person_are_listed_without_their_token(self):
        person = self.person("Jo")
        device_id = self.add_device(person["id"])
        status, reply = self.call("GET", f"/api/people/{person['id']}/devices")
        self.assertEqual(status, 200)
        (device,) = reply["devices"]
        self.assertEqual((device["id"], device["name"], device["pending_uploads"], device["revoked_at"]), (device_id, "Jo's iPhone", 3, None))
        self.assertFalse({"token_hash", "token"} & set(device))

    def test_revoking_a_device_is_idempotent_turns_cleanup_off_and_records_an_event(self):
        person = self.person("Jo")
        device_id = self.add_device(person["id"])
        status, reply = self.call("DELETE", f"/api/devices/{device_id}")
        self.assertEqual(status, 200)
        revoked = reply["device"]["revoked_at"]
        self.assertIsInstance(revoked, float)
        self.assertFalse(reply["device"]["cleanup_enabled"])
        self.assertEqual(self.call("DELETE", f"/api/devices/{device_id}")[1]["device"]["revoked_at"], revoked)
        self.assertEqual(self.call("DELETE", "/api/devices/999"), (404, {"error": "no such device"}))
        self.assertEqual(self.call("DELETE", "/api/devices/x"), (404, {"error": "no such device"}))
        status, events = self.call("GET", "/api/events")
        self.assertEqual([e["type"] for e in events["events"] if e["type"] == "device_revoked"], ["device_revoked"])
        self.assertEqual(self.call("DELETE", f"/api/people/{person['id']}")[0], 409)


    def test_revoking_a_device_forgets_its_token_and_it_cannot_register_again(self):
        person = self.person("Jo")
        device_id = self.add_device(person["id"])
        with db.session(self.engine) as opened:
            device = opened.get(Device, device_id)
            device.push_token_sealed, device.push_token_digest, device.push_token_at = "enc:v1:stored-before-the-removal", "d" * 64, 1.0
        self.assertEqual(self.call("DELETE", f"/api/devices/{device_id}")[0], 200)
        with db.session(self.engine) as opened:
            device = opened.get(Device, device_id)
            self.assertEqual((device.push_token_sealed, device.push_token_digest), (None, None))
            self.assertIsNotNone(device.push_token_at)
        self.assertEqual(self.call("PUT", "/api/device/push-token", {"token": "a1" * 32})[0], 404)

class NoSecretKeyTests(ApiCase):
    extra_env = {"DARKROOM_SECRET_KEY": ""}

    def test_a_key_is_refused_and_nothing_is_kept(self):
        person = self.person("Jo")
        with no_leaks(self, IMMICH_KEY, database=self.engine, replies=self.replies):
            status, reply = self.call("POST", "/api/people", {"name": "Sam", "immich_api_key": IMMICH_KEY})
            self.assertEqual(status, 503)
            self.assertIn("DARKROOM_SECRET_KEY", reply["error"])
            status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": IMMICH_KEY})
            self.assertEqual(status, 503)
        with db.session(self.engine) as opened:
            self.assertEqual([p.slug for p in opened.execute(select(Person)).scalars()], ["jo"])


if __name__ == "__main__":
    unittest.main()
