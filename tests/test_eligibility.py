from __future__ import annotations

import dataclasses
import os
import sqlite3
import unittest
from unittest import mock

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, eligibility, status, uploads
from darkroom.storage import db
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
        self.assertEqual(len(eligibility.REASONS), 14)
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


class ConfirmAndDeletedTests(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.jo = self.home.person("jo")
        self.phone = self.home.device(self.jo)
        self.google = self.home.account(self.jo)

    def test_confirm_marks_only_what_passes_and_records_an_event(self):
        good = self.home.photo(self.phone, through=self.google)
        bad = self.home.photo(self.phone)
        confirmed, rejected = self.home.confirm(self.phone, good, bad)
        self.assertEqual([entry.asset_id for entry in confirmed], [good.id])
        self.assertEqual(reasons(rejected), {bad.id: eligibility.NOT_CONFIRMED})
        self.assertEqual((good.asset.eligible_at, good.asset.eligible_confirmed_at), (self.home.now, self.home.now))
        self.assertEqual((bad.asset.eligible_at, bad.asset.eligible_confirmed_at), (None, None))
        event = self.home.opened.execute(select(Event).where(Event.type == "cleanup_confirmed")).scalar_one()
        self.assertEqual((event.subject, event.detail), ("jo", "1 of 2 confirmed"))

    def test_a_failed_recheck_clears_an_earlier_confirmation(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.home.confirm(self.phone, photo)
        os.remove(self.home.photos.root / photo.items["photo"].path)
        self.home.eligible(self.phone, photo)
        self.assertEqual((photo.asset.eligible_at, photo.asset.eligible_confirmed_at), (None, None))
        recorded, rejected = eligibility.record_deleted(self.home.opened, self.phone.id, [photo.id], self.home.now)
        self.assertEqual((recorded, reasons(rejected)), ([], {photo.id: eligibility.NOT_CONFIRMED_FOR_DELETION}))

    def test_deleted_is_recorded_only_for_confirmed_assets_of_this_iphone(self):
        confirmed = self.home.photo(self.phone, through=self.google)
        listed_only = self.home.photo(self.phone, through=self.google)
        other = self.home.photo(self.home.device(self.jo), through=self.google)
        self.home.confirm(self.phone, confirmed)
        self.home.eligible(self.phone, listed_only)
        recorded, rejected = eligibility.record_deleted(self.home.opened, self.phone.id, [confirmed.id, listed_only.id, other.id, "NEVER-SEEN"], self.home.now)
        self.assertEqual(recorded, [confirmed.id])
        self.assertEqual(reasons(rejected), {listed_only.id: eligibility.NOT_CONFIRMED_FOR_DELETION, other.id: eligibility.UNKNOWN_ASSET,
                                             "NEVER-SEEN": eligibility.UNKNOWN_ASSET})
        self.assertEqual(confirmed.asset.deleted_at, self.home.now)
        self.assertIsNone(confirmed.asset.eligible_at)
        self.assertEqual((listed_only.asset.deleted_at, other.asset.deleted_at), (None, None))
        event = self.home.opened.execute(select(Event).where(Event.type == "cleanup_deleted").order_by(Event.id)).scalars().first()
        self.assertEqual(event.detail, "1 recorded, 3 refused")
        with self.assertRaises(ValueError):
            eligibility.record_deleted(self.home.opened, self.phone.id, [confirmed.id, confirmed.id], self.home.now)

    def test_a_repeated_report_of_a_recorded_deletion_is_recorded_again_and_changes_nothing(self):
        confirmed = self.home.photo(self.phone, through=self.google)
        listed_only = self.home.photo(self.phone, through=self.google)
        other = self.home.photo(self.home.device(self.jo), through=self.google)
        self.home.confirm(self.phone, confirmed)
        self.home.eligible(self.phone, listed_only)
        eligibility.record_deleted(self.home.opened, self.phone.id, [confirmed.id], self.home.now)
        ids = [confirmed.id, listed_only.id, other.id, "NEVER-SEEN"]
        again, refused = eligibility.record_deleted(self.home.opened, self.phone.id, ids, self.home.now + 5)
        self.assertEqual(again, [confirmed.id])
        self.assertEqual(reasons(refused), {listed_only.id: eligibility.NOT_CONFIRMED_FOR_DELETION, other.id: eligibility.UNKNOWN_ASSET,
                                            "NEVER-SEEN": eligibility.UNKNOWN_ASSET})
        self.assertEqual(confirmed.asset.deleted_at, self.home.now)
        self.assertEqual((listed_only.asset.deleted_at, other.asset.deleted_at), (None, None))

    def test_a_deleted_asset_is_never_offered_again(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.home.confirm(self.phone, photo)
        eligibility.record_deleted(self.home.opened, self.phone.id, [photo.id], self.home.now)
        self.assertEqual(reasons(self.home.eligible(self.phone, photo).rejected), {photo.id: eligibility.ALREADY_DELETED})
        self.assertEqual(reasons(self.home.confirm(self.phone, photo)[1]), {photo.id: eligibility.ALREADY_DELETED})


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
        self.home.confirm(self.phone, photo)
        self.assertIsNotNone(self.phone.eligibility_checked_at)
        eligibility.set_cleanup(self.home.opened, self.phone.id, False, False, self.home.now)
        self.home.opened.refresh(photo.asset)
        self.assertEqual((photo.asset.eligible_at, photo.asset.eligible_confirmed_at), (None, None))
        self.assertIsNone(self.phone.eligibility_checked_at)
        recorded, _ = eligibility.record_deleted(self.home.opened, self.phone.id, [photo.id], self.home.now)
        self.assertEqual(recorded, [])

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

    def test_status_counts_what_the_last_evaluation_found_and_not_what_was_deleted(self):
        first = self.home.photo(self.phone, ("photo", "live_video"), through=self.google)
        second = self.home.photo(self.phone, through=self.google)
        third = self.home.photo(self.phone)
        self.home.eligible(self.phone, first, second, third)
        size = sum(item.bytes for photo in (first, second) for item in photo.items.values())
        shown = self.device_on_status()
        self.assertEqual((shown["eligible"], shown["eligible_bytes"], shown["eligible_checked"]), (2, size, self.home.now))
        self.home.confirm(self.phone, second)
        eligibility.record_deleted(self.home.opened, self.phone.id, [second.id], self.home.now)
        self.assertEqual(self.device_on_status()["eligible"], 1)

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


class WriteLockTests(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.jo = self.home.person("jo")
        self.phone = self.home.device(self.jo)
        self.photo = self.home.photo(self.phone, through=self.home.account(self.jo))
        self.home.opened.commit()
        self.database = self.home.engine.url.database
        impatient = create_engine(str(self.home.engine.url), connect_args={"timeout": 0.05})
        event.listen(impatient, "connect", db.set_sqlite_pragmas)
        self.addCleanup(impatient.dispose)
        self.impatient = impatient

    def test_confirm_holds_the_write_lock_so_a_reversal_waits_for_it(self):
        with Session(self.impatient, expire_on_commit=False) as opened:
            confirmed, _ = eligibility.confirm(opened, self.home.settings, self.home.photos, self.phone.id, [self.home.reported(self.photo)], self.home.now)
            self.assertEqual([entry.asset_id for entry in confirmed], [self.photo.id])
            other = sqlite3.connect(self.database, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    other.execute("UPDATE dedupe_decisions SET reversed_at = 9.0")
            finally:
                other.close()
            opened.commit()
        other = sqlite3.connect(self.database, timeout=0)
        try:
            other.execute("UPDATE dedupe_decisions SET reversed_at = 9.0")
            other.commit()
        finally:
            other.close()

    def test_a_confirm_that_cannot_get_the_write_lock_is_busy_and_changes_nothing(self):
        holder = sqlite3.connect(self.database, timeout=0)
        holder.execute("BEGIN IMMEDIATE")
        try:
            for call in (lambda opened: eligibility.confirm(opened, self.home.settings, self.home.photos, self.phone.id, [self.home.reported(self.photo)],
                                                            self.home.now),
                         lambda opened: eligibility.record_deleted(opened, self.phone.id, [self.photo.id], self.home.now),
                         lambda opened: eligibility.set_cleanup(opened, self.phone.id, False, False, self.home.now)):
                with Session(self.impatient) as opened, self.assertRaises(eligibility.Busy):
                    call(opened)
        finally:
            holder.rollback()
            holder.close()
        check = sqlite3.connect(self.database)
        try:
            self.assertEqual(check.execute("SELECT eligible_at, eligible_confirmed_at, deleted_at FROM phone_assets").fetchall(), [(None, None, None)])
            self.assertEqual(check.execute("SELECT cleanup_enabled FROM devices").fetchall(), [(1,)])
        finally:
            check.close()


class CheckForgetsTests(unittest.TestCase):
    def test_a_new_check_that_changes_the_asset_forgets_its_evaluation(self):
        home = Household(self)
        jo = home.person("jo")
        phone = home.device(jo)
        photo = home.photo(phone, through=home.account(jo))
        home.confirm(phone, photo)
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
