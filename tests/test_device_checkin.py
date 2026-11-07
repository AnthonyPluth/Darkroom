import json
import time
import unittest

from darkroom import oidc
from darkroom.domain import devices
from darkroom.server import auth
from darkroom.storage import db
from darkroom.storage.models import Device
from tests.device_world import TOKEN, DeviceCase
from tests.shared import fetch


class CheckinTests(DeviceCase):
    def owner_cookie(self):
        with db.session(self.engine) as opened:
            oidc.remember_user(opened, oidc.Identity("owner", "owner@example.test", "Owner", ("parents",), True), time.time())
            token = oidc.new_session(opened, self.settings, "owner", time.time())
        return {"Cookie": f"{auth.SESSION_COOKIE}={token}"}

    def test_a_check_in_is_shown_per_device_on_the_people_page(self):
        status, reply = self.call("POST", "/api/device/checkin", {"pending_uploads": 7, "app_version": "1.2.3"})
        self.assertEqual((status, reply), (200, {"ok": True}))
        status, _, body = fetch(self.base, "GET", f"/api/people/{self.person_id}/devices", self.owner_cookie())
        self.assertEqual(status, 200)
        listed = json.loads(body)["devices"]
        self.assertEqual([(entry["pending_uploads"], entry["app_version"]) for entry in listed], [(7, "1.2.3")])
        self.assertAlmostEqual(listed[0]["last_checkin"], time.time(), delta=30)
        self.assertNotIn(TOKEN.encode(), body)
        status, _, body = fetch(self.base, "GET", "/api/status", self.owner_cookie())
        entry = json.loads(body)["people"][0]["devices"][0]
        self.assertEqual(entry["pending_uploads"], 7)

    def test_the_app_version_may_be_left_out_or_cleared(self):
        self.assertEqual(self.call("POST", "/api/device/checkin", {"pending_uploads": 0, "app_version": "1.0"})[0], 200)
        self.assertEqual(self.call("POST", "/api/device/checkin", {"pending_uploads": 0})[0], 200)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Device, self.device_id).app_version)

    def test_any_device_request_counts_as_a_check_in(self):
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Device, self.device_id).last_checkin)
        self.call("GET", "/api/device/me")
        with db.session(self.engine) as opened:
            self.assertIsNotNone(opened.get(Device, self.device_id).last_checkin)

    def test_bad_check_ins_are_400_and_change_nothing(self):
        for body in ({}, {"pending_uploads": -1}, {"pending_uploads": "3"}, {"pending_uploads": True}, {"pending_uploads": 1, "app_version": "x" * 51},
                     {"pending_uploads": 1, "app_version": 5}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/device/checkin", body)[0], 400)
        with db.session(self.engine) as opened:
            device = opened.get(Device, self.device_id)
            self.assertEqual((device.pending_uploads, device.app_version), (0, None))

    def test_a_revoked_device_cannot_check_in(self):
        with db.session(self.engine) as opened:
            devices.revoke(opened, self.device_id, 5.0)
        self.assertEqual(self.call("POST", "/api/device/checkin", {"pending_uploads": 9})[0], 401)
        self.assertEqual(self.call("POST", "/api/device/checkin", {"pending_uploads": 9}, token=None)[0], 401)
        with db.session(self.engine) as opened:
            device = opened.get(Device, self.device_id)
            self.assertEqual((device.pending_uploads, device.last_checkin), (0, None))


if __name__ == "__main__":
    unittest.main()
