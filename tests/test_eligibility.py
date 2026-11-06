from __future__ import annotations

import dataclasses
import os
import sqlite3
import unittest
from unittest import mock

from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from darkroom.domain import dedupe, eligibility, status, uploads
from darkroom.storage.models import Event, PhoneAsset, User
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


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.jo = self.home.person("jo")
        self.phone = self.home.device(self.jo, cleanup=False)
        self.google = self.home.account(self.jo)

    def test_turning_cleanup_on_needs_the_icloud_confirmation(self):
        with self.assertRaises(eligibility.NotAllowed):
            eligibility.set_cleanup(self.home.opened, self.phone.id, True, False, self.home.now)
        self.assertFalse(self.phone.cleanup_enabled)
        eligibility.set_cleanup(self.home.opened, self.phone.id, True, True, self.home.now)
        self.assertTrue(self.phone.cleanup_enabled)
        types = [event.type for event in self.home.opened.execute(select(Event).order_by(Event.id)).scalars()]
        self.assertEqual(types, ["cleanup_enabled"])

    def test_a_signed_out_iphone_cannot_be_turned_on(self):
        self.phone.revoked_at = 5.0
        with self.assertRaises(eligibility.NotAllowed):
            eligibility.set_cleanup(self.home.opened, self.phone.id, True, True, self.home.now)
        self.assertFalse(self.phone.cleanup_enabled)

    def test_turning_cleanup_off_forgets_every_earlier_evaluation(self):
        eligibility.set_cleanup(self.home.opened, self.phone.id, True, True, self.home.now)
        photo = self.home.photo(self.phone, through=self.google)
        self.home.eligible(self.phone, photo)
        self.assertIsNotNone(self.phone.eligibility_checked_at)
        eligibility.set_cleanup(self.home.opened, self.phone.id, False, False, self.home.now)
        self.home.opened.refresh(photo.asset)
        self.assertEqual((photo.asset.eligible_at, photo.asset.eligible_confirmed_at), (None, None))
        self.assertIsNone(self.phone.eligibility_checked_at)

    def test_setting_the_same_value_changes_nothing(self):
        eligibility.set_cleanup(self.home.opened, self.phone.id, False, False, self.home.now)
        self.assertEqual(self.home.opened.execute(select(Event)).scalars().all(), [])

    def test_an_unknown_device_is_refused(self):
        with self.assertRaises(eligibility.NoSuchDevice):
            eligibility.set_cleanup(self.home.opened, 999, False, False, self.home.now)


class CountTests(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.jo = self.home.person("jo")
        self.phone = self.home.device(self.jo)
        self.google = self.home.account(self.jo)

    def device_on_status(self):
        people = status.overview(self.home.opened, self.home.settings, self.home.photos, self.home.now)["people"]
        return people[0]["devices"][0]

    def test_a_reversal_followed_by_an_evaluation_of_a_different_asset_is_not_counted(self):
        first = self.home.photo(self.phone, through=self.google)
        second = self.home.photo(self.phone, through=self.google)
        self.home.eligible(self.phone, first, second)
        self.assertEqual(self.device_on_status()["eligible"], 2)
        dedupe.reverse(self.home.opened, first.decisions[0].id, self.home.now, "owner")
        self.home.eligible(self.phone, second)
        shown = self.device_on_status()
        self.assertEqual((shown["eligible"], shown["eligible_bytes"]), (1, second.items["photo"].bytes))

    def test_a_file_removed_after_the_evaluation_is_not_counted(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.home.eligible(self.phone, photo)
        os.remove(self.home.photos.root / photo.items["photo"].path)
        self.assertEqual(self.device_on_status()["eligible"], 0)

    def test_status_counting_writes_nothing(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.home.eligible(self.phone, photo)
        dedupe.reverse(self.home.opened, photo.decisions[0].id, self.home.now, "owner")
        before = self.home.state()
        self.device_on_status()
        self.assertEqual(self.home.state(), before)

    def test_a_lapsed_sign_in_counts_zero(self):
        self.home.eligible(self.phone, self.home.photo(self.phone, through=self.google))
        self.home.opened.get(User, "sub-jo").last_seen = 1.0
        self.assertEqual(self.device_on_status()["eligible"], 0)

    def test_status_shows_no_count_until_the_iphone_has_asked(self):
        self.home.photo(self.phone, through=self.google)
        shown = self.device_on_status()
        self.assertEqual((shown["eligible"], shown["eligible_bytes"], shown["eligible_checked"]), (None, None, None))

    def test_status_counts_what_the_last_evaluation_found(self):
        first = self.home.photo(self.phone, ("photo", "live_video"), through=self.google)
        second = self.home.photo(self.phone, through=self.google)
        third = self.home.photo(self.phone)
        self.home.eligible(self.phone, first, second, third)
        size = sum(item.bytes for photo in (first, second) for item in photo.items.values())
        shown = self.device_on_status()
        self.assertEqual((shown["eligible"], shown["eligible_bytes"], shown["eligible_checked"]), (2, size, self.home.now))

    def test_a_disabled_iphone_counts_zero(self):
        self.home.photo(self.phone, through=self.google)
        self.home.eligible(self.phone, self.home.photo(self.phone, through=self.google))
        self.phone.cleanup_enabled = False
        self.assertEqual((self.device_on_status()["eligible"], self.device_on_status()["eligible_bytes"]), (0, 0))


class StatusAtScaleTests(unittest.TestCase):
    MARKED = 2000

    def test_status_checks_thousands_of_marked_photos_in_a_fixed_number_of_queries(self):
        home = Household(self)
        jo = home.person("jo")
        phone = home.device(jo)
        google = home.account(jo)
        photos = [home.photo(phone, ("photo", "live_video") if n % 4 == 0 else ("photo",), through=google) for n in range(self.MARKED)]
        for start in range(0, self.MARKED, 500):
            self.assertEqual(len(home.eligible(phone, *photos[start:start + 500]).eligible), len(photos[start:start + 500]))
        home.opened.commit()
        statements: list[str] = []

        def counted(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(home.engine, "before_cursor_execute", counted)
        self.addCleanup(event.remove, home.engine, "before_cursor_execute", counted)

        def shown():
            statements.clear()
            device = status.overview(home.opened, home.settings, home.photos, home.now)["people"][0]["devices"][0]
            return device["eligible"], len(statements)

        number, queries = shown()
        self.assertEqual(number, self.MARKED)
        self.assertLessEqual(queries, 30)
        dedupe.reverse(home.opened, photos[10].decisions[0].id, home.now, "owner")
        os.remove(home.photos.root / photos[20].items["photo"].path)
        number, queries = shown()
        self.assertEqual(number, self.MARKED - 2)
        self.assertLessEqual(queries, 30)
        home.opened.get(User, "sub-jo").last_seen = 1.0
        self.assertEqual(shown()[0], 0)


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
