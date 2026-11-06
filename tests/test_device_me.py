import time
import unittest

from darkroom import oidc
from darkroom.domain import devices
from darkroom.storage import db
from darkroom.storage.models import Device, Person, User
from tests.device_world import OTHER_TOKEN, TOKEN, DeviceCase
from tests.privacy import no_leaks


class DeviceMeTests(DeviceCase):
    def test_it_answers_the_person_and_the_device_name_and_nothing_else(self):
        with no_leaks(self, "jo@example.test", TOKEN, replies=self.replies):
            status, reply = self.call("GET", "/api/device/me")
        self.assertEqual((status, reply), (200, {"person": "Jo", "device": "jo iPhone"}))

    def test_it_follows_a_rename_of_the_person_and_of_the_device(self):
        with db.session(self.engine) as opened:
            opened.get(Person, self.person_id).name = "Josephine"
            opened.get(Device, self.device_id).name = "Josephine's iPhone"
        self.assertEqual(self.call("GET", "/api/device/me"), (200, {"person": "Josephine", "device": "Josephine's iPhone"}))

    def test_each_device_is_told_its_own_person(self):
        self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.assertEqual(self.call("GET", "/api/device/me", token=OTHER_TOKEN), (200, {"person": "Sam", "device": "sam iPhone"}))
        self.assertEqual(self.call("GET", "/api/device/me")[1]["person"], "Jo")

    def test_a_missing_unknown_or_malformed_token_is_401_with_no_reason(self):
        generic = {"error": "sign in required"}
        self.assertEqual(self.call("GET", "/api/device/me", token=None), (401, generic))
        self.assertEqual(self.call("GET", "/api/device/me", token="device-token-canary-unknown-000000"), (401, generic))
        self.assertEqual(self.call("GET", "/api/device/me", token=TOKEN[:-1]), (401, generic))
        self.assertEqual(self.call("GET", "/api/device/me", token=None, headers={"Cookie": "darkroom_session=" + TOKEN}), (401, generic))

    def test_a_revoked_device_is_401_with_its_reason(self):
        with db.session(self.engine) as opened:
            devices.revoke(opened, self.device_id, 5.0)
        self.assertEqual(self.call("GET", "/api/device/me"), (401, {"error": "sign in required", "reason": "device_revoked"}))

    def test_a_lapsed_sign_in_is_401_and_the_same_token_works_again(self):
        with db.session(self.engine) as opened:
            opened.query(User).update({"last_seen": time.time() - (oidc.NO_RECHECK_DAYS + 1) * 86_400})
        self.assertEqual(self.call("GET", "/api/device/me"), (401, {"error": "sign in required", "reason": "sign_in_lapsed"}))
        with db.session(self.engine) as opened:
            opened.query(User).update({"last_seen": time.time()})
        self.assertEqual(self.call("GET", "/api/device/me")[0], 200)

    def test_access_removed_from_the_account_is_401_sign_in_refused(self):
        with db.session(self.engine) as opened:
            opened.query(User).update({"groups": '["guests"]'})
        self.assertEqual(self.call("GET", "/api/device/me"), (401, {"error": "sign in required", "reason": "sign_in_refused"}))

    def test_a_deleted_person_is_401_with_no_reason(self):
        with db.session(self.engine) as opened:
            opened.query(Person).delete()
        self.assertEqual(self.call("GET", "/api/device/me"), (401, {"error": "sign in required"}))

    def test_nothing_but_get_is_routed_and_a_body_changes_nothing(self):
        self.assertEqual(self.call("POST", "/api/device/me", {"person": "Eve"})[0], 404)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Person, self.person_id).name, "Jo")
        self.assertEqual(self.call("GET", "/api/device/me")[1]["person"], "Jo")


if __name__ == "__main__":
    unittest.main()
