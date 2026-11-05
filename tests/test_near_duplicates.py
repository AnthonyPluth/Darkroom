import ast
import math
import random
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darkroom.domain import media, near_duplicates
from darkroom.domain.near_duplicates import Frame, Thresholds
from darkroom.storage.models import DedupeDecision, MediaItem, ReviewItem
from tests.fixtures import media as fixtures
from tests.shared import add_person, own_database

SOURCE = Path(near_duplicates.__file__)
WIDE = Thresholds(phash=64, dhash=64, burst_phash=64, burst_dhash=64)


def frame(item_id, phash=0, dhash=0, **times):
    return Frame(item_id, phash, dhash, **times)


def bits(count, offset=0):
    return ((1 << count) - 1) << offset


def naive(frames, thresholds):
    anchors, grouped = [], {}
    for each in sorted(frames, key=lambda f: f.id):
        found = [(sum(close), anchor.id, close) for anchor in anchors if (close := near_duplicates.related(each, anchor, thresholds))]
        if not found:
            anchors.append(each)
            grouped[each.id] = [near_duplicates.Member(each.id, 0, 0)]
        else:
            _, anchor_id, close = min(found, key=lambda entry: entry[:2])
            grouped[anchor_id].append(near_duplicates.Member(each.id, *close))
    return [near_duplicates.Group(anchor, tuple(members)) for anchor, members in grouped.items() if len(members) > 1]


class ThresholdTests(unittest.TestCase):
    def test_the_defaults_are_valid_and_a_burst_is_looser_than_a_pair(self):
        defaults = Thresholds()
        self.assertEqual((defaults.phash, defaults.dhash), (6, 8))
        self.assertEqual((defaults.burst_phash, defaults.burst_dhash, defaults.burst_seconds), (10, 14, 2.0))

    def test_a_distance_outside_the_hash_is_refused(self):
        for bad in ({"phash": -1}, {"dhash": 65}, {"burst_phash": 65}, {"burst_dhash": -1}):
            with self.assertRaises(ValueError):
                Thresholds(**bad)

    def test_a_burst_may_not_be_stricter_than_a_pair(self):
        with self.assertRaises(ValueError):
            Thresholds(phash=8, burst_phash=7)
        with self.assertRaises(ValueError):
            Thresholds(dhash=9, burst_dhash=8)

    def test_a_burst_lasts_a_finite_number_of_seconds(self):
        for bad in (-1.0, math.nan, math.inf):
            with self.assertRaises(ValueError):
                Thresholds(burst_seconds=bad)


class FrameTests(unittest.TestCase):
    def test_two_sixteen_digit_hashes_make_a_frame(self):
        made = near_duplicates.frame_of(4, "f0f0f0f0f0f0f0f0", "00000000000000ff", 12.5, "2023-11-14T22:13:20")
        self.assertEqual(made, Frame(4, 0xF0F0F0F0F0F0F0F0, 0xFF, 12.5, "2023-11-14T22:13:20"))

    def test_a_missing_short_long_or_non_hex_hash_makes_none(self):
        good = "f0f0f0f0f0f0f0f0"
        for phash, dhash in ((None, good), (good, None), ("", good), (good, good[:-1]), (good + "0", good), ("zz" * 8, good), (good, "g" * 16)):
            self.assertIsNone(near_duplicates.frame_of(1, phash, dhash))

    def test_distance_counts_differing_bits(self):
        self.assertEqual(near_duplicates.distance(0, bits(5)), 5)
        self.assertEqual(near_duplicates.distance(bits(64), 0), 64)


class PhotosThatLookAlike(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.count = 0

    def frame_of(self, image, **times):
        self.count += 1
        hashes = media.perceptual_hashes(fixtures.save_jpeg(image, self.folder / f"{self.count}.jpg"))
        made = near_duplicates.frame_of(self.count, hashes.phash, hashes.dhash, **times)
        assert made is not None
        return made

    def test_a_recompressed_a_resized_and_a_brightened_copy_join_the_original(self):
        for seed in (1, 2, 3, 4, 5, 6):
            self.count = 0
            image = fixtures.scene(seed)
            frames = [self.frame_of(each) for each in (image, fixtures.recompressed(image), fixtures.resized(image), fixtures.brightened(image))]
            found = near_duplicates.group(frames)
            self.assertEqual([group.ids for group in found], [(1, 2, 3, 4)], seed)
            self.assertEqual(found[0].anchor, 1)

    def test_different_scenes_never_group(self):
        frames = [self.frame_of(fixtures.scene(seed)) for seed in range(1, 31)]
        self.assertEqual(near_duplicates.group(frames), [])

    def test_a_group_lists_how_far_each_member_is_from_its_anchor(self):
        image = fixtures.scene(5)
        frames = [self.frame_of(image), self.frame_of(fixtures.recompressed(image))]
        (found,) = near_duplicates.group(frames)
        self.assertEqual(found.members[0], near_duplicates.Member(1, 0, 0))
        self.assertEqual(found.members[1], near_duplicates.Member(2, 2, 0))

    def test_a_shifted_shot_joins_only_a_burst(self):
        image = fixtures.scene(3)
        moment = 1_700_000_000.0
        original = self.frame_of(image, captured_utc=moment)
        for apart, joins in ((0.0, True), (1.5, True), (2.0, True), (2.5, False), (3600.0, False)):
            moved = self.frame_of(fixtures.shifted(image, 6, 3), captured_utc=moment + apart)
            self.assertEqual(len(near_duplicates.group([original, moved])), 1 if joins else 0, apart)

    def test_a_shifted_shot_without_a_time_is_not_a_burst(self):
        image = fixtures.scene(3)
        self.assertEqual(near_duplicates.group([self.frame_of(image), self.frame_of(fixtures.shifted(image, 6, 3))]), [])

    def test_a_burst_falls_back_to_the_wall_clock_when_there_is_no_instant(self):
        image = fixtures.scene(3)
        first = self.frame_of(image, captured_local="2023-11-14T22:13:20")
        second = self.frame_of(fixtures.shifted(image, 6, 3), captured_local="2023-11-14T22:13:21")
        self.assertEqual(len(near_duplicates.group([first, second])), 1)

    def test_a_shot_moved_far_is_not_a_burst_however_close_in_time(self):
        image = fixtures.scene(1)
        moment = 1_700_000_000.0
        frames = [self.frame_of(image, captured_utc=moment), self.frame_of(fixtures.shifted(image, 12, 6), captured_utc=moment)]
        self.assertEqual(near_duplicates.group(frames), [])


class GroupingRules(unittest.TestCase):
    def test_no_frames_and_a_single_frame_make_no_group(self):
        self.assertEqual(near_duplicates.group([]), [])
        self.assertEqual(near_duplicates.group([frame(1)]), [])

    def test_the_lowest_id_is_the_anchor_whatever_the_order(self):
        frames = [frame(7), frame(3, dhash=bits(2)), frame(5, phash=bits(1))]
        (found,) = near_duplicates.group(frames)
        self.assertEqual((found.anchor, found.ids), (3, (3, 5, 7)))
        self.assertEqual(found, near_duplicates.group(frames[::-1])[0])

    def test_a_repeated_id_counts_once(self):
        self.assertEqual(near_duplicates.group([frame(1), frame(1)]), [])

    def test_a_chain_does_not_stretch_a_group_past_its_anchor(self):
        first, second, third = frame(1), frame(2, phash=bits(5)), frame(3, phash=bits(5) | bits(5, 5))
        (found,) = near_duplicates.group([first, second, third])
        self.assertEqual(found.ids, (1, 2))

    def test_a_shot_joins_the_closer_of_two_anchors(self):
        far, near = frame(1), frame(2, phash=bits(12))
        joiner = frame(3, phash=bits(11))
        found = near_duplicates.group([far, near, joiner], Thresholds(phash=11, dhash=11, burst_phash=11, burst_dhash=11))
        self.assertEqual([group.ids for group in found], [(2, 3)])

    def test_a_tie_goes_to_the_anchor_with_the_lower_id(self):
        first, second = frame(1, phash=bits(3)), frame(2, phash=bits(3, 3))
        found = near_duplicates.group([first, second, frame(3)], Thresholds(phash=3, dhash=3, burst_phash=3, burst_dhash=3))
        self.assertEqual([group.ids for group in found], [(1, 3)])

    def test_both_hashes_must_agree(self):
        self.assertEqual(near_duplicates.group([frame(1), frame(2, phash=bits(1), dhash=bits(9))]), [])
        self.assertEqual(near_duplicates.group([frame(1), frame(2, phash=bits(7), dhash=bits(1))]), [])
        self.assertEqual(len(near_duplicates.group([frame(1), frame(2, phash=bits(6), dhash=bits(8))])), 1)

    def test_the_limits_are_inclusive(self):
        strict = Thresholds(phash=0, dhash=0, burst_phash=0, burst_dhash=0)
        self.assertEqual(len(near_duplicates.group([frame(1, 5, 9), frame(2, 5, 9)], strict)), 1)
        self.assertEqual(near_duplicates.group([frame(1, 5, 9), frame(2, 4, 9)], strict), [])

    def test_a_threshold_of_the_whole_hash_groups_everything(self):
        found = near_duplicates.group([frame(1), frame(2, bits(64), bits(64)), frame(3, bits(32), bits(32))], WIDE)
        self.assertEqual([group.ids for group in found], [(1, 2, 3)])

    def test_it_agrees_with_comparing_every_pair(self):
        chance = random.Random(2024)
        for thresholds in (Thresholds(), Thresholds(phash=0, dhash=0, burst_phash=0, burst_dhash=0), Thresholds(phash=63, dhash=40, burst_phash=63, burst_dhash=63), WIDE):
            centres = [chance.getrandbits(64) for _ in range(30)]
            frames = []
            for number in range(400):
                centre = chance.choice(centres)
                noise = sum(1 << chance.randrange(64) for _ in range(chance.randrange(0, 14)))
                other = sum(1 << chance.randrange(64) for _ in range(chance.randrange(0, 14)))
                frames.append(frame(number, centre ^ noise, chance.getrandbits(64) if number % 7 == 0 else centre ^ other, captured_utc=chance.choice([None, 100.0, 101.0, 105.0])))
            self.assertEqual(near_duplicates.group(frames, thresholds), naive(frames, thresholds), thresholds)


class SuggestingFromTheLibrary(unittest.TestCase):
    PHASH = "f0f0f0f0f0f0f0f0"
    DHASH = "0f0f0f0f0f0f0f0f"

    def setUp(self):
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.jo, self.sam = add_person(self.opened, "jo"), add_person(self.opened, "sam")
        self.number = 0

    def item(self, person_id, source="phone", phash=PHASH, dhash=DHASH, **fields):
        self.number += 1
        values = dict(person_id=person_id, source=source, path=f"p{person_id}/{self.number}.jpg", sha1=f"{self.number:040x}", bytes=1, phash=phash, dhash=dhash, indexed_at=1.0)
        values.update(fields)
        row = MediaItem(**values)
        self.opened.add(row)
        self.opened.flush()
        return row

    def test_a_phone_original_and_its_takeout_copy_are_suggested_together(self):
        phone, takeout = self.item(self.jo), self.item(self.jo, source="takeout", phash="f0f0f0f0f0f0f0f1")
        (found,) = near_duplicates.suggest(self.opened, self.jo)
        self.assertEqual((found.anchor, found.ids), (phone.id, (phone.id, takeout.id)))
        self.assertEqual(found.members[1].phash_distance, 1)

    def test_a_person_is_never_grouped_with_someone_elses_photo(self):
        self.item(self.jo)
        self.item(self.sam)
        self.assertEqual(near_duplicates.suggest(self.opened, self.jo), [])
        self.assertEqual(near_duplicates.suggest(self.opened, self.sam), [])

    def test_an_item_without_both_valid_hashes_is_left_out(self):
        self.item(self.jo)
        for phash, dhash in ((None, self.DHASH), (self.PHASH, None), ("short", self.DHASH), (self.PHASH, "z" * 16)):
            self.item(self.jo, phash=phash, dhash=dhash)
        self.assertEqual(near_duplicates.suggest(self.opened, self.jo), [])

    def test_the_times_of_the_items_decide_a_burst(self):
        a = self.item(self.jo, captured_utc=100.0)
        b = self.item(self.jo, phash="f0f0f0f0f0f0f00f", captured_utc=101.0)
        c = self.item(self.jo, phash="f0f0f0f0f0f0f00f", captured_utc=900.0)
        (found,) = near_duplicates.suggest(self.opened, self.jo)
        self.assertEqual(found.ids, (a.id, b.id))
        self.assertNotIn(c.id, found.ids)

    def test_a_looser_threshold_can_be_passed(self):
        a = self.item(self.jo)
        b = self.item(self.jo, phash="f0f0f0f0f0f0f00f")
        self.assertEqual(near_duplicates.suggest(self.opened, self.jo), [])
        loose = Thresholds(phash=10, dhash=10, burst_phash=10, burst_dhash=10)
        self.assertEqual([group.ids for group in near_duplicates.suggest(self.opened, self.jo, loose)], [(a.id, b.id)])

    def count(self, model):
        return self.opened.execute(select(func.count()).select_from(model)).scalar_one()

    def test_suggesting_changes_nothing(self):
        self.item(self.jo)
        self.item(self.jo, source="takeout")
        before = (self.count(MediaItem), self.count(DedupeDecision), self.count(ReviewItem))
        self.assertEqual(len(near_duplicates.suggest(self.opened, self.jo)), 1)
        self.assertEqual((self.count(MediaItem), self.count(DedupeDecision), self.count(ReviewItem)), before)
        self.assertFalse(self.opened.new or self.opened.dirty or self.opened.deleted)
        self.assertEqual(self.opened.execute(select(MediaItem.confirmed_in_google)).scalars().all(), [False, False])

    def test_the_module_only_reads_and_decides_nothing(self):
        tree = ast.parse(SOURCE.read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module for alias in node.names}
        self.assertFalse({name for name in imported if name.split(".")[-1] in {"os", "shutil", "subprocess", "eligibility", "review", "devices", "exports"}})
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(names & {"confirmed_in_google", "DedupeDecision", "ReviewItem", "Decision", "apply", "reverse", "decide", "delete", "flush", "commit"})
        from_sqlalchemy = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy" for alias in node.names}
        self.assertEqual(from_sqlalchemy, {"select"})
        on_the_session = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and isinstance(node.func.value, ast.Name) and node.func.value.id == "opened"}
        self.assertEqual(on_the_session, {"execute"})
        used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "dedupe"}
        self.assertEqual(used, {"seconds_apart"})
