import json
from unittest import mock

from sqlalchemy.orm import Session

from darkroom.domain import holds, managed_ingest
from darkroom.server import routes
from darkroom.storage.models import (DedupeDecision, Export, ExportPart, GoogleAccount, ManagedImportIntent, ManagedImportReceipt, MediaItem, Person, ReviewItem,
                                     TakeoutEntry)
from tests.api_world import ApiCase
from tests.privacy import no_leaks

ENTRY_NAME = "Takeout/Google Photos/Photos from 2019/generated-hold-name-canary-one.jpg"
KEPT_NAME = "generated-hold-name-canary-two.jpg"
BACKFILL_NAME = "generated-hold-name-canary-three.jpg"
COORDINATE = "45.4340123"
DESCRIPTION = "a made-up description canary for a held photo"
OWNER, OTHER_OWNER = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
SHA1 = "a" * 40
ASSET = "33333333-3333-4333-8333-333333333333"
CLOCK = {"wall_clock": "2021-03-04T05:06:07", "file_utc": None, "google_utc": 1614834367.0, "place_known": False, "description_length": None,
         "description_unwritable": False}
DIFFERS = {"differs": ["capture_time", "offset"], "immich_taken": "2019-07-04T12:30:15+00:00", "immich_offset": 0, "intended_taken": "2019-07-04T14:30:15+02:00",
           "intended_offset": 120}
UNUSABLE = {"kind": "trashed"}
EXPECTED = json.dumps({"description": DESCRIPTION, "latitude": float(COORDINATE), "longitude": 12.3388123})


class HoldsApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as opened:
            jo = Person(name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0)
            sam = Person(name="Sam", slug="sam", originals_path="o2", google_only_path="g2", created=1.0)
            opened.add_all([jo, sam])
            opened.flush()
            opened.add(GoogleAccount(email="jo@example.test", default_person_id=jo.id, created=1.0))
            opened.flush()
            opened.add_all([Export(id=1, account_id=1, kind="manual"), Export(id=2, account_id=1, kind="manual")])
            opened.flush()
            opened.add_all([ExportPart(export_id=1, number=1), TakeoutEntry(export_id=1, part_number=1, name=ENTRY_NAME, kind="media", bytes=5, crc32=1, outcome="placed", listed_at=1.0),
                            TakeoutEntry(export_id=1, part_number=1, name="Takeout/Google Photos/Photos from 2019/b.jpg", kind="media", bytes=5, crc32=2, outcome="placed", listed_at=1.0)])
            opened.add_all([ManagedImportIntent(id=1, person_id=jo.id, export_id=1, owner_id=OWNER, resource_manifest="{}", created_at=1.0),
                            ManagedImportIntent(id=2, person_id=jo.id, export_id=None, kind="backfill", owner_id=OWNER, resource_manifest="{}", created_at=1.0),
                            ManagedImportIntent(id=3, person_id=sam.id, export_id=2, owner_id=OTHER_OWNER, resource_manifest="{}", created_at=1.0)])
            opened.add(MediaItem(id=1, person_id=jo.id, source="takeout", path=f"jo/google-only/{BACKFILL_NAME}", sha1=SHA1, bytes=5, indexed_at=1.0))
            decision = DedupeDecision(export_id=1, account_id=1, source_path=f"Takeout/{KEPT_NAME}", person_id=jo.id, tier=4, reason="made up", decided_by="auto", decided_at=1.0)
            opened.add(decision)
            opened.flush()
            opened.add(ReviewItem(id=1, decision_id=decision.id, status="kept_both"))
            opened.flush()
            self.receipt(opened, 1, 1, "takeout-entry:1", "unknown_offset", CLOCK, "prepared")
            self.receipt(opened, 2, 1, "takeout-entry:2", "not_enriched", DIFFERS, "bytes_verified")
            self.receipt(opened, 3, 1, "review:1", "unusable", UNUSABLE, "returned")
            self.receipt(opened, 4, 2, "backfill:media-item:1", "not_settled", DIFFERS | {"differs": ["place"]}, "bytes_verified")
            self.receipt(opened, 5, 3, "takeout-entry:1", "unusable", {"kind": "other_owner"}, "returned")
            self.receipt(opened, 6, 1, "takeout-entry:3", "mismatch", DIFFERS, "bytes_verified")
            self.receipt(opened, 7, 1, "takeout-entry:4", None, None, "bytes_verified")
            opened.commit()

    def receipt(self, opened, receipt_id, intent_id, key, hold, facts, state):
        extra = {}
        if state != "prepared":
            extra = {"asset_id": ASSET, "upload_outcome": "created"}
        if state == "bytes_verified":
            extra |= {"verified_sha1": SHA1, "verified_bytes": 5, "bytes_verified_at": 3.0}
        opened.add(ManagedImportReceipt(id=receipt_id, intent_id=intent_id, resource_key=key, compound_key=key, source_sha1=SHA1, expected_bytes=5, expected_metadata=EXPECTED,
                                        state=state, metadata_hold=hold, hold_facts=None if facts is None else json.dumps(facts), updated_at=float(receipt_id), **extra))

    def listed(self, query=""):
        status, body = self.call("GET", f"/api/holds{query}")
        self.assertEqual(status, 200, body)
        return body

    def test_the_route_needs_a_signed_in_session(self):
        found = routes.find("GET", "/api/holds")
        self.assertIsNotNone(found)
        self.assertEqual(found.auth, "session")

    def test_every_terminal_hold_of_every_source_is_listed_newest_first_and_nothing_else(self):
        body = self.listed()
        self.assertEqual((body["total"], [(row["id"], row["source"], row["hold"], row["person_id"]) for row in body["holds"]]),
                         (5, [(5, "import", "unusable", 2), (4, "backfill", "not_settled", 1), (3, "kept", "unusable", 1), (2, "import", "not_enriched", 1),
                              (1, "import", "unknown_offset", 1)]))
        self.assertEqual(set(managed_ingest.TERMINAL), set(holds.ACTIONS))

    def test_a_hold_shows_its_file_name_cause_facts_and_the_actions_the_design_allows(self):
        by_id = {row["id"]: row for row in self.listed()["holds"]}
        self.assertEqual(by_id[1], {"id": 1, "source": "import", "person_id": 1, "export_id": 1, "name": "generated-hold-name-canary-one.jpg", "hold": "unknown_offset",
                                    "reason": holds.CAUSES["unknown_offset"], "facts": CLOCK, "actions": ["offset", "skip"], "bytes_verified_at": None, "updated_at": 1.0})
        self.assertEqual((by_id[3]["name"], by_id[3]["export_id"], by_id[3]["facts"], by_id[3]["actions"]), (KEPT_NAME, 1, UNUSABLE, ["recheck", "skip"]))
        self.assertEqual((by_id[4]["name"], by_id[4]["export_id"], by_id[4]["bytes_verified_at"], by_id[4]["actions"]), (BACKFILL_NAME, None, 3.0, ["recheck", "accept"]))
        self.assertEqual((by_id[2]["facts"], by_id[2]["actions"]), (DIFFERS, ["accept", "record_offset", "recheck"]))

    def test_an_offset_is_recorded_only_when_the_offset_is_what_differs_and_every_hold_has_its_actions(self):
        self.assertEqual(holds.allowed("not_enriched", {"differs": ["description"]}), ["accept", "recheck"])
        self.assertEqual(holds.allowed("not_enriched", None), ["accept", "recheck"])
        self.assertEqual(holds.allowed("not_enriched", {"differs": "offset"}), ["accept", "recheck"])
        self.assertEqual({hold: holds.allowed(hold, None) for hold in ("no_instant", "unwritable")}, {"no_instant": ["capture_time", "skip"], "unwritable": ["drop_description", "skip"]})

    def test_the_list_never_carries_a_coordinate_a_description_a_suggested_offset_or_a_key(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET hold_facts = '{\"wall_clock\": \"2021-03-04T05:06:07\", \"latitude\": 45.4340123, "
                                       "\"suggested_offset\": 120, \"description\": \"a made-up description canary for a held photo\"}' WHERE id = 1")
        self.replies.clear()
        with no_leaks(self, COORDINATE, DESCRIPTION, "12.3388123", replies=self.replies):
            body = self.listed()
            self.listed("?source=kept")
        self.assertEqual(next(row["facts"] for row in body["holds"] if row["id"] == 1), {"wall_clock": "2021-03-04T05:06:07"})
        keys = {key for row in body["holds"] for key in row["facts"] or {}}
        self.assertTrue(keys <= set(holds.FACT_KEYS))
        self.assertFalse({"suggested_offset", "offset", "latitude", "longitude", "description"} & keys)

    def test_facts_that_are_not_json_or_not_an_object_read_as_none(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET hold_facts = 'not json' WHERE id = 1")
            connection.exec_driver_sql("UPDATE managed_import_receipts SET hold_facts = '[1, 2]' WHERE id = 2")
            connection.exec_driver_sql("UPDATE managed_import_receipts SET hold_facts = NULL WHERE id = 3")
        by_id = {row["id"]: row for row in self.listed()["holds"]}
        self.assertEqual([by_id[number]["facts"] for number in (1, 2, 3)], [None, None, None])
        self.assertEqual(by_id[2]["actions"], ["accept", "recheck"])

    def test_the_list_filters_by_person_and_source_and_counts_what_matches(self):
        sam = self.listed("?person=2")
        self.assertEqual((sam["total"], [row["id"] for row in sam["holds"]]), (1, [5]))
        kept = self.listed("?source=kept")
        self.assertEqual((kept["total"], [row["id"] for row in kept["holds"]]), (1, [3]))
        imported = self.listed("?source=import&person=1")
        self.assertEqual((imported["total"], [row["id"] for row in imported["holds"]]), (2, [2, 1]))
        self.assertEqual(self.listed("?person=9"), {"holds": [], "total": 0})

    def test_the_list_pages_on_from_the_last_hold_seen(self):
        self.assertEqual([row["id"] for row in self.listed("?after=3")["holds"]], [2, 1])
        self.assertEqual(self.listed("?after=1"), {"holds": [], "total": 5})
        with mock.patch.object(holds, "LIST_LIMIT", 2):
            first = self.listed()
            second = self.listed(f"?after={first['holds'][-1]['id']}")
        self.assertEqual(([row["id"] for row in first["holds"]], [row["id"] for row in second["holds"]], second["total"]), ([5, 4], [3, 2], 5))

    def test_a_bad_query_is_refused_without_naming_any_photo(self):
        replies = []
        with no_leaks(self, "generated-hold-name-canary", COORDINATE, replies=replies):
            for query, message in (("source=other", "source is one of import, kept, backfill"), ("person=x", "person must be a whole number"),
                                   ("after=-1", "after must be a whole number"), ("person=99999999999999", "person must be a whole number")):
                with self.subTest(query=query):
                    status, body = self.call("GET", f"/api/holds?{query}")
                    replies.append(json.dumps(body).encode())
                    self.assertEqual((status, body), (400, {"error": message}))

    def test_listing_changes_nothing_and_a_hold_that_is_committed_or_not_terminal_is_not_listed(self):
        with self.engine.connect() as connection:
            before = connection.exec_driver_sql("SELECT id, state, metadata_hold, hold_facts, updated_at FROM managed_import_receipts ORDER BY id").all()
        self.listed()
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT id, state, metadata_hold, hold_facts, updated_at FROM managed_import_receipts ORDER BY id").all(), before)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET state = 'committed', committed_at = 4.0, metadata_evidence = '{}', metadata_verified_at = 4.0, "
                                       "metadata_hold = NULL WHERE id = 2")
        self.assertEqual([row["id"] for row in self.listed()["holds"]], [5, 4, 3, 1])

    def test_a_receipt_of_no_known_source_is_not_listed(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE managed_import_receipts SET resource_key = 'part/other', compound_key = 'part/other' WHERE id = 1")
        self.assertEqual([row["id"] for row in self.listed()["holds"]], [5, 4, 3, 2])

    def test_a_hold_whose_source_row_is_gone_has_no_name(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM media_items")
        self.assertIsNone(next(row["name"] for row in self.listed()["holds"] if row["id"] == 4))
