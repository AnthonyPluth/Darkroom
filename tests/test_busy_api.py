import dataclasses
import unittest

from sqlalchemy import select

from darkroom.server.handler import BUSY_MESSAGE
from darkroom.storage import db
from darkroom.storage.models import GoogleAccount, Person, Pixel
from tests.api_world import ApiCase
from tests.shared import another_writer_holds_the_lock, impatient_engine, start_server

TRY_AGAIN = "Darkroom is busy with another change; try again in a moment"
DELETE_AGAIN = "Darkroom is busy with another change; try deleting the account again in a moment"
PIXEL_BUSY = "Darkroom is busy copying or pruning this Pixel's photos; try again in a moment"


class BusyApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.person_id = self.person()["id"]
        self.account_id = self.account(self.person_id)["id"]
        self.pixel_id = self.pixel(self.account_id)["id"]
        self.base, _ = start_server(self, dataclasses.replace(self.settings, public_url=""), impatient_engine(self, self.engine, 0.1))

    def busy_calls(self):
        return {
            "delete account": ("DELETE", f"/api/accounts/{self.account_id}", None, DELETE_AGAIN),
            "update account": ("PATCH", f"/api/accounts/{self.account_id}", {"email": "other@example.test"}, TRY_AGAIN),
            "update pixel": ("PATCH", f"/api/pixels/{self.pixel_id}", {"name": "Renamed"}, PIXEL_BUSY),
            "delete pixel": ("DELETE", f"/api/pixels/{self.pixel_id}", None, PIXEL_BUSY),
            "create person": ("POST", "/api/people", {"name": "Sam Example"}, BUSY_MESSAGE),
            "create account": ("POST", "/api/accounts", {"email": "sam@example.test", "default_person_id": self.person_id}, BUSY_MESSAGE),
            "create pixel": ("POST", "/api/pixels", {"name": "Pixel B", "google_account_id": self.account_id}, BUSY_MESSAGE),
        }

    def test_a_write_that_cannot_get_the_lock_answers_503_to_try_again_and_changes_nothing(self):
        with another_writer_holds_the_lock(self.engine):
            for name, (method, path, body, says) in self.busy_calls().items():
                with self.subTest(route=name):
                    status, reply = self.call(method, path, body)
                    self.assertEqual((status, reply), (503, {"error": says}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(Person.name)).scalars().all(), ["Jo Example"])
            self.assertEqual(opened.execute(select(GoogleAccount.email)).scalars().all(), ["jo.photos@example.test"])
            self.assertEqual(opened.execute(select(Pixel.name)).scalars().all(), ["Pixel A"])

    def test_reads_do_not_wait_for_another_writer(self):
        with another_writer_holds_the_lock(self.engine):
            for path in ("/api/accounts", "/api/pixels", "/api/people"):
                with self.subTest(path=path):
                    self.assertEqual(self.call("GET", path)[0], 200)


if __name__ == "__main__":
    unittest.main()
