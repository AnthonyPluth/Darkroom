import os
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain.ingest import cleanup
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, Export, ImportRun, MediaItem, ReviewItem
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


class InterruptedRemovalTests(Imported):
    def leftover(self, name, data=None):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        path = folder / f"{paths.DROPPING}0123456789abcdef-{name}"
        path.write_bytes((folder / "IMG_2001.JPG").read_bytes() if data is None else data)
        return path

    def test_an_old_dropping_file_in_google_only_is_swept_and_counted(self):
        stale = self.leftover("IMG_9.JPG")
        before = {key: value for key, value in self.kept_tree().items() if paths.DROPPING not in key}
        report = self.clean(at=TOO_SOON)
        self.assertFalse(stale.exists())
        self.assertEqual(report.dropped, 1)
        self.assertEqual({key: value for key, value in self.kept_tree().items()}, before)
        details = [row.detail for row in self.world.opened.execute(select(Event).where(Event.type == "cleanup")).scalars()]
        self.assertTrue(any(detail.endswith("1 interrupted removals cleared") for detail in details))
        self.assertFalse(any("IMG_9" in detail for detail in details))

    def test_a_dropping_file_with_no_identical_copy_left_is_kept(self):
        for data in (b"the only copy of this photo", None):
            with self.subTest(identical_copy_is_a_library_row=data is None):
                stale = self.leftover("IMG_9.JPG", data)
                if data is None:
                    (self.root / "jo" / "google-only" / "2017" / "07" / "IMG_2001.JPG").unlink()
                report = self.clean(at=TOO_SOON)
                self.assertTrue(stale.is_file())
                self.assertEqual(report.dropped, 0)

    def test_a_dropping_file_whose_copy_is_a_pending_review_hold_is_swept(self):
        stale = self.leftover("IMG_1005.JPG", self.hold.read_bytes())
        self.assertEqual(self.clean(at=TOO_SOON).dropped, 1)
        self.assertFalse(stale.exists())

    def test_a_dropping_file_whose_only_copy_is_a_decided_pairs_hold_is_kept(self):
        review = self.review()
        review.status, review.resolved_at = "kept_both", TOO_SOON - DAY
        self.world.opened.commit()
        stale = self.leftover("IMG_1005.JPG", self.hold.read_bytes())
        self.assertEqual(self.clean(at=TOO_SOON).dropped, 0)
        self.assertTrue(stale.is_file())
        self.assertTrue(self.hold.is_file())

    def test_a_dropping_sidecar_goes_only_when_its_photo_is_gone_or_goes_too(self):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        for photo_data, expected in ((None, False), (b"the only copy of this photo", True), (b"", False)):
            with self.subTest(photo_data=photo_data):
                sidecar = folder / f"{paths.DROPPING}0123456789abcdef-IMG_9.JPG.xmp"
                sidecar.write_bytes(b"<x:xmpmeta>a sidecar whose delete failed</x:xmpmeta>")
                photo = folder / f"{paths.DROPPING}0123456789abcdef-IMG_9.JPG"
                if photo_data == b"":
                    photo.write_bytes((folder / "IMG_2001.JPG").read_bytes())
                elif photo_data is not None:
                    photo.write_bytes(photo_data)
                self.clean(at=TOO_SOON)
                self.assertEqual(sidecar.is_file(), expected)
                self.assertEqual(photo.is_file(), expected)
                sidecar.unlink(missing_ok=True)
                photo.unlink(missing_ok=True)

    def test_a_dropping_file_a_row_still_records_is_put_back_with_its_sidecar(self):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        photo, sidecar = folder / "IMG_2001.JPG", folder / "IMG_2001.JPG.xmp"
        data, xmp_data = photo.read_bytes(), sidecar.read_bytes()
        os.rename(photo, folder / f"{paths.DROPPING}0123456789abcdef-IMG_2001.JPG")
        os.rename(sidecar, folder / f"{paths.DROPPING}0123456789abcdef-IMG_2001.JPG.xmp")
        report = self.clean(at=TOO_SOON)
        self.assertEqual((photo.read_bytes(), sidecar.read_bytes()), (data, xmp_data))
        self.assertEqual(list(folder.glob(f"{paths.DROPPING}*")), [])
        self.assertEqual((report.restored, report.dropped), (1, 0))
        details = [row.detail for row in self.world.opened.execute(select(Event).where(Event.type == "cleanup")).scalars()]
        self.assertTrue(any(detail.endswith("1 interrupted removals put back") for detail in details))

    def set_aside(self, name="IMG_2001.JPG"):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        photo, sidecar = folder / name, folder / f"{name}.xmp"
        aside, sidecar_aside = folder / f"{paths.DROPPING}0123456789abcdef-{name}", folder / f"{paths.DROPPING}0123456789abcdef-{name}.xmp"
        data, xmp_data = photo.read_bytes(), sidecar.read_bytes()
        os.rename(photo, aside)
        os.rename(sidecar, sidecar_aside)
        return photo, sidecar, aside, sidecar_aside, data, xmp_data

    def test_a_put_back_never_replaces_what_is_at_the_original_path(self):
        outside = self.root.parent / "outside.JPG"
        outside.write_bytes(b"a file outside the photo folder")
        for kind in ("file", "link"):
            with self.subTest(kind=kind):
                photo, sidecar, aside, sidecar_aside, data, xmp_data = self.set_aside()
                if kind == "file":
                    photo.write_bytes(b"a different photo now at that path")
                else:
                    photo.symlink_to(outside)
                report = self.clean(at=TOO_SOON)
                self.assertEqual(report.restored, 0)
                if kind == "file":
                    self.assertEqual(photo.read_bytes(), b"a different photo now at that path")
                else:
                    self.assertEqual((photo.is_symlink(), outside.read_bytes()), (True, b"a file outside the photo folder"))
                self.assertEqual((aside.read_bytes(), sidecar_aside.read_bytes()), (data, xmp_data))
                self.assertFalse(sidecar.exists())
                photo.unlink()
                os.rename(aside, photo)
                os.rename(sidecar_aside, sidecar)

    def test_a_row_with_a_different_sha1_gets_no_put_back(self):
        photo, sidecar, aside, sidecar_aside, data, xmp_data = self.set_aside()
        row = self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%/IMG_2001.JPG"))).scalar_one()
        row.sha1 = "0" * 40
        self.world.opened.commit()
        report = self.clean(at=TOO_SOON)
        self.assertEqual((report.restored, report.dropped), (0, 0))
        self.assertFalse(photo.exists())
        self.assertFalse(sidecar.exists())
        self.assertEqual((aside.read_bytes(), sidecar_aside.read_bytes()), (data, xmp_data))

    def test_a_dropping_name_no_row_records_is_not_put_back(self):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        stale = self.leftover("IMG_77.JPG")
        report = self.clean(at=TOO_SOON)
        self.assertEqual((report.restored, report.dropped), (0, 1))
        self.assertFalse((folder / "IMG_77.JPG").exists())
        self.assertFalse(stale.exists())

    def test_a_sidecar_whose_photo_was_put_back_without_it_is_kept(self):
        photo, sidecar, aside, sidecar_aside, data, xmp_data = self.set_aside()
        sidecar.write_bytes(b"<x:xmpmeta>a sidecar written since</x:xmpmeta>")
        for _ in range(2):
            self.clean(at=TOO_SOON)
            self.assertEqual(photo.read_bytes(), data)
            self.assertEqual(sidecar.read_bytes(), b"<x:xmpmeta>a sidecar written since</x:xmpmeta>")
            self.assertEqual(sidecar_aside.read_bytes(), xmp_data)
            self.assertFalse(aside.exists())

    def sidecar_set_aside(self):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        photo, sidecar = folder / "IMG_2001.JPG", folder / "IMG_2001.JPG.xmp"
        xmp_data = sidecar.read_bytes()
        review = self.review()
        review.status, review.resolved_at = "kept_both", TOO_SOON - DAY
        self.world.opened.commit()
        paths.set_sidecar_aside(self.world.photos, str(photo.relative_to(self.root)), review.id, cleanup.undone_count(self.world.opened, review.id))
        (aside,) = folder.glob(f"{paths.DROPPING}*{paths.SIDECAR_SUFFIX}")
        self.assertTrue(paths.record_of(aside).is_file())
        return photo, sidecar, aside, xmp_data

    def test_a_sidecar_set_aside_alone_is_put_back_while_a_row_records_its_photo(self):
        _, sidecar, aside, xmp_data = self.sidecar_set_aside()
        self.assertFalse(sidecar.exists())
        report = self.clean(at=TOO_SOON)
        self.assertEqual((report.restored, report.dropped), (1, 0))
        self.assertEqual(sidecar.read_bytes(), xmp_data)
        self.assertFalse(aside.exists())
        self.assertFalse(paths.record_of(aside).exists())
        self.assertEqual(self.kept_tree(), self.kept)

    def test_a_sidecar_set_aside_alone_waits_out_the_grace_period_of_its_record_not_of_its_inode(self):
        _, sidecar, aside, xmp_data = self.sidecar_set_aside()
        record = paths.record_of(aside)
        just_written = TOO_SOON - cleanup.DROPPING_GRACE + 60
        os.utime(record, (just_written, just_written))
        self.assertLess(aside.stat().st_ctime, TOO_SOON - cleanup.DROPPING_GRACE)
        report = self.clean(at=TOO_SOON)
        self.assertEqual((report.restored, report.dropped), (0, 0))
        self.assertEqual((aside.read_bytes(), record.is_file(), sidecar.exists()), (xmp_data, True, False))
        report = self.clean(at=just_written + cleanup.DROPPING_GRACE + 1)
        self.assertEqual(report.restored, 1)
        self.assertEqual(sidecar.read_bytes(), xmp_data)

    def test_a_record_whose_sidecar_was_never_set_aside_is_removed_and_nothing_else_changes(self):
        folder = self.root / "jo" / "google-only" / "2017" / "07"
        record = folder / f"{paths.DROPPING}0123456789abcdef.json"
        record.write_bytes(b'{"sha1": "' + b"0" * 40 + b'", "review": 1, "undone": 0}')
        report = self.clean(at=TOO_SOON)
        self.assertEqual((report.restored, report.dropped, report.failed), (0, 0, 0))
        self.assertFalse(record.exists())
        self.assertEqual(self.kept_tree(), self.kept)

    def test_a_sidecar_set_aside_alone_is_never_put_back_onto_another_photo_or_over_a_sidecar(self):
        outside = self.root.parent / "outside.JPG"
        outside.write_bytes(b"a file outside the photo folder")
        for case in ("another photo, recorded", "a row with another sha1", "no row at that path", "a sidecar written since", "no photo", "a link",
                     "the pair is no longer kept", "no record", "a record that is not one"):
            with self.subTest(case=case):
                photo, sidecar, aside, xmp_data = self.sidecar_set_aside()
                data = photo.read_bytes()
                row = self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("%/IMG_2001.JPG"))).scalar_one()
                kept_row = (row.path, row.sha1)
                if case == "another photo, recorded":
                    photo.write_bytes(b"a different photo now at that path")
                    row.sha1 = paths.file_sha1(photo)
                elif case == "a row with another sha1":
                    row.sha1 = "0" * 40
                elif case == "no row at that path":
                    row.path = row.path.replace("IMG_2001.JPG", "IMG_2001-elsewhere.JPG")
                elif case == "a sidecar written since":
                    sidecar.write_bytes(b"<x:xmpmeta>a sidecar written since</x:xmpmeta>")
                elif case == "no photo":
                    photo.unlink()
                elif case == "the pair is no longer kept":
                    self.review().status = "pending"
                elif case == "no record":
                    paths.record_of(aside).unlink()
                elif case == "a record that is not one":
                    paths.record_of(aside).write_bytes(b'{"sha1": "x", "review": 1, "undone": 0}')
                else:
                    photo.unlink()
                    photo.symlink_to(outside)
                self.world.opened.commit()
                report = self.clean(at=TOO_SOON)
                self.assertEqual(report.restored, 0)
                self.assertEqual(aside.read_bytes(), xmp_data)
                if case == "a sidecar written since":
                    self.assertEqual(sidecar.read_bytes(), b"<x:xmpmeta>a sidecar written since</x:xmpmeta>")
                else:
                    self.assertFalse(sidecar.exists())
                self.assertEqual(outside.read_bytes(), b"a file outside the photo folder")
                row.path, row.sha1 = kept_row
                self.world.opened.commit()
                photo.unlink(missing_ok=True)
                photo.write_bytes(data)
                sidecar.unlink(missing_ok=True)
                os.rename(aside, sidecar)
                paths.record_of(aside).unlink(missing_ok=True)

    def test_a_dropping_file_still_in_its_grace_period_is_kept(self):
        fresh = self.leftover("IMG_9.JPG")
        report = cleanup.clean(self.world.opened, self.world.photos, fresh.stat().st_ctime + cleanup.DROPPING_GRACE - 1)
        self.assertTrue(fresh.exists())
        self.assertEqual(report.dropped, 0)

    def test_only_google_only_dropping_files_are_swept(self):
        original = self.root / "jo" / "originals" / f"{paths.DROPPING}0123456789abcdef-IMG_1.JPG"
        original.write_bytes(b"an original with an odd name")
        outside = self.root.parent / "elsewhere.JPG"
        outside.write_bytes(b"outside")
        link = self.root / "jo" / "google-only" / f"{paths.DROPPING}link-x.JPG"
        link.symlink_to(outside)
        self.clean(at=TOO_SOON)
        self.assertTrue(original.is_file())
        self.assertTrue(outside.is_file())
        self.assertTrue(link.is_symlink())

    def test_a_sweep_that_fails_is_recorded_and_cleanup_goes_on(self):
        self.leftover("IMG_9.JPG")
        with mock.patch.object(paths, "sweep_dropping", side_effect=PermissionError("read-only")):
            report = self.clean(at=TOO_SOON)
        self.assertEqual(report.failed, 1)
        events = [(row.subject, row.detail) for row in self.world.opened.execute(select(Event).where(Event.type == "cleanup_failed")).scalars()]
        self.assertIn(("google-only", "PermissionError"), events)


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
