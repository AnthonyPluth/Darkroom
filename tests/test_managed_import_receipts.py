import json
import unittest

from alembic import command
from sqlalchemy import delete, inspect, insert, select
from sqlalchemy.exc import IntegrityError

from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount, ManagedImportIntent, ManagedImportReceipt, Person
from tests.shared import own_database_url

SHA1 = "a" * 40
BIG_BYTES = 5 * 1024 ** 4 + 17
SNAPSHOT = ' {"caption":"  generated\\ntext ","date":"2024-01-02T03:04:05.678+05:45","empty":"","rating":0,"unicode":"Á 雪"} \n'
STATES = ("prepared", "outcome_unknown", "returned", "bytes_verified", "metadata_verified", "committed")


class ManagedImportReceiptTests(unittest.TestCase):
    def setUp(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        with self.engine.connect() as connection:
            self.head_revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        with self.engine.begin() as connection:
            connection.execute(insert(Person), [
                {"id": 1, "name": "Jo", "slug": "jo", "originals_path": "o", "google_only_path": "g", "created": -0.125},
                {"id": 2, "name": "Sam", "slug": "sam", "originals_path": "s", "google_only_path": "t", "created": 1.25},
            ])
            connection.execute(insert(GoogleAccount).values(id=1, email="jo@example.test", default_person_id=1, created=1.0))
            connection.execute(insert(Export).values(id=1, account_id=1, kind="manual"))
            connection.execute(insert(ManagedImportIntent).values(**self.intent()))

    def intent(self, **changes):
        return {"id": 1, "person_id": 1, "export_id": 1, "owner_id": "generated-owner-a", "created_at": -0.125,
                "resource_manifest": SNAPSHOT} | changes

    def receipt(self, state="prepared", **changes):
        values = {"intent_id": 1, "resource_key": "part/雪/still Á", "compound_key": "generated-live-photo",
                  "source_sha1": SHA1, "expected_bytes": BIG_BYTES, "expected_metadata": SNAPSHOT,
                  "state": state, "updated_at": 1.125}
        if state in STATES[2:]:
            values |= {"asset_id": "generated-asset", "upload_outcome": "duplicate"}
        if state in STATES[3:]:
            values |= {"verified_sha1": SHA1, "verified_bytes": BIG_BYTES, "bytes_verified_at": 2.125}
        if state in STATES[4:]:
            values |= {"metadata_evidence": SNAPSHOT, "metadata_verified_at": 3.125}
        if state == "committed":
            values["committed_at"] = 4.125
        return values | changes

    def refused(self, model, values):
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(insert(model).values(**values))

    def test_prepared_intent_keeps_exact_snapshots_identity_and_large_byte_counts(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportReceipt).values(**self.receipt()))
        with db.session(self.engine) as opened:
            intent = opened.get(ManagedImportIntent, 1)
            receipt = opened.scalars(select(ManagedImportReceipt)).one()
            self.assertEqual((intent.owner_id, intent.created_at, intent.schema_version), ("generated-owner-a", -0.125, 1))
            self.assertEqual(intent.resource_manifest, SNAPSHOT)
            self.assertEqual((receipt.resource_key, receipt.compound_key, receipt.source_sha1, receipt.expected_bytes),
                             ("part/雪/still Á", "generated-live-photo", SHA1, BIG_BYTES))
            self.assertEqual(receipt.expected_metadata, SNAPSHOT)
            self.assertEqual(json.loads(receipt.expected_metadata)["rating"], 0)
            self.assertEqual((receipt.state, receipt.asset_id, receipt.committed_at), ("prepared", None, None))

    def test_each_recorded_state_round_trips_without_promoting_other_resources(self):
        for state in STATES:
            with self.engine.begin() as connection:
                connection.execute(insert(ManagedImportReceipt).values(**self.receipt(state, resource_key=state)))
        with db.session(self.engine) as opened:
            rows = opened.scalars(select(ManagedImportReceipt).order_by(ManagedImportReceipt.id)).all()
            self.assertEqual(tuple(row.state for row in rows), STATES)
            self.assertEqual([row.committed_at for row in rows], [None] * 5 + [4.125])
            self.assertEqual(rows[-1].metadata_evidence, SNAPSHOT)
            self.assertEqual(rows[-1].verified_bytes, BIG_BYTES)
            self.assertEqual(rows[-1].updated_at, 1.125)

    def test_retry_identity_cannot_create_a_second_intent_or_receipt(self):
        self.refused(ManagedImportIntent, self.intent(id=2))
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportReceipt).values(**self.receipt()))
        self.refused(ManagedImportReceipt, self.receipt())
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(ManagedImportIntent)).all()), 1)
            self.assertEqual(len(connection.execute(select(ManagedImportReceipt)).all()), 1)

    def test_changed_source_and_distinct_owners_retain_separate_receipts(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportIntent).values(**self.intent(id=2, person_id=2, owner_id="generated-owner-b")))
            connection.execute(insert(ManagedImportIntent).values(**self.intent(id=3, owner_id="generated-reassigned-owner")))
            for values in (self.receipt(), self.receipt(source_sha1="b" * 40), self.receipt(intent_id=2), self.receipt(intent_id=3)):
                connection.execute(insert(ManagedImportReceipt).values(**values))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedImportReceipt.intent_id, ManagedImportReceipt.source_sha1)
                                                .order_by(ManagedImportReceipt.id)).all(), [(1, SHA1), (1, "b" * 40), (2, SHA1), (3, SHA1)])

    def test_unknown_states_bad_counts_and_incomplete_verification_are_refused(self):
        cases = [self.receipt(state="failed"), self.receipt(expected_bytes=-1), self.receipt(source_sha1="short"),
                 self.receipt(resource_key=""), self.receipt(compound_key=""), self.receipt("returned", asset_id=""),
                 self.receipt("returned", asset_id=None), self.receipt("returned", upload_outcome=None),
                 self.receipt("returned", upload_outcome="accepted"), self.receipt(asset_id="premature"),
                 self.receipt("returned", verified_sha1=SHA1), self.receipt("bytes_verified", verified_sha1=None),
                 self.receipt("bytes_verified", verified_sha1="b" * 40), self.receipt("bytes_verified", verified_bytes=None),
                 self.receipt("bytes_verified", verified_bytes=BIG_BYTES - 1), self.receipt("bytes_verified", verified_bytes=-1),
                 self.receipt("bytes_verified", bytes_verified_at=None), self.receipt("bytes_verified", metadata_evidence="{}"),
                 self.receipt("metadata_verified", metadata_evidence=None), self.receipt("metadata_verified", metadata_verified_at=None),
                 self.receipt("metadata_verified", committed_at=1.0), self.receipt("committed", committed_at=None),
                 self.receipt("committed", metadata_evidence=None), self.receipt("committed", verified_sha1=None)]
        for values in cases:
            with self.subTest(values=values):
                self.refused(ManagedImportReceipt, values)

    def test_missing_parents_and_unknown_snapshot_versions_are_refused(self):
        for values in (self.intent(id=2, person_id=99), self.intent(id=2, export_id=99), self.intent(id=2, schema_version=2), self.intent(id=2, owner_id="")):
            with self.subTest(values=values):
                self.refused(ManagedImportIntent, values)
        self.refused(ManagedImportReceipt, self.receipt(intent_id=99))

    def test_referenced_intent_export_and_person_cannot_erase_the_journal_by_cascade(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportIntent).values(**self.intent(id=2, person_id=2, owner_id="generated-owner-b")))
            connection.execute(insert(ManagedImportReceipt).values(**self.receipt("committed")))
        for statement in (delete(ManagedImportIntent).where(ManagedImportIntent.id == 1),
                          delete(Export).where(Export.id == 1), delete(Person).where(Person.id == 2)):
            with self.subTest(statement=str(statement)), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(statement)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedImportReceipt.state)).scalar_one(), "committed")

    def test_downgrade_refuses_an_intent_before_any_resource_receipt_exists(self):
        config = db.alembic_config(self.engine)
        with self.assertRaisesRegex(RuntimeError, "retain the additive schema"), self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0025")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedImportIntent.resource_manifest)).scalar_one(), SNAPSHOT)
            self.assertEqual(connection.execute(select(ManagedImportReceipt)).all(), [])
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), self.head_revision)

    def test_downgrade_refuses_prepared_or_committed_journal_and_preserves_every_record(self):
        for state in ("prepared", "committed"):
            with self.engine.begin() as connection:
                connection.execute(insert(ManagedImportReceipt).values(**self.receipt(state, resource_key=state)))
            config = db.alembic_config(self.engine)
            with self.assertRaisesRegex(RuntimeError, "retain the additive schema"), self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.downgrade(config, "0025")
            with self.engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), self.head_revision)
                self.assertEqual(len(connection.execute(select(ManagedImportReceipt)).all()), 1 if state == "prepared" else 2)
                self.assertEqual(connection.execute(select(ManagedImportIntent.resource_manifest)).scalar_one(), SNAPSHOT)
            self.assertIn("managed_import_receipts", inspect(self.engine).get_table_names())
