import os
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import review
from darkroom.domain.ingest import cleanup
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, MediaItem
from tests.import_world import NOW, sha1_of
from tests.review_world import GOOGLE_ONLY_NAME, PAIR_NAME, ReviewCase, failing_commit

NOT_SERVED = {"error": "the Google account this photo came from doesn't serve that person"}


class SharedAccountTests(ReviewCase):
    def reassign(self, item_id, person_id):
        return self.call("POST", f"/api/media/{item_id}/reassign", {"person_id": person_id})

    def test_reassigning_moves_the_file_and_its_sidecar_and_rescans_both_libraries(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        old = self.world.root / item.path
        data, sidecar = sha1_of(old), old.with_name(f"{old.name}.xmp")
        self.assertTrue(sidecar.is_file())
        before_flags, before_tree = self.confirmed(), self.tree()
        scans = len(self.world.scanned())
        status, body = self.reassign(item.id, self.world.ids["sam"])
        self.assertEqual(status, 200, body)
        moved = self.world.root / "sam" / "google-only" / old.parent.parent.name / old.parent.name / GOOGLE_ONLY_NAME
        self.assertEqual((body["media"]["person_id"], body["media"]["path"]), (self.world.ids["sam"], str(moved.relative_to(self.world.root / "sam"))))
        self.assertEqual(sha1_of(moved), data)
        self.assertTrue(moved.with_name(f"{moved.name}.xmp").is_file())
        self.assertFalse(old.exists())
        self.assertFalse(sidecar.exists())
        after = self.tree()
        self.assertEqual({k: v for k, v in after.items() if not k.endswith((GOOGLE_ONLY_NAME, f"{GOOGLE_ONLY_NAME}.xmp"))},
                         {k: v for k, v in before_tree.items() if not k.endswith((GOOGLE_ONLY_NAME, f"{GOOGLE_ONLY_NAME}.xmp"))})
        self.assertEqual(sorted(self.world.scanned()[scans:]), ["lib-jo", "lib-sam"])
        self.assertEqual(self.confirmed(), before_flags)
        self.assertFalse(self.opened.get(MediaItem, item.id).confirmed_in_google)
        self.assertEqual(self.decision_of(GOOGLE_ONLY_NAME).person_id, self.world.ids["jo"])
        events = [(row.type, row.detail) for row in self.opened.execute(select(Event).where(Event.type == "media_reassigned")).scalars()]
        ((kind, detail),) = events
        self.assertEqual(kind, "media_reassigned")
        self.assertRegex(detail, rf"^person {self.world.ids['jo']} to person {self.world.ids['sam']}, copy [0-9a-f]{{16}}$")
        self.assertEqual(list(self.world.root.rglob(f"{paths.PLACING}*")), [])

    def test_the_existing_sidecar_moves_byte_for_byte(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        sidecar = (self.world.root / item.path).with_name(f"{GOOGLE_ONLY_NAME}.xmp")
        sidecar.write_bytes(b"<x:xmpmeta>edited by hand, kept as is</x:xmpmeta>")
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"])[0], 200)
        moved = self.world.root / self.opened.get(MediaItem, item.id).path
        self.assertEqual(moved.with_name(f"{GOOGLE_ONLY_NAME}.xmp").read_bytes(), b"<x:xmpmeta>edited by hand, kept as is</x:xmpmeta>")
        self.assertFalse(sidecar.exists())

    def test_a_different_sidecar_already_at_the_target_stops_the_move(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        old = self.world.root / item.path
        clash = self.world.root / "sam" / "google-only" / old.parent.parent.name / old.parent.name / f"{GOOGLE_ONLY_NAME}.xmp"
        clash.parent.mkdir(parents=True, exist_ok=True)
        clash.write_bytes(b"someone else's sidecar")
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "the photo could not be moved; it was left where it was"}))
        self.assertTrue(old.is_file())
        self.assertTrue(old.with_name(f"{GOOGLE_ONLY_NAME}.xmp").is_file())
        self.assertEqual(clash.read_bytes(), b"someone else's sidecar")

    def test_a_photo_without_a_sidecar_gets_one_from_its_takeout_metadata(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        (self.world.root / item.path).with_name(f"{GOOGLE_ONLY_NAME}.xmp").unlink()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"])[0], 200)
        moved = self.world.root / self.opened.get(MediaItem, item.id).path
        self.assertTrue(moved.with_name(f"{GOOGLE_ONLY_NAME}.xmp").is_file())

    def test_a_move_that_fails_after_copying_leaves_nothing_behind(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before_tree, before_flags = self.tree(), self.confirmed()
        sidecar = (self.world.root / item.path).with_name(f"{GOOGLE_ONLY_NAME}.xmp")
        for patched, without_sidecar in (("set_library_copy_aside", False), ("carry_sidecar", False), ("set_library_copy_aside", True)):
            with self.subTest(patched=patched, without_sidecar=without_sidecar):
                kept = sidecar.read_bytes()
                if without_sidecar:
                    sidecar.unlink()
                real = getattr(paths, patched)
                calls = []

                def failing(*args, real=real, calls=calls, **kwargs):
                    calls.append(args)
                    if len(calls) == 1:
                        raise paths.NotALibraryCopy("made to fail")
                    return real(*args, **kwargs)

                with mock.patch.object(paths, patched, side_effect=failing):
                    self.assertEqual(self.reassign(item.id, self.world.ids["sam"]),
                                     (409, {"error": "the photo could not be moved; it was left where it was"}))
                expected = before_tree if not without_sidecar else {k: v for k, v in before_tree.items() if not k.endswith(f"{GOOGLE_ONLY_NAME}.xmp")}
                self.assertEqual(self.tree(), expected)
                self.assertEqual(self.confirmed(), before_flags)
                self.assertEqual(self.opened.get(MediaItem, item.id).person_id, self.world.ids["jo"])
                if without_sidecar:
                    sidecar.write_bytes(kept)

    def test_a_sidecar_writer_failure_leaves_nothing_behind(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        (self.world.root / item.path).with_name(f"{GOOGLE_ONLY_NAME}.xmp").unlink()
        before = self.tree()
        with mock.patch.object(review.xmp, "fill", side_effect=RuntimeError("exiftool broke")):
            with self.assertRaises(RuntimeError):
                opened = self.opened
                with review.saving(opened) as effects:
                    review.reassign(opened, self.world.photos, item.id, self.world.ids["sam"], 1.0, effects)
        self.assertEqual(self.tree(), before)

    def test_a_failing_sidecar_removal_leaves_the_tree_and_database_unchanged(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before_tree, before_flags = self.tree(), self.confirmed()
        real, failed = os.rename, []

        def rename(source, target):
            if str(source).endswith(".xmp") and not failed:
                failed.append(source)
                raise PermissionError("made to fail")
            return real(source, target)

        with mock.patch.object(paths.os, "rename", side_effect=rename):
            self.assertEqual(self.reassign(item.id, self.world.ids["sam"]),
                             (409, {"error": "the photo could not be moved; it was left where it was"}))
        self.assertTrue(failed)
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(self.confirmed(), before_flags)
        row = self.opened.get(MediaItem, item.id)
        self.assertEqual((row.person_id, row.path), (item.person_id, item.path))
        self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])

    def test_a_sidecar_writer_failure_is_a_409_and_changes_nothing(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        (self.world.root / item.path).with_name(f"{GOOGLE_ONLY_NAME}.xmp").unlink()
        before = self.tree()
        for failure in (exiftool.Missing("no exiftool"), exiftool.WriteFailed("bad tag")):
            with self.subTest(failure=failure), mock.patch.object(review.xmp, "fill", side_effect=failure):
                self.assertEqual(self.reassign(item.id, self.world.ids["sam"]),
                                 (409, {"error": "the photo could not be moved; it was left where it was"}))
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.opened.get(MediaItem, item.id).person_id, self.world.ids["jo"])

    def test_a_stray_sidecar_at_the_target_is_never_changed(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        old = self.world.root / item.path
        old.with_name(f"{GOOGLE_ONLY_NAME}.xmp").unlink()
        stray = self.world.root / "sam" / "google-only" / old.parent.parent.name / old.parent.name / f"{GOOGLE_ONLY_NAME}.xmp"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"<x:xmpmeta>a stray sidecar, left untouched</x:xmpmeta>")
        before = self.tree()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "the photo could not be moved; it was left where it was"}))
        self.assertEqual(stray.read_bytes(), b"<x:xmpmeta>a stray sidecar, left untouched</x:xmpmeta>")
        self.assertEqual(self.tree(), before)

    def test_reassigning_back_returns_the_file(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before = self.tree()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"])[0], 200)
        self.assertEqual(self.reassign(item.id, self.world.ids["jo"])[0], 200)
        self.assertEqual(self.tree(), before)

    def test_a_kept_pair_can_be_reassigned_and_undo_still_takes_it_back(self):
        review_id = self.pending().id
        before = self.tree()
        kept = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})[1]["review"]["kept_item_id"]
        self.assertEqual(self.reassign(kept, self.world.ids["sam"])[0], 200)
        self.assertTrue((self.world.root / "sam" / "google-only" / "2019" / "08" / PAIR_NAME).is_file())
        listed = self.call("GET", "/api/review?status=kept_both")[1]["reviews"][0]
        self.assertEqual((listed["person_id"], listed["kept_person_id"]), (self.world.ids["jo"], self.world.ids["sam"]))
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.tree(), before)

    def test_a_person_the_account_does_not_serve_is_refused(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        kim = self.stranger()
        before_tree, before_flags = self.tree(), self.confirmed()
        for person_id in (kim, 999):
            with self.subTest(person_id=person_id):
                self.assertEqual(self.reassign(item.id, person_id), (409, NOT_SERVED))
        self.assertEqual((self.tree(), self.confirmed()), (before_tree, before_flags))
        self.assertEqual(self.opened.get(MediaItem, item.id).person_id, self.world.ids["jo"])

    def test_a_phone_original_cannot_be_reassigned(self):
        item = self.item_named("IMG_3001.JPG", "phone")
        before = self.tree()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "only a photo from a Takeout export can be reassigned"}))
        self.assertEqual(self.tree(), before)

    def test_a_takeout_item_outside_google_only_cannot_be_reassigned(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        opened = self.opened
        opened.get(MediaItem, item.id).path = "jo/originals/IMG_1001.JPG.copy"
        opened.commit()
        before = self.tree()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "only a photo in a google-only folder can be reassigned"}))
        self.assertEqual(self.tree(), before)

    def test_a_takeout_item_in_someone_elses_folder_cannot_be_reassigned(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        opened = self.opened
        opened.get(MediaItem, item.id).person_id = self.world.ids["sam"]
        opened.commit()
        self.assertEqual(self.reassign(item.id, self.world.ids["jo"]),
                         (409, {"error": "only a photo in its person's google-only folder can be reassigned"}))

    def test_an_item_a_later_import_matched_is_refused(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        opened = self.opened
        opened.add(DedupeDecision(export_id=self.export.id, account_id=self.world.account, source_path="later/x.JPG", person_id=item.person_id,
                                  matched_item_id=item.id, tier=1, reason="same SHA-1 as a takeout item", decided_by="auto", decided_at=2.0))
        opened.commit()
        before = self.tree()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"]),
                         (409, {"error": "a later import matched this photo; reverse that decision before reassigning it"}))
        self.assertEqual(self.tree(), before)

    def test_a_person_who_already_has_the_photo_is_refused(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        opened = self.opened
        opened.add(MediaItem(person_id=self.world.ids["sam"], source="phone", path="sam/originals/same.JPG", sha1=item.sha1, bytes=1, indexed_at=1.0))
        opened.commit()
        self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "Sam already has this photo"}))

    def test_a_stale_row_at_the_target_place_stops_the_move_before_the_source_goes(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        old = self.world.root / item.path
        target = self.world.root / "sam" / "google-only" / old.parent.parent.name / old.parent.name / GOOGLE_ONLY_NAME
        for file_there in (False, True):
            with self.subTest(file_there=file_there):
                opened = self.opened
                stale = MediaItem(person_id=self.world.ids["sam"], source="takeout", path=str(target.relative_to(self.world.root)),
                                  sha1="0" * 40, bytes=1, indexed_at=1.0)
                opened.add(stale)
                opened.commit()
                if file_there:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(old.read_bytes())
                before_tree, before_flags = self.tree(), self.confirmed()
                self.assertEqual(self.reassign(item.id, self.world.ids["sam"]),
                                 (409, {"error": "another photo of Sam's is recorded at that place, so the photo was left where it was"}))
                self.assertEqual(self.tree(), before_tree)
                self.assertEqual(self.confirmed(), before_flags)
                self.assertTrue(old.is_file())
                self.assertTrue(old.with_name(f"{GOOGLE_ONLY_NAME}.xmp").is_file())
                row = self.opened.get(MediaItem, item.id)
                self.assertEqual((row.person_id, row.path), (item.person_id, item.path))
                opened = self.opened
                opened.delete(opened.get(MediaItem, stale.id))
                opened.commit()
                target.unlink(missing_ok=True)

    def test_a_database_failure_after_copying_keeps_the_source(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before = self.tree()
        with mock.patch.object(review.events, "record", side_effect=RuntimeError("database went away")):
            with self.assertRaises(RuntimeError):
                opened = self.opened
                with review.saving(opened) as effects:
                    review.reassign(opened, self.world.photos, item.id, self.world.ids["sam"], 1.0, effects)
        self.assertEqual(self.tree(), before)

    def test_a_commit_that_fails_puts_the_photo_back(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before_tree, before_flags = self.tree(), self.confirmed()
        with failing_commit(self.world.opened):
            self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "the change could not be saved, so it was undone"}))
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(self.confirmed(), before_flags)
        row = self.opened.get(MediaItem, item.id)
        self.assertEqual((row.person_id, row.path), (item.person_id, item.path))
        self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])

    def test_a_sidecar_made_beside_an_identical_file_already_there_goes_when_the_move_fails(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        old = self.world.root / item.path
        old.with_name(f"{GOOGLE_ONLY_NAME}.xmp").unlink()
        target = self.world.root / "sam" / "google-only" / old.parent.parent.name / old.parent.name / GOOGLE_ONLY_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old.read_bytes())
        before = self.tree()
        with mock.patch.object(paths, "set_library_copy_aside", side_effect=paths.NotALibraryCopy("made to fail")):
            self.assertEqual(self.reassign(item.id, self.world.ids["sam"]), (409, {"error": "the photo could not be moved; it was left where it was"}))
        self.assertEqual(self.tree(), before)
        self.assertTrue(target.is_file())
        self.assertFalse(target.with_name(f"{GOOGLE_ONLY_NAME}.xmp").exists())

    def test_a_copy_that_changed_on_disk_is_left_alone(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        (self.world.root / item.path).write_bytes(b"changed by someone else")
        status, body = self.reassign(item.id, self.world.ids["sam"])
        self.assertEqual((status, body), (409, {"error": "the photo could not be moved; it was left where it was"}))
        self.assertTrue((self.world.root / item.path).is_file())

    def test_bad_requests(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        self.assertEqual(self.call("POST", "/api/media/999/reassign", {"person_id": 1}), (404, {"error": "no such photo"}))
        self.assertEqual(self.call("POST", "/api/media/x/reassign", {"person_id": 1}), (404, {"error": "no such photo"}))
        for body, message in (({}, "person_id must be a whole number"), ({"person_id": "2"}, "person_id must be a whole number"),
                              ({"person_id": 0}, "person_id must be between 1 and 2147483647")):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", f"/api/media/{item.id}/reassign", body), (400, {"error": message}))


class InterruptedReassignTests(ReviewCase):
    def dies_while_reassigning(self, item_id, crash=None):
        self.opened.close()
        dying = Session(self.world.engine)
        try:
            if crash is None:
                review.reassign(dying, self.world.photos, item_id, self.world.ids["sam"], NOW, review.FileEffects())
            else:
                with mock.patch.object(paths, crash, side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    review.reassign(dying, self.world.photos, item_id, self.world.ids["sam"], NOW, review.FileEffects())
        finally:
            dying.close()

    def clean(self):
        opened = self.opened
        report = cleanup.clean(opened, self.world.photos, time.time() + cleanup.DROPPING_GRACE + 60)
        opened.commit()
        return report

    def kept(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        return item.id, item.path

    def target(self, path):
        old = self.world.root / path
        return self.world.root / "sam" / "google-only" / old.parent.parent.name / old.parent.name / old.name

    def test_a_reassign_that_dies_before_its_commit_leaves_no_copy_once_cleanup_runs(self):
        item_id, path = self.kept()
        before = self.tree()
        for crash in ("carry_sidecar", "set_library_copy_aside", None):
            with self.subTest(dies_before=crash or "the commit"):
                self.dies_while_reassigning(item_id, crash)
                self.assertTrue(self.target(path).is_file())
                report = self.clean()
                self.assertEqual(report.taken_back, 1)
                self.assertEqual(report.restored, 1 if crash is None else 0)
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.opened.get(MediaItem, item_id).path, path)
                details = [row.detail for row in self.opened.execute(select(Event).where(Event.type == "cleanup")).scalars()]
                self.assertTrue(details[-1].endswith("1 interrupted moves taken back"))

    def test_a_committed_reassign_keeps_its_copy_when_its_markers_were_left(self):
        item_id, path = self.kept()
        with mock.patch.object(paths, "clear_placing"):
            self.assertEqual(self.call("POST", f"/api/media/{item_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        self.assertNotEqual(list(self.world.root.rglob(f"{paths.PLACING}*")), [])
        moved = self.tree()
        report = self.clean()
        self.assertEqual(report.taken_back, 0)
        self.assertEqual(list(self.world.root.rglob(f"{paths.PLACING}*")), [])
        self.assertEqual(self.tree(), {key: value for key, value in moved.items() if paths.PLACING not in key})
        self.assertTrue(self.target(path).is_file())
        self.assertEqual(self.opened.get(MediaItem, item_id).path, str(self.target(path).relative_to(self.world.root)))

    def test_an_interrupted_copy_is_kept_while_no_other_copy_is(self):
        item_id, path = self.kept()
        old = self.world.root / path
        self.dies_while_reassigning(item_id)
        for aside in old.parent.glob(f"{paths.DROPPING}*"):
            aside.unlink()
        data = self.target(path).read_bytes()
        report = self.clean()
        self.assertEqual(report.taken_back, 0)
        self.assertEqual(self.target(path).read_bytes(), data)
        self.assertEqual(len(list(self.target(path).parent.glob(f"{paths.PLACING}*"))), 1)

    def test_an_interrupted_reassign_onto_an_identical_photo_takes_back_only_the_sidecar_it_wrote(self):
        item_id, path = self.kept()
        target = self.target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((self.world.root / path).read_bytes())
        before = self.tree()
        self.dies_while_reassigning(item_id)
        self.assertTrue(target.with_name(f"{target.name}.xmp").is_file())
        report = self.clean()
        self.assertEqual(report.taken_back, 0)
        self.assertEqual(self.tree(), before)

    def without_markers(self, files):
        return {key: value for key, value in files.items() if paths.PLACING not in key}

    def retried(self, item_id, path, crash):
        self.dies_while_reassigning(item_id, crash)
        self.assertNotEqual(list(self.target(path).parent.glob(f"{paths.PLACING}*")), [])
        self.assertEqual(self.call("POST", f"/api/media/{item_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        target = self.target(path)
        self.assertEqual(self.opened.get(MediaItem, item_id).path, str(target.relative_to(self.world.root)))
        return target, self.without_markers(self.tree())

    def test_a_retry_that_adopted_an_interrupted_copy_keeps_it_and_its_sidecar_while_another_copy_exists(self):
        item_id, path = self.kept()
        source = self.world.root / path
        twin = source.with_name(f"twin-{source.name}")
        twin.write_bytes(source.read_bytes())
        opened = self.opened
        opened.add(MediaItem(person_id=self.world.ids["jo"], source="takeout", path=str(twin.relative_to(self.world.root)),
                             sha1=sha1_of(twin), bytes=twin.stat().st_size, indexed_at=1.0))
        opened.commit()
        target, moved = self.retried(item_id, path, "carry_sidecar")
        data, xmp_data = target.read_bytes(), target.with_name(f"{target.name}.xmp").read_bytes()
        report = self.clean()
        self.assertEqual(report.taken_back, 0)
        self.assertEqual((target.read_bytes(), target.with_name(f"{target.name}.xmp").read_bytes()), (data, xmp_data))
        self.assertEqual(self.tree(), moved)
        self.assertEqual(self.opened.get(MediaItem, item_id).path, str(target.relative_to(self.world.root)))

    def test_a_retry_that_adopted_a_sidecar_an_interrupted_reassign_wrote_keeps_it(self):
        item_id, path = self.kept()
        target = self.target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((self.world.root / path).read_bytes())
        target, moved = self.retried(item_id, path, "set_library_copy_aside")
        xmp_data = target.with_name(f"{target.name}.xmp").read_bytes()
        report = self.clean()
        self.assertEqual(report.taken_back, 0)
        self.assertEqual(target.with_name(f"{target.name}.xmp").read_bytes(), xmp_data)
        self.assertEqual(self.tree(), moved)

    def test_a_marker_whose_place_now_holds_another_file_never_touches_that_file(self):
        item_id, path = self.kept()
        target = self.target(path)
        self.dies_while_reassigning(item_id)
        target.unlink()
        target.write_bytes(b"a different photo put there since")
        self.clean()
        self.assertEqual(target.read_bytes(), b"a different photo put there since")
        self.assertEqual(list(target.parent.glob(f"{paths.PLACING}*")), [])
        self.assertTrue((self.world.root / path).is_file())

    def test_a_sidecar_changed_after_an_interrupted_reassign_is_kept(self):
        item_id, path = self.kept()
        for change, crash in (("edited", None), ("replaced", None), ("created", "carry_sidecar")):
            with self.subTest(change=change):
                self.dies_while_reassigning(item_id, crash)
                sidecar = paths.sidecar_of(self.target(path))
                if change == "edited":
                    sidecar.write_bytes(sidecar.read_bytes() + b"<!-- edited by hand -->")
                elif change == "replaced":
                    sidecar.unlink()
                    sidecar.write_bytes(b"<x:xmpmeta>replaced by hand</x:xmpmeta>")
                else:
                    self.assertFalse(sidecar.exists())
                    sidecar.write_bytes(b"<x:xmpmeta>made by hand</x:xmpmeta>")
                data = sidecar.read_bytes()
                self.assertEqual(self.clean().taken_back, 1)
                self.assertFalse(self.target(path).exists())
                self.assertEqual(sidecar.read_bytes(), data)
                self.assertTrue((self.world.root / path).is_file())
                sidecar.unlink()

    def test_a_copy_that_could_not_be_hard_linked_is_still_taken_back(self):
        item_id, path = self.kept()
        before = self.tree()
        with mock.patch("os.link", side_effect=PermissionError("this share has no hard links")):
            self.dies_while_reassigning(item_id)
        self.assertTrue(self.target(path).is_file())
        report = self.clean()
        self.assertEqual((report.taken_back, report.restored), (1, 1))
        self.assertEqual(self.tree(), before)

    def test_a_photo_with_the_longest_name_a_set_aside_allows_is_reassigned_and_taken_back(self):
        item_id, path = self.kept()
        old = self.world.root / path
        old.with_name(f"{GOOGLE_ONLY_NAME}.xmp").unlink()
        longest = old.with_name("L" * (255 - len(f"{paths.DROPPING}0123456789abcdef-") - len(old.suffix)) + old.suffix)
        self.assertEqual(len(longest.name), 228)
        os.rename(old, longest)
        opened = self.opened
        opened.get(MediaItem, item_id).path = str(longest.relative_to(self.world.root))
        opened.commit()
        before = self.tree()
        self.dies_while_reassigning(item_id)
        self.assertTrue(self.target(str(longest.relative_to(self.world.root))).is_file())
        self.assertEqual(self.clean().taken_back, 1)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.call("POST", f"/api/media/{item_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        self.assertTrue(self.target(str(longest.relative_to(self.world.root))).is_file())
        self.assertEqual(list(self.world.root.rglob(f"{paths.PLACING}*")), [])

    def test_an_identical_photo_another_writer_put_there_during_the_copy_is_left_after_an_interruption(self):
        item_id, path = self.kept()
        target = self.target(path)
        real = paths.write_once

        def another_writer_first(destination, chunks):
            if destination == target:
                destination.write_bytes((self.world.root / path).read_bytes())
                raise paths.AlreadyExists(destination.name)
            return real(destination, chunks)

        with mock.patch.object(paths, "write_once", side_effect=another_writer_first):
            self.dies_while_reassigning(item_id, "present")
        (record,) = target.parent.glob(f"{paths.PLACING}*")
        self.assertFalse(paths.read_placing(record).made)
        data = target.read_bytes()
        self.assertEqual(self.clean().taken_back, 0)
        self.assertEqual(target.read_bytes(), data)
        self.assertTrue((self.world.root / path).is_file())

    def test_a_sidecar_that_landed_just_before_an_interruption_is_taken_back_with_the_copy(self):
        item_id, path = self.kept()
        source = self.world.root / path
        for carried in (True, False):
            with self.subTest(carried=carried):
                if not carried:
                    paths.sidecar_of(source).unlink()
                before = self.tree()
                folder, real = self.target(path).parent, os.link

                def dies_once_the_sidecar_is_there(origin, destination, real=real, folder=folder):
                    real(origin, destination)
                    if str(destination).endswith(paths.SIDECAR_SUFFIX) and Path(destination).parent == folder:
                        raise KeyboardInterrupt

                with mock.patch.object(paths.os, "link", side_effect=dies_once_the_sidecar_is_there), self.assertRaises(KeyboardInterrupt):
                    self.dies_while_reassigning(item_id)
                self.assertTrue(paths.sidecar_of(self.target(path)).is_file())
                self.assertEqual(self.clean().taken_back, 1)
                self.assertEqual(self.tree(), before)

    def test_an_identical_sidecar_another_writer_put_there_first_is_never_taken_back(self):
        item_id, path = self.kept()
        source = self.world.root / path
        for carried in (True, False):
            with self.subTest(carried=carried):
                if not carried:
                    paths.sidecar_of(source).unlink()
                folder, real = self.target(path).parent, os.link

                def another_writer_first(origin, destination, real=real, folder=folder):
                    if str(destination).endswith(paths.SIDECAR_SUFFIX) and Path(destination).parent == folder:
                        Path(destination).write_bytes(Path(origin).read_bytes())
                        raise FileExistsError(destination)
                    real(origin, destination)

                with mock.patch.object(paths.os, "link", side_effect=another_writer_first), self.assertRaises(review.WrongState):
                    self.dies_while_reassigning(item_id)
                sidecar = paths.sidecar_of(self.target(path))
                data = sidecar.read_bytes()
                self.assertEqual(self.clean().taken_back, 1)
                self.assertFalse(self.target(path).exists())
                self.assertEqual(sidecar.read_bytes(), data)
                sidecar.unlink()

    def test_a_sidecar_beside_a_photo_replaced_after_an_interrupted_reassign_is_kept(self):
        item_id, path = self.kept()
        target = self.target(path)
        self.dies_while_reassigning(item_id)
        sidecar = paths.sidecar_of(target)
        data = sidecar.read_bytes()
        target.unlink()
        target.write_bytes(b"a different photo the person put there")
        self.assertEqual(self.clean().taken_back, 0)
        self.assertEqual(target.read_bytes(), b"a different photo the person put there")
        self.assertEqual(sidecar.read_bytes(), data)
        self.assertEqual(list(target.parent.glob(f"{paths.PLACING}*")), [])

    def as_old_markers(self, target):
        (record,) = target.parent.glob(f"{paths.PLACING}*")
        placing = paths.read_placing(record)
        token = paths.PLACING_RECORD.match(record.name).group(1)
        if placing.made:
            os.link(target, target.with_name(f"{paths.PLACING}{token}-{target.name}"))
        if placing.new_sidecar:
            target.with_name(f"{paths.PLACING}{token}-{target.name}.xmp").write_bytes(b"")
        record.unlink()
        return token

    def key(self, path):
        return str(path.relative_to(self.world.root))

    def test_markers_left_by_the_previous_release_are_cleared_and_never_remove_a_copy(self):
        item_id, path = self.kept()
        before = self.tree()
        self.dies_while_reassigning(item_id)
        self.as_old_markers(self.target(path))
        sidecar = paths.sidecar_of(self.target(path))
        data, xmp_data = self.target(path).read_bytes(), sidecar.read_bytes()
        report = self.clean()
        self.assertEqual((report.taken_back, report.restored), (0, 1))
        self.assertEqual((self.target(path).read_bytes(), sidecar.read_bytes()), (data, xmp_data))
        self.assertEqual(list(self.world.root.rglob(f"{paths.PLACING}*")), [])
        left = {self.key(self.target(path)), self.key(sidecar)}
        self.assertEqual({key: value for key, value in self.tree().items() if key not in left}, before)
        self.target(path).unlink()
        sidecar.unlink()
        with mock.patch.object(paths, "clear_placing"):
            self.assertEqual(self.call("POST", f"/api/media/{item_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        self.as_old_markers(self.target(path))
        moved = self.without_markers(self.tree())
        self.assertEqual(self.clean().taken_back, 0)
        self.assertEqual(self.tree(), moved)
        self.assertEqual(list(self.world.root.rglob(f"{paths.PLACING}*")), [])

    def test_an_old_sidecar_marker_never_removes_a_sidecar(self):
        item_id, path = self.kept()
        target = self.target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((self.world.root / path).read_bytes())
        before = self.tree()
        for change in ("as written", "edited"):
            with self.subTest(change=change):
                self.dies_while_reassigning(item_id)
                self.as_old_markers(target)
                sidecar = paths.sidecar_of(target)
                if change == "edited":
                    sidecar.write_bytes(sidecar.read_bytes() + b"<!-- edited by hand -->")
                data = sidecar.read_bytes()
                self.assertEqual(self.clean().taken_back, 0)
                self.assertEqual(sidecar.read_bytes(), data)
                self.assertEqual({key: value for key, value in self.tree().items() if key != self.key(sidecar)}, before)
                self.assertEqual(list(target.parent.glob(f"{paths.PLACING}*")), [])
                sidecar.unlink()

    def test_an_old_marker_whose_place_now_holds_another_photo_never_touches_it(self):
        item_id, path = self.kept()
        target = self.target(path)
        self.dies_while_reassigning(item_id)
        self.as_old_markers(target)
        target.unlink()
        target.write_bytes(b"a different photo put there since")
        self.assertEqual(self.clean().taken_back, 0)
        self.assertEqual(target.read_bytes(), b"a different photo put there since")
        self.assertTrue(paths.sidecar_of(target).is_file())
        self.assertEqual(list(target.parent.glob(f"{paths.PLACING}*")), [])
        self.assertTrue((self.world.root / path).is_file())


class SinglePersonAccountTests(ReviewCase):
    pixel_people = ()

    def test_only_the_default_person_is_allowed(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/media/{item.id}/reassign", {"person_id": self.world.ids["sam"]}), (409, NOT_SERVED))
        status, body = self.call("POST", f"/api/media/{item.id}/reassign", {"person_id": self.world.ids["jo"]})
        self.assertEqual((status, body["media"]["person_id"]), (200, self.world.ids["jo"]))
        self.assertEqual(self.tree(), before)

    def test_the_pair_offers_only_the_one_person(self):
        body = self.call("GET", f"/api/review/{self.pending().id}")[1]
        self.assertEqual([person["id"] for person in body["served_people"]], [self.world.ids["jo"]])


if __name__ == "__main__":
    unittest.main()
