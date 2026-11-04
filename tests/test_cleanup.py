import os
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain.ingest import cleanup
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, Export, ImportRun, ReviewItem
from tests.import_world import DAY, NOW, World, tree

LATER = NOW + 14 * DAY
TOO_SOON = NOW + 13 * DAY


class Imported(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export_id = self.world.hand_dropped().id
        self.world.run_import(self.export_id)
        self.root = self.world.root
        self.staging = self.root / "_inbox/jo/export-1"
        self.zips = sorted(self.world.photos.manual_inbox("jo").glob("*.zip"))
        self.hold = self.root / "_inbox/jo/review/export-1/IMG_1005.JPG"
        self.kept = self.kept_tree()

    def kept_tree(self):
        return {key: value for key, value in tree(self.root).items() if not key.startswith("_inbox/")}

    def clean(self, at=LATER):
        report = cleanup.clean(self.world.opened, self.world.photos, at)
        self.world.opened.commit()
        return report

    def review(self):
        return self.world.opened.execute(select(ReviewItem)).scalar_one()

    def resolve(self, at):
        review = self.review()
        review.status, review.resolved_at = "skipped", at
        self.world.opened.commit()
        return review


class StagingAndPartsTests(Imported):
    def test_staging_and_zip_parts_go_14_days_after_the_import(self):
        self.assertTrue(self.staging.is_dir())
        self.assertEqual(len(self.zips), 2)
        report = self.clean()
        self.assertEqual((report.staging, report.parts), (1, 2))
        self.assertFalse(self.staging.exists())
        self.assertFalse(any(part.exists() for part in self.zips))
        self.assertEqual(self.kept_tree(), self.kept)
        self.assertTrue(self.hold.is_file())
        self.assertEqual(self.world.opened.execute(select(Event).where(Event.type == "cleanup")).scalar_one().detail,
                         "1 staging folders, 2 zip parts, 0 review holds removed; 0 refused")

    def test_nothing_goes_before_14_days(self):
        report = self.clean(TOO_SOON)
        self.assertEqual((report.staging, report.parts, report.holds), (0, 0, 0))
        self.assertTrue(self.staging.is_dir())
        self.assertTrue(all(part.is_file() for part in self.zips))

    def test_an_export_that_is_not_imported_is_left_alone(self):
        for status in ("failed", "importing", "unpacking", "downloaded"):
            with self.subTest(status=status):
                self.world.opened.get(Export, self.export_id).status = status
                self.world.opened.commit()
                self.clean(LATER + 365 * DAY)
                self.assertTrue(self.staging.is_dir())
                self.assertTrue(all(part.is_file() for part in self.zips))

    def test_a_run_that_did_not_finish_does_not_start_the_clock(self):
        self.world.opened.execute(ImportRun.__table__.update().values(status="failed"))
        self.world.opened.commit()
        self.clean(LATER + 365 * DAY)
        self.assertTrue(self.staging.is_dir())

    def test_a_pending_review_without_a_held_copy_keeps_the_staging_folder(self):
        self.review().hold_path = None
        self.world.opened.commit()
        report = self.clean()
        self.assertEqual(report.kept, 1)
        self.assertTrue(self.staging.is_dir())
        self.assertTrue(all(part.is_file() for part in self.zips))

    def test_zip_parts_outside_the_photo_folder_are_never_removed(self):
        elsewhere = self.world.base / "elsewhere"
        elsewhere.mkdir()
        for part in self.zips:
            os.replace(part, elsewhere / part.name)
        self.world.opened.get(Export, self.export_id).inbox_dir = str(elsewhere)
        self.world.opened.commit()
        report = self.clean()
        self.assertEqual(report.parts, 0)
        self.assertEqual(len(list(elsewhere.glob("*.zip"))), 2)

    def test_a_staging_folder_that_is_a_link_to_originals_is_refused(self):
        self.resolve(NOW)
        os.rename(self.staging, self.world.base / "moved-staging")
        os.symlink(self.root / "jo/originals", self.staging)
        report = self.clean()
        self.assertEqual((report.staging, report.refused), (0, 1))
        self.assertEqual(self.kept_tree(), self.kept)

    def test_a_zip_part_that_points_into_originals_or_google_only_is_refused(self):
        self.resolve(NOW)
        export = self.world.opened.get(Export, self.export_id)
        for target in ("jo/originals", "jo/google-only/2017/07"):
            for part in self.zips:
                (self.root / target / part.name).write_bytes(b"a file that only shares the part's name")
        self.kept = self.kept_tree()
        for target in ("jo/originals", "jo/google-only/2017/07"):
            with self.subTest(target=target):
                export.inbox_dir = str(self.root / "_inbox/jo/manual/../../../" / target)
                self.world.opened.commit()
                report = self.clean()
                self.assertEqual((report.parts, report.refused), (0, 2))
                self.assertEqual(self.kept_tree(), self.kept)

    def test_a_run_with_items_that_could_not_be_imported_keeps_staging_and_parts(self):
        self.world.opened.execute(ImportRun.__table__.update().values(failed=2))
        self.world.opened.commit()
        for at in (LATER, LATER + DAY, LATER + 365 * DAY):
            report = self.clean(at)
            self.assertEqual((report.staging, report.parts, report.kept), (0, 0, 1))
        self.assertTrue(self.staging.is_dir())
        self.assertTrue(all(part.is_file() for part in self.zips))
        kept = list(self.world.opened.execute(select(Event).where(Event.type == "cleanup_kept")).scalars())
        self.assertEqual([(e.subject, e.detail) for e in kept], [("export 1", "staging and zip parts kept: 2 items could not be imported")])

    def test_a_pending_hold_that_is_missing_a_link_or_different_keeps_staging_and_parts(self):
        good = self.hold.read_bytes()
        for broken in ("missing", "link", "different", "elsewhere"):
            with self.subTest(hold=broken):
                review = self.review()
                review.hold_path = "_inbox/jo/review/export-1/IMG_1005.JPG"
                self.hold.unlink(missing_ok=True)
                if broken == "link":
                    os.symlink(self.staging / "staging/Takeout/Google Photos/Photos from 2019/IMG_1005.JPG", self.hold)
                elif broken == "different":
                    self.hold.write_bytes(b"not the held photo")
                elif broken == "elsewhere":
                    self.hold.write_bytes(good)
                    (self.root / "_inbox/jo/review/export-2").mkdir(exist_ok=True)
                    (self.root / "_inbox/jo/review/export-2/IMG_1005.JPG").write_bytes(good)
                    review.hold_path = "_inbox/jo/review/export-2/IMG_1005.JPG"
                self.world.opened.commit()
                report = self.clean()
                self.assertEqual((report.staging, report.parts, report.kept), (0, 0, 1))
                self.assertTrue(self.staging.is_dir())
                self.assertTrue(all(part.is_file() for part in self.zips))
        self.hold.unlink()
        self.hold.write_bytes(good)
        self.review().hold_path = "_inbox/jo/review/export-1/IMG_1005.JPG"
        self.world.opened.commit()
        self.assertEqual(self.clean().staging, 1)

    def test_a_filesystem_error_is_recorded_and_the_rest_of_cleanup_goes_on(self):
        self.resolve(NOW)
        with mock.patch.object(paths, "remove_inbox_tree", side_effect=PermissionError("/private/path")):
            report = self.clean(NOW + 100 * DAY)
        self.assertEqual((report.failed, report.holds), (1, 1))
        self.assertFalse(self.hold.exists())
        self.assertIsNone(self.review().hold_path)
        failed = list(self.world.opened.execute(select(Event).where(Event.type == "cleanup_failed")).scalars())
        self.assertEqual([(e.subject, e.detail) for e in failed], [("export 1", "PermissionError")])
        self.assertIn("1 failed", self.world.opened.execute(select(Event).where(Event.type == "cleanup")).scalar_one().detail)
        self.assertEqual(self.clean(NOW + 101 * DAY).staging, 1)

    def test_a_filesystem_error_on_a_hold_is_recorded_and_the_hold_kept(self):
        review = self.resolve(NOW)
        with mock.patch.object(paths, "remove_inbox_file", side_effect=OSError("/private/path")):
            report = self.clean(NOW + 100 * DAY)
        self.assertEqual((report.holds, report.failed), (0, 2))
        self.assertEqual(review.hold_path, "_inbox/jo/review/export-1/IMG_1005.JPG")
        self.assertTrue(self.hold.is_file())
        failed = [(e.subject, e.detail) for e in self.world.opened.execute(select(Event).where(Event.type == "cleanup_failed").order_by(Event.id)).scalars()]
        self.assertEqual(failed, [("export 1", "OSError"), (f"review {review.id}", "OSError")])

    def test_parts_left_after_staging_went_are_finished_on_the_next_run(self):
        with mock.patch.object(paths, "remove_inbox_file", side_effect=OSError("/private/path")):
            report = self.clean()
        self.assertEqual((report.staging, report.parts, report.failed), (1, 0, 1))
        self.assertFalse(self.staging.exists())
        self.assertTrue(all(part.is_file() for part in self.zips))
        report = self.clean(LATER + DAY)
        self.assertEqual((report.staging, report.parts, report.kept), (0, 2, 0))
        self.assertFalse(any(part.exists() for part in self.zips))
        self.assertTrue(self.hold.is_file())
        self.assertEqual(self.review().hold_path, "_inbox/jo/review/export-1/IMG_1005.JPG")
        self.assertEqual(len(list(self.world.opened.execute(select(Event).where(Event.type == cleanup.VERIFIED)).scalars())), 1)
        self.assertEqual(self.kept_tree(), self.kept)

    def test_parts_stay_when_a_pending_hold_went_bad_after_staging_was_removed(self):
        good = self.hold.read_bytes()
        with mock.patch.object(paths, "remove_inbox_file", side_effect=OSError("/private/path")):
            self.clean()
        self.assertFalse(self.staging.exists())
        for broken in ("missing", "link", "different"):
            with self.subTest(hold=broken):
                self.hold.unlink(missing_ok=True)
                if broken == "link":
                    other = self.root / "_inbox/jo/other.jpg"
                    other.write_bytes(good)
                    os.symlink(other, self.hold)
                elif broken == "different":
                    self.hold.write_bytes(b"not the held photo")
                report = self.clean(LATER + DAY)
                self.assertEqual((report.parts, report.kept), (0, 1))
                self.assertTrue(all(part.is_file() for part in self.zips))
        self.hold.unlink(missing_ok=True)
        self.hold.write_bytes(good)
        self.assertEqual(self.clean(LATER + 2 * DAY).parts, 2)

    def test_without_the_verification_record_parts_stay_once_staging_is_gone(self):
        import shutil

        shutil.rmtree(self.staging)
        report = self.clean(LATER + 365 * DAY)
        self.assertEqual((report.staging, report.parts, report.kept), (0, 0, 1))
        self.assertTrue(all(part.is_file() for part in self.zips))
        self.assertTrue(self.hold.is_file())
        self.assertEqual(list(self.world.opened.execute(select(Event).where(Event.type == cleanup.VERIFIED)).scalars()), [])

    def test_the_verification_record_is_saved_before_staging_is_removed(self):
        seen = []
        real = paths.remove_inbox_tree

        def removing(photos, path):
            with Session(self.world.engine) as fresh:
                seen.append(cleanup.holds_verified(fresh, fresh.get(Export, self.export_id)))
            return real(photos, path)

        with mock.patch.object(paths, "remove_inbox_tree", removing):
            self.clean()
        self.assertTrue(seen and all(seen))

    def test_a_second_cleanup_finds_nothing_more(self):
        self.clean()
        report = self.clean(LATER + DAY)
        self.assertEqual((report.staging, report.parts, report.holds, report.refused), (0, 0, 0, 0))
        self.assertEqual(self.kept_tree(), self.kept)


class ReviewHoldTests(Imported):
    def test_a_resolved_hold_goes_14_days_after_it_was_resolved(self):
        self.resolve(NOW + DAY)
        self.assertEqual(self.clean(NOW + 14 * DAY).holds, 0)
        self.assertTrue(self.hold.is_file())
        self.assertEqual(self.clean(NOW + 15 * DAY).holds, 1)
        self.assertFalse(self.hold.exists())
        self.assertIsNone(self.review().hold_path)

    def test_a_pending_hold_is_never_removed(self):
        report = self.clean(NOW + 1000 * DAY)
        self.assertEqual(report.holds, 0)
        self.assertTrue(self.hold.is_file())
        self.assertEqual(self.review().hold_path, "_inbox/jo/review/export-1/IMG_1005.JPG")

    def test_a_hold_file_shared_with_a_pending_review_is_kept(self):
        review = self.resolve(NOW)
        decision = self.world.opened.get(DedupeDecision, review.decision_id)
        twin = DedupeDecision(export_id=decision.export_id, account_id=decision.account_id, source_path="Takeout/twin.JPG", tier=4, reason="t",
                              decided_by="auto", decided_at=NOW)
        self.world.opened.add(twin)
        self.world.opened.flush()
        self.world.opened.add(ReviewItem(decision_id=twin.id, status="pending", hold_path=review.hold_path))
        self.world.opened.commit()
        report = self.clean(NOW + 100 * DAY)
        self.assertEqual((report.holds, report.kept, report.staging), (0, 2, 0))
        self.assertTrue(self.hold.is_file())

    def test_a_hold_path_into_originals_or_google_only_is_refused(self):
        review = self.resolve(NOW)
        for tampered in ("jo/originals/IMG_1001.JPG", "jo/google-only/2017/07/IMG_2001.JPG", "_inbox/jo/review/../../../jo/originals/IMG_1001.JPG",
                         "/etc/passwd", "_inbox/jo/export-1/staging/Takeout/Google Photos/Photos from 2019/IMG_1001.JPG"):
            with self.subTest(hold_path=tampered):
                review.hold_path = tampered
                self.world.opened.commit()
                report = self.clean(NOW + 100 * DAY)
                self.assertEqual(report.holds, 0)
                self.assertGreaterEqual(report.refused, 1)
                self.assertEqual(self.kept_tree(), self.kept)

    def test_a_hold_that_is_a_link_is_refused(self):
        self.resolve(NOW)
        self.hold.unlink()
        os.symlink(self.root / "jo/originals/IMG_1001.JPG", self.hold)
        report = self.clean(NOW + 100 * DAY)
        self.assertEqual((report.holds, report.refused), (0, 1))
        self.assertEqual(self.kept_tree(), self.kept)

    def test_a_hold_already_gone_is_forgotten(self):
        self.resolve(NOW)
        self.hold.unlink()
        self.assertEqual(self.clean(NOW + 100 * DAY).holds, 1)
        self.assertIsNone(self.review().hold_path)


class RemovableTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self, people=("jo",), pixel_people=())
        self.photos = self.world.photos
        self.root = self.world.root
        (self.root / "_inbox/jo/export-9/staging").mkdir(parents=True)
        (self.root / "jo/originals/a.jpg").write_bytes(b"original")
        (self.root / "jo/google-only/b.jpg").write_bytes(b"google")

    def test_only_paths_inside_a_persons_inbox_are_removable(self):
        self.assertEqual(self.photos.removable(self.root / "_inbox/jo/export-9"), self.root / "_inbox/jo/export-9")
        for refused in (self.root / "_inbox", self.root / "_inbox/jo", self.root / "jo/originals", self.root / "jo/originals/a.jpg",
                        self.root / "jo/google-only/b.jpg", self.root / "jo", self.root, self.world.base):
            with self.subTest(path=refused), self.assertRaises(paths.NotInTheInbox):
                self.photos.removable(refused)

    def test_the_removers_refuse_the_wrong_kind_and_skip_what_is_missing(self):
        self.assertFalse(paths.remove_inbox_tree(self.photos, self.root / "_inbox/jo/export-8"))
        self.assertFalse(paths.remove_inbox_file(self.photos, self.root / "_inbox/jo/nothing.zip"))
        with self.assertRaises(paths.NotInTheInbox):
            paths.remove_inbox_file(self.photos, self.root / "_inbox/jo/export-9")
        (self.root / "_inbox/jo/part.zip").write_bytes(b"zip")
        with self.assertRaises(paths.NotInTheInbox):
            paths.remove_inbox_tree(self.photos, self.root / "_inbox/jo/part.zip")
        with self.assertRaises(paths.NotInTheInbox):
            paths.remove_inbox_file(self.photos, self.root / "jo/originals/a.jpg")
        self.assertTrue(paths.remove_inbox_tree(self.photos, self.root / "_inbox/jo/export-9"))
        self.assertEqual((self.root / "jo/originals/a.jpg").read_bytes(), b"original")
        self.assertEqual((self.root / "jo/google-only/b.jpg").read_bytes(), b"google")

    def test_a_link_inside_a_removed_tree_is_removed_without_following_it(self):
        os.symlink(self.root / "jo/originals", self.root / "_inbox/jo/export-9/staging/originals-link")
        self.assertTrue(paths.remove_inbox_tree(self.photos, self.root / "_inbox/jo/export-9"))
        self.assertEqual((self.root / "jo/originals/a.jpg").read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
