from __future__ import annotations

import dataclasses
import os
import sqlite3
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from darkroom.domain import eligibility, uploads
from darkroom.storage.models import PhoneAsset, User
from tests.eligibility_world import Household


def reasons(rejected: list[eligibility.Rejected]) -> dict[str, str]:
    return {entry.asset_id: entry.reason for entry in rejected}


class EligibleListTests(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.jo = self.home.person("jo")
        self.phone = self.home.device(self.jo)
        self.google = self.home.account(self.jo)

    def test_a_matched_photo_is_listed_with_its_kinds_and_size(self):
        photo = self.home.photo(self.phone, ("photo", "live_video", "adjusted"), through=self.google)
        found = self.home.eligible(self.phone, photo)
        size = sum(item.bytes for item in photo.items.values())
        self.assertTrue(found.cleanup_enabled)
        self.assertEqual(found.eligible, [eligibility.Eligible(photo.id, size, ("photo", "live_video", "adjusted"))])
        self.assertEqual(found.rejected, [])
        self.assertEqual(photo.asset.eligible_at, self.home.now)
        self.assertIsNone(photo.asset.eligible_confirmed_at)

    def test_each_reason_is_fixed_text(self):
        self.assertEqual(len(eligibility.REASONS), 13)
        for code, text in eligibility.REASONS.items():
            self.assertTrue(text.endswith("."), code)
            self.assertNotIn("{", text)

    def test_an_asset_this_iphone_never_reported_is_unknown(self):
        other = self.home.device(self.home.person("sam"))
        photo = self.home.photo(other, through=self.home.account(self.home.person("sam")))
        found = self.home.eligible(self.phone, photo)
        self.assertEqual(reasons(found.rejected), {photo.id: eligibility.UNKNOWN_ASSET})
        self.assertIsNotNone(photo.asset.id)
        self.assertIsNone(photo.asset.eligible_at)

    def test_a_photo_without_an_original_resource_is_never_listed(self):
        photo = self.home.photo(self.phone, ("live_video",), through=self.google)
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.NO_ORIGINAL})

    def test_a_resource_not_uploaded_yet_holds_the_photo_back(self):
        photo = self.home.photo(self.phone, ("photo", "adjusted"), through=self.google)
        photo.rows["adjusted"].uploaded_at = None
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.NOT_UPLOADED})

    def test_a_resource_whose_item_is_another_persons_is_not_on_unraid(self):
        sam = self.home.person("sam")
        photo = self.home.photo(self.phone, through=self.google)
        photo.items["photo"].person_id = sam.id
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.NOT_ON_UNRAID})

    def test_a_resource_whose_item_has_another_hash_or_size_is_not_on_unraid(self):
        for field, value in (("sha1", "0" * 40), ("bytes", 1), ("source", "takeout")):
            with self.subTest(field=field):
                photo = self.home.photo(self.phone, through=self.google)
                setattr(photo.items["photo"], field, value)
                self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.NOT_ON_UNRAID})

    def test_a_linked_item_that_is_gone_is_not_on_unraid(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.home.opened.delete(photo.items["photo"])
        self.home.opened.flush()
        self.home.opened.refresh(photo.rows["photo"])
        self.assertIsNone(photo.rows["photo"].media_item_id)
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.NOT_UPLOADED})

    def test_a_flag_without_a_backing_decision_is_not_enough(self):
        photo = self.home.photo(self.phone)
        photo.items["photo"].confirmed_in_google = True
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.NO_AUTOMATIC_MATCH})

    def test_a_decision_whose_export_belongs_to_another_account_does_not_count(self):
        photo = self.home.photo(self.phone, through=self.google)
        photo.decisions[0].account_id = self.home.account(self.jo).id
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.ACCOUNT_NOT_SERVING})

    def test_a_decision_assigning_the_photo_to_someone_else_does_not_count(self):
        photo = self.home.photo(self.phone, through=self.google)
        photo.decisions[0].person_id = self.home.person("sam").id
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.ACCOUNT_NOT_SERVING})

    def test_a_file_replaced_by_a_link_or_a_folder_is_missing(self):
        photo = self.home.photo(self.phone, through=self.google)
        path = self.home.photos.root / photo.items["photo"].path
        target = path.with_name("elsewhere.bin")
        os.rename(path, target)
        os.symlink(target, path)
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.FILE_MISSING})
        os.unlink(path)
        os.mkdir(path)
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.FILE_MISSING})

    def test_a_stored_path_outside_the_persons_originals_is_missing(self):
        photo = self.home.photo(self.phone, through=self.google)
        for path in ("jo/google-only/x.HEIC", "jo/originals/../originals/" + photo.items["photo"].path.rsplit("/", 1)[1], "../outside.HEIC"):
            with self.subTest(path=path):
                photo.items["photo"].path = path
                self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.FILE_MISSING})

    def test_a_folder_link_in_the_path_is_missing(self):
        photo = self.home.photo(self.phone, through=self.google)
        name = photo.items["photo"].path.rsplit("/", 1)[1]
        os.symlink(self.home.photos.originals("jo"), self.home.photos.originals("jo") / "linked")
        photo.items["photo"].path = f"jo/originals/linked/{name}"
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.FILE_MISSING})

    def test_listing_the_same_asset_twice_is_refused(self):
        photo = self.home.photo(self.phone, through=self.google)
        reported = self.home.reported(photo)
        with self.assertRaises(ValueError):
            eligibility.eligible_assets(self.home.opened, self.home.settings, self.home.photos, self.phone.id, [reported, reported], self.home.now)

    def test_a_kind_listed_twice_is_a_change(self):
        photo = self.home.photo(self.phone, through=self.google)
        reported = self.home.reported(photo)
        doubled = eligibility.Reported(photo.id, reported.resources * 2)
        found = eligibility.eligible_assets(self.home.opened, self.home.settings, self.home.photos, self.phone.id, [doubled], self.home.now)
        self.assertEqual(reasons(found.rejected), {photo.id: eligibility.RESOURCES_CHANGED})

    def test_a_device_that_is_gone_is_refused(self):
        with self.assertRaises(eligibility.NoSuchDevice):
            eligibility.eligible_assets(self.home.opened, self.home.settings, self.home.photos, 999, [], self.home.now)

    def test_without_sign_in_configured_nothing_is_eligible(self):
        photo = self.home.photo(self.phone, through=self.google)
        unconfigured = dataclasses.replace(self.home.settings, oidc_issuer=None)
        found = eligibility.eligible_assets(self.home.opened, unconfigured, self.home.photos, self.phone.id, [self.home.reported(photo)], self.home.now)
        self.assertFalse(found.cleanup_enabled)
        self.assertEqual(reasons(found.rejected), {photo.id: eligibility.ACCESS_LAPSED})

    def test_a_locked_database_is_busy(self):
        locked = OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
        with mock.patch.object(self.home.opened, "execute", side_effect=locked), self.assertRaises(eligibility.Busy):
            self.home.eligible(self.phone)
        other = OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error"))
        with mock.patch.object(self.home.opened, "execute", side_effect=other), self.assertRaises(OperationalError):
            self.home.eligible(self.phone)


class CheckForgetsTests(unittest.TestCase):
    def test_a_new_check_that_changes_the_asset_forgets_its_evaluation(self):
        home = Household(self)
        jo = home.person("jo")
        phone = home.device(jo)
        photo = home.photo(phone, through=home.account(jo))
        home.eligible(phone, photo)
        row = photo.rows["photo"]
        declared = uploads.Asset(photo.id, (uploads.Resource("photo", row.sha1, row.bytes, row.filename), uploads.Resource("adjusted", "a" * 40, 5, "IMG_A.HEIC")))
        uploads.check(home.opened, phone.id, jo.id, [declared], home.now)
        home.opened.refresh(photo.asset)
        self.assertEqual((photo.asset.eligible_at, photo.asset.eligible_confirmed_at), (None, None))

    def test_access_lapse_is_read_from_the_user_record(self):
        home = Household(self)
        jo = home.person("jo")
        phone = home.device(jo)
        photo = home.photo(phone, through=home.account(jo))
        home.opened.get(User, "sub-jo").last_seen = 1.0
        self.assertEqual(reasons(home.eligible(phone, photo).rejected), {photo.id: eligibility.ACCESS_LAPSED})
        self.assertIsNone(home.opened.execute(select(PhoneAsset.eligible_at)).scalar_one())


if __name__ == "__main__":
    unittest.main()
