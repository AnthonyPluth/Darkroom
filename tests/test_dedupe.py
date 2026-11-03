import unittest
from pathlib import Path

from darkroom.domain import dedupe
from darkroom.domain.ingest.pairing import TakeoutItem
from darkroom.storage.models import MediaItem, Setting
from darkroom.storage.settings_keys import KEEP_EDITED_COPIES
from tests.dedupe_world import LOCAL, MOMENT, PHASH, World, flip

OFF = dedupe.DedupeSettings()
KEEP = dedupe.DedupeSettings(keep_edited_copies=True)


class SinglePerson(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.jo = self.world.person("jo")
        self.account = self.world.account("jo@x.test", self.jo)

    def decide(self, candidate, settings=OFF):
        return dedupe.decide(self.world.index(self.account), candidate, settings)


class TierOneTests(SinglePerson):
    def test_the_same_sha1_matches_the_phone_item(self):
        phone = self.world.item(self.jo, width=1, phash=None)
        decision = self.decide(self.world.candidate(sha1=phone.sha1))
        self.assertEqual((decision.tier, decision.action, decision.matched_item_id, decision.person_id), (1, "skip", phone.id, self.jo))
        self.assertEqual(decision.matched_source, "phone")
        self.assertTrue(decision.confirms)

    def test_a_different_sha1_with_nothing_else_alike_is_not_tier_one(self):
        self.world.item(self.jo, phash=None, captured_utc=None, captured_local=None)
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)

    def test_a_reimport_skips_at_tier_one_against_the_placed_takeout_item(self):
        placed = self.world.item(self.jo, source="takeout", export_id=self.world.export(self.account).id)
        decision = self.decide(self.world.candidate(sha1=placed.sha1))
        self.assertEqual((decision.tier, decision.matched_item_id, decision.matched_source), (1, placed.id, "takeout"))
        self.assertFalse(decision.confirms)

    def test_a_phone_item_wins_over_a_placed_takeout_copy_of_the_same_bytes(self):
        placed = self.world.item(self.jo, source="takeout", export_id=self.world.export(self.account).id)
        phone = self.world.item(self.jo, sha1=placed.sha1)
        self.assertEqual(self.decide(self.world.candidate(sha1=placed.sha1)).matched_item_id, phone.id)

    def test_a_takeout_item_without_an_export_is_not_loaded(self):
        orphan = self.world.item(self.jo, source="takeout", export_id=None, phash=None)
        self.assertEqual(self.world.index(self.account).takeout, ())
        self.assertEqual(self.decide(self.world.candidate(sha1=orphan.sha1, phash=None)).tier, 5)


class TierTwoTests(SinglePerson):
    def test_the_same_second_camera_and_size_match(self):
        phone = self.world.item(self.jo)
        decision = self.decide(self.world.candidate())
        self.assertEqual((decision.tier, decision.action, decision.matched_item_id), (2, "skip", phone.id))
        self.assertTrue(decision.confirms)

    def test_one_pixel_of_difference_fails(self):
        self.world.item(self.jo)
        self.assertEqual(self.decide(self.world.candidate(width=4031, phash=None)).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(height=3025, phash=None)).tier, 5)

    def test_a_missing_size_never_matches(self):
        self.world.item(self.jo, width=None, height=None)
        self.assertEqual(self.decide(self.world.candidate(width=None, height=None, phash=None)).tier, 5)

    def test_an_empty_model_on_either_side_fails(self):
        self.world.item(self.jo, model="")
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(model="", phash=None)).tier, 5)

    def test_a_missing_make_fails(self):
        self.world.item(self.jo, make=None)
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)

    def test_make_and_model_compare_without_case(self):
        self.world.item(self.jo, make="APPLE", model="iphone 15 pro ")
        self.assertEqual(self.decide(self.world.candidate()).tier, 2)

    def test_another_camera_fails(self):
        self.world.item(self.jo, model="iPhone 14")
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)

    def test_one_second_apart_fails(self):
        self.world.item(self.jo)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT + 1, phash=None)).tier, 5)

    def test_utc_wins_over_wall_clock_when_both_sides_have_it(self):
        self.world.item(self.jo)
        self.assertEqual(self.decide(self.world.candidate(captured_local="2023-11-15T00:13:20")).tier, 2)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT + 3600, phash=None)).tier, 5)

    def test_wall_clock_is_compared_when_either_side_lacks_utc(self):
        self.world.item(self.jo, captured_utc=None)
        self.assertEqual(self.decide(self.world.candidate()).tier, 2)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None)).tier, 2)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local="2023-11-14T22:13:21", phash=None)).tier, 5)

    def test_a_missing_time_on_either_side_never_matches(self):
        self.world.item(self.jo, captured_utc=None, captured_local=None)
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)
        self.world.opened.query(MediaItem).update({"captured_utc": MOMENT, "captured_local": LOCAL})
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local=None, phash=None)).tier, 5)

    def test_an_unreadable_wall_clock_never_matches(self):
        self.world.item(self.jo, captured_utc=None, captured_local="not a time")
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local="not a time", phash=None)).tier, 5)

    def test_sub_seconds_must_agree_when_both_sides_have_them(self):
        self.world.item(self.jo, captured_utc=MOMENT + 0.25)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT + 0.25)).tier, 2)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT + 0.5, phash=None)).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT)).tier, 2)

    def test_videos_match_on_duration_to_the_second(self):
        clip = self.world.item(self.jo, suffix=".MOV", width=None, height=None, duration_ms=12_400, phash=None)
        self.assertEqual(self.decide(self.world.candidate(is_video=True, width=None, height=None, duration_ms=12_900, phash=None)).matched_item_id, clip.id)
        self.assertEqual(self.decide(self.world.candidate(is_video=True, duration_ms=13_000, phash=None)).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(is_video=True, duration_ms=None, phash=None)).tier, 5)

    def test_a_photo_never_matches_a_video(self):
        self.world.item(self.jo, suffix=".MOV", duration_ms=1000, phash=None)
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)

    def test_an_edited_copy_is_never_tier_two(self):
        self.world.item(self.jo)
        decision = self.decide(self.world.candidate(is_edited=True))
        self.assertNotIn(decision.tier, (1, 2))
        self.assertFalse(decision.confirms)

    def test_an_owner_outside_the_served_set_fails(self):
        sam = self.world.person("sam")
        self.world.item(sam)
        decision = self.decide(self.world.candidate())
        self.assertEqual((decision.tier, decision.person_id, decision.matched_item_id), (5, self.jo, None))


class TierTwoAmbiguityTests(SinglePerson):
    def test_two_burst_frames_in_the_same_second_go_to_review(self):
        first, second = self.world.item(self.jo), self.world.item(self.jo)
        decision = self.decide(self.world.candidate())
        self.assertEqual((decision.tier, decision.action, decision.matched_item_id), (4, "review", first.id))
        self.assertIn("2 phone items", decision.reason)
        self.assertFalse(decision.confirms)
        self.assertNotEqual(second.id, decision.matched_item_id)

    def test_differing_sub_seconds_let_each_frame_match_its_own(self):
        first = self.world.item(self.jo, captured_utc=MOMENT + 0.1)
        second = self.world.item(self.jo, captured_utc=MOMENT + 0.2)
        frames = [self.world.candidate(captured_utc=MOMENT + 0.2), self.world.candidate(captured_utc=MOMENT + 0.1)]
        decided = dedupe.decide_all(self.world.index(self.account), frames, OFF)
        self.assertEqual([(d.tier, d.matched_item_id) for d in decided], [(2, second.id), (2, first.id)])

    def test_two_takeout_items_claiming_one_phone_item_both_go_to_review(self):
        only = self.world.item(self.jo)
        frames = [self.world.candidate(), self.world.candidate()]
        decided = dedupe.decide_all(self.world.index(self.account), frames, OFF)
        self.assertEqual([(d.tier, d.matched_item_id) for d in decided], [(4, only.id), (4, only.id)])
        self.assertIn("1 other Takeout items", decided[0].reason)

    def test_byte_identical_takeout_copies_do_not_contest_each_other(self):
        only = self.world.item(self.jo)
        twin = self.world.candidate()
        decided = dedupe.decide_all(self.world.index(self.account), [twin, self.world.candidate(sha1=twin.sha1)], OFF)
        self.assertEqual([(d.tier, d.matched_item_id) for d in decided], [(2, only.id), (2, only.id)])

    def test_an_edited_copy_does_not_contest_its_original(self):
        only = self.world.item(self.jo)
        original = self.world.candidate()
        edited = self.world.candidate(is_edited=True, edited_of=original.source_path)
        decided = dedupe.decide_all(self.world.index(self.account), [edited, original], OFF)
        self.assertEqual([(d.tier, d.matched_item_id) for d in decided], [(3, only.id), (2, only.id)])

    def test_burst_frames_owned_by_two_people_go_to_the_default_persons_review(self):
        sam, kim = self.world.person("sam"), self.world.person("kim")
        shared = self.world.account("family@x.test", self.jo, (sam, kim))
        self.world.item(sam)
        self.world.item(kim)
        self.world.item(self.world.person("lee"))
        decision = dedupe.decide(self.world.index(shared), self.world.candidate(), OFF)
        self.assertEqual((decision.tier, decision.person_id), (4, self.jo))
        self.assertIn("2 phone items", decision.reason)


class TierThreeTests(SinglePerson):
    def pair(self, **original_fields):
        original = self.world.candidate(**original_fields)
        edited = self.world.candidate(is_edited=True, edited_of=original.source_path, sha1="e" * 40, phash=None)
        return original, edited

    def test_an_edited_copy_of_a_tier_one_match_is_skipped(self):
        phone = self.world.item(self.jo)
        original, edited = self.pair(sha1=phone.sha1)
        decided = dedupe.decide_all(self.world.index(self.account), [original, edited], OFF)
        self.assertEqual([(d.tier, d.action) for d in decided], [(1, "skip"), (3, "skip")])
        self.assertEqual((decided[1].matched_item_id, decided[1].person_id), (phone.id, self.jo))
        self.assertIn("tier 1", decided[1].reason)
        self.assertFalse(decided[1].confirms)

    def test_an_edited_copy_of_a_tier_two_match_is_skipped(self):
        self.world.item(self.jo)
        decided = dedupe.decide_all(self.world.index(self.account), list(self.pair()), OFF)
        self.assertEqual([d.tier for d in decided], [2, 3])

    def test_keeping_edited_copies_places_them(self):
        self.world.item(self.jo)
        decided = dedupe.decide_all(self.world.index(self.account), list(self.pair()), KEEP)
        self.assertEqual([(d.tier, d.action) for d in decided], [(2, "skip"), (5, "google_only")])

    def test_an_edited_copy_whose_original_matched_at_tier_four_is_not_tier_three(self):
        self.world.item(self.jo, width=1, height=1)
        decided = dedupe.decide_all(self.world.index(self.account), list(self.pair()), OFF)
        self.assertEqual([d.tier for d in decided], [4, 5])

    def test_an_edited_copy_whose_original_is_google_only_is_not_tier_three(self):
        decided = dedupe.decide_all(self.world.index(self.account), list(self.pair()), OFF)
        self.assertEqual([d.tier for d in decided], [5, 5])

    def test_an_edited_copy_without_its_original_in_the_export_is_not_tier_three(self):
        self.world.item(self.jo)
        lone = self.world.candidate(is_edited=True, edited_of=None, phash=None)
        self.assertEqual(dedupe.decide_all(self.world.index(self.account), [lone], OFF)[0].tier, 5)


class TierFourTests(SinglePerson):
    def test_distance_eight_passes_and_nine_fails(self):
        phone = self.world.item(self.jo, width=1)
        passing = self.decide(self.world.candidate(phash=flip(PHASH, 8)))
        self.assertEqual((passing.tier, passing.action, passing.matched_item_id), (4, "review", phone.id))
        self.assertIn("distance 8", passing.reason)
        self.assertEqual(self.decide(self.world.candidate(phash=flip(PHASH, 9))).tier, 5)

    def test_two_seconds_pass_and_three_fail(self):
        self.world.item(self.jo)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT + 2)).tier, 4)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT - 2)).tier, 4)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=MOMENT + 3)).tier, 5)

    def test_wall_clock_seconds_count_when_utc_is_missing(self):
        self.world.item(self.jo, captured_utc=None, width=1)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local="2023-11-14T22:13:22")).tier, 4)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local="2023-11-14T22:13:23")).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local="garbage")).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(captured_utc=None, captured_local=None)).tier, 5)

    def test_a_missing_or_unreadable_phash_never_matches(self):
        self.world.item(self.jo, width=1)
        self.assertEqual(self.decide(self.world.candidate(phash=None)).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(phash="zz" * 8)).tier, 5)
        self.assertEqual(self.decide(self.world.candidate(phash="f0f0")).tier, 5)

    def test_the_nearest_phone_item_is_named(self):
        self.world.item(self.jo, width=1, phash=flip(PHASH, 5))
        near = self.world.item(self.jo, width=1, phash=flip(PHASH, 2))
        self.assertEqual(self.decide(self.world.candidate()).matched_item_id, near.id)

    def test_review_belongs_to_the_owner_of_the_near_item(self):
        sam = self.world.person("sam")
        shared = self.world.account("family@x.test", self.jo, (sam,))
        near = self.world.item(sam, width=1)
        decision = dedupe.decide(self.world.index(shared), self.world.candidate(), OFF)
        self.assertEqual((decision.tier, decision.person_id, decision.matched_item_id), (4, sam, near.id))


class TierFiveTests(SinglePerson):
    def test_nothing_alike_is_google_only_for_the_default_person(self):
        decision = self.decide(self.world.candidate())
        self.assertEqual((decision.tier, decision.action, decision.person_id, decision.matched_item_id), (5, "google_only", self.jo, None))
        self.assertEqual(decision.reason, "no match")


class SharedPixelTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.jo, self.sam, self.kim = (self.world.person(slug) for slug in ("jo", "sam", "kim"))
        self.account = self.world.account("family@x.test", self.jo, (self.sam, self.kim))
        self.export = self.world.export(self.account)

    def test_a_match_goes_to_the_uploaders_item_and_google_only_to_the_default_person(self):
        self.world.item(self.jo, captured_utc=MOMENT - 600, phash=None)
        kims = self.world.item(self.kim)
        sams = self.world.item(self.sam, captured_utc=MOMENT + 600, phash=None)
        items = [self.world.candidate(), self.world.candidate(sha1=sams.sha1, captured_utc=MOMENT + 600, phash=None), self.world.candidate(captured_utc=MOMENT + 9000, phash=None)]
        decided = dedupe.decide_all(self.world.index(self.account), items, OFF)
        self.assertEqual([(d.tier, d.person_id, d.matched_item_id) for d in decided], [(2, self.kim, kims.id), (1, self.sam, sams.id), (5, self.jo, None)])
        for decision in decided:
            dedupe.apply(self.world.opened, decision, self.export, 5.0)
        self.assertEqual(self.world.confirmed(), {kims.id, sams.id})

    def test_the_index_holds_every_served_person_and_nobody_else(self):
        outsider = self.world.person("lee")
        self.world.item(outsider)
        index = self.world.index(self.account)
        self.assertEqual(index.person_ids, {self.jo, self.sam, self.kim})
        self.assertEqual(index.default_person_id, self.jo)
        self.assertEqual(index.phone, ())


class OverlappingAccountsTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.jo, self.sam, self.kim = (self.world.person(slug) for slug in ("jo", "sam", "kim"))
        self.a = self.world.account("a@x.test", self.jo, (self.sam,))
        self.b = self.world.account("b@x.test", self.kim, (self.sam,))

    def test_neither_account_matches_the_others_people(self):
        jos, kims = self.world.item(self.jo, phash=None), self.world.item(self.kim, captured_utc=MOMENT + 9000, phash=None)
        from_a = dedupe.decide(self.world.index(self.a), self.world.candidate(sha1=kims.sha1, phash=None, captured_utc=MOMENT + 9000), OFF)
        from_b = dedupe.decide(self.world.index(self.b), self.world.candidate(sha1=jos.sha1, phash=None), OFF)
        self.assertEqual((from_a.tier, from_a.person_id), (5, self.jo))
        self.assertEqual((from_b.tier, from_b.person_id), (5, self.kim))

    def test_takeout_items_placed_for_one_account_are_not_matched_by_the_other(self):
        placed = self.world.item(self.sam, source="takeout", export_id=self.world.export(self.b).id, phash=None)
        self.assertEqual(self.world.index(self.a).takeout, ())
        decision = dedupe.decide(self.world.index(self.a), self.world.candidate(sha1=placed.sha1, phash=None), OFF)
        self.assertEqual((decision.tier, decision.person_id), (5, self.jo))
        again = dedupe.decide(self.world.index(self.b), self.world.candidate(sha1=placed.sha1, phash=None), OFF)
        self.assertEqual((again.tier, again.matched_item_id), (1, placed.id))

    def test_a_person_both_accounts_serve_is_matched_by_each(self):
        sams = self.world.item(self.sam)
        for account in (self.a, self.b):
            decision = dedupe.decide(self.world.index(account), self.world.candidate(sha1=sams.sha1), OFF)
            self.assertEqual((decision.tier, decision.account_id, decision.matched_item_id), (1, account, sams.id))


class PureIndexTests(unittest.TestCase):
    def test_decide_ignores_items_of_people_outside_the_index_even_when_handed_them(self):
        outsider = dedupe.Shot(7, 99, "phone", "a" * 40, PHASH, MOMENT, LOCAL, 4032, 3024, None, "Apple", "iPhone 15 Pro", False)
        placed = dedupe.Shot(8, 99, "takeout", "b" * 40, PHASH, MOMENT, LOCAL, 4032, 3024, None, "Apple", "iPhone 15 Pro", False)
        index = dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=(outsider,), takeout=(placed,))
        world = World(self)
        for sha1 in ("a" * 40, "b" * 40, "c" * 40):
            decision = dedupe.decide(index, world.candidate(sha1=sha1), OFF)
            self.assertEqual((decision.tier, decision.person_id, decision.matched_item_id), (5, 1, None))


class LoadTests(unittest.TestCase):
    def test_an_unknown_account_cannot_be_loaded(self):
        world = World(self)
        with self.assertRaises(LookupError):
            world.index(99)

    def test_the_setting_defaults_to_skipping_edited_copies(self):
        world = World(self)
        self.assertFalse(dedupe.load_settings(world.opened).keep_edited_copies)
        for value, expected in (("true", True), (" Yes ", True), ("1", True), ("0", False), ("false", False), ("maybe", False), (None, False)):
            world.opened.merge(Setting(key=KEEP_EDITED_COPIES, value=value))
            world.opened.flush()
            self.assertEqual(dedupe.load_settings(world.opened).keep_edited_copies, expected, value)

    def test_a_takeout_item_becomes_a_candidate_relative_to_the_export(self):
        root = Path("/staging/export-1")
        fields = MediaItem(sha1="a" * 40, phash=PHASH, captured_utc=MOMENT, captured_local=LOCAL, width=10, height=20, duration_ms=None, make="Apple", model="X")
        edited = TakeoutItem(path=root / "Photos from 2023" / "IMG_1-edited.jpg", sha1="a" * 40, copies=(), sidecar=None, edited_of=root / "Photos from 2023" / "IMG_1.jpg")
        made = dedupe.candidate_of(edited, fields, root)
        self.assertEqual((made.source_path, made.edited_of, made.is_edited, made.is_video), ("Photos from 2023/IMG_1-edited.jpg", "Photos from 2023/IMG_1.jpg", True, False))
        self.assertEqual((made.width, made.height, made.make, made.model, made.captured_utc), (10, 20, "Apple", "X", MOMENT))
        clip = dedupe.candidate_of(TakeoutItem(path=root / "a" / "VID_2.MP4", sha1="b", copies=(), sidecar=None), fields, root)
        self.assertEqual((clip.source_path, clip.edited_of, clip.is_edited, clip.is_video), ("a/VID_2.MP4", None, False, True))


if __name__ == "__main__":
    unittest.main()
