import ast
import hashlib
import random
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from darkroom.domain import dedupe
from darkroom.storage.models import DedupeDecision, Event, MediaItem, ReviewItem
from tests.dedupe_world import MOMENT, PHASH, World, flip

OFF = dedupe.DedupeSettings()
SOURCE = Path(dedupe.__file__)


class Household(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.jo, self.sam = self.world.person("jo"), self.world.person("sam")
        self.account = self.world.account("family@x.test", self.jo, (self.sam,))
        self.export = self.world.export(self.account)

    def decide(self, candidate):
        return dedupe.decide(self.world.index(self.account), candidate, OFF)

    def apply(self, decision, export=None, now=10.0):
        return dedupe.apply(self.world.opened, decision, export or self.export, now)


class CandidateSetIsTheServedPeople(Household):
    def test_an_identical_file_of_a_served_person_is_matched(self):
        sams = self.world.item(self.sam)
        decision = self.decide(self.world.candidate(sha1=sams.sha1))
        self.assertEqual((decision.tier, decision.matched_item_id), (1, sams.id))

    def test_an_identical_file_of_a_person_the_account_does_not_serve_is_google_only_for_the_default_person(self):
        outsider = self.world.person("lee")
        theirs = self.world.item(outsider)
        decision = self.decide(self.world.candidate(sha1=theirs.sha1))
        self.assertEqual((decision.tier, decision.person_id, decision.matched_item_id), (5, self.jo, None))
        self.apply(decision)
        self.assertEqual(self.world.confirmed(), set())

    def test_apply_refuses_a_match_outside_the_served_set_and_writes_nothing(self):
        theirs = self.world.item(self.world.person("lee"))
        forged = dedupe.Decision("a.jpg", self.account, self.jo, 1, "skip", "same SHA-1", theirs.id, "phone")
        with self.assertRaises(ValueError):
            self.apply(forged)
        self.assertEqual((self.world.decisions(), self.world.confirmed()), ([], set()))

    def test_apply_refuses_to_assign_an_item_to_someone_the_account_does_not_serve(self):
        lee = self.world.person("lee")
        with self.assertRaises(ValueError):
            self.apply(dedupe.Decision("a.jpg", self.account, lee, 5, "google_only", "no match"))
        self.assertEqual(self.world.decisions(), [])

    def test_apply_refuses_a_decision_made_for_another_account(self):
        other = self.world.account("other@x.test", self.sam)
        with self.assertRaises(ValueError):
            self.apply(dedupe.Decision("a.jpg", other, self.sam, 5, "google_only", "no match"))
        self.assertEqual(self.world.decisions(), [])

    def test_apply_refuses_a_takeout_match_from_another_accounts_export(self):
        other = self.world.account("other@x.test", self.sam)
        placed = self.world.item(self.sam, source="takeout", export_id=self.world.export(other).id)
        orphan = self.world.item(self.sam, source="takeout", export_id=None)
        for item in (placed, orphan):
            with self.assertRaises(ValueError):
                self.apply(dedupe.Decision(f"{item.id}.jpg", self.account, self.sam, 1, "skip", "same SHA-1", item.id, "takeout"))
        self.assertEqual(self.world.decisions(), [])

    def test_apply_refuses_a_confirming_decision_that_assigns_the_item_to_someone_else(self):
        sams = self.world.item(self.sam)
        with self.assertRaises(ValueError):
            self.apply(dedupe.Decision("a.jpg", self.account, self.jo, 1, "skip", "same SHA-1", sams.id, "phone"))
        self.assertEqual((self.world.decisions(), self.world.confirmed()), ([], set()))

    def test_apply_refuses_a_missing_matched_item_and_a_tier_two_takeout_match(self):
        placed = self.world.item(self.sam, source="takeout", export_id=self.export.id)
        with self.assertRaises(ValueError):
            self.apply(dedupe.Decision("a.jpg", self.account, self.sam, 1, "skip", "same SHA-1", 999, "phone"))
        with self.assertRaises(ValueError):
            self.apply(dedupe.Decision("b.jpg", self.account, self.sam, 2, "skip", "same shot", placed.id, "phone"))
        self.assertEqual((self.world.decisions(), self.world.confirmed()), ([], set()))


class NothingIsDeletedMovedOrWritten(Household):
    def tree(self, root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_deciding_applying_and_reversing_only_record_decisions(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        for name in ("jo/originals/IMG_0001.HEIC", "jo/google-only/2023/11/a.jpg", "_inbox/jo/review/1/b.jpg", "staging/1/Takeout/c.jpg"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(name.encode())
        before = self.tree(root)
        phone = self.world.item(self.jo, path=str(root / "jo/originals/IMG_0001.HEIC"))
        self.world.item(self.sam, width=1)
        items = [self.world.candidate(sha1=phone.sha1), self.world.candidate(), self.world.candidate(phash=None, captured_utc=None)]
        rows = [self.apply(d) for d in dedupe.decide_all(self.world.index(self.account), items, OFF)]
        for row in rows:
            dedupe.reverse(self.world.opened, row.id, 20.0, "owner")
        self.assertEqual(self.tree(root), before)
        self.assertEqual(self.world.count(MediaItem), 2)
        self.assertEqual(len(self.world.decisions()), 3)

    def test_the_module_has_no_way_to_touch_a_file_or_delete_a_row(self):
        tree = ast.parse(SOURCE.read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imported & {"os", "shutil", "subprocess", "tempfile", "io"})
        forbidden = {"open", "unlink", "rename", "rmdir", "rmtree", "remove", "write_text", "write_bytes", "touch", "mkdir", "delete", "move", "copy", "copyfile", "chmod", "execute_delete"}
        calls = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "") for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertEqual(calls & forbidden, set())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertNotIn("delete", names)


class OnlyAutomaticTierOneAndTwoSetTheFlag(Household):
    def test_automatic_tier_one_and_two_set_the_flag_on_the_matched_phone_item(self):
        exact, shot = self.world.item(self.jo, captured_utc=MOMENT - 600), self.world.item(self.sam)
        for candidate in (self.world.candidate(sha1=exact.sha1, captured_utc=MOMENT - 600), self.world.candidate()):
            self.apply(self.decide(candidate))
        self.assertEqual([d.tier for d in self.world.decisions()], [1, 2])
        self.assertEqual(self.world.confirmed(), {exact.id, shot.id})

    def test_tiers_three_four_and_five_never_set_the_flag(self):
        phone = self.world.item(self.jo)
        near = self.world.item(self.sam, width=1, captured_utc=MOMENT + 500)
        original = self.world.candidate(sha1=phone.sha1)
        edited = self.world.candidate(is_edited=True, edited_of=original.source_path, sha1="e" * 40)
        review = self.world.candidate(captured_utc=MOMENT + 501, phash=flip(PHASH, 3))
        alone = self.world.candidate(captured_utc=MOMENT + 9000)
        decided = dedupe.decide_all(self.world.index(self.account), [original, edited, review, alone], OFF)
        self.assertEqual([d.tier for d in decided], [1, 3, 4, 5])
        for decision in decided[1:]:
            self.assertFalse(decision.confirms)
            self.apply(decision)
        self.assertEqual(self.world.confirmed(), set())
        self.assertEqual(self.world.opened.get(MediaItem, near.id).confirmed_in_google, False)

    def test_an_owner_made_tier_one_or_two_decision_never_sets_the_flag(self):
        phone = self.world.item(self.jo)
        for number, tier in enumerate((1, 2)):
            decision = dedupe.Decision(f"{number}.jpg", self.account, self.jo, tier, "skip", "owner resolved", phone.id, "phone", decided_by="owner")
            self.assertFalse(decision.confirms)
            self.apply(decision)
        self.assertEqual(self.world.confirmed(), set())
        self.assertEqual([d.decided_by for d in self.world.decisions()], ["owner", "owner"])

    def test_a_tier_one_match_on_a_placed_takeout_item_sets_no_flag(self):
        placed = self.world.item(self.jo, source="takeout", export_id=self.export.id)
        self.apply(self.decide(self.world.candidate(sha1=placed.sha1)))
        self.assertEqual(self.world.confirmed(), set())

    def test_a_decision_naming_a_takeout_source_never_flags_a_phone_item(self):
        phone = self.world.item(self.jo)
        self.apply(dedupe.Decision("a.jpg", self.account, self.jo, 1, "skip", "same SHA-1", phone.id, "takeout"))
        self.assertEqual(self.world.confirmed(), set())


class DecisionsAreRecordedAndReversible(Household):
    def test_a_decision_stores_the_export_account_tier_reason_and_who_decided(self):
        phone = self.world.item(self.sam)
        row = self.apply(self.decide(self.world.candidate()), now=42.0)
        stored = self.world.opened.get(DedupeDecision, row.id)
        self.assertEqual(
            (stored.export_id, stored.account_id, stored.tier, stored.reason, stored.decided_by, stored.decided_at, stored.matched_item_id, stored.person_id, stored.reversed_at),
            (self.export.id, self.account, 2, "same capture second, camera and size", "auto", 42.0, phone.id, self.sam, None),
        )
        self.assertTrue(stored.source_path.startswith("Takeout/"))

    def test_reversing_the_only_confirming_decision_clears_the_flag(self):
        phone = self.world.item(self.jo)
        row = self.apply(self.decide(self.world.candidate(sha1=phone.sha1)))
        self.assertEqual(self.world.confirmed(), {phone.id})
        reversed_ = dedupe.reverse(self.world.opened, row.id, 50.0, "owner")
        self.assertEqual(reversed_.reversed_at, 50.0)
        self.assertEqual(self.world.confirmed(), set())
        events = list(self.world.opened.execute(select(Event).where(Event.type == "dedupe_reversed")).scalars())
        self.assertEqual([(e.subject, e.detail) for e in events], [(f"decision {row.id}", "tier 1, by owner")])

    def test_reversing_one_of_two_confirming_decisions_keeps_the_flag(self):
        phone = self.world.item(self.jo)
        first = self.apply(self.decide(self.world.candidate(sha1=phone.sha1)))
        self.apply(self.decide(self.world.candidate()), export=self.world.export(self.account))
        dedupe.reverse(self.world.opened, first.id, 50.0, "owner")
        self.assertEqual(self.world.confirmed(), {phone.id})

    def test_an_owner_made_or_reversed_decision_does_not_keep_the_flag(self):
        phone = self.world.item(self.jo)
        confirming = self.apply(self.decide(self.world.candidate(sha1=phone.sha1)))
        self.apply(dedupe.Decision("owner.jpg", self.account, self.jo, 1, "skip", "owner resolved", phone.id, "phone", decided_by="owner"))
        other = self.apply(self.decide(self.world.candidate()), export=self.world.export(self.account))
        dedupe.reverse(self.world.opened, other.id, 40.0, "owner")
        self.assertEqual(self.world.confirmed(), {phone.id})
        dedupe.reverse(self.world.opened, confirming.id, 50.0, "auto")
        self.assertEqual(self.world.confirmed(), set())

    def test_reversing_a_review_decision_leaves_a_tier_one_flag_alone(self):
        phone = self.world.item(self.jo)
        self.apply(self.decide(self.world.candidate(sha1=phone.sha1)))
        review = self.apply(self.decide(self.world.candidate(width=1)))
        self.assertEqual(review.tier, 4)
        dedupe.reverse(self.world.opened, review.id, 50.0, "owner")
        self.assertEqual(self.world.confirmed(), {phone.id})

    def test_reversing_twice_keeps_the_first_time_and_one_event(self):
        phone = self.world.item(self.jo)
        row = self.apply(self.decide(self.world.candidate(sha1=phone.sha1)))
        dedupe.reverse(self.world.opened, row.id, 50.0, "owner")
        dedupe.reverse(self.world.opened, row.id, 60.0, "owner")
        self.assertEqual(self.world.opened.get(DedupeDecision, row.id).reversed_at, 50.0)
        self.assertEqual(self.world.count(Event), 1)
        self.assertEqual(self.world.confirmed(), set())

    def test_reapplying_a_reversed_decision_does_not_confirm_again(self):
        phone = self.world.item(self.jo)
        decision = self.decide(self.world.candidate(sha1=phone.sha1))
        row = self.apply(decision)
        dedupe.reverse(self.world.opened, row.id, 50.0, "owner")
        self.assertEqual(self.apply(decision).id, row.id)
        self.assertEqual(self.world.confirmed(), set())

    def test_reversing_a_google_only_decision_touches_no_item(self):
        row = self.apply(self.decide(self.world.candidate(phash=None)))
        self.assertEqual(dedupe.reverse(self.world.opened, row.id, 50.0, "owner").reversed_at, 50.0)
        self.assertEqual(self.world.confirmed(), set())

    def test_reversing_an_unknown_decision_fails(self):
        with self.assertRaises(LookupError):
            dedupe.reverse(self.world.opened, 404, 50.0, "owner")


class ApplyingTwiceYieldsOneDecision(Household):
    def test_the_same_item_applied_twice_is_one_decision_and_one_review(self):
        self.world.item(self.jo, width=1)
        decision = self.decide(self.world.candidate())
        first, second = self.apply(decision, now=10.0), self.apply(decision, now=20.0)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.world.decisions()), 1)
        self.assertEqual([(r.decision_id, r.status) for r in self.world.reviews()], [(first.id, "pending")])
        self.assertEqual(self.world.decisions()[0].decided_at, 10.0)

    def test_a_different_decision_for_the_same_item_does_not_replace_the_first(self):
        phone = self.world.item(self.jo, phash=None)
        google_only = self.apply(dedupe.Decision("a.jpg", self.account, self.jo, 5, "google_only", "no match"))
        again = self.apply(dedupe.Decision("a.jpg", self.account, self.jo, 1, "skip", "same SHA-1", phone.id, "phone"))
        self.assertEqual((again.id, again.tier), (google_only.id, 5))
        self.assertEqual(self.world.confirmed(), set())

    def test_the_same_path_in_another_export_is_its_own_decision(self):
        decision = self.decide(self.world.candidate(phash=None))
        self.apply(decision)
        self.apply(decision, export=self.world.export(self.account))
        self.assertEqual(len(self.world.decisions()), 2)

    def test_the_database_refuses_a_second_row_for_the_same_item(self):
        self.apply(self.decide(self.world.candidate(source_path="x.jpg", phash=None)))
        self.world.opened.add(DedupeDecision(export_id=self.export.id, account_id=self.account, source_path="x.jpg", tier=5, reason="no match", decided_by="auto", decided_at=1.0))
        with self.assertRaises(IntegrityError):
            self.world.opened.flush()
        self.world.opened.rollback()


class TierTwoNeedsExactlyOneCandidate(Household):
    def test_one_candidate_matches_and_confirms(self):
        only = self.world.item(self.sam)
        self.apply(self.decide(self.world.candidate()))
        self.assertEqual(self.world.confirmed(), {only.id})

    def test_two_burst_frames_go_to_review_and_set_no_flag(self):
        first, second = self.world.item(self.jo), self.world.item(self.sam)
        row = self.apply(self.decide(self.world.candidate()))
        self.assertEqual(row.tier, 4)
        self.assertEqual([r.status for r in self.world.reviews()], ["pending"])
        self.assertEqual(self.world.confirmed(), set())
        self.assertNotEqual(first.id, second.id)

    def test_the_same_burst_with_sub_seconds_matches_each_frame_to_its_own(self):
        first = self.world.item(self.jo, captured_utc=MOMENT + 0.31)
        second = self.world.item(self.jo, captured_utc=MOMENT + 0.62)
        frames = [self.world.candidate(captured_utc=MOMENT + 0.31), self.world.candidate(captured_utc=MOMENT + 0.62)]
        for decision in dedupe.decide_all(self.world.index(self.account), frames, OFF):
            self.apply(decision)
        self.assertEqual([(d.tier, d.matched_item_id) for d in self.world.decisions()], [(2, first.id), (2, second.id)])
        self.assertEqual(self.world.confirmed(), {first.id, second.id})

    def test_two_takeout_frames_for_one_phone_item_go_to_review(self):
        self.world.item(self.jo)
        for decision in dedupe.decide_all(self.world.index(self.account), [self.world.candidate(), self.world.candidate()], OFF):
            self.apply(decision)
        self.assertEqual([d.tier for d in self.world.decisions()], [4, 4])
        self.assertEqual(self.world.confirmed(), set())


class FlagImpliesAnUnreversedAutomaticMatch(unittest.TestCase):
    def test_random_households_never_flag_without_an_unreversed_automatic_tier_one_or_two(self):
        for seed in range(25):
            with self.subTest(seed=seed):
                self.run_household(random.Random(seed))

    def run_household(self, rng):
        world = World(self)
        people = [world.person(f"p{n}") for n in range(4)]
        accounts = [world.account(f"a{n}@x.test", people[n], tuple(rng.sample(people, rng.randint(0, 3)))) for n in range(3)]
        hashes = [f"{n:040x}" for n in range(1, 9)]
        phashes = [PHASH, flip(PHASH, 3), flip(PHASH, 8), flip(PHASH, 9), None]
        def fields():
            return dict(
                sha1=rng.choice(hashes), phash=rng.choice(phashes),
                captured_utc=rng.choice([MOMENT, MOMENT + 0.5, MOMENT + 1, MOMENT + 2, MOMENT + 3, None]),
                captured_local=rng.choice(["2023-11-14T22:13:20", "2023-11-14T22:13:21", None]),
                width=rng.choice([4032, 4031, None]), height=3024, make=rng.choice(["Apple", "apple", "", None]), model="iPhone 15 Pro",
            )
        for _ in range(12):
            world.item(rng.choice(people), **fields())
        settings = dedupe.DedupeSettings(keep_edited_copies=rng.random() < 0.5)
        rows = []
        for _ in range(4):
            account = rng.choice(accounts)
            export = world.export(account)
            items = [world.candidate(**fields()) for _ in range(6)]
            items += [world.candidate(is_edited=True, edited_of=rng.choice(items).source_path, **fields()) for _ in range(2)]
            for decision in dedupe.decide_all(world.index(account), items, settings):
                rows.append(dedupe.apply(world.opened, decision, export, 1.0))
                if rng.random() < 0.3:
                    dedupe.apply(world.opened, decision, export, 2.0)
            for row in rng.sample(rows, min(len(rows), 3)):
                dedupe.reverse(world.opened, row.id, 3.0, rng.choice(["owner", "auto"]))
        self.assert_flags_hold(world)

    def assert_flags_hold(self, world):
        for item_id in world.confirmed():
            item = world.opened.get(MediaItem, item_id)
            self.assertEqual(item.source, "phone")
            backing = [
                d for d in world.decisions()
                if d.matched_item_id == item_id and d.reversed_at is None and d.decided_by == "auto" and d.tier in (1, 2)
            ]
            self.assertTrue(backing, f"item {item_id} is flagged without a confirming decision")
            for decision in backing:
                self.assertIn(item.person_id, dedupe.accounts.served_people(world.opened, decision.account_id))
        keys = [(d.export_id, d.source_path) for d in world.decisions()]
        self.assertEqual(len(keys), len(set(keys)))
        reviews = {r.decision_id for r in world.opened.execute(select(ReviewItem)).scalars()}
        self.assertEqual(reviews, {d.id for d in world.decisions() if d.tier == 4})


if __name__ == "__main__":
    unittest.main()
