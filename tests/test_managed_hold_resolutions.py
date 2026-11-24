import ast
import hashlib
import inspect
import json
import threading
from datetime import UTC, datetime
from unittest import mock

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from darkroom import tls
from darkroom.domain import hold_resolutions, holds, managed_metadata
from darkroom.server import auth, routes
from darkroom.server.api import holds as holds_api
from darkroom.server.common import Principal
from darkroom.storage.models import (DedupeDecision, Event, ManagedHoldResolution, ManagedImportIntent, ManagedImportReceipt, Person, ReviewItem, TakeoutEntry,
                                     User)
from tests.privacy import no_leaks
from tests.test_managed_holds import ASSET, BACKFILL_NAME, COORDINATE, DESCRIPTION, DIFFERS, ENTRY_NAME, KEPT_NAME, SHA1, HoldsWorld

NOW = 1_800_000_000.0
VENICE = (45.4340123, 12.3388123)
CLOCK_FACTS = {"wall_clock": "2021-03-04T05:06:07", "file_utc": None, "google_utc": 1614834367.0, "place_known": True, "description_length": None,
               "description_unwritable": False}
DIFFERS_JSON = json.dumps(DIFFERS)
TOMORROW = datetime.fromtimestamp(NOW + 86400, UTC).replace(tzinfo=None).isoformat(timespec="seconds")
TOMORROW_AND_A_SECOND = datetime.fromtimestamp(NOW + 86401, UTC).replace(tzinfo=None).isoformat(timespec="seconds")
AWKWARD = json.dumps({"description": "lone \ud800 surrogate é and a tab\t", "latitude": None, "longitude": None}, sort_keys=True)


def snapshot_at(place, hold):
    latitude, longitude = place if place else (None, None)
    return managed_metadata.snapshot(managed_metadata.Intended("single", "IMAGE", None, None, None, latitude, longitude, None if place is None else "media", DESCRIPTION, "media",
                                                                (), hold))


class ResolutionWorld(HoldsWorld):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as opened:
            self.receipt(opened, 8, 1, "takeout-entry:5", "no_instant", {"wall_clock": None, "file_utc": None, "google_utc": None, "place_known": False,
                                                                           "description_length": None, "description_unwritable": False}, "prepared")
            self.receipt(opened, 9, 1, "takeout-entry:6", "unwritable", {"wall_clock": None, "file_utc": None, "google_utc": 1614834367.0, "place_known": False,
                                                                          "description_length": 9, "description_unwritable": True}, "prepared")
            self.receipt(opened, 10, 1, "takeout-entry:7", "unknown_offset", CLOCK_FACTS, "prepared")
            self.receipt(opened, 11, 1, "takeout-entry:8", "no_instant", CLOCK_FACTS | {"wall_clock": None}, "prepared")
            opened.commit()
        self.where(10, snapshot_at(VENICE, "unknown_offset"))
        self.where(11, snapshot_at(VENICE, "no_instant"))

    def where(self, receipt_id, expected):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE managed_import_receipts SET expected_metadata = :expected WHERE id = :id"), {"expected": expected, "id": receipt_id})

    def resolve(self, receipt_id, body):
        return self.call("POST", f"/api/holds/{receipt_id}/resolve", body)

    def undo(self, receipt_id):
        return self.call("POST", f"/api/holds/{receipt_id}/undo")

    def rows(self):
        with self.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT receipt_id, hold, action, offset_minutes, taken_local, state, result, decided_by_person_id FROM managed_hold_resolutions "
                                              "ORDER BY id").all()

    def receipts(self):
        with self.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT id, state, metadata_hold, hold_facts, expected_metadata, updated_at, send_attempts, send_lease_until "
                                              "FROM managed_import_receipts ORDER BY id").all()

    def events(self):
        with self.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT type, subject, detail FROM events WHERE type IN ('managed_hold_resolved', 'managed_hold_undone', 'managed_hold_applied') ORDER BY id").all()

    def apply(self, receipt_id, action, **values):
        with Session(self.engine) as opened:
            receipt = opened.get(ManagedImportReceipt, receipt_id)
            previous = json.dumps({"expected_metadata": receipt.expected_metadata, "metadata_hold": receipt.metadata_hold, "hold_facts": receipt.hold_facts})
            opened.add(ManagedHoldResolution(receipt_id=receipt_id, hold=receipt.metadata_hold, action=action, state="applied", result="cleared", previous_metadata=previous,
                                             decided_at=1.0, applied_at=2.0, **values))
            receipt.metadata_hold, receipt.hold_facts = None, None
            receipt.expected_metadata = json.dumps({"changed": True})
            opened.commit()


class ResolveTests(ResolutionWorld):
    def test_the_routes_need_a_signed_in_session(self):
        for path in ("/api/holds/{id}/resolve", "/api/holds/{id}/undo"):
            found = routes.find("POST", path.replace("{id}", "1"))
            self.assertIsNotNone(found)
            self.assertEqual(found.auth, "session")

    def test_an_offset_is_recorded_for_the_next_tick_and_changes_nothing_else(self):
        before = self.receipts()
        with mock.patch.object(holds_api, "now", return_value=NOW):
            status, body = self.resolve(1, {"action": "offset", "offset_minutes": 120})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["resolution"], {"id": 1, "receipt_id": 1, "hold": "unknown_offset", "action": "offset", "offset_minutes": 120, "taken_local": None,
                                              "state": "recorded", "result": None, "decided_by_person_id": None, "decided_at": NOW, "applied_at": None, "undone_at": None,
                                              "can_undo": True})
        self.assertEqual(self.rows(), [(1, "unknown_offset", "offset", 120, None, "recorded", None, None)])
        self.assertEqual(self.receipts(), before)
        self.assertEqual(self.events(), [("managed_hold_resolved", "receipt 1", "unknown_offset offset")])

    def test_a_capture_time_is_recorded_with_its_offset_for_a_hold_with_no_instant(self):
        status, body = self.resolve(8, {"action": "capture_time", "taken_local": "2019-07-04T12:30:15", "offset_minutes": -330})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.rows(), [(8, "no_instant", "capture_time", -330, "2019-07-04T12:30:15", "recorded", None, None)])

    def test_every_action_the_design_allows_is_recorded_on_its_hold(self):
        cases = [(1, {"action": "skip"}), (8, {"action": "skip"}), (9, {"action": "drop_description"}), (3, {"action": "recheck"}), (2, {"action": "accept"}),
                 (4, {"action": "recheck"}), (5, {"action": "skip"})]
        for number, body in cases:
            with self.subTest(receipt=number, body=body):
                status, reply = self.resolve(number, body)
                self.assertEqual(status, 200, reply)
        status, reply = self.resolve(2, {"action": "record_offset", "offset_minutes": 0})
        self.assertEqual(status, 409, reply)
        self.undo(2)
        status, reply = self.resolve(2, {"action": "record_offset", "offset_minutes": 0})
        self.assertEqual(status, 200, reply)
        self.assertEqual({(row[1], row[2]) for row in self.rows() if row[5] == "recorded"},
                         {("unknown_offset", "skip"), ("no_instant", "skip"), ("unwritable", "drop_description"), ("unusable", "recheck"), ("not_settled", "recheck"),
                          ("not_enriched", "record_offset"), ("unusable", "skip")})

    def test_an_action_the_hold_does_not_allow_is_refused_and_records_nothing(self):
        holds_of = {1: "unknown_offset", 8: "no_instant", 9: "unwritable", 2: "not_enriched", 4: "not_settled", 3: "unusable"}
        every = ("offset", "capture_time", "drop_description", "accept", "record_offset", "recheck", "skip")
        for number, hold in holds_of.items():
            for action in every:
                if action in holds.allowed(hold, holds.facts_of(DIFFERS_JSON if hold == "not_enriched" else None)):
                    continue
                with self.subTest(hold=hold, action=action):
                    status, body = self.resolve(number, {"action": action, "offset_minutes": 60, "taken_local": "2020-01-01T00:00:00"})
                    self.assertEqual((status, body), (422, {"error": hold_resolutions.NOT_ALLOWED}))
        for action in ("bogus", "keep_both", "", "OFFSET"):
            with self.subTest(action=action):
                self.assertEqual(self.resolve(1, {"action": action, "offset_minutes": 60})[0], 422 if action else 400)
        self.assertEqual(self.rows(), [])

    def test_recording_an_offset_for_a_hold_where_the_offset_is_not_what_differs_is_refused(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""UPDATE managed_import_receipts SET hold_facts = '{"differs": ["description"]}' WHERE id = 2""")
        self.assertEqual(self.resolve(2, {"action": "record_offset", "offset_minutes": 60}), (422, {"error": hold_resolutions.NOT_ALLOWED}))
        self.assertEqual(self.rows(), [])

    def test_no_offset_is_invented_so_a_value_the_action_needs_must_come_from_the_owner(self):
        for number, body, reason in ((1, {"action": "offset"}, hold_resolutions.NEEDS_OFFSET), (1, {"action": "offset", "offset_minutes": None}, hold_resolutions.NEEDS_OFFSET),
                                     (8, {"action": "capture_time", "taken_local": "2020-01-01T00:00:00"}, hold_resolutions.NEEDS_OFFSET),
                                     (8, {"action": "capture_time", "offset_minutes": 60}, hold_resolutions.NEEDS_TIME),
                                     (8, {"action": "capture_time"}, hold_resolutions.NEEDS_OFFSET),
                                     (2, {"action": "record_offset"}, hold_resolutions.NEEDS_OFFSET)):
            with self.subTest(body=body):
                self.assertEqual(self.resolve(number, body), (422, {"error": reason}))
        self.assertEqual(self.rows(), [])

    def test_a_value_the_action_does_not_take_is_refused(self):
        for number, body, reason in ((1, {"action": "skip", "offset_minutes": 60}, hold_resolutions.NO_OFFSET),
                                     (1, {"action": "offset", "offset_minutes": 60, "taken_local": "2020-01-01T00:00:00"}, hold_resolutions.NO_TIME),
                                     (9, {"action": "drop_description", "taken_local": "2020-01-01T00:00:00"}, hold_resolutions.NO_TIME),
                                     (3, {"action": "recheck", "offset_minutes": 0}, hold_resolutions.NO_OFFSET)):
            with self.subTest(body=body):
                self.assertEqual(self.resolve(number, body), (422, {"error": reason}))
        status, body = self.resolve(1, {"action": "skip", "suggested": 1})
        self.assertEqual((status, body), (422, {"error": "the request has a field that no resolution takes"}))
        self.assertEqual(self.rows(), [])

    def test_an_offset_outside_the_range_or_the_step_or_not_a_whole_number_is_refused_and_the_edges_are_accepted(self):
        for value in (-735, 855, 7, -721, 841, 1.5, 120.0, "60", True, [60], {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(self.resolve(1, {"action": "offset", "offset_minutes": value}), (422, {"error": hold_resolutions.BAD_OFFSET}))
        self.assertEqual(self.rows(), [])
        for value in (-720, 840, 345, -30, 0, 15):
            with self.subTest(value=value):
                status, body = self.resolve(1, {"action": "offset", "offset_minutes": value})
                self.assertEqual((status, body["resolution"]["offset_minutes"]), (200, value))
                self.assertEqual(self.undo(1)[0], 200)

    def test_a_capture_time_must_be_a_plain_date_and_time_between_1900_and_tomorrow(self):
        later = {"action": "capture_time", "offset_minutes": 0}
        with mock.patch.object(holds_api, "now", return_value=NOW):
            for text in ("2020-01-01 00:00:00", "2020-01-01T00:00", "2020-01-01T00:00:00+02:00", "2020-01-01T00:00:00Z", "1899-12-31T23:59:59", "2020-02-30T00:00:00",
                         "2020-13-01T00:00:00", "2020-01-01T24:00:00", "abcdefghijklmnopqrs", 20200101, ["2020-01-01T00:00:00"], "", "2030-01-01T00:00:00", TOMORROW_AND_A_SECOND):
                with self.subTest(text=text):
                    status, body = self.resolve(8, later | {"taken_local": text})
                    self.assertEqual(status, 422, body)
                    self.assertIn(body["error"], (hold_resolutions.BAD_TIME, hold_resolutions.NEEDS_TIME))
            self.assertEqual(self.rows(), [])
            for text in ("1900-01-01T00:00:00", TOMORROW, "2026-11-24T12:00:00"):
                with self.subTest(text=text):
                    status, body = self.resolve(8, later | {"taken_local": text})
                    self.assertEqual((status, body["resolution"]["taken_local"]), (200, text))
                    self.undo(8)

    def test_an_offset_that_disagrees_with_the_place_immich_will_see_is_refused_without_naming_one(self):
        replies = []
        with no_leaks(self, COORDINATE, "12.3388123", replies=replies):
            status, body = self.resolve(10, {"action": "offset", "offset_minutes": 120})
            replies.append(json.dumps(body).encode())
            self.assertEqual((status, body), (422, {"error": hold_resolutions.PLACE}))
            status, body = self.resolve(11, {"action": "capture_time", "taken_local": "2021-03-04T05:06:07", "offset_minutes": 0})
            self.assertEqual((status, body), (422, {"error": hold_resolutions.PLACE}))
        self.assertNotRegex(hold_resolutions.PLACE, r"\d")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.resolve(10, {"action": "offset", "offset_minutes": 60})[0], 200)
        self.assertEqual(self.resolve(11, {"action": "capture_time", "taken_local": "2021-07-04T05:06:07", "offset_minutes": 120})[0], 200)

    def test_a_place_that_is_not_in_the_snapshot_or_has_no_zone_does_not_stop_an_offset(self):
        self.where(10, snapshot_at(None, "unknown_offset"))
        self.assertEqual(self.resolve(10, {"action": "offset", "offset_minutes": 330})[0], 200)
        self.where(11, "not json")
        self.assertEqual(self.resolve(11, {"action": "capture_time", "taken_local": "2021-03-04T05:06:07", "offset_minutes": 330})[0], 200)

    def test_a_second_resolution_of_the_same_hold_is_refused_until_the_first_is_undone(self):
        self.assertEqual(self.resolve(1, {"action": "offset", "offset_minutes": 60})[0], 200)
        self.assertEqual(self.resolve(1, {"action": "skip"}), (409, {"error": hold_resolutions.ALREADY}))
        self.assertEqual(self.resolve(1, {"action": "offset", "offset_minutes": 120}), (409, {"error": hold_resolutions.ALREADY}))
        self.assertEqual([row[3] for row in self.rows()], [60])
        self.assertEqual(self.undo(1)[0], 200)
        self.assertEqual(self.resolve(1, {"action": "skip"})[0], 200)
        self.assertEqual([row[5] for row in self.rows()], ["undone", "recorded"])

    def test_two_resolutions_arriving_together_record_exactly_one(self):
        start, statuses = threading.Barrier(2), []

        def post(value):
            start.wait()
            statuses.append(self.resolve(1, {"action": "offset", "offset_minutes": value})[0])

        threads = [threading.Thread(target=post, args=(value,)) for value in (60, 120)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(len(self.rows()), 1)

    def test_a_resolution_that_lost_a_race_the_check_did_not_see_is_a_conflict_and_not_a_second_row(self):
        with Session(self.engine) as opened:
            opened.add(ManagedHoldResolution(receipt_id=1, hold="unknown_offset", action="skip", state="recorded", decided_at=1.0))
            opened.commit()
        with mock.patch.object(hold_resolutions, "open_row", return_value=None):
            self.assertEqual(self.resolve(1, {"action": "offset", "offset_minutes": 60}), (409, {"error": hold_resolutions.ALREADY}))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.events(), [])

    def test_a_receipt_that_is_not_held_or_does_not_exist_cannot_be_resolved(self):
        self.assertEqual(self.resolve(6, {"action": "skip"}), (409, {"error": hold_resolutions.NOT_HELD}))
        self.assertEqual(self.resolve(7, {"action": "skip"}), (409, {"error": hold_resolutions.NOT_HELD}))
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET state = 'committed', committed_at = 4.0, metadata_evidence = '{}', metadata_verified_at = 4.0 WHERE id = 2")
        self.assertEqual(self.resolve(2, {"action": "accept"}), (409, {"error": hold_resolutions.NOT_HELD}))
        self.assertEqual(self.resolve(999, {"action": "skip"}), (404, {"error": holds_api.NO_HOLD}))
        self.assertEqual(self.undo(999), (404, {"error": holds_api.NO_HOLD}))
        self.assertEqual(self.call("POST", "/api/holds/x/resolve", {"action": "skip"}), (404, {"error": holds_api.NO_HOLD}))
        self.assertEqual(self.call("POST", "/api/holds/x/undo"), (404, {"error": holds_api.NO_HOLD}))
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET resource_key = 'part/other', compound_key = 'part/other' WHERE id = 1")
        self.assertEqual(self.resolve(1, {"action": "skip"}), (404, {"error": holds_api.NO_HOLD}))
        self.assertEqual(self.rows(), [])

    def test_a_malformed_body_is_refused_before_anything_is_recorded(self):
        self.assertEqual(self.call("POST", "/api/holds/1/resolve", raw=b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/holds/1/resolve", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", "/api/holds/1/resolve", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/holds/1/resolve", {"action": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/holds/1/resolve", raw=b" " * 2000)[0], 400)
        self.assertEqual(self.rows(), [])

    def test_the_list_shows_the_open_resolution_and_whether_it_can_be_undone(self):
        self.assertEqual(self.resolve(1, {"action": "offset", "offset_minutes": 120})[0], 200)
        by_id = {row["id"]: row for row in self.listed()["holds"]}
        self.assertEqual((by_id[1]["resolution"]["action"], by_id[1]["resolution"]["state"], by_id[1]["resolution"]["can_undo"], by_id[1]["resolution"]["offset_minutes"]),
                         ("offset", "recorded", True, 120))
        self.assertIsNone(by_id[2]["resolution"])
        self.undo(1)
        self.assertIsNone({row["id"]: row for row in self.listed()["holds"]}[1]["resolution"])

    def test_the_decision_names_who_made_it(self):
        with Session(self.engine) as opened:
            sam = opened.scalars(select(Person).where(Person.slug == "sam")).one()
            sam.email = "sam@example.com"
            opened.merge(User(sub="sam", email="sam@example.com", email_verified=True))
            opened.commit()
            sam_id = sam.id
        with mock.patch.object(auth, "LOCAL_OWNER", Principal("session", "sam", "sam@example.com", "Sam", ())):
            status, body = self.resolve(1, {"action": "skip"})
            self.assertEqual((status, body["resolution"]["decided_by_person_id"]), (200, sam_id))
            self.undo(1)
        self.assertEqual(self.events(), [("managed_hold_resolved", "receipt 1", f"unknown_offset skip, by person {sam_id}"),
                                         ("managed_hold_undone", "receipt 1", f"unknown_offset skip, by person {sam_id}")])
        with mock.patch.object(auth, "LOCAL_OWNER", Principal("session", "x", "nobody@example.com", "Nobody", ())):
            self.assertIsNone(self.resolve(1, {"action": "skip"})[1]["resolution"]["decided_by_person_id"])


class NothingHappensOutsideTheDatabaseTests(ResolutionWorld):
    def every_request(self):
        sent = []
        for number, body in ((1, {"action": "offset", "offset_minutes": 90}), (8, {"action": "capture_time", "taken_local": "2020-05-05T05:05:05", "offset_minutes": 60}),
                             (9, {"action": "drop_description"}), (2, {"action": "accept"}), (4, {"action": "recheck"}), (3, {"action": "skip"})):
            sent.append(self.resolve(number, body)[0])
            sent.append(self.undo(number)[0])
            sent.append(self.resolve(number, body)[0])
        sent += [self.resolve(5, {"action": "recheck"})[0], self.resolve(1, {"action": "skip"})[0], self.undo(6)[0], self.resolve(1, {"action": "bogus"})[0]]
        return sent

    def test_no_resolution_or_undo_reaches_immich_or_any_other_host(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a hold resolution reached out")

        with mock.patch.object(tls, "urlopen", side_effect=refuse):
            sent = self.every_request()
        self.assertEqual(sent.count(500), 0)
        self.assertIn(200, sent)
        imported = {name for node in ast.walk(ast.parse(inspect.getsource(hold_resolutions))) for name in
                    ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])}
        self.assertEqual({name for name in imported if name.startswith(("darkroom.providers", "darkroom.tls", "urllib", "subprocess", "http"))}, set())
        self.assertNotIn("immich", imported)

    def test_nothing_is_written_deleted_or_added_beside_a_photo(self):
        for folder, name in (("originals", "IMG_0001.jpg"), ("google-only", "from-google.jpg"), ("_inbox/jo/held/export-1", "held.jpg"), ("_inbox/jo/export-1", "part-1.zip")):
            (self.photos / folder).mkdir(parents=True, exist_ok=True)
            (self.photos / folder / name).write_bytes(f"made-up bytes of {name}".encode())

        def digest():
            return {str(path.relative_to(self.photos)): hashlib.sha1(path.read_bytes()).hexdigest() for path in sorted(self.photos.rglob("*")) if path.is_file()}

        before = digest()
        self.every_request()
        self.assertEqual(digest(), before)
        self.assertEqual([path for path in self.photos.rglob("*") if path.suffix == ".xmp"], [])

    def test_nothing_is_deleted_by_a_resolution_a_skip_or_an_undo(self):
        def counts():
            with Session(self.engine) as opened:
                return [len(opened.scalars(select(model)).all()) for model in (ManagedImportReceipt, ManagedImportIntent, TakeoutEntry, ReviewItem, DedupeDecision, Person)]

        before, receipts = counts(), self.receipts()
        self.every_request()
        self.assertEqual(counts(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_a_resolution_never_changes_a_receipt_even_when_it_is_the_one_that_is_recorded(self):
        before = self.receipts()
        for number, body in ((1, {"action": "offset", "offset_minutes": 60}), (9, {"action": "drop_description"}), (2, {"action": "record_offset", "offset_minutes": 0})):
            self.assertEqual(self.resolve(number, body)[0], 200)
        self.assertEqual(self.receipts(), before)

    def test_what_a_hold_names_never_reaches_a_reply_an_event_or_the_log(self):
        replies = []
        canaries = (ENTRY_NAME.rsplit("/", 1)[1], KEPT_NAME, BACKFILL_NAME, DESCRIPTION, COORDINATE, "12.3388123")
        with no_leaks(self, *canaries, replies=replies):
            for number, body in ((1, {"action": "offset", "offset_minutes": 60}), (1, {"action": "skip"}), (3, {"action": "accept"}), (10, {"action": "offset", "offset_minutes": 120}),
                                 (999, {"action": "skip"}), (9, {"action": "drop_description"}), (2, {"action": "record_offset", "offset_minutes": 999})):
                replies.append(json.dumps(self.resolve(number, body)[1]).encode())
            replies.append(json.dumps(self.undo(1)[1]).encode())
        for event in self.events():
            self.assertFalse([word for word in canaries if word in " ".join(str(part) for part in event)])
        with Session(self.engine) as opened:
            texts = " ".join(f"{event.subject} {event.detail}" for event in opened.scalars(select(Event)))
        self.assertFalse([word for word in canaries if word in texts])


class UndoTests(ResolutionWorld):
    def test_undoing_a_recorded_resolution_marks_it_undone_and_changes_nothing_else(self):
        before = self.receipts()
        self.resolve(1, {"action": "offset", "offset_minutes": 60})
        with mock.patch.object(holds_api, "now", return_value=NOW):
            status, body = self.undo(1)
        self.assertEqual((status, body["resolution"]["state"], body["resolution"]["can_undo"]), (200, "undone", False))
        self.assertEqual(self.rows(), [(1, "unknown_offset", "offset", 60, None, "undone", None, None)])
        self.assertEqual(self.receipts(), before)
        self.assertEqual([event[0] for event in self.events()], ["managed_hold_resolved", "managed_hold_undone"])
        self.assertEqual(self.undo(1), (409, {"error": hold_resolutions.NOTHING_TO_UNDO}))

    def test_an_undo_with_nothing_recorded_is_a_conflict(self):
        self.assertEqual(self.undo(1), (409, {"error": hold_resolutions.NOTHING_TO_UNDO}))
        self.assertEqual(self.events(), [])

    def test_an_applied_resolution_that_was_not_sent_is_undone_with_the_hold_back_byte_for_byte(self):
        awkward = (("an unwritable description with a lone surrogate", 9, "unwritable", "drop_description", {}, AWKWARD),
                   ("an offset of -00:30", 1, "unknown_offset", "offset", {"offset_minutes": -30}, snapshot_at(VENICE, "unknown_offset")),
                   ("an offset of +14:00", 10, "unknown_offset", "offset", {"offset_minutes": 840}, snapshot_at(None, "unknown_offset")),
                   ("a capture time at 1900-01-01", 8, "no_instant", "capture_time", {"offset_minutes": -720, "taken_local": "1900-01-01T00:00:00"}, snapshot_at(None, "no_instant")))
        for label, number, hold, action, values, expected in awkward:
            with self.subTest(label):
                self.where(number, expected)
                with self.engine.begin() as connection:
                    connection.execute(text("UPDATE managed_import_receipts SET hold_facts = :facts WHERE id = :id"), {"facts": json.dumps({"description_length": 9, "note": "é"}), "id": number})
                before = [row for row in self.receipts() if row[0] == number]
                self.apply(number, action, **values)
                self.assertNotEqual(next(row for row in self.receipts() if row[0] == number)[2:5], before[0][2:5])
                status, body = self.undo(number)
                self.assertEqual((status, body["resolution"]["state"]), (200, "undone"), body)
                after = next(row for row in self.receipts() if row[0] == number)
                self.assertEqual(after[1:5], before[0][1:5])
                self.assertEqual((after[2], after[7]), (hold, None))

    def test_a_hold_that_was_sent_or_is_being_sent_or_committed_cannot_be_undone(self):
        cases = (("a send was attempted", "UPDATE managed_import_receipts SET send_attempts = 1 WHERE id = 1"),
                 ("a lease is held", "UPDATE managed_import_receipts SET send_lease_until = 99999999999.0 WHERE id = 1"),
                 ("the upload returned", f"UPDATE managed_import_receipts SET state = 'returned', asset_id = '{ASSET}', upload_outcome = 'created' WHERE id = 1"),
                 ("it is committed", f"UPDATE managed_import_receipts SET state = 'committed', asset_id = '{ASSET}', upload_outcome = 'created', verified_sha1 = '{SHA1}', "
                                     "verified_bytes = 5, bytes_verified_at = 3.0, metadata_evidence = '{}', metadata_verified_at = 4.0, committed_at = 4.0, "
                                     "metadata_hold = NULL WHERE id = 1"))
        for label, statement in cases:
            with self.subTest(label):
                self.apply(1, "offset", offset_minutes=60)
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(statement)
                before = self.receipts()
                self.assertEqual(self.undo(1), (409, {"error": hold_resolutions.SENT}))
                self.assertEqual(self.receipts(), before)
                self.assertEqual(self.rows()[-1][5], "applied")
                self.assertEqual(self.events(), [])
                with self.engine.begin() as connection:
                    connection.exec_driver_sql("DELETE FROM managed_hold_resolutions")
                    connection.exec_driver_sql("UPDATE managed_import_receipts SET state = 'prepared', asset_id = NULL, upload_outcome = NULL, verified_sha1 = NULL, verified_bytes = NULL, "
                                               "bytes_verified_at = NULL, metadata_evidence = NULL, metadata_verified_at = NULL, committed_at = NULL, send_attempts = 0, "
                                               "send_lease_until = NULL, metadata_hold = 'unknown_offset', hold_facts = NULL WHERE id = 1")

    def test_the_list_says_an_applied_resolution_that_was_sent_cannot_be_undone(self):
        self.apply(1, "offset", offset_minutes=60)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET metadata_hold = 'unknown_offset', send_attempts = 1 WHERE id = 1")
        row = {row["id"]: row for row in self.listed()["holds"]}[1]["resolution"]
        self.assertEqual((row["state"], row["can_undo"], row["result"]), ("applied", False, "cleared"))
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET send_attempts = 0 WHERE id = 1")
        self.assertTrue({row["id"]: row for row in self.listed()["holds"]}[1]["resolution"]["can_undo"])

    def test_an_applied_resolution_whose_saved_metadata_cannot_be_read_is_not_undone(self):
        for saved in (None, "not json", "[1]", json.dumps({"metadata_hold": None}), json.dumps({"expected_metadata": 5}),
                      json.dumps({"expected_metadata": "{}", "metadata_hold": 5}), json.dumps({"expected_metadata": "{}", "hold_facts": 5})):
            with self.subTest(saved=saved):
                self.apply(1, "offset", offset_minutes=60)
                with self.engine.begin() as connection:
                    connection.execute(text("UPDATE managed_hold_resolutions SET previous_metadata = :saved"), {"saved": saved})
                before = self.receipts()
                self.assertEqual(self.undo(1), (409, {"error": hold_resolutions.UNREADABLE}))
                self.assertEqual(self.receipts(), before)
                with self.engine.begin() as connection:
                    connection.exec_driver_sql("DELETE FROM managed_hold_resolutions")
                    connection.exec_driver_sql("UPDATE managed_import_receipts SET metadata_hold = 'unknown_offset' WHERE id = 1")

    def test_an_undo_that_loses_the_receipt_to_a_send_between_the_check_and_the_write_is_refused(self):
        self.apply(1, "offset", offset_minutes=60)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET send_attempts = 1 WHERE id = 1")
        before = self.receipts()
        with mock.patch.object(hold_resolutions, "unsent", return_value=True):
            self.assertEqual(self.undo(1), (409, {"error": hold_resolutions.SENT}))
        self.assertEqual(self.receipts(), before)
        self.assertEqual(self.rows()[0][5], "applied")
        self.assertEqual(self.events(), [])

    def test_a_superseded_resolution_cannot_be_undone_and_the_next_one_can_be_recorded(self):
        self.apply(1, "offset", offset_minutes=60)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_hold_resolutions SET state = 'superseded'")
            connection.execute(text("UPDATE managed_import_receipts SET metadata_hold = 'not_settled', state = 'bytes_verified', asset_id = :asset, upload_outcome = 'created', "
                                    "verified_sha1 = :sha1, verified_bytes = 5, bytes_verified_at = 3.0, hold_facts = :facts WHERE id = 1"),
                               {"asset": ASSET, "sha1": SHA1, "facts": json.dumps({"differs": ["place"]})})
        self.assertEqual(self.undo(1), (409, {"error": hold_resolutions.NOTHING_TO_UNDO}))
        status, body = self.resolve(1, {"action": "accept"})
        self.assertEqual((status, body["resolution"]["hold"], body["resolution"]["action"]), (200, "not_settled", "accept"))
        self.assertEqual([row[5] for row in self.rows()], ["superseded", "recorded"])
        self.assertIsNone(next(row for row in self.listed()["holds"] if row["id"] == 1)["resolution"]["applied_at"])


class TableRuleTests(ResolutionWorld):
    def insert(self, **values):
        columns = {"receipt_id": 8, "hold": "no_instant", "action": "skip", "state": "recorded", "decided_at": 1.0} | values
        with Session(self.engine) as opened:
            opened.add(ManagedHoldResolution(**columns))
            opened.commit()

    def test_the_database_holds_the_design_s_rules_even_for_a_writer_that_skips_the_routes(self):
        refused = ({"hold": "other"}, {"action": "offset"}, {"action": "accept"}, {"hold": "unknown_offset", "action": "capture_time"},
                   {"action": "capture_time", "offset_minutes": 60}, {"action": "capture_time", "offset_minutes": 60, "taken_local": "2020-01-01"},
                   {"hold": "unknown_offset", "action": "offset"}, {"hold": "unknown_offset", "action": "offset", "offset_minutes": 7},
                   {"hold": "unknown_offset", "action": "offset", "offset_minutes": 845}, {"hold": "unknown_offset", "action": "offset", "offset_minutes": -735},
                   {"action": "skip", "offset_minutes": 0}, {"action": "skip", "taken_local": "2020-01-01T00:00:00"}, {"state": "other"}, {"state": "applied"},
                   {"state": "applied", "applied_at": 2.0}, {"state": "undone"}, {"state": "recorded", "result": "cleared"}, {"state": "recorded", "applied_at": 2.0},
                   {"state": "applied", "applied_at": 2.0, "result": "made-up"}, {"state": "applied", "applied_at": 2.0, "result": "rehold:"}, {"receipt_id": 9999})
        for values in refused:
            with self.subTest(values=values), self.assertRaises(IntegrityError):
                self.insert(**values)
        self.assertEqual(self.rows(), [])
        for values in ({"receipt_id": 1, "hold": "unknown_offset", "action": "offset", "offset_minutes": -720},
                       {"receipt_id": 2, "hold": "unknown_offset", "action": "offset", "offset_minutes": 840},
                       {"receipt_id": 9, "action": "capture_time", "offset_minutes": 345, "taken_local": "1900-01-01T00:00:00"},
                       {"receipt_id": 10, "state": "applied", "applied_at": 2.0, "result": "rehold:unwritable"}, {"receipt_id": 11, "state": "undone", "undone_at": 2.0}):
            with self.subTest(values=values):
                self.insert(**values)
        self.assertEqual(len(self.rows()), 5)

    def test_one_receipt_has_at_most_one_recorded_or_applied_resolution_and_the_rest_pile_up(self):
        self.insert()
        for state, extra in (("recorded", {}), ("applied", {"applied_at": 2.0, "result": "cleared"})):
            with self.subTest(state=state), self.assertRaises(IntegrityError):
                self.insert(state=state, **extra)
        with Session(self.engine) as opened:
            row = opened.scalars(select(ManagedHoldResolution)).one()
            row.state, row.undone_at = "undone", 2.0
            opened.commit()
        self.insert()
        with Session(self.engine) as opened:
            row = opened.scalars(select(ManagedHoldResolution).where(ManagedHoldResolution.state == "recorded")).one()
            row.state = "superseded"
            opened.commit()
        self.insert()
        self.assertEqual([row[5] for row in self.rows()], ["undone", "superseded", "recorded"])

    def test_a_receipt_with_a_resolution_cannot_be_deleted(self):
        self.insert()
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM managed_import_receipts WHERE id = 8")
        self.assertEqual(len(self.rows()), 1)

    def test_every_hold_the_list_can_show_has_the_actions_the_table_accepts(self):
        pairs = {(hold, action) for hold, actions in holds.ACTIONS.items() for action in actions}
        self.assertEqual(len(pairs), 13)
        for number, (hold, action) in enumerate(sorted(pairs)):
            with self.subTest(hold=hold, action=action):
                values = {"offset_minutes": 60} if action in ("offset", "capture_time", "record_offset") else {}
                values |= {"taken_local": "2020-01-01T00:00:00"} if action == "capture_time" else {}
                self.insert(hold=hold, action=action, receipt_id=1 + number % 3, state="undone", undone_at=1.0, **values)
        self.assertEqual(len(self.rows()), 13)
