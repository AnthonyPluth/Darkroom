from unittest import mock

from sqlalchemy import select

from darkroom.domain import retention_gate, review
from darkroom.domain.ingest import cleanup, finish_export, part_free
from darkroom.storage import db, paths
from darkroom.storage.models import DedupeDecision, Event, ReviewItem
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_managed_ingest import KEY
from tests.test_managed_release import RELEASE_AT, ReleaseFixture
from tests.test_part_free import swap
from tests.test_part_ingest import Crash

NAME = "IMG_1005.JPG"
NEAR = {"offset": "+02:00", "make": "Apple", "model": "iPhone 11", "colour": (90, 90, 200)}


class HoldReleaseFixture(ReleaseFixture):
    def setUp(self):
        def build(builder):
            self.world.original("jo", NAME, self.world.scratch_jpeg("t4a.jpg", taken="2019:08:01 10:00:00", **NEAR))
            builder.photo(NAME, data=self.world.scratch_jpeg("t4b.jpg", taken="2019:08:01 10:00:01", **NEAR), gps=None, part=0)

        self.imported(build)
        self.review_id = self.world.opened.execute(select(ReviewItem.id)).scalar_one()
        self.entry = self.world.entry(f"Photos from 2019/{NAME}")
        self.assertEqual((self.entry.outcome, self.world.opened.get(ReviewItem, self.review_id).status), ("review", "pending"))

    def item(self):
        self.world.opened.expire_all()
        return self.world.opened.get(ReviewItem, self.review_id)

    def hold_file(self):
        return self.world.root / self.item().hold_path

    def resolve(self, at=NOW + 5):
        review.resolve(self.world.opened, self.world.photos, self.review_id, "keep_both", at, review.FileEffects())
        self.world.opened.commit()
        finish_export.finish_ready(self.world.opened, self.world.settings, at + 5, self.world.tools)
        self.assertEqual(self.item().status, "kept_both")

    def free_the_zip(self):
        with mock.patch.object(part_free, "release_holds", return_value=[]):
            self.assertEqual(self.tick().parts, 1)
        self.assertFalse(self.zip.exists())

    def journal(self):
        found = self.item()
        return found.hold_free_state, found.hold_release_point_id, found.hold_path

    def hold_reasons(self):
        return list(self.world.opened.scalars(select(Event.detail).where(Event.type == part_free.HOLD_KEPT).order_by(Event.id)))


class ReviewHoldReleaseTests(HoldReleaseFixture):
    def test_a_resolved_hold_goes_after_its_zip_only_with_every_condition_and_is_recorded_under_the_point(self):
        self.resolve()
        hold = self.hold_file()
        recovery = self.authorize()
        start = len(self.world.fake.requests())
        with no_leaks(self, NAME, KEY, str(hold)):
            freed = self.tick()
        self.assertEqual((freed.parts, freed.holds, freed.failed), (1, 1, 0), self.reasons() + self.hold_reasons())
        self.assertFalse(hold.exists())
        self.assertFalse(self.zip.exists())
        self.assertEqual(self.journal(), ("freed", recovery, None))
        self.assertEqual(self.world.opened.scalars(select(Event.detail).where(Event.type == "export_hold_freed")).all(), [f"review {self.review_id}"])
        self.assertEqual({method for method, _ in self.world.fake.requests()[start:]}, {"GET"})
        self.assertEqual(self.world.fake.state.assets[0].trashed, False)

    def test_the_hold_waits_for_a_zip_that_still_needs_it_and_goes_when_the_zip_has(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        with mock.patch.object(part_free, "free_part", return_value=False):
            kept = self.tick()
        self.assertEqual((kept.parts, kept.holds, kept.kept), (0, 0, 1))
        self.assertTrue(hold.is_file() and self.zip.is_file())
        self.assertEqual(self.hold_reasons(), [f"review {self.review_id}: zip_needs_hold"])
        self.assertEqual(self.journal()[:2], (None, None))
        self.assertEqual(self.tick().holds, 1)
        self.assertFalse(hold.exists())

    def test_the_hold_waits_for_a_held_copy_that_still_depends_on_it(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        self.entry.held_path = "_inbox/jo/held/export-1/99.jpg"
        self.world.opened.commit()
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
        self.assertIn(f"review {self.review_id}: zip_needs_hold", self.hold_reasons())
        self.entry.held_path = None
        self.world.opened.commit()
        self.assertEqual(self.tick().holds, 1)

    def test_a_pending_hold_is_never_a_candidate(self):
        hold = self.hold_file()
        self.authorize()
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
        self.assertEqual(self.item().status, "pending")
        self.assertEqual(self.hold_reasons(), [])

    def test_a_hold_resolved_less_than_14_days_ago_is_not_a_candidate_even_when_the_export_and_a_point_pass(self):
        self.resolve(at=NOW + 2 * DAY)
        hold = self.hold_file()
        self.authorize()
        kept = self.tick(at=RELEASE_AT)
        self.assertEqual((kept.holds, kept.kept), (0, 0))
        self.assertTrue(hold.is_file())
        self.assertEqual(self.hold_reasons(), [])
        self.assertEqual(self.journal()[:2], (None, None))
        self.backup(ahead=2 * DAY)
        self.assertEqual(self.tick(at=RELEASE_AT + 2 * DAY).holds, 1)
        self.assertFalse(hold.exists())

    def test_the_setting_off_keeps_the_hold_and_makes_no_immich_request(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        start = len(self.world.fake.requests())
        self.assertEqual(self.tick(enabled=False).holds, 0)
        self.assertTrue(hold.is_file())
        self.assertEqual(self.world.fake.requests()[start:], [])
        self.assertEqual(self.journal()[:2], (None, None))
        self.assertIn(f"review {self.review_id}: release_off", self.hold_reasons())

    def test_no_rehearsal_boundary_or_backup_keeps_the_hold(self):
        self.resolve()
        hold = self.hold_file()
        self.assertEqual(self.tick().holds, 0)
        self.assertIn(f"review {self.review_id}: no_rehearsal", self.hold_reasons())
        recovery = self.authorize()
        with db.session(self.world.engine, writing=True) as opened:
            retention_gate.withdraw(opened, RELEASE_AT, "boundary")
        self.assertEqual(self.tick().holds, 0)
        self.assertIn(f"review {self.review_id}: boundary_not_accepted", self.hold_reasons())
        self.assertTrue(hold.is_file())
        self.assertIsNotNone(recovery)

    def test_a_failed_latest_backup_or_no_point_keeps_the_hold(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.backup(healthy=False, ahead=45)
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
        self.assertIn(f"review {self.review_id}: backup_failed", self.hold_reasons())

    def test_a_receipt_that_is_not_committed_keeps_the_hold(self):
        review.resolve(self.world.opened, self.world.photos, self.review_id, "keep_both", NOW + 5, review.FileEffects())
        self.world.opened.commit()
        hold = self.hold_file()
        self.authorize()
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
        self.assertIn(f"review {self.review_id}: receipt_not_committed", self.hold_reasons())

    def test_a_fresh_refusal_from_immich_keeps_the_hold_and_clears_the_journal(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        asset = next(found for found in self.world.fake.state.assets if found.name == NAME)
        for field, wrong in (("trashed", True), ("offline", True), ("library_id", "external"), ("owner_id", "elsewhere")):
            with self.subTest(field=field), mock.patch.object(asset, field, wrong):
                kept = self.tick()
                self.assertEqual(kept.holds, 0)
                self.assertTrue(hold.is_file())
                self.assertEqual(self.journal()[:2], (None, None))
        self.assertIn(f"review {self.review_id}: immich_unconfirmed", self.hold_reasons())

    def test_an_outage_keeps_the_hold_and_stops_the_persons_releases(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.world.fake.mode("500")
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file() and self.zip.is_file())
        self.assertEqual(self.journal()[:2], (None, None))

    def test_a_hold_whose_bytes_changed_is_kept(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        swap(hold, b"a different file")
        self.assertEqual(self.tick().holds, 0)
        self.assertEqual(hold.read_bytes(), b"a different file")
        self.assertIn(f"review {self.review_id}: hash_differs", self.hold_reasons())

    def test_a_hold_swapped_after_immich_answers_survives_and_the_journal_is_cleared(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        read = retention_gate.confirm_release

        def read_then_swap(*args):
            found = read(*args)
            swap(hold, b"unrelated generated file")
            return found

        with mock.patch.object(retention_gate, "confirm_release", side_effect=read_then_swap):
            self.assertEqual(self.tick().holds, 0)
        self.assertEqual(hold.read_bytes(), b"unrelated generated file")
        self.assertEqual(self.journal()[:2], (None, None))
        self.assertIn(f"review {self.review_id}: changed", self.hold_reasons())

    def test_a_hard_link_or_a_link_at_the_hold_keeps_it(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        twin = hold.with_name("twin.JPG")
        twin.hardlink_to(hold)
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
        twin.unlink()
        self.assertEqual(self.tick().holds, 1)

    def test_a_hold_shared_with_another_review_keeps_it(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        decision = self.world.opened.get(DedupeDecision, self.item().decision_id)
        copied = {column.name: getattr(decision, column.name) for column in DedupeDecision.__table__.columns if column.name != "id"}
        other = DedupeDecision(**(copied | {"source_path": "Photos from 2019/other.JPG"}))
        self.world.opened.add(other)
        self.world.opened.flush()
        self.world.opened.add(ReviewItem(decision_id=other.id, status="pending", hold_path=self.item().hold_path, hold_sha1=self.item().hold_sha1))
        self.world.opened.commit()
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
        self.assertIn(f"review {self.review_id}: hold_shared", self.hold_reasons())

    def test_a_crash_before_the_unlink_requires_fresh_custody_when_retried(self):
        self.resolve()
        hold = self.hold_file()
        recovery = self.authorize()
        self.free_the_zip()
        with mock.patch.object(paths, "remove_review_hold", side_effect=Crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        self.assertEqual(self.journal()[:2], ("freeing", recovery))
        next(found for found in self.world.fake.state.assets if found.name == NAME).trashed = True
        self.assertEqual(self.tick().holds, 0)
        self.assertEqual(self.journal()[:2], (None, None))
        self.assertTrue(hold.is_file())

    def test_a_crash_after_the_unlink_is_recorded_freed_only_from_a_folder_that_confirms_the_absence(self):
        self.resolve()
        hold = self.hold_file()
        recovery = self.authorize()
        self.free_the_zip()
        delete = paths.remove_review_hold

        def remove_then_crash(*args):
            delete(*args)
            raise Crash()

        with mock.patch.object(paths, "remove_review_hold", side_effect=remove_then_crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        self.assertEqual(self.journal()[:2], ("freeing", recovery))
        self.assertFalse(hold.exists())
        with mock.patch.object(paths, "inbox_entry_absent", side_effect=FileNotFoundError):
            unread = self.tick(enabled=False)
        self.assertEqual((unread.holds, unread.failed), (0, 1))
        self.assertEqual(self.world.opened.scalars(select(Event.detail).where(Event.type == "export_hold_free_failed")).all(),
                         [f"review {self.review_id}: FileNotFoundError"])
        self.assertEqual(self.journal()[:2], ("freeing", recovery))
        self.assertIsNotNone(self.journal()[2])
        self.assertEqual(self.tick(enabled=False).holds, 1)
        self.assertEqual(self.journal(), ("freed", recovery, None))

    def test_a_crash_after_the_unlink_finishes_on_the_next_run_without_a_new_immich_read(self):
        self.resolve()
        recovery = self.authorize()
        self.free_the_zip()
        delete = paths.remove_review_hold

        def remove_then_crash(*args):
            delete(*args)
            raise Crash()

        with mock.patch.object(paths, "remove_review_hold", side_effect=remove_then_crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        start = len(self.world.fake.requests())
        self.assertEqual(self.tick(enabled=False).holds, 1)
        self.assertEqual(self.journal(), ("freed", recovery, None))
        self.assertEqual(self.world.fake.requests()[start:], [])

    def test_a_claim_left_by_a_crash_is_put_back_before_the_next_attempt(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        real = paths.rename_no_replace

        def claim_then_crash(source, target, folder, into=None):
            real(source, target, folder, into)
            raise Crash()

        with mock.patch.object(paths, "rename_no_replace", side_effect=claim_then_crash), self.assertRaises(Crash):
            self.tick()
        self.world.opened.rollback()
        self.assertFalse(hold.exists())
        self.assertEqual(self.tick(enabled=False).holds, 0)
        self.assertEqual(hold.read_bytes()[:2], b"\xff\xd8")
        self.assertEqual(self.tick().holds, 1)
        self.assertFalse(hold.exists())

    def test_the_legacy_cleanup_still_keeps_a_managed_hold_after_14_days(self):
        self.resolve()
        hold = self.hold_file()
        self.assertEqual(cleanup.clean(self.world.opened, self.world.photos, NOW + 30 * DAY, self.world.settings).holds, 0)
        self.assertTrue(hold.is_file())

    def test_a_reversed_decision_keeps_the_hold(self):
        self.resolve()
        hold = self.hold_file()
        self.authorize()
        self.free_the_zip()
        decision = self.world.opened.get(DedupeDecision, self.item().decision_id)
        decision.reversed_at = NOW + 20
        self.world.opened.commit()
        self.assertEqual(self.tick().holds, 0)
        self.assertTrue(hold.is_file())
