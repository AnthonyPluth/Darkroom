from __future__ import annotations

from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import eligibility
from darkroom.storage import db
from darkroom.storage.models import Device, Event
from tests.api_world import ApiCase
from tests.eligibility_world import Household


class DeviceSwitchTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.jo = self.person("Jo", email="jo@example.test")
        self.device_id = self.add_device(self.jo["id"])

    def events(self) -> list[str]:
        with db.session(self.engine) as opened:
            return [event.type for event in opened.execute(select(Event).where(Event.type.like("cleanup_%")).order_by(Event.id)).scalars()]

    def enabled(self) -> bool:
        with db.session(self.engine) as opened:
            return opened.get(Device, self.device_id).cleanup_enabled

    def test_turning_cleanup_on_needs_the_icloud_confirmation(self):
        status, reply = self.call("PATCH", f"/api/devices/{self.device_id}", {"cleanup_enabled": True})
        self.assertEqual(status, 409)
        self.assertIn("iCloud Photos is off", reply["error"])
        self.assertEqual((self.enabled(), self.events()), (False, []))
        status, reply = self.call("PATCH", f"/api/devices/{self.device_id}", {"cleanup_enabled": True, "icloud_photos_off": True})
        self.assertEqual((status, reply["device"]["cleanup_enabled"]), (200, True))
        status, reply = self.call("PATCH", f"/api/devices/{self.device_id}", {"cleanup_enabled": False})
        self.assertEqual((status, reply["device"]["cleanup_enabled"]), (200, False))
        self.assertEqual(self.events(), ["cleanup_enabled", "cleanup_disabled"])

    def test_a_revoked_iphone_cannot_be_turned_on(self):
        self.call("DELETE", f"/api/devices/{self.device_id}")
        status, _ = self.call("PATCH", f"/api/devices/{self.device_id}", {"cleanup_enabled": True, "icloud_photos_off": True})
        self.assertEqual((status, self.enabled()), (409, False))

    def test_bad_requests_change_nothing(self):
        for path, body, expected in ((f"/api/devices/{self.device_id}", {}, 400), (f"/api/devices/{self.device_id}", {"cleanup_enabled": "yes"}, 400),
                                     (f"/api/devices/{self.device_id}", {"cleanup_enabled": True, "icloud_photos_off": 1}, 400),
                                     ("/api/devices/999", {"cleanup_enabled": False}, 404), ("/api/devices/x", {"cleanup_enabled": False}, 404)):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.call("PATCH", path, body)[0], expected)
        self.assertEqual((self.enabled(), self.events()), (False, []))

    def test_a_busy_database_answers_503(self):
        with mock.patch.object(eligibility, "set_cleanup", side_effect=eligibility.Busy("busy")):
            status, _ = self.call("PATCH", f"/api/devices/{self.device_id}", {"cleanup_enabled": False})
        self.assertEqual(status, 503)

    def household(self) -> tuple[Household, Device]:
        opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(opened.close)
        home = Household(self, opened, self.photos)
        person = home.person(self.jo["slug"])
        device = opened.get(Device, self.device_id)
        device.cleanup_enabled, device.sub = True, f"sub-{person.slug}"
        return home, device

    def test_status_shows_the_count_and_space_after_the_iphone_has_asked(self):
        home, device = self.household()
        opened = home.opened
        photo = home.photo(device, ("photo", "adjusted"), through=home.account(home.person(self.jo["slug"])))
        opened.commit()
        shown = self.call("GET", "/api/status")[1]["people"][0]["devices"][0]
        self.assertEqual((shown["eligible"], shown["eligible_bytes"]), (None, None))
        home.eligible(device, photo)
        opened.commit()
        shown = self.call("GET", "/api/status")[1]["people"][0]["devices"][0]
        self.assertEqual((shown["eligible"], shown["eligible_bytes"]), (0, 0), "without a sign-in provider nothing can be deleted")
        with mock.patch.object(eligibility.oidc, "device_access_lapsed", return_value=None):
            shown = self.call("GET", "/api/status")[1]["people"][0]["devices"][0]
        self.assertEqual((shown["cleanup_enabled"], shown["eligible"], shown["eligible_bytes"]), (True, 1, sum(item.bytes for item in photo.items.values())))
