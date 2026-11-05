from __future__ import annotations

import os
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, eligibility
from darkroom.storage import db
from darkroom.storage.models import Device, Event, PhoneAsset
from tests.api_world import ApiCase
from tests.device_world import TOKEN, DeviceCase
from tests.eligibility_world import Household, Photo
from tests.privacy import no_leaks


def body_of(home: Household, *photos: Photo) -> dict[str, object]:
    return {"assets": [{"id": entry.photokit_id, "resources": [{"kind": r.kind, "sha1": r.sha1, "bytes": r.bytes, "filename": r.filename}
                                                                for r in entry.resources]} for entry in (home.reported(photo) for photo in photos)]}


class CleanupApiTests(DeviceCase):
    def setUp(self):
        super().setUp()
        self.opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.home = Household(self, self.opened, self.root)
        self.jo = self.home.person("jo")
        self.phone = self.opened.get(Device, self.device_id)
        self.phone.cleanup_enabled = True
        self.google = self.home.account(self.jo)

    def commit(self):
        self.opened.commit()

    def canaries(self, *photos: Photo) -> list[str]:
        out = [TOKEN]
        for photo in photos:
            for item in photo.items.values():
                out += [item.path, item.sha1, item.path.rsplit("/", 1)[1]]
        return out

    def test_the_whole_round_lists_confirms_and_records_without_leaking(self):
        good = self.home.photo(self.phone, ("photo", "live_video"), through=self.google)
        unmatched = self.home.photo(self.phone)
        self.commit()
        with no_leaks(self, *self.canaries(good, unmatched), replies=self.replies):
            status, listed = self.call("POST", "/api/device/eligible", body_of(self.home, good, unmatched))
            self.assertEqual(status, 200, listed)
            self.assertTrue(listed["cleanup_enabled"])
            size = sum(item.bytes for item in good.items.values())
            self.assertEqual(listed["eligible"], [{"asset_id": good.id, "bytes": size, "resources": ["photo", "live_video"]}])
            self.assertEqual(listed["rejected"], [{"asset_id": unmatched.id, "reason": "not_confirmed", "message": eligibility.REASONS["not_confirmed"]}])
            status, confirmed = self.call("POST", "/api/device/eligible/confirm", body_of(self.home, good, unmatched))
            self.assertEqual(status, 200, confirmed)
            self.assertEqual(confirmed["confirmed"], [good.id])
            self.assertEqual([entry["reason"] for entry in confirmed["rejected"]], ["not_confirmed"])
            status, deleted = self.call("POST", "/api/device/deleted", {"asset_ids": [good.id, unmatched.id]})
            self.assertEqual(status, 200, deleted)
            self.assertEqual(deleted["recorded"], [good.id])
            self.assertEqual([entry["reason"] for entry in deleted["rejected"]], ["not_confirmed_for_deletion"])
        with db.session(self.engine) as opened:
            stored = {asset.photokit_id: asset.deleted_at for asset in opened.execute(select(PhoneAsset)).scalars()}
            self.assertIsNotNone(stored[good.id])
            self.assertIsNone(stored[unmatched.id])
            types = [event.type for event in opened.execute(select(Event).order_by(Event.id)).scalars()]
        self.assertEqual(types, ["cleanup_confirmed", "cleanup_deleted"])

    def test_sending_the_deleted_report_again_records_the_same_ids_and_changes_nothing(self):
        good = self.home.photo(self.phone, through=self.google)
        self.commit()
        self.call("POST", "/api/device/eligible/confirm", body_of(self.home, good))
        first = self.call("POST", "/api/device/deleted", {"asset_ids": [good.id]})
        with db.session(self.engine) as opened:
            deleted_at = opened.execute(select(PhoneAsset.deleted_at)).scalar_one()
        again = self.call("POST", "/api/device/deleted", {"asset_ids": [good.id]})
        self.assertEqual((first, again), ((200, {"recorded": [good.id], "rejected": []}), (200, {"recorded": [good.id], "rejected": []})))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(PhoneAsset.deleted_at)).scalar_one(), deleted_at)

    def test_a_reversal_or_a_removed_file_after_the_list_is_refused_at_confirm(self):
        reversed_one = self.home.photo(self.phone, through=self.google)
        removed_one = self.home.photo(self.phone, through=self.google)
        self.commit()
        status, listed = self.call("POST", "/api/device/eligible", body_of(self.home, reversed_one, removed_one))
        self.assertEqual((status, len(listed["eligible"])), (200, 2))
        with db.session(self.engine) as opened:
            dedupe.reverse(opened, reversed_one.decisions[0].id, self.home.now, "owner")
        os.remove(self.root / removed_one.items["photo"].path)
        status, confirmed = self.call("POST", "/api/device/eligible/confirm", body_of(self.home, reversed_one, removed_one))
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["confirmed"], [])
        self.assertEqual({entry["asset_id"]: entry["reason"] for entry in confirmed["rejected"]}, {reversed_one.id: "not_confirmed", removed_one.id: "file_missing"})

    def test_cleanup_off_lists_nothing_and_confirms_nothing(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.phone.cleanup_enabled = False
        self.commit()
        status, listed = self.call("POST", "/api/device/eligible", body_of(self.home, photo))
        self.assertEqual((status, listed["cleanup_enabled"], listed["eligible"]), (200, False, []))
        status, confirmed = self.call("POST", "/api/device/eligible/confirm", body_of(self.home, photo))
        self.assertEqual((status, confirmed["confirmed"], [entry["reason"] for entry in confirmed["rejected"]]), (200, [], ["cleanup_disabled"]))

    def test_a_revoked_iphone_is_not_let_in(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.phone.revoked_at = 5.0
        self.commit()
        for path in ("/api/device/eligible", "/api/device/eligible/confirm"):
            self.assertEqual(self.call("POST", path, body_of(self.home, photo)), (401, {"error": "sign in required"}))
        self.assertEqual(self.call("POST", "/api/device/deleted", {"asset_ids": [photo.id]})[0], 401)

    def test_bad_bodies_are_refused_and_change_nothing(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.commit()
        listed = body_of(self.home, photo)
        bad = [{}, {"assets": []}, {"assets": listed["assets"] * 2}, {"assets": [{"id": photo.id, "resources": [{"kind": "raw"}]}]}]
        for path in ("/api/device/eligible", "/api/device/eligible/confirm"):
            for body in bad:
                with self.subTest(path=path, body=body):
                    self.assertEqual(self.call("POST", path, body)[0], 400)
        for body in ({}, {"ids": [photo.id]}, {"asset_ids": []}, {"asset_ids": [1]}, {"asset_ids": [photo.id, photo.id]}, {"asset_ids": [""]},
                     {"asset_ids": ["x"] * 501}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/device/deleted", body)[0], 400)
        with db.session(self.engine) as opened:
            asset = opened.execute(select(PhoneAsset)).scalar_one()
            self.assertEqual((asset.eligible_at, asset.eligible_confirmed_at, asset.deleted_at), (None, None, None))

    def test_a_busy_database_answers_503_and_confirms_nothing(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.commit()
        for name, path, body in (("eligible_assets", "/api/device/eligible", body_of(self.home, photo)),
                                 ("confirm", "/api/device/eligible/confirm", body_of(self.home, photo)),
                                 ("record_deleted", "/api/device/deleted", {"asset_ids": [photo.id]})):
            for error in (eligibility.Busy("busy"), eligibility.NoSuchDevice("gone")):
                with self.subTest(name=name, error=type(error).__name__), mock.patch.object(eligibility, name, side_effect=error):
                    status, reply = self.call("POST", path, body)
                    self.assertEqual(status, 503 if isinstance(error, eligibility.Busy) else 401)
                    self.assertNotIn("confirmed", reply)


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

    def test_reversing_the_match_on_the_review_page_between_the_list_and_confirm_is_refused(self):
        home, device = self.household()
        photo = home.photo(device, through=home.account(home.person(self.jo["slug"])))
        self.assertEqual(len(home.eligible(device, photo).eligible), 1)
        home.opened.commit()
        status, _ = self.call("POST", f"/api/decisions/{photo.decisions[0].id}/reverse")
        self.assertEqual(status, 200)
        home.opened.expire_all()
        confirmed, rejected = home.confirm(device, photo)
        self.assertEqual((confirmed, rejected), ([], [eligibility.Rejected(photo.id, eligibility.NOT_CONFIRMED)]))

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
