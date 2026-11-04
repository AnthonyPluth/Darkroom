import os
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import review
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, MediaItem
from tests.import_world import sha1_of
from tests.review_world import GOOGLE_ONLY_NAME, PAIR_NAME, ReviewCase

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
        self.assertEqual(events, [("media_reassigned", f"person {self.world.ids['jo']} to person {self.world.ids['sam']}")])

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
                review.reassign(self.opened, self.world.photos, item.id, self.world.ids["sam"], 1.0)
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
                review.reassign(self.opened, self.world.photos, item.id, self.world.ids["sam"], 1.0)
        self.assertEqual(self.tree(), before)

    def test_a_commit_that_fails_puts_the_photo_back(self):
        item = self.item_named(GOOGLE_ONLY_NAME)
        before_tree, before_flags = self.tree(), self.confirmed()
        real = Session.commit

        def failing(session):
            if session is not self.world.opened:
                raise OSError("disk full")
            return real(session)

        with mock.patch.object(Session, "commit", autospec=True, side_effect=failing):
            self.assertEqual(self.reassign(item.id, self.world.ids["sam"])[0], 500)
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(self.confirmed(), before_flags)
        row = self.opened.get(MediaItem, item.id)
        self.assertEqual((row.person_id, row.path), (item.person_id, item.path))
        self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])

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
