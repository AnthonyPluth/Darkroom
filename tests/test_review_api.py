import os
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.domain import review
from darkroom.domain.ingest import cleanup, place, run
from darkroom.providers import exiftool
from darkroom.server import routes
from darkroom.server.api import review as review_api
from darkroom.storage import paths
from darkroom.storage.models import DedupeDecision, Event, MediaItem, ReviewItem
from tests.import_world import NOW, sha1_of
from tests.privacy import no_leaks
from tests.review_world import GOOGLE_ONLY_NAME, PAIR_NAME, ReviewCase

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
            review.resolve(self.opened, self.world.photos, review_id, "skip", 1.0)
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
            review.resolve(self.opened, self.world.photos, review_id, "keep_both", 1.0)
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
                             (409, {"error": "the held copy could not be placed in google-only, and the copy it made there could not be removed"}))
        self.assertTrue((folder / PAIR_NAME).is_file())
        self.assertEqual(self.pending().id, review_id)
        self.assertEqual([c for c in logged.call_args_list if c.args[0].startswith("keep both")], [mock.call("keep both rollback left files", left=1)])

    def test_keep_both_works_once_staging_is_cleaned(self):
        staging = self.world.root / "_inbox" / "jo" / "export-1" / "staging"
        for path in sorted(staging.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        status, body = self.call("POST", f"/api/review/{self.pending().id}/resolve", {"action": "keep_both"})
        self.assertEqual(status, 200, body)
        self.assertTrue((self.world.root / "jo" / "google-only" / "2019" / "08" / PAIR_NAME).is_file())

    def test_a_bad_action_is_400(self):
        review_id = self.pending().id
        for body, message in (({"action": "delete"}, "action is keep_both; skip leaves the pair for later and saves nothing"), ({}, "action is required"), ({"action": 3}, "action must be text")):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", f"/api/review/{review_id}/resolve", body), (400, {"error": message}))
        with self.assertRaises(review.WrongState):
            review.resolve(self.opened, self.world.photos, review_id, "delete", 1.0)


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
