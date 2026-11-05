from __future__ import annotations

import os
import random
import stat
import unittest

from sqlalchemy import select

from darkroom.domain import dedupe, eligibility
from darkroom.storage.models import DedupeDecision, Device, Export, GoogleAccount, MediaItem, PhoneAsset, PhoneResource, Pixel, PixelPerson, User
from tests.eligibility_world import Household, Photo, reported_of


class InvariantCase(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.jo = self.home.person("jo")
        self.phone = self.home.device(self.jo)
        self.google = self.home.account(self.jo)

    def assert_offered(self, photo: Photo, device: Device | None = None) -> None:
        device = device or self.phone
        found = self.home.eligible(device, photo)
        self.assertEqual([entry.asset_id for entry in found.eligible], [photo.id], found.rejected)
        confirmed, rejected = self.home.confirm(device, photo)
        self.assertEqual(([entry.asset_id for entry in confirmed], rejected), ([photo.id], []))

    def assert_refused(self, photo: Photo, reason: str, device: Device | None = None,
                       reported: eligibility.Reported | None = None) -> None:
        device = device or self.phone
        sent = [reported or self.home.reported(photo)]
        state, files = self.home.state(), self.home.files()
        found = eligibility.eligible_assets(self.home.opened, self.home.settings, self.home.photos, device.id, sent, self.home.now)
        self.assertEqual(found.eligible, [])
        self.assertEqual(found.rejected, [eligibility.Rejected(photo.id, reason)])
        confirmed, rejected = eligibility.confirm(self.home.opened, self.home.settings, self.home.photos, device.id, sent, self.home.now)
        self.assertEqual(confirmed, [])
        self.assertEqual(rejected, [eligibility.Rejected(photo.id, reason)])
        recorded, _ = eligibility.record_deleted(self.home.opened, device.id, [photo.id], self.home.now)
        self.assertEqual(recorded, [])
        self.assertEqual(self.home.state(), state)
        self.assertEqual(self.home.files(), files)


class EveryResourceIsOnUnraid(InvariantCase):
    def test_a_photo_with_every_resource_uploaded_and_linked_is_offered(self):
        self.assert_offered(self.home.photo(self.phone, ("photo", "live_video", "adjusted"), through=self.google))

    def test_any_resource_not_uploaded_holds_the_photo_back(self):
        for kind in ("photo", "live_video", "adjusted"):
            with self.subTest(kind=kind):
                photo = self.home.photo(self.phone, ("photo", "live_video", "adjusted"), through=self.google)
                photo.rows[kind].uploaded_at = None
                self.assert_refused(photo, eligibility.NOT_UPLOADED)

    def test_any_resource_linked_to_a_different_file_holds_the_photo_back(self):
        for kind in ("photo", "live_video", "adjusted"):
            with self.subTest(kind=kind):
                photo = self.home.photo(self.phone, ("photo", "live_video", "adjusted"), through=self.google)
                other = self.home.photo(self.phone, through=self.google)
                photo.rows[kind].media_item_id = other.items["photo"].id
                self.assert_refused(photo, eligibility.NOT_ON_UNRAID)

    def test_a_resource_linked_to_another_persons_item_holds_the_photo_back(self):
        sam = self.home.person("sam")
        photo = self.home.photo(self.phone, ("photo", "live_video"), through=self.google)
        photo.items["live_video"].person_id = sam.id
        self.assert_refused(photo, eligibility.NOT_ON_UNRAID)


class ConfirmedThroughAnAccountServingTheOwner(InvariantCase):
    def test_a_match_through_the_persons_own_account_counts(self):
        self.assert_offered(self.home.photo(self.phone, through=self.google))

    def test_a_shared_account_serving_both_people_counts_for_the_second_person(self):
        sam = self.home.person("sam")
        sams_phone = self.home.device(sam)
        shared = self.home.account(self.jo, pixel_people=(self.jo, sam))
        self.assert_offered(self.home.photo(sams_phone, through=shared), sams_phone)

    def test_a_single_person_household_behaves_the_same(self):
        self.assert_offered(self.home.photo(self.phone, ("photo", "live_video"), through=self.home.account(self.jo, pixel_people=(self.jo,))))

    def test_an_identical_photo_never_matched_for_the_second_person_is_not_eligible(self):
        sam = self.home.person("sam")
        sams_phone = self.home.device(sam)
        only_jo = self.home.account(self.jo, pixel_people=(self.jo,))
        photo = self.home.photo(sams_phone)
        export = self.home.export(only_jo)
        dedupe.apply(self.home.opened, dedupe.Decision(source_path="Takeout/Google Photos/same.HEIC", account_id=only_jo.id, person_id=self.jo.id,
                                                       tier=5, action="google_only", reason="made up"), export, self.home.now)
        self.assertFalse(photo.items["photo"].confirmed_in_google)
        self.assert_refused(photo, eligibility.NOT_CONFIRMED, sams_phone)

    def test_a_hand_made_decision_from_an_account_not_serving_the_person_is_refused(self):
        sam = self.home.person("sam")
        sams_phone = self.home.device(sam)
        only_jo = self.home.account(self.jo, pixel_people=(self.jo,))
        photo = self.home.photo(sams_phone)
        export = self.home.export(only_jo)
        self.home.opened.add(DedupeDecision(export_id=export.id, account_id=only_jo.id, source_path="hand-made", person_id=sam.id,
                                            matched_item_id=photo.items["photo"].id, tier=1, reason="hand-made", decided_by="auto", decided_at=1.0))
        photo.items["photo"].confirmed_in_google = True
        self.assert_refused(photo, eligibility.ACCOUNT_NOT_SERVING, sams_phone)

    def test_an_account_that_stopped_serving_the_person_no_longer_counts(self):
        sam = self.home.person("sam")
        sams_phone = self.home.device(sam)
        shared = self.home.account(self.jo, pixel_people=(self.jo, sam))
        photo = self.home.photo(sams_phone, through=shared)
        self.home.opened.execute(PixelPerson.__table__.delete().where(PixelPerson.person_id == sam.id))
        self.assert_refused(photo, eligibility.ACCOUNT_NOT_SERVING, sams_phone)

    def test_the_flag_alone_or_a_reversed_decision_is_not_enough(self):
        photo = self.home.photo(self.phone, through=self.google)
        dedupe.reverse(self.home.opened, photo.decisions[0].id, self.home.now, "owner")
        self.assert_refused(photo, eligibility.NOT_CONFIRMED)
        photo.items["photo"].confirmed_in_google = True
        self.assert_refused(photo, eligibility.NO_AUTOMATIC_MATCH)

    def test_a_live_photo_whose_video_half_is_unmatched_is_not_eligible(self):
        photo = self.home.photo(self.phone, ("photo", "live_video"), through=self.google, unmatched=("live_video",))
        self.assert_refused(photo, eligibility.NOT_CONFIRMED)


class TheFileIsStillThere(InvariantCase):
    def test_a_file_at_its_recorded_size_counts(self):
        self.assert_offered(self.home.photo(self.phone, ("photo", "adjusted"), through=self.google))

    def test_a_missing_or_resized_file_is_refused(self):
        for change in ("removed", "resized"):
            for kind in ("photo", "adjusted"):
                with self.subTest(change=change, kind=kind):
                    photo = self.home.photo(self.phone, ("photo", "adjusted"), through=self.google)
                    path = self.home.photos.root / photo.items[kind].path
                    if change == "removed":
                        os.remove(path)
                    else:
                        path.write_bytes(path.read_bytes() + b"more")
                    self.assert_refused(photo, eligibility.FILE_MISSING)


class ThePhoneReportsWhatTheServerKnows(InvariantCase):
    def changed(self, photo: Photo, resources: tuple[eligibility.ReportedResource, ...]) -> eligibility.Reported:
        return eligibility.Reported(photo.id, resources)

    def test_the_same_resources_count(self):
        self.assert_offered(self.home.photo(self.phone, ("photo", "live_video"), through=self.google))

    def test_a_new_adjusted_version_makes_the_photo_ineligible(self):
        photo = self.home.photo(self.phone, through=self.google)
        reported = self.home.reported(photo)
        extra = (*reported.resources, eligibility.ReportedResource("adjusted", "b" * 40, 10, "IMG_9999_adjusted.HEIC"))
        self.assert_refused(photo, eligibility.RESOURCES_CHANGED, reported=self.changed(photo, extra))

    def test_a_missing_dropped_or_changed_resource_makes_the_photo_ineligible(self):
        photo = self.home.photo(self.phone, ("photo", "live_video"), through=self.google)
        reported = self.home.reported(photo)
        first, second = reported.resources
        for resources in ((first,), (), (first, eligibility.ReportedResource(second.kind, "c" * 40, second.bytes, second.filename)),
                          (first, eligibility.ReportedResource(second.kind, second.sha1, second.bytes + 1, second.filename)),
                          (first, eligibility.ReportedResource(second.kind, second.sha1, second.bytes, "OTHER.MOV")),
                          (first, eligibility.ReportedResource("adjusted", second.sha1, second.bytes, second.filename))):
            with self.subTest(resources=len(resources)):
                self.assert_refused(photo, eligibility.RESOURCES_CHANGED, reported=self.changed(photo, resources))


class TheDeviceMayClean(InvariantCase):
    def test_an_enabled_signed_in_iphone_is_offered_photos(self):
        self.assert_offered(self.home.photo(self.phone, through=self.google))

    def test_cleanup_off_lists_nothing_and_confirms_nothing(self):
        photos = [self.home.photo(self.phone, through=self.google) for _ in range(3)]
        self.phone.cleanup_enabled = False
        sent = [self.home.reported(photo) for photo in photos]
        state = self.home.state()
        found = eligibility.eligible_assets(self.home.opened, self.home.settings, self.home.photos, self.phone.id, sent, self.home.now)
        self.assertFalse(found.cleanup_enabled)
        self.assertEqual(found.eligible, [])
        self.assertEqual({entry.reason for entry in found.rejected}, {eligibility.CLEANUP_DISABLED})
        confirmed, rejected = eligibility.confirm(self.home.opened, self.home.settings, self.home.photos, self.phone.id, sent, self.home.now)
        self.assertEqual((confirmed, len(rejected)), ([], 3))
        self.assertEqual(self.home.state(), state)

    def test_a_revoked_iphone_is_refused(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.phone.revoked_at = 5.0
        self.assert_refused(photo, eligibility.DEVICE_REVOKED)

    def test_a_person_whose_access_lapsed_is_refused(self):
        user = self.home.opened.get(User, "sub-jo")
        for change in ("groups", "last_seen", "unverified", "other_sub"):
            with self.subTest(change=change):
                photo = self.home.photo(self.phone, through=self.google)
                saved = (user.groups, user.last_seen, user.email_verified, self.phone.sub)
                if change == "groups":
                    user.groups = '["guests"]'
                elif change == "last_seen":
                    user.last_seen = self.home.now - 400 * 86_400
                elif change == "unverified":
                    user.email_verified = False
                else:
                    self.phone.sub = "someone-else"
                self.assert_refused(photo, eligibility.ACCESS_LAPSED)
                user.groups, user.last_seen, user.email_verified, self.phone.sub = saved


class EveryAnswerIsComputedAfresh(InvariantCase):
    def test_reversing_the_match_between_eligible_and_confirm_is_refused(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.assertEqual(len(self.home.eligible(self.phone, photo).eligible), 1)
        dedupe.reverse(self.home.opened, photo.decisions[0].id, self.home.now, "owner")
        confirmed, rejected = self.home.confirm(self.phone, photo)
        self.assertEqual((confirmed, rejected), ([], [eligibility.Rejected(photo.id, eligibility.NOT_CONFIRMED)]))
        self.assertEqual((photo.asset.eligible_at, photo.asset.eligible_confirmed_at, photo.asset.deleted_at), (None, None, None))

    def test_removing_the_file_between_eligible_and_confirm_is_refused(self):
        photo = self.home.photo(self.phone, ("photo", "live_video"), through=self.google)
        self.assertEqual(len(self.home.eligible(self.phone, photo).eligible), 1)
        os.remove(self.home.photos.root / photo.items["live_video"].path)
        self.assertEqual(self.home.confirm(self.phone, photo), ([], [eligibility.Rejected(photo.id, eligibility.FILE_MISSING)]))

    def test_turning_cleanup_off_between_eligible_and_confirm_is_refused(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.home.eligible(self.phone, photo)
        eligibility.set_cleanup(self.home.opened, self.phone.id, False, False, self.home.now)
        self.assertEqual(self.home.confirm(self.phone, photo), ([], [eligibility.Rejected(photo.id, eligibility.CLEANUP_DISABLED)]))

    def test_a_stored_eligible_mark_is_never_trusted(self):
        photo = self.home.photo(self.phone)
        photo.asset.eligible_at, photo.asset.eligible_confirmed_at = 1.0, 1.0
        self.assertEqual(self.home.confirm(self.phone, photo), ([], [eligibility.Rejected(photo.id, eligibility.NOT_CONFIRMED)]))
        self.assertEqual((photo.asset.eligible_at, photo.asset.eligible_confirmed_at), (None, None))

    def test_confirm_needs_no_earlier_list(self):
        photo = self.home.photo(self.phone, through=self.google)
        self.assertEqual([entry.asset_id for entry in self.home.confirm(self.phone, photo)[0]], [photo.id])

    def test_deleted_is_recorded_only_for_the_ids_reported(self):
        first, second = (self.home.photo(self.phone, through=self.google) for _ in range(2))
        self.home.confirm(self.phone, first, second)
        recorded, rejected = eligibility.record_deleted(self.home.opened, self.phone.id, [second.id], self.home.now)
        self.assertEqual((recorded, rejected), ([second.id], []))
        self.assertEqual((first.asset.deleted_at, second.asset.deleted_at), (None, self.home.now))


class NothingButAnAutomaticTierOneOrTwoMakesAnythingEligible(InvariantCase):
    def test_tier_two_counts_like_tier_one(self):
        self.assert_offered(self.home.photo(self.phone, through=self.google, tier=2))

    def test_tiers_three_four_and_five_never_count(self):
        for tier in (3, 4, 5):
            with self.subTest(tier=tier):
                photo = self.home.photo(self.phone, through=self.google, tier=tier)
                self.assert_refused(photo, eligibility.NOT_CONFIRMED)
                photo.items["photo"].confirmed_in_google = True
                self.assert_refused(photo, eligibility.NO_AUTOMATIC_MATCH)

    def test_an_owner_made_decision_never_counts(self):
        photo = self.home.photo(self.phone, through=self.google, decided_by="owner")
        self.assert_refused(photo, eligibility.NOT_CONFIRMED)
        photo.items["photo"].confirmed_in_google = True
        self.assert_refused(photo, eligibility.NO_AUTOMATIC_MATCH)

    def test_a_review_action_after_reversing_never_counts(self):
        photo = self.home.photo(self.phone, through=self.google, tier=4)
        dedupe.reverse(self.home.opened, photo.decisions[0].id, self.home.now, "owner")
        self.home.match(photo.items["photo"], self.google, tier=1, decided_by="owner")
        self.assert_refused(photo, eligibility.NOT_CONFIRMED)

    def test_a_reassigned_item_never_counts_for_the_new_person(self):
        sam = self.home.person("sam")
        sams_phone = self.home.device(sam)
        shared = self.home.account(self.jo, pixel_people=(self.jo, sam))
        jos = self.home.photo(self.phone, through=shared)
        sams = self.home.photo(sams_phone)
        jos.items["photo"].person_id = sam.id
        sams.rows["photo"].media_item_id = jos.items["photo"].id
        sams.rows["photo"].sha1, sams.rows["photo"].bytes, sams.rows["photo"].filename = (jos.rows["photo"].sha1, jos.rows["photo"].bytes,
                                                                                           jos.rows["photo"].filename)
        self.assert_refused(sams, eligibility.ACCOUNT_NOT_SERVING, sams_phone)
        self.assert_refused(jos, eligibility.NOT_ON_UNRAID)


class EveryConfirmedIdKeepsTheRules(unittest.TestCase):
    KINDS = (("photo",), ("photo", "live_video"), ("photo", "adjusted"), ("photo", "live_video", "adjusted"), ("live_video",))

    def test_random_households_confirm_only_what_re_derives_as_safe(self):
        self.tally = {"confirmed": 0, "rejected": 0}
        for seed in range(30):
            with self.subTest(seed=seed):
                self.run_household(random.Random(seed))
        self.assertGreater(self.tally["confirmed"], 20)
        self.assertGreater(self.tally["rejected"], 100)

    def run_household(self, rng: random.Random) -> None:
        home = Household(self)
        people = [home.person(slug) for slug in ("ana", "ben", "cy")]
        accounts = [home.account(people[0], pixel_people=tuple(rng.sample(people, rng.randint(0, 3)))), home.account(people[1]),
                    home.account(people[2], pixel_people=tuple(rng.sample(people, rng.randint(0, 2))))]
        exports = [home.export(account) for account in accounts]
        exports.append(Export(account_id=accounts[1].id, kind="manual"))
        home.opened.add(exports[-1])
        home.opened.flush()
        for person in people:
            device = home.device(person, cleanup=rng.random() < 0.85)
            if rng.random() < 0.08:
                device.revoked_at = 3.0
            if rng.random() < 0.08:
                home.opened.get(User, f"sub-{person.slug}").last_seen = home.now - 400 * 86_400
            sent = [self.random_photo(home, rng, device, people, accounts, exports) for _ in range(10)]
            confirmed, rejected = eligibility.confirm(home.opened, home.settings, home.photos, device.id, sent, home.now)
            self.tally["confirmed"] += len(confirmed)
            self.tally["rejected"] += len(rejected)
            by_id = {entry.photokit_id: entry for entry in sent}
            self.assertEqual({entry.asset_id for entry in confirmed} | {entry.asset_id for entry in rejected}, set(by_id))
            for entry in confirmed:
                self.assertTrue(self.re_derived(home, device, by_id[entry.asset_id]), entry.asset_id)
            for entry in rejected:
                self.assertIn(entry.reason, eligibility.REASONS)

    def random_photo(self, home, rng, device, people, accounts, exports) -> eligibility.Reported:
        clean = rng.random() < 0.5
        own = next(account for account in accounts if account.default_person_id == device.person_id)
        photo = home.photo(device, rng.choice(self.KINDS), through=own if clean else None)
        for kind, row in photo.rows.items():
            item = photo.items[kind]
            fault = "none" if clean and rng.random() < 0.9 else rng.choice(["none"] * 3 + ["not_uploaded", "unlinked", "other_person", "other_hash",
                                                                                              "no_file", "resized", "link", "takeout"])
            if fault == "not_uploaded":
                row.uploaded_at = None
            elif fault == "unlinked":
                row.media_item_id = None
            elif fault == "other_person":
                item.person_id = rng.choice(people).id
            elif fault == "other_hash":
                item.sha1 = "e" * 40
            elif fault == "takeout":
                item.source = "takeout"
            elif fault in ("no_file", "resized", "link"):
                path = home.photos.root / item.path
                if fault == "no_file":
                    os.remove(path)
                elif fault == "resized":
                    path.write_bytes(b"x")
                else:
                    os.rename(path, path.with_name("moved.bin"))
                    os.symlink(path.with_name("moved.bin"), path)
            for _ in range(rng.choice([0, 0, 1, 1, 2])):
                account = rng.choice(accounts)
                export = rng.choice([candidate for candidate in exports if candidate.account_id == account.id] + [rng.choice(exports)])
                home.opened.add(DedupeDecision(export_id=export.id, account_id=account.id, source_path=f"r-{rng.random()}",
                                               person_id=rng.choice([device.person_id, device.person_id, rng.choice(people).id]), matched_item_id=item.id,
                                               tier=rng.choice([1, 1, 2, 3, 4, 5]), reason="random", decided_by=rng.choice(["auto", "auto", "owner"]),
                                               decided_at=1.0, reversed_at=rng.choice([None, None, None, 2.0])))
            item.confirmed_in_google = rng.random() < 0.9
        home.opened.flush()
        reported = reported_of(home.opened, photo.asset)
        change = rng.random()
        if change < 0.05:
            return eligibility.Reported(reported.photokit_id, reported.resources[:-1])
        if change < 0.1:
            return eligibility.Reported(reported.photokit_id, (*reported.resources, eligibility.ReportedResource("adjusted", "d" * 40, 3, "NEW.HEIC")))
        if change < 0.13:
            photo.asset.deleted_at = 2.0
        return reported

    def re_derived(self, home, device: Device, reported: eligibility.Reported) -> bool:
        opened = home.opened
        user = opened.get(User, device.sub)
        if not device.cleanup_enabled or device.revoked_at is not None or user is None or home.now - user.last_seen > 30 * 86_400:
            return False
        asset = opened.execute(select(PhoneAsset).where(PhoneAsset.device_id == device.id, PhoneAsset.photokit_id == reported.photokit_id)).scalar_one()
        if asset.deleted_at is not None:
            return False
        rows = {row.kind: row for row in opened.execute(select(PhoneResource).where(PhoneResource.asset_id == asset.id)).scalars()}
        if {r.kind: (r.sha1, r.bytes, r.filename) for r in reported.resources} != {k: (r.sha1, r.bytes, r.filename) for k, r in rows.items()}:
            return False
        if "photo" not in rows or len(reported.resources) != len(rows):
            return False
        for row in rows.values():
            item = opened.get(MediaItem, row.media_item_id) if row.media_item_id is not None else None
            if row.uploaded_at is None or item is None or item.source != "phone" or item.person_id != device.person_id or item.sha1 != row.sha1:
                return False
            if not item.confirmed_in_google:
                return False
            backed = False
            for decision in opened.execute(select(DedupeDecision).where(DedupeDecision.matched_item_id == item.id)).scalars():
                export = opened.get(Export, decision.export_id)
                if (decision.reversed_at is None and decision.decided_by == "auto" and decision.tier in (1, 2) and decision.person_id == device.person_id
                        and export is not None and export.account_id == decision.account_id and device.person_id in self.served(opened, decision.account_id)):
                    backed = True
            if not backed:
                return False
            path = home.photos.root / item.path
            if path.is_symlink() or not path.exists() or not stat.S_ISREG(os.lstat(path).st_mode) or os.lstat(path).st_size != item.bytes:
                return False
        return True

    def served(self, opened, account_id: int) -> set[int]:
        account = opened.get(GoogleAccount, account_id)
        by_pixel = opened.execute(select(PixelPerson.person_id).join(Pixel, Pixel.id == PixelPerson.pixel_id).where(Pixel.google_account_id == account_id)).scalars()
        return {account.default_person_id, *by_pixel}


if __name__ == "__main__":
    unittest.main()
