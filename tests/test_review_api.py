import os
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.domain import review
from darkroom.domain.ingest import cleanup, place, run
from darkroom.providers import exiftool
from darkroom.server import auth, routes
from darkroom.server.common import Principal
from darkroom.server.api import review as review_api
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, MediaItem, Person, ReviewItem, User
from tests.import_world import NOW, sha1_of
from tests.privacy import no_leaks
from tests.review_world import GOOGLE_ONLY_NAME, PAIR_NAME, ReviewCase, failing_commit

DAY = 24 * 3600.0
ROUTES = (
    ("GET", "/api/review"), ("GET", "/api/review/{id}"), ("GET", "/api/review/{id}/thumb/{side}"), ("POST", "/api/review/{id}/resolve"),
    ("POST", "/api/review/{id}/undo"), ("GET", "/api/decisions"), ("POST", "/api/decisions/{id}/reverse"), ("GET", "/api/media"),
    ("POST", "/api/media/{id}/reassign"),
)


class ReviewQueueTests(ReviewCase):
    def test_the_routes_need_a_signed_in_session(self):
        for method, path in ROUTES:
            with self.subTest(route=path):
                found = routes.find(method, path.replace("{id}", "1").replace("{side}", "library"))
                self.assertIsNotNone(found)
                self.assertEqual(found.auth, "session")

    def test_the_queue_lists_the_pending_pair(self):
        status, body = self.call("GET", "/api/review?status=pending")
        self.assertEqual(status, 200)
        self.assertEqual(body["pending"], 1)
        (listed,) = body["reviews"]
        self.assertEqual({key: listed[key] for key in ("status", "tier", "name", "library_name", "resolved_at", "undo_until", "kept_item_id")},
                         {"status": "pending", "tier": 4, "name": PAIR_NAME, "library_name": PAIR_NAME, "resolved_at": None, "undo_until": None,
                          "kept_item_id": None})
        self.assertEqual(self.call("GET", "/api/review?status=kept_both"), (200, {"reviews": [], "pending": 1, "before": 0}))
        self.assertEqual(self.call("GET", "/api/review")[1]["reviews"], [listed])

    def test_the_pending_queue_pages_on_from_the_last_pair_seen(self):
        pair = self.pending().id
        self.assertEqual(self.call("GET", f"/api/review?status=pending&after={pair}"), (200, {"reviews": [], "pending": 1, "before": 1}))
        status, body = self.call("GET", f"/api/review?status=pending&after={pair - 1}")
        self.assertEqual((status, [listed["id"] for listed in body["reviews"]], body["before"]), (200, [pair], 0))
        for query, message in (("status=kept_both&after=1", "after is only for status=pending"), ("after=1", "after is only for status=pending"),
                               ("status=pending&after=x", "after must be a whole number")):
            with self.subTest(query=query):
                self.assertEqual(self.call("GET", f"/api/review?{query}"), (400, {"error": message}))

    def test_an_unknown_status_is_refused(self):
        self.assertEqual(self.call("GET", "/api/review?status=gone"), (400, {"error": "status is one of pending, kept_both, skipped, undone"}))

    def test_a_pair_shows_both_sides_with_paths_relative_to_the_person(self):
        status, body = self.call("GET", f"/api/review/{self.pending().id}")
        self.assertEqual(status, 200)
        library, exported = body["library"], body["export"]
        self.assertEqual((library["path"], library["source"], library["make"], library["model"], library["width"], library["height"]),
                         (f"originals/{PAIR_NAME}", "phone", "Apple", "iPhone 11", 64, 48))
        self.assertEqual(library["captured_local"], "2019-08-01T10:00:00")
        self.assertEqual((exported["held"], exported["name"], exported["captured_local"], exported["make"]),
                         (True, PAIR_NAME, "2019-08-01T10:00:01", "Apple"))
        self.assertEqual(exported["path"], f"Takeout/Google Photos/Photos from 2019/{PAIR_NAME}")
        self.assertEqual(exported["bytes"], self.hold().stat().st_size)
        self.assertIsInstance(body["phash_distance"], int)
        self.assertEqual([person["name"] for person in body["served_people"]], ["Jo", "Sam"])
        self.assertEqual(body["default_person_id"], self.world.ids["jo"])

    def test_a_missing_pair_is_404(self):
        for path in ("/api/review/999", "/api/review/x", "/api/review/999/thumb/library"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0], 404)
        for path in ("/api/review/999/resolve", "/api/review/999/undo", "/api/review/x/undo"):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path, {"action": "keep_both"}), (404, {"error": "no such review item"}))

    def test_a_pair_whose_hold_is_gone_shows_the_export_side_unheld(self):
        self.hold().unlink()
        exported = self.call("GET", f"/api/review/{self.pending().id}")[1]["export"]
        self.assertEqual((exported["held"], exported["bytes"], exported["name"]), (False, None, PAIR_NAME))


class ResolveTests(ReviewCase):
    def test_keep_both_places_the_held_copy_in_google_only_and_never_confirms(self):
        before, hold = self.confirmed(), self.hold()
        review_id = self.pending().id
        status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["review"]["status"], "kept_both")
        placed = self.world.root / "jo" / "google-only" / "2019" / "08" / PAIR_NAME
        self.assertEqual(sha1_of(placed), sha1_of(hold))
        self.assertTrue(hold.is_file())
        kept = self.item_named(PAIR_NAME)
        self.assertEqual((kept.person_id, kept.path, kept.export_id, kept.confirmed_in_google), (self.world.ids["jo"], f"jo/google-only/2019/08/{PAIR_NAME}", self.export.id, False))
        self.assertEqual(body["review"]["kept_item_id"], kept.id)
        self.assertEqual({key: flag for key, flag in self.confirmed().items() if key in before}, before)
        self.assertEqual(self.decision_of(PAIR_NAME).tier, 4)
        self.assertEqual(self.world.scanned()[-1], "lib-jo")

    def test_skip_is_not_a_server_action_and_changes_nothing(self):
        before_tree, before_flags = self.tree(), self.confirmed()
        review_id = self.pending().id
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "skip"}),
                         (400, {"error": "action is keep_both; skip leaves the pair for later and saves nothing"}))
        with self.assertRaises(review.WrongState):
            review.resolve(self.opened, self.world.photos, review_id, "skip", 1.0, review.FileEffects())
        self.assertEqual((self.tree(), self.confirmed()), (before_tree, before_flags))
        row = self.opened.get(ReviewItem, review_id)
        self.assertEqual((row.status, row.resolved_at), ("pending", None))
        self.assertIsNotNone(row.hold_path)

    def test_a_decided_pair_cannot_be_decided_again(self):
        review_id = self.pending().id
        self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        before_tree, before_flags = self.tree(), self.confirmed()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                         (409, {"error": "this pair is already decided; undo that decision first"}))
        self.assertEqual((self.tree(), self.confirmed()), (before_tree, before_flags))

    def test_a_pair_whose_decision_was_reversed_cannot_be_decided(self):
        row = self.pending()
        opened = self.opened
        opened.get(DedupeDecision, row.decision_id).reversed_at = 5.0
        opened.commit()
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{row.id}/resolve", {"action": "keep_both"}),
                         (409, {"error": "this pair's dedupe decision was reversed"}))
        self.assertEqual(self.tree(), before)

    def test_keep_both_without_the_held_copy_is_refused(self):
        self.hold().unlink()
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{self.pending().id}/resolve", {"action": "keep_both"}),
                         (409, {"error": "the held copy from the export is gone, so it cannot be kept"}))
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.pending().status, "pending")

    def test_keep_both_refuses_a_hold_that_no_longer_matches_the_staged_file(self):
        self.hold().write_bytes(b"something else entirely")
        self.assertEqual(self.call("POST", f"/api/review/{self.pending().id}/resolve", {"action": "keep_both"}),
                         (409, {"error": "the held copy no longer matches the file in the export, so it was not kept"}))
        self.assertFalse((self.world.root / "jo" / "google-only" / "2019" / "08" / PAIR_NAME).exists())

    def test_keep_both_that_fails_after_copying_leaves_nothing_behind(self):
        before_tree, before_flags = self.tree(), self.confirmed()
        review_id = self.pending().id
        for failure in (exiftool.WriteFailed("bad tag"), exiftool.Missing("no exiftool"), PermissionError("read-only")):
            with self.subTest(failure=failure), mock.patch.object(place.xmp, "fill", side_effect=failure):
                self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                                 (409, {"error": "the held copy could not be placed in google-only; nothing was changed"}))
                self.assertEqual(self.tree(), before_tree)
                self.assertEqual(self.confirmed(), before_flags)
                self.assertEqual(self.pending().id, review_id)
        with mock.patch.object(place.xmp, "fill", side_effect=RuntimeError("unexpected")), self.assertRaises(RuntimeError):
            with review.saving(self.opened) as effects:
                review.resolve(self.opened, self.world.photos, review_id, "keep_both", 1.0, effects)
        self.assertEqual(self.tree(), before_tree)

    def test_keep_both_rollback_skips_a_file_it_did_not_place_and_still_answers_409(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        stray = folder / "AAA_arrived_meanwhile.JPG"
        before_flags, review_id = self.confirmed(), self.pending().id

        def fill_and_race(*args, **kwargs):
            stray.write_bytes(b"a file someone else put here")
            raise exiftool.WriteFailed("bad tag")

        with mock.patch.object(place.xmp, "fill", side_effect=fill_and_race), mock.patch.object(review.monitoring, "log") as logged:
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                             (409, {"error": "the held copy could not be placed in google-only; nothing was changed"}))
        self.assertEqual(stray.read_bytes(), b"a file someone else put here")
        self.assertFalse((folder / PAIR_NAME).exists())
        self.assertEqual(self.confirmed(), before_flags)
        self.assertEqual(self.pending().id, review_id)
        self.assertEqual([c for c in logged.call_args_list if c.args[0].startswith("keep both")], [])

    def test_keep_both_rollback_that_cannot_remove_its_copy_still_answers_409(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        review_id = self.pending().id
        with mock.patch.object(place.xmp, "fill", side_effect=exiftool.WriteFailed("bad tag")), \
                mock.patch.object(review.paths, "drop_library_copy", side_effect=PermissionError("read-only")), \
                mock.patch.object(review.monitoring, "log") as logged:
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                             (409, {"error": "the change failed, and 1 file it made could not be removed"}))
        self.assertTrue((folder / PAIR_NAME).is_file())
        self.assertEqual(self.pending().id, review_id)
        self.assertEqual([c for c in logged.call_args_list if c.args[0].startswith("file changes")], [mock.call("file changes not undone", left=1)])

    def test_keep_both_rollback_leaves_an_identical_file_someone_else_added(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        other = folder / "AAA_identical_meanwhile.JPG"
        review_id, hold = self.pending().id, self.hold()

        def fill_and_race(*args, **kwargs):
            other.write_bytes(hold.read_bytes())
            raise exiftool.WriteFailed("bad tag")

        with mock.patch.object(place.xmp, "fill", side_effect=fill_and_race):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                             (409, {"error": "the held copy could not be placed in google-only; nothing was changed"}))
        self.assertEqual(other.read_bytes(), hold.read_bytes())
        self.assertFalse((folder / PAIR_NAME).exists())
        self.assertEqual(self.pending().id, review_id)

    def test_keep_both_whose_commit_fails_leaves_no_files(self):
        before_tree, before_flags = self.tree(), self.confirmed()
        review_id = self.pending().id
        with failing_commit(self.world.opened):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}), (409, {"error": "the change could not be saved, so it was undone"}))
        self.assertEqual((self.tree(), self.confirmed()), (before_tree, before_flags))
        self.assertEqual(self.pending().id, review_id)

    def cleaned(self):
        staging = self.world.root / "_inbox" / "jo" / "export-1" / "staging"
        hit = review.found(self.opened, self.pending().id)
        staged, found_sidecar = review.staged_sidecar(self.world.opened, self.world.photos, hit.export, hit.decision.source_path)
        self.assertIsNotNone(staged)
        raw = found_sidecar.read_text(encoding="utf-8")
        cleanup.clean(self.world.opened, self.world.photos, NOW + 15 * DAY)
        self.world.opened.commit()
        self.assertFalse(staging.exists())
        return raw

    def test_keep_both_works_once_staging_is_cleaned(self):
        raw = self.cleaned()
        status, body = self.call("POST", f"/api/review/{self.pending().id}/resolve", {"action": "keep_both"})
        self.assertEqual(status, 200, body)
        self.assertTrue((self.world.root / "jo" / "google-only" / "2019" / "08" / PAIR_NAME).is_file())
        self.assertEqual(self.opened.get(MediaItem, body["review"]["kept_item_id"]).sidecar_json, raw)

    def test_keep_both_after_cleanup_refuses_a_hold_that_changed_since_it_was_verified(self):
        self.cleaned()
        before_flags, review_id = self.confirmed(), self.pending().id
        self.hold().write_bytes(b"changed after it was verified")
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                         (409, {"error": "the held copy no longer matches the file in the export, so it was not kept"}))
        self.assertEqual((self.tree(), self.confirmed()), (before, before_flags))
        self.assertEqual(self.pending().id, review_id)

    def test_a_pair_kept_before_cleanup_and_undone_after_it_can_be_kept_again(self):
        review_id = self.pending().id
        staging = self.world.root / "_inbox" / "jo" / "export-1" / "staging"
        with mock.patch.object(review_api, "now", return_value=NOW + 5 * DAY):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})[0], 200)
        cleanup.clean(self.world.opened, self.world.photos, NOW + 15 * DAY)
        self.world.opened.commit()
        self.assertFalse(staging.exists())
        with mock.patch.object(review_api, "now", return_value=NOW + 16 * DAY):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
            status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["review"]["status"], "kept_both")
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
            self.hold().write_bytes(b"changed after it was held")
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                             (409, {"error": "the held copy no longer matches the file in the export, so it was not kept"}))

    def test_keep_both_refuses_a_hold_nothing_can_check_any_more(self):
        staging = self.world.root / "_inbox" / "jo" / "export-1" / "staging"
        for path in sorted(staging.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        opened = self.opened
        opened.get(ReviewItem, self.pending().id).hold_sha1 = None
        opened.commit()
        before, review_id = self.tree(), self.pending().id
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                         (409, {"error": "the held copy can no longer be checked against the export, so it was not kept"}))
        self.assertEqual(self.tree(), before)

    def test_a_bad_action_is_400(self):
        review_id = self.pending().id
        for body, message in (({"action": "delete"}, "action is keep_both; skip leaves the pair for later and saves nothing"), ({}, "action is required"), ({"action": 3}, "action must be text")):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", body), (400, {"error": message}))
        with self.assertRaises(review.WrongState):
            review.resolve(self.opened, self.world.photos, review_id, "delete", 1.0, review.FileEffects())


class UndoTests(ReviewCase):
    def resolve(self, action):
        review_id = self.pending().id
        status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": action})
        self.assertEqual(status, 200, body)
        return review_id, body["review"]["resolved_at"]

    def test_past_decisions_list_every_pair_still_inside_its_undo_window(self):
        review_id, _ = self.resolve("keep_both")
        with mock.patch.object(review, "LIST_LIMIT", 0):
            status, body = self.call("GET", "/api/review?status=kept_both")
        self.assertEqual((status, [listed["id"] for listed in body["reviews"]]), (200, [review_id]))
        opened = self.opened
        opened.get(ReviewItem, review_id).resolved_at = 1.0
        opened.commit()
        with mock.patch.object(review, "LIST_LIMIT", 0):
            self.assertEqual(self.call("GET", "/api/review?status=kept_both")[1]["reviews"], [])

    def test_undoing_keep_both_takes_the_copy_back_out_and_returns_the_pair(self):
        before_tree, before_flags = self.tree(), self.confirmed()
        review_id, _ = self.resolve("keep_both")
        status, body = self.call("POST", f"/api/review/{review_id}/undo")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["review"]["status"], body["review"]["resolved_at"], body["review"]["kept_item_id"]), ("pending", None, None))
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(self.confirmed(), before_flags)
        self.assertEqual(self.opened.execute(select(MediaItem).where(MediaItem.path.like(f"%{PAIR_NAME}"), MediaItem.source == "takeout")).all(), [])
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})[0], 200)

    def already_there(self, sidecar):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        folder.mkdir(parents=True, exist_ok=True)
        photo = folder / PAIR_NAME
        photo.write_bytes(self.hold().read_bytes())
        if sidecar is not None:
            photo.with_name(f"{PAIR_NAME}.xmp").write_bytes(sidecar)
        return photo, photo.with_name(f"{PAIR_NAME}.xmp")

    def test_undo_leaves_an_identical_photo_and_sidecar_that_were_already_there(self):
        for sidecar_data in (b"<x:xmpmeta>edited by you</x:xmpmeta>", None):
            with self.subTest(had_sidecar=sidecar_data is not None):
                photo, sidecar = self.already_there(sidecar_data)
                data = photo.read_bytes()
                review_id, _ = self.resolve("keep_both")
                kept_sidecar = sidecar.read_bytes() if sidecar.exists() else None
                self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
                self.assertEqual(photo.read_bytes(), data)
                if sidecar_data is None:
                    self.assertFalse(sidecar.exists())
                else:
                    self.assertEqual(sidecar.read_bytes(), kept_sidecar)
                self.assertEqual(self.opened.execute(select(MediaItem).where(MediaItem.path.like(f"%{PAIR_NAME}"), MediaItem.source == "takeout")).all(), [])
                self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])
                photo.unlink()
                sidecar.unlink(missing_ok=True)

    def test_undo_leaves_a_sidecar_that_was_already_there_and_takes_the_photo_it_copied(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = folder / f"{PAIR_NAME}.xmp"
        sidecar.write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        review_id, _ = self.resolve("keep_both")
        self.assertTrue((folder / PAIR_NAME).is_file())
        kept_sidecar = sidecar.read_bytes()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertFalse((folder / PAIR_NAME).exists())
        self.assertEqual(sidecar.read_bytes(), kept_sidecar)

    def test_undo_leaves_a_row_and_file_that_were_already_recorded(self):
        photo, _ = self.already_there(None)
        opened = self.opened
        opened.add(MediaItem(person_id=self.world.ids["jo"], source="takeout", path=str(photo.relative_to(self.world.root)),
                             sha1=sha1_of(photo), bytes=photo.stat().st_size, indexed_at=1.0))
        opened.commit()
        before = self.tree()
        review_id, _ = self.resolve("keep_both")
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.tree(), before)
        self.assertEqual(len(self.opened.execute(select(MediaItem).where(MediaItem.path == str(photo.relative_to(self.world.root)))).all()), 1)

    def test_undo_follows_the_latest_keep_both_even_when_the_clock_went_back(self):
        photo, sidecar = self.already_there(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        with mock.patch.object(review_api, "now", return_value=NOW + 2 * DAY):
            review_id, _ = self.resolve("keep_both")
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        photo.unlink()
        sidecar.unlink()
        before = self.tree()
        with mock.patch.object(review_api, "now", return_value=NOW + DAY):
            self.resolve("keep_both")
            self.assertTrue(photo.is_file())
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.tree(), before)

    def kept_then_reassigned(self, photo_at_target=True, sidecar_at_target=False):
        review_id, _ = self.resolve("keep_both")
        kept_id = self.item_named(PAIR_NAME).id
        mine = self.world.root / "jo" / "google-only" / "2019" / "08" / PAIR_NAME
        target = self.world.root / "sam" / "google-only" / "2019" / "08" / PAIR_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        mine.with_name(f"{PAIR_NAME}.xmp").write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        if photo_at_target:
            target.write_bytes(mine.read_bytes())
        if sidecar_at_target:
            target.with_name(f"{PAIR_NAME}.xmp").write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        before = self.tree()
        status, body = self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["sam"]})
        self.assertEqual(status, 200, body)
        return review_id, kept_id, mine, target, before

    def test_undo_after_a_reassign_over_an_identical_photo_and_sidecar_leaves_both_byte_for_byte(self):
        review_id, _, mine, target, before = self.kept_then_reassigned(sidecar_at_target=True)
        sam = {key: value for key, value in before.items() if key.startswith("sam/")}
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual({key: value for key, value in self.tree().items() if key.startswith("sam/")}, sam)
        self.assertEqual(target.with_name(f"{PAIR_NAME}.xmp").read_bytes(), b"<x:xmpmeta>edited by you</x:xmpmeta>")
        self.assertFalse(mine.exists())
        self.assertEqual(self.pending().id, review_id)
        self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])

    def test_undo_after_a_reassign_over_an_identical_photo_takes_only_the_sidecar_the_reassign_made(self):
        review_id, _, _, target, before = self.kept_then_reassigned()
        self.assertTrue(target.with_name(f"{PAIR_NAME}.xmp").is_file())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(sha1_of(target), before[str(target.relative_to(self.world.root))])
        self.assertFalse(target.with_name(f"{PAIR_NAME}.xmp").exists())

    def test_undo_after_a_reassign_over_nothing_removes_what_the_reassign_made(self):
        review_id, _, mine, target, _ = self.kept_then_reassigned(photo_at_target=False)
        self.assertTrue(target.is_file())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(f"{PAIR_NAME}.xmp").exists())
        self.assertFalse(mine.exists())
        self.assertEqual(self.pending().id, review_id)

    def test_undo_after_two_reassigns_follows_the_last_one(self):
        review_id, kept_id, mine, target, _ = self.kept_then_reassigned(photo_at_target=False)
        mine.write_bytes(target.read_bytes())
        mine.with_name(f"{PAIR_NAME}.xmp").write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        jo = {key: value for key, value in self.tree().items() if key.startswith("jo/")}
        status, body = self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["jo"]})
        self.assertEqual(status, 200, body)
        self.assertFalse(target.exists())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual({key: value for key, value in self.tree().items() if key.startswith("jo/")}, jo)
        self.assertFalse(target.with_name(f"{PAIR_NAME}.xmp").exists())

    def test_undo_after_two_reassigns_over_nothing_removes_the_copy(self):
        before = self.tree()
        review_id, kept_id, mine, _, _ = self.kept_then_reassigned(photo_at_target=False)
        status, body = self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["jo"]})
        self.assertEqual(status, 200, body)
        self.assertTrue(mine.is_file())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.tree(), before)

    def adopted_then_reassigned(self, photo_at_target):
        photo, _ = self.already_there(None)
        data = photo.read_bytes()
        review_id, _ = self.resolve("keep_both")
        kept_id = self.item_named(PAIR_NAME).id
        target = self.world.root / "sam" / "google-only" / "2019" / "08" / PAIR_NAME
        if photo_at_target:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        status, body = self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["sam"]})
        self.assertEqual(status, 200, body)
        return review_id, kept_id, photo, target, data

    def test_undo_after_keep_both_adopted_a_photo_and_a_reassign_moved_it_keeps_the_photo(self):
        for photo_at_target in (False, True):
            with self.subTest(photo_at_target=photo_at_target):
                review_id, _, photo, target, data = self.adopted_then_reassigned(photo_at_target)
                self.assertFalse(photo.exists())
                self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
                self.assertEqual(target.read_bytes(), data)
                self.assertFalse(target.with_name(f"{PAIR_NAME}.xmp").exists())
                self.assertEqual(self.pending().id, review_id)
                self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])
                target.unlink()

    def test_undo_after_an_adopted_photo_was_reassigned_twice_keeps_it_where_it_ended(self):
        review_id, kept_id, photo, target, data = self.adopted_then_reassigned(False)
        status, body = self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["jo"]})
        self.assertEqual(status, 200, body)
        self.assertFalse(target.exists())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(photo.read_bytes(), data)
        self.assertFalse(target.exists())

    def test_undo_after_keep_both_adopted_a_row_and_a_reassign_moved_it_keeps_the_row_with_its_photo(self):
        photo, _ = self.already_there(None)
        opened = self.opened
        opened.add(MediaItem(person_id=self.world.ids["jo"], source="takeout", path=str(photo.relative_to(self.world.root)),
                             sha1=sha1_of(photo), bytes=photo.stat().st_size, indexed_at=1.0, export_id=self.export.id))
        opened.commit()
        data = photo.read_bytes()
        review_id, _ = self.resolve("keep_both")
        kept_id = self.item_named(PAIR_NAME).id
        self.assertEqual(self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        row = self.opened.get(MediaItem, kept_id)
        self.assertEqual((self.world.root / row.path).read_bytes(), data)

    def test_undo_after_an_adopted_sidecar_was_deleted_and_a_reassign_wrote_a_new_one_removes_it(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = folder / f"{PAIR_NAME}.xmp"
        sidecar.write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        review_id, _ = self.resolve("keep_both")
        kept_id = self.item_named(PAIR_NAME).id
        sidecar.unlink()
        sam = {key: value for key, value in self.tree().items() if key.startswith("sam/")}

        def fill(path, *args, **kwargs):
            paths.sidecar_of(path).write_bytes(b"<x:xmpmeta>from the Takeout metadata</x:xmpmeta>")
            return True

        with mock.patch.object(review.xmp, "fill", side_effect=fill):
            self.assertEqual(self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        target = self.world.root / self.opened.get(MediaItem, kept_id).path
        self.assertEqual(target.with_name(f"{PAIR_NAME}.xmp").read_bytes(), b"<x:xmpmeta>from the Takeout metadata</x:xmpmeta>")
        (detail,) = self.opened.execute(select(Event.detail).where(Event.type == "media_reassigned", Event.subject == f"item {kept_id}")).scalars()
        self.assertEqual(review.adopted_in(detail), set())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(f"{PAIR_NAME}.xmp").exists())
        self.assertEqual({key: value for key, value in self.tree().items() if key.startswith("sam/")}, sam)
        self.assertEqual(self.pending().id, review_id)

    def test_a_reassign_still_carries_an_adopted_sidecar_as_adopted(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{PAIR_NAME}.xmp").write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        review_id, _ = self.resolve("keep_both")
        kept_id = self.item_named(PAIR_NAME).id
        carried = (folder / f"{PAIR_NAME}.xmp").read_bytes()
        self.assertEqual(self.call("POST", f"/api/media/{kept_id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        target = self.world.root / self.opened.get(MediaItem, kept_id).path
        (detail,) = self.opened.execute(select(Event.detail).where(Event.type == "media_reassigned", Event.subject == f"item {kept_id}")).scalars()
        self.assertEqual(review.adopted_in(detail), {"sidecar"})
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertFalse(target.exists())
        self.assertEqual(target.with_name(f"{PAIR_NAME}.xmp").read_bytes(), carried)

    def test_the_reassign_event_records_what_it_adopted_and_still_names_who_moved_it(self):
        _, kept_id, _, _, _ = self.kept_then_reassigned(sidecar_at_target=True)
        (detail,) = self.opened.execute(select(Event.detail).where(Event.type == "media_reassigned", Event.subject == f"item {kept_id}")).scalars()
        self.assertIn(f"{review.ADOPTED}photo, sidecar", detail)
        self.assertEqual(review.adopted_in(detail), {"photo", "sidecar"})
        self.assertEqual(review.adopted_in(f"{detail}, by person 7"), {"photo", "sidecar"})
        self.assertEqual(review.actor_of(f"{detail}, by person 7"), 7)

    def test_undo_after_keep_both_over_a_stale_row_restores_the_stale_state(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        relative = str((folder / PAIR_NAME).relative_to(self.world.root))
        opened = self.opened
        opened.add(MediaItem(person_id=self.world.ids["jo"], source="takeout", path=relative, sha1=sha1_of(self.hold()), bytes=1, indexed_at=1.0))
        opened.commit()
        before_tree = self.tree()
        review_id, _ = self.resolve("keep_both")
        self.assertTrue((folder / PAIR_NAME).is_file())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.tree(), before_tree)
        (row,) = self.opened.execute(select(MediaItem).where(MediaItem.path == relative)).scalars()
        self.assertEqual((row.sha1, row.bytes), (sha1_of(self.hold()), 1))
        self.assertEqual(self.opened.get(ReviewItem, review_id).status, "pending")

    def test_undo_after_keep_both_over_a_stale_row_and_a_reassign_to_an_empty_place_keeps_the_photo_the_row_points_at(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        relative = str((folder / PAIR_NAME).relative_to(self.world.root))
        opened = self.opened
        stale = MediaItem(person_id=self.world.ids["jo"], source="takeout", path=relative, sha1=sha1_of(self.hold()), bytes=1, indexed_at=1.0,
                          export_id=self.export.id)
        opened.add(stale)
        opened.commit()
        data = self.hold().read_bytes()
        review_id, _ = self.resolve("keep_both")
        self.assertEqual(self.call("POST", f"/api/media/{stale.id}/reassign", {"person_id": self.world.ids["sam"]})[0], 200)
        target = self.world.root / self.opened.get(MediaItem, stale.id).path
        self.assertTrue(target.is_relative_to(self.world.root / "sam" / "google-only"))
        self.assertFalse((folder / PAIR_NAME).exists())
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        row = self.opened.get(MediaItem, stale.id)
        self.assertEqual((row.person_id, row.path), (self.world.ids["sam"], str(target.relative_to(self.world.root))))
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(target.with_name(f"{PAIR_NAME}.xmp").exists())
        self.assertFalse((folder / PAIR_NAME).exists())
        self.assertEqual(self.opened.get(ReviewItem, review_id).status, "pending")
        self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])

    def test_keep_both_over_a_stale_row_with_other_bytes_is_refused_and_changes_nothing(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        relative = str((folder / PAIR_NAME).relative_to(self.world.root))
        for file_there in (False, True):
            with self.subTest(file_there=file_there):
                opened = self.opened
                stale = MediaItem(person_id=self.world.ids["jo"], source="takeout", path=relative, sha1="0" * 40, bytes=1, indexed_at=1.0)
                opened.add(stale)
                opened.commit()
                if file_there:
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / PAIR_NAME).write_bytes(self.hold().read_bytes())
                before_tree, before_flags, review_id = self.tree(), self.confirmed(), self.pending().id
                self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                                 (409, {"error": "another photo is recorded at that place in google-only, so the pair was not kept"}))
                self.assertEqual((self.tree(), self.confirmed()), (before_tree, before_flags))
                row = self.opened.get(MediaItem, stale.id)
                self.assertEqual((row.sha1, row.bytes, row.path), ("0" * 40, 1, relative))
                decision = self.opened.get(DedupeDecision, self.pending().decision_id)
                self.assertIsNone(decision.takeout_item_id)
                self.assertEqual(self.opened.get(ReviewItem, review_id).status, "pending")
                opened = self.opened
                opened.delete(opened.get(MediaItem, stale.id))
                opened.commit()
                (folder / PAIR_NAME).unlink(missing_ok=True)

    def test_a_new_photo_beside_a_sidecar_that_cannot_be_read_is_taken_back(self):
        folder = self.world.root / "jo" / "google-only" / "2019" / "08"
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = folder / f"{PAIR_NAME}.xmp"
        sidecar.write_bytes(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        before_tree, review_id = self.tree(), self.pending().id
        real = type(sidecar).read_bytes

        def read_bytes(path):
            if path == sidecar:
                raise PermissionError("made unreadable")
            return real(path)

        with mock.patch.object(type(sidecar), "read_bytes", autospec=True, side_effect=read_bytes):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                             (409, {"error": "the held copy could not be placed in google-only; nothing was changed"}))
        self.assertFalse((folder / PAIR_NAME).exists())
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(self.pending().id, review_id)
        self.assertEqual(self.opened.execute(select(MediaItem).where(MediaItem.path.like(f"%{PAIR_NAME}"), MediaItem.source == "takeout")).all(), [])

    def test_a_keep_both_that_fails_puts_back_a_sidecar_it_changed(self):
        _, sidecar = self.already_there(b"<x:xmpmeta>edited by you</x:xmpmeta>")
        before_tree, review_id = self.tree(), self.pending().id

        def fill(path, *args, **kwargs):
            sidecar.write_bytes(sidecar.read_bytes() + b"<x:merged/>")
            return True

        with mock.patch.object(place.xmp, "fill", side_effect=fill), failing_commit(self.world.opened):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"}),
                             (409, {"error": "the change could not be saved, so it was undone"}))
        self.assertEqual(sidecar.read_bytes(), b"<x:xmpmeta>edited by you</x:xmpmeta>")
        self.assertEqual(self.tree(), before_tree)
        self.assertEqual(self.pending().id, review_id)

    def test_an_undo_whose_commit_fails_keeps_the_copy_and_can_be_tried_again(self):
        review_id, _ = self.resolve("keep_both")
        kept_tree, kept_flags = self.tree(), self.confirmed()
        with failing_commit(self.world.opened):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"), (409, {"error": "the change could not be saved, so it was undone"}))
        self.assertEqual((self.tree(), self.confirmed()), (kept_tree, kept_flags))
        self.assertEqual(self.opened.get(ReviewItem, review_id).status, "kept_both")
        self.assertEqual([path.name for path in self.world.root.rglob(".dropping-*")], [])
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)

    def test_undo_is_allowed_inside_the_window_and_refused_after(self):
        review_id, resolved = self.resolve("keep_both")
        with mock.patch.object(review_api, "now", return_value=resolved + 14 * DAY):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"), (409, {"error": (
                "this decision is more than 14 days old and its held copy is cleaned up, so it can no longer be undone")}))
        self.assertEqual(self.pending_or_none(), None)
        with mock.patch.object(review_api, "now", return_value=resolved + 14 * DAY - 1):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.pending().id, review_id)

    def test_cleanup_removes_the_hold_only_after_the_window_so_undo_and_cleanup_never_overlap(self):
        review_id, resolved = self.resolve("keep_both")
        hold = self.world.root / self.opened.get(ReviewItem, review_id).hold_path
        cleanup.clean(self.world.reopen(), self.world.photos, resolved + 14 * DAY - 1)
        self.world.opened.commit()
        self.assertTrue(hold.is_file())
        cleanup.clean(self.world.reopen(), self.world.photos, resolved + 14 * DAY)
        self.world.opened.commit()
        self.assertFalse(hold.exists())
        with mock.patch.object(review_api, "now", return_value=resolved + 14 * DAY):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 409)

    def test_undo_without_the_hold_is_refused(self):
        review_id, _ = self.resolve("keep_both")
        (self.world.root / self.opened.get(ReviewItem, review_id).hold_path).unlink()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"),
                         (409, {"error": "the held copy from the export is gone, so this decision can no longer be undone"}))

    def test_a_pending_pair_cannot_be_undone(self):
        self.assertEqual(self.call("POST", f"/api/review/{self.pending().id}/undo"), (409, {"error": "only a kept pair can be undone"}))

    def test_an_undone_status_cannot_be_undone(self):
        row = self.pending()
        opened = self.opened
        found = opened.get(ReviewItem, row.id)
        found.status, found.resolved_at = "undone", 1.0
        opened.commit()
        self.assertEqual(self.call("POST", f"/api/review/{row.id}/undo")[0], 409)

    def test_undo_leaves_a_kept_copy_another_import_matched(self):
        review_id, _ = self.resolve("keep_both")
        opened = self.opened
        kept = self.item_named(PAIR_NAME)
        opened.add(DedupeDecision(export_id=self.export.id, account_id=self.world.account, source_path="later/IMG.JPG", person_id=kept.person_id,
                                  matched_item_id=kept.id, tier=1, reason="same SHA-1 as a takeout item", decided_by="auto", decided_at=2.0))
        opened.commit()
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"),
                         (409, {"error": "a later import matched the kept copy; reverse that decision before undoing this one"}))
        self.assertEqual(self.tree(), before)

    def test_a_reversed_pair_cannot_go_back_to_the_queue(self):
        review_id, _ = self.resolve("keep_both")
        self.assertEqual(self.call("POST", f"/api/decisions/{self.decision_of(PAIR_NAME).id}/reverse")[0], 200)
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"),
                         (409, {"error": "this pair's dedupe decision was reversed, so the pair can no longer go back to the queue"}))
        self.assertEqual(self.opened.get(ReviewItem, review_id).status, "kept_both")
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.call("GET", "/api/review?status=pending")[1]["reviews"], [])

    def test_undo_proceeds_once_the_later_match_is_reversed(self):
        review_id, _ = self.resolve("keep_both")
        opened = self.opened
        kept = self.item_named(PAIR_NAME)
        later = DedupeDecision(export_id=self.export.id, account_id=self.world.account, source_path="later/IMG.JPG", person_id=kept.person_id,
                               matched_item_id=kept.id, tier=1, reason="same SHA-1 as a takeout item", decided_by="auto", decided_at=2.0)
        opened.add(later)
        opened.commit()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 409)
        self.assertEqual(self.call("POST", f"/api/decisions/{later.id}/reverse")[0], 200)
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertFalse((self.world.root / "jo" / "google-only" / "2019" / "08" / PAIR_NAME).exists())

    def test_a_failing_sidecar_removal_on_undo_changes_nothing(self):
        review_id, _ = self.resolve("keep_both")
        kept = self.item_named(PAIR_NAME)
        sidecar = (self.world.root / kept.path).with_name(f"{PAIR_NAME}.xmp")
        if not sidecar.exists():
            sidecar.write_bytes(b"<x:xmpmeta/>")
        before = self.tree()
        real = os.rename

        def rename(source, target):
            if str(source).endswith(".xmp"):
                raise PermissionError("made to fail")
            return real(source, target)

        with mock.patch.object(paths.os, "rename", side_effect=rename):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"),
                             (409, {"error": "the kept copy could not be removed, so nothing was changed"}))
        self.assertEqual(self.tree(), before)
        row = self.opened.get(ReviewItem, review_id)
        self.assertEqual(row.status, "kept_both")
        self.assertEqual(self.opened.get(DedupeDecision, row.decision_id).takeout_item_id, kept.id)
        self.assertIsNotNone(self.opened.get(MediaItem, kept.id))
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)

    def test_undo_detaches_a_kept_copy_that_another_decision_also_placed(self):
        review_id, _ = self.resolve("keep_both")
        opened = self.opened
        kept = self.item_named(PAIR_NAME)
        opened.add(DedupeDecision(export_id=self.export.id, account_id=self.world.account, source_path="other/IMG.JPG", person_id=kept.person_id,
                                  takeout_item_id=kept.id, tier=5, reason="no match", decided_by="auto", decided_at=2.0))
        opened.commit()
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
        self.assertEqual(self.tree(), before)
        self.assertIsNotNone(self.opened.get(MediaItem, kept.id))

    def test_undo_leaves_a_kept_copy_that_moved_away_from_google_only(self):
        review_id, _ = self.resolve("keep_both")
        opened = self.opened
        opened.get(MediaItem, self.item_named(PAIR_NAME).id).path = "jo/originals/moved-away.JPG"
        opened.commit()
        before = self.tree()
        self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo"),
                         (409, {"error": "the kept copy is not where it was placed, so it was left alone"}))
        self.assertEqual(self.tree(), before)

    def pending_or_none(self):
        return self.opened.execute(select(ReviewItem).where(ReviewItem.status == "pending")).scalar_one_or_none()


class DecisionTests(ReviewCase):
    def test_the_decisions_list_filters_by_tier_person_and_export(self):
        status, body = self.call("GET", "/api/decisions")
        self.assertEqual(status, 200)
        self.assertEqual((body["total"], body["page"], body["page_size"]), (len(body["decisions"]), 0, 50))
        tiers = self.call("GET", "/api/decisions?tier=1")[1]["decisions"]
        self.assertTrue(tiers)
        self.assertTrue(all(row["tier"] == 1 for row in tiers))
        sams = self.call("GET", f"/api/decisions?person={self.world.ids['sam']}")[1]["decisions"]
        self.assertTrue(sams)
        self.assertTrue(all(row["person_id"] == self.world.ids["sam"] for row in sams))
        self.assertEqual(self.call("GET", "/api/decisions?export=999")[1]["decisions"], [])
        self.assertEqual(self.call("GET", "/api/decisions?page=1")[1]["decisions"], [])
        (pair,) = self.call("GET", "/api/decisions?tier=4")[1]["decisions"]
        self.assertEqual((pair["name"], pair["review_status"]), (PAIR_NAME, "pending"))

    def test_bad_filters_are_400(self):
        for query, message in (("tier=6", "tier must be a whole number"), ("person=x", "person must be a whole number"), ("page=-1", "page must be a whole number")):
            with self.subTest(query=query):
                self.assertEqual(self.call("GET", f"/api/decisions?{query}"), (400, {"error": message}))

    def test_reversing_a_tier_one_match_clears_the_flag(self):
        decision = self.decision_of("IMG_1001.JPG")
        phone = self.item_named("IMG_1001.JPG", "phone")
        self.assertTrue(phone.confirmed_in_google)
        unmatched = run.unmatched_count(self.opened, phone.person_id, NOW)
        status, body = self.call("POST", f"/api/decisions/{decision.id}/reverse")
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(body["decision"]["reversed_at"])
        self.assertFalse(self.opened.get(MediaItem, phone.id).confirmed_in_google)
        self.assertEqual(run.unmatched_count(self.opened, phone.person_id, NOW), unmatched + 1)
        self.assertEqual(self.call("POST", f"/api/decisions/{decision.id}/reverse"), (409, {"error": "this decision is already reversed"}))

    def test_reversing_keeps_the_flag_while_another_tier_one_or_two_decision_confirms(self):
        decision = self.decision_of("IMG_1003.JPG")
        phone = self.item_named("IMG_1003.JPG", "phone")
        opened = self.opened
        opened.add(DedupeDecision(export_id=self.export.id, account_id=self.world.account, source_path="again/IMG_1003.JPG", person_id=phone.person_id,
                                  matched_item_id=phone.id, tier=1, reason="same SHA-1 as a phone item", decided_by="auto", decided_at=2.0))
        opened.commit()
        self.assertEqual(self.call("POST", f"/api/decisions/{decision.id}/reverse")[0], 200)
        self.assertTrue(self.opened.get(MediaItem, phone.id).confirmed_in_google)

    def test_a_pending_pair_is_decided_in_the_queue_not_reversed(self):
        decision = self.decision_of(PAIR_NAME)
        self.assertEqual(self.call("POST", f"/api/decisions/{decision.id}/reverse"),
                         (409, {"error": "this pair is waiting in the review queue; decide it there instead"}))
        self.assertIsNone(self.opened.get(DedupeDecision, decision.id).reversed_at)

    def test_reversing_a_decided_pair_never_sets_the_flag(self):
        self.call("POST", f"/api/review/{self.pending().id}/resolve", {"action": "keep_both"})
        before = self.confirmed()
        self.assertEqual(self.call("POST", f"/api/decisions/{self.decision_of(PAIR_NAME).id}/reverse")[0], 200)
        self.assertEqual(self.confirmed(), before)

    def test_a_missing_decision_is_404(self):
        for path in ("/api/decisions/999/reverse", "/api/decisions/x/reverse"):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path), (404, {"error": "no such dedupe decision"}))


class MissingGpsTests(ReviewCase):
    def test_the_list_has_only_items_without_a_location_newest_first(self):
        status, body = self.call("GET", "/api/media?gps=none")
        self.assertEqual(status, 200)
        names = [row["name"] for row in body["media"]]
        self.assertNotIn(GOOGLE_ONLY_NAME, names)
        self.assertIn("IMG_2002.JPG", names)
        self.assertEqual(body["total"], len(names))
        times = [row["captured_utc"] for row in body["media"] if row["captured_utc"] is not None]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertIsNone(body["media"][-1]["captured_utc"])
        self.assertTrue(all(row["lat"] is None or row["lon"] is None for row in body["media"]))

    def test_the_list_filters_by_person_and_time(self):
        sam = self.world.ids["sam"]
        sams = self.call("GET", f"/api/media?gps=none&person={sam}")[1]["media"]
        self.assertTrue(sams)
        self.assertTrue(all(row["person_id"] == sam for row in sams))
        recent = self.call("GET", "/api/media?gps=none&from=1560000000&to=1570000000")[1]["media"]
        self.assertTrue(recent)
        self.assertTrue(all(1560000000 <= row["captured_utc"] < 1570000000 for row in recent))
        self.assertEqual(self.call("GET", "/api/media?gps=none&page=5")[1]["media"], [])

    def test_bad_filters_are_400(self):
        for query, message in (("", "gps=none is the only filter so far"), ("gps=exif", "gps=none is the only filter so far"),
                               ("gps=none&from=soon", "from must be a time in seconds"), ("gps=none&to=nan", "to must be a time in seconds"),
                               ("gps=none&to=1e20", "to must be a time in seconds"), ("gps=none&person=-2", "person must be a whole number")):
            with self.subTest(query=query):
                self.assertEqual(self.call("GET", f"/api/media?{query}"), (400, {"error": message}))


class PrivacyTests(ReviewCase):
    def test_review_actions_log_no_file_names_and_reply_no_absolute_paths(self):
        root = str(self.world.root)
        review_id = self.pending().id
        with no_leaks(self, PAIR_NAME, GOOGLE_ONLY_NAME, root):
            self.call("GET", "/api/review")
            self.call("GET", f"/api/review/{review_id}/thumb/library")
            self.call("GET", f"/api/review/{review_id}/thumb/export")
            self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
            self.call("POST", f"/api/review/{review_id}/undo")
            self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
            self.call("POST", f"/api/media/{self.item_named(GOOGLE_ONLY_NAME).id}/reassign", {"person_id": self.world.ids["sam"]})
            self.call("GET", "/api/review/999/thumb/library")
        with no_leaks(self, root, replies=[reply for reply in self.replies if not reply.startswith(b"\xff\xd8")]):
            self.call("GET", f"/api/review/{review_id}")
            self.call("GET", "/api/media?gps=none")
            self.call("GET", "/api/decisions")
        details = " ".join(f"{row.subject} {row.detail}" for row in self.opened.execute(select(Event)).scalars())
        for canary in (PAIR_NAME, GOOGLE_ONLY_NAME, root):
            self.assertNotIn(canary, details)


if __name__ == "__main__":
    unittest.main()


class DecidedByTests(ReviewCase):
    def sign_in_as(self, slug):
        opened = self.opened
        opened.get(Person, self.world.ids[slug]).email = f"{slug}@example.com"
        opened.merge(User(sub=slug, email=f"{slug}@example.com", email_verified=True))
        opened.commit()
        return mock.patch.object(auth, "LOCAL_OWNER", Principal("session", slug, f"{slug}@example.com", slug.title(), ()))

    def listed(self):
        return self.call("GET", "/api/review?status=kept_both")[1]["reviews"]

    def test_a_decision_names_the_person_who_made_it(self):
        review_id = self.pending().id
        with self.sign_in_as("sam"):
            status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        self.assertEqual((status, body["review"]["decided_by_person_id"]), (200, self.world.ids["sam"]))
        self.assertEqual([row["decided_by_person_id"] for row in self.listed()], [self.world.ids["sam"]])
        with self.sign_in_as("jo"):
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/undo")[0], 200)
            self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})[1]["review"]["decided_by_person_id"], self.world.ids["jo"])
        self.assertEqual([row["decided_by_person_id"] for row in self.listed()], [self.world.ids["jo"]])

    def test_a_decision_by_someone_no_person_matches_has_no_name(self):
        review_id = self.pending().id
        with mock.patch.object(auth, "LOCAL_OWNER", Principal("session", "x", "nobody@example.com", "Nobody", ())):
            status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        self.assertEqual((status, body["review"]["decided_by_person_id"]), (200, None))

    def test_a_decision_recorded_before_people_were_named_has_no_name(self):
        review_id = self.pending().id
        with self.sign_in_as("jo"):
            self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        opened = self.opened
        opened.execute(select(Event).where(Event.type == "review_resolved")).scalar_one().detail = "keep_both"
        opened.commit()
        self.assertEqual([row["decided_by_person_id"] for row in self.listed()], [None])

    def test_a_reassigned_copy_names_the_person_who_moved_it(self):
        review_id = self.pending().id
        with self.sign_in_as("jo"):
            kept = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})[1]["review"]
        with self.sign_in_as("sam"):
            status, _ = self.call("POST", f"/api/media/{kept['kept_item_id']}/reassign", {"person_id": self.world.ids["sam"]})
        self.assertEqual(status, 200)
        (row,) = self.listed()
        self.assertEqual((row["kept_person_id"], row["decided_by_person_id"]), (self.world.ids["sam"], self.world.ids["sam"]))

    def test_the_name_is_taken_from_the_latest_decision_by_id(self):
        review_id = self.pending().id
        with mock.patch.object(review_api, "now", return_value=NOW + 2 * DAY), self.sign_in_as("jo"):
            self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
            self.call("POST", f"/api/review/{review_id}/undo")
        with mock.patch.object(review_api, "now", return_value=NOW + DAY), self.sign_in_as("sam"):
            self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
        self.assertEqual([row["decided_by_person_id"] for row in self.listed()], [self.world.ids["sam"]])

    def test_two_people_with_one_email_or_an_unverified_email_give_nobody(self):
        review_id = self.pending().id
        opened = self.opened
        opened.get(Person, self.world.ids["jo"]).email = "shared@example.com"
        opened.get(Person, self.world.ids["sam"]).email = "shared@example.com"
        opened.commit()
        opened.add(User(sub="x", email="shared@example.com", email_verified=True))
        opened.commit()
        principal = Principal("session", "x", "shared@example.com", "Shared", ())
        with mock.patch.object(auth, "LOCAL_OWNER", principal):
            status, body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})
            self.assertEqual((status, body["review"]["decided_by_person_id"]), (200, None))
            self.call("POST", f"/api/review/{review_id}/undo")
        opened = self.opened
        opened.get(Person, self.world.ids["sam"]).email = None
        opened.get(User, "x").email_verified = False
        opened.commit()
        with mock.patch.object(auth, "LOCAL_OWNER", principal):
            body = self.call("POST", f"/api/review/{review_id}/resolve", {"action": "keep_both"})[1]
        self.assertIsNone(body["review"]["decided_by_person_id"])
