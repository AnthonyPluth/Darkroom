import unittest

from alembic import command
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount, ManagedAssetOrigin, ManagedAssetVersion, ManagedImportIntent, ManagedImportReceipt, Person
from tests.shared import own_database_url

SHA1 = "a" * 40
BIG_BYTES = 5 * 1024 ** 4 + 17
SNAPSHOT = ' {"caption":"  generated\\ntext ","date":"2024-01-02T03:04:05.678+05:45","unicode":"Á 雪","rating":0} \n'


class ManagedAssetCatalogTests(unittest.TestCase):
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
            for export_id in (1, 2):
                connection.execute(insert(Export).values(id=export_id, account_id=1, kind="manual"))
            for intent_id, person_id, owner_id, export_id in ((1, 1, "owner-a", 1), (2, 2, "owner-b", 1), (3, 1, "owner-a", 2)):
                connection.execute(insert(ManagedImportIntent).values(id=intent_id, person_id=person_id, owner_id=owner_id,
                                                                     export_id=export_id, resource_manifest=SNAPSHOT, created_at=-0.125))
            for values in (self.receipt(1), self.receipt(2, intent_id=2), self.receipt(3, asset_id="asset-b"),
                           self.receipt(4, source_sha1="b" * 40, verified_sha1="b" * 40), self.receipt(5, state="returned"), self.receipt(6, intent_id=3)):
                connection.execute(insert(ManagedImportReceipt).values(**values))
            connection.execute(insert(ManagedAssetVersion).values(**self.version()))

    def receipt(self, receipt_id, state="committed", **changes):
        values = {"id": receipt_id, "intent_id": 1, "resource_key": f"generated-resource-{receipt_id}", "compound_key": "generated-group",
                  "source_sha1": SHA1, "expected_bytes": BIG_BYTES, "expected_metadata": SNAPSHOT, "state": state,
                  "asset_id": "asset-a", "upload_outcome": "duplicate", "updated_at": 1.125}
        if state == "committed":
            values |= {"verified_sha1": SHA1, "verified_bytes": BIG_BYTES, "bytes_verified_at": 2.125,
                       "metadata_evidence": SNAPSHOT, "metadata_verified_at": 3.125, "committed_at": 4.125}
        return values | changes

    def version(self, **changes):
        return {"id": 1, "person_id": 1, "owner_id": "owner-a", "asset_id": "asset-a", "sha1": SHA1,
                "kind": "IMAGE", "metadata_snapshot": SNAPSHOT, "seen_at": -0.125} | changes

    def origin(self, kind="takeout", **changes):
        values = {"version_id": 1, "person_id": 1, "owner_id": "owner-a", "asset_id": "asset-a", "sha1": SHA1,
                  "kind": kind, "source_key": "generated-receipt-1", "evidence_snapshot": SNAPSHOT, "recorded_at": 5.125}
        if kind == "takeout":
            values |= {"intent_id": 1, "receipt_id": 1, "receipt_state": "committed"}
        return values | changes

    def refused(self, model, values):
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(insert(model).values(**values))

    def test_resource_versions_preserve_unknown_sizes_metadata_and_large_verified_counts(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedAssetVersion).values(**self.version(id=2, sha1="b" * 40, state="bytes_verified", bytes=BIG_BYTES,
                                                                                bytes_verified_at=2.125, linked_video_asset_id="generated-video")))
        with db.session(self.engine) as opened:
            observed, verified = opened.scalars(select(ManagedAssetVersion).order_by(ManagedAssetVersion.id)).all()
            self.assertEqual((observed.state, observed.bytes, observed.bytes_verified_at), ("observed", None, None))
            self.assertEqual((verified.bytes, verified.bytes_verified_at, verified.linked_video_asset_id), (BIG_BYTES, 2.125, "generated-video"))
            self.assertEqual(verified.metadata_snapshot, SNAPSHOT)
            self.assertEqual(observed.seen_at, -0.125)

    def test_same_bytes_different_owners_ids_and_changed_versions_remain_distinct(self):
        for values in (self.version(id=2, person_id=2, owner_id="owner-b"), self.version(id=3, sha1="b" * 40),
                       self.version(id=4, asset_id="asset-b"), self.version(id=5, owner_id="reassigned-owner")):
            with self.engine.begin() as connection:
                connection.execute(insert(ManagedAssetVersion).values(**values))
        self.refused(ManagedAssetVersion, self.version(id=6))
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(ManagedAssetVersion)).all()), 5)

    def test_availability_states_preserve_prior_verification_as_history(self):
        for number, state in enumerate(("missing", "trashed", "offline", "external", "superseded"), start=2):
            values = self.version(id=number, asset_id=f"asset-{number}", state=state, bytes_verified_at=1.5)
            if state == "external":
                values["library_id"] = "generated-external-library"
            with self.engine.begin() as connection:
                connection.execute(insert(ManagedAssetVersion).values(**values))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedAssetVersion.bytes_verified_at).where(ManagedAssetVersion.state == "superseded")).scalar_one(), 1.5)

    def test_invalid_custody_identity_size_and_kind_are_refused(self):
        cases = ({"owner_id": ""}, {"asset_id": ""}, {"sha1": "short"}, {"bytes": -1}, {"kind": "AUDIO"},
                 {"state": "stored"}, {"state": "bytes_verified", "bytes_verified_at": 1.0},
                 {"state": "bytes_verified", "bytes": BIG_BYTES}, {"state": "external"}, {"library_id": ""},
                 {"linked_video_asset_id": ""}, {"person_id": 99},
                 {"state": "bytes_verified", "bytes": BIG_BYTES, "bytes_verified_at": 1.0, "library_id": "external"})
        for changes in cases:
            with self.subTest(changes=changes):
                self.refused(ManagedAssetVersion, self.version(id=9, asset_id="new-asset") | changes)

    def test_phone_and_multiple_takeout_origins_coexist_without_overwriting_each_other(self):
        with self.engine.begin() as connection:
            for values in (self.origin("phone", source_key="generated-mobile-origin"), self.origin(),
                           self.origin(intent_id=3, receipt_id=6, source_key="generated-receipt-6")):
                connection.execute(insert(ManagedAssetOrigin).values(**values))
        with db.session(self.engine) as opened:
            origins = opened.scalars(select(ManagedAssetOrigin).order_by(ManagedAssetOrigin.id)).all()
            self.assertEqual([row.kind for row in origins], ["phone", "takeout", "takeout"])
            self.assertEqual([row.receipt_id for row in origins], [None, 1, 6])
            self.assertEqual(origins[0].evidence_snapshot, SNAPSHOT)
        self.refused(ManagedAssetOrigin, self.origin())

    def test_other_origins_can_record_independent_or_committed_upload_evidence(self):
        with self.engine.begin() as connection:
            for values in (self.origin("legacy"), self.origin("other"),
                           self.origin("legacy", source_key="uploaded-legacy", intent_id=1, receipt_id=1, receipt_state="committed")):
                connection.execute(insert(ManagedAssetOrigin).values(**values))
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(ManagedAssetOrigin)).all()), 3)

    def test_wrong_owner_intent_asset_checksum_and_uncommitted_receipt_links_are_refused(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedAssetVersion).values(**self.version(id=2, person_id=2, owner_id="owner-b")))
        cases = ({"person_id": 2}, {"owner_id": "owner-b"}, {"version_id": 2}, {"intent_id": 2, "receipt_id": 2},
                 {"receipt_id": 2}, {"receipt_id": 3}, {"receipt_id": 4}, {"receipt_id": 5}, {"asset_id": "asset-b"},
                 {"sha1": "b" * 40}, {"version_id": 99}, {"receipt_state": "returned"}, {"receipt_state": None},
                 {"receipt_id": None}, {"intent_id": None}, {"kind": "unknown"}, {"source_key": ""})
        for changes in cases:
            with self.subTest(changes=changes):
                self.refused(ManagedAssetOrigin, self.origin() | changes)
        self.refused(ManagedAssetOrigin, self.origin(kind="phone", intent_id=1, receipt_id=1, receipt_state="committed"))
        self.refused(ManagedAssetOrigin, self.origin(kind="other", receipt_id=1))
        self.refused(ManagedAssetOrigin, self.origin(kind="takeout", intent_id=None, receipt_id=None, receipt_state=None))

    def test_origin_binding_prevents_rewriting_or_cascading_away_referenced_history(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedAssetOrigin).values(**self.origin()))
        statements = (delete(ManagedAssetVersion).where(ManagedAssetVersion.id == 1), delete(ManagedImportReceipt).where(ManagedImportReceipt.id == 1),
                      update(ManagedAssetVersion).where(ManagedAssetVersion.id == 1).values(sha1="b" * 40),
                      update(ManagedImportIntent).where(ManagedImportIntent.id == 1).values(owner_id="other-owner"),
                      update(ManagedImportReceipt).where(ManagedImportReceipt.id == 1).values(asset_id="asset-b"),
                      update(ManagedImportReceipt).where(ManagedImportReceipt.id == 1).values(state="metadata_verified", committed_at=None))
        for statement in statements:
            with self.subTest(statement=str(statement)), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(statement)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedAssetOrigin.evidence_snapshot)).scalar_one(), SNAPSHOT)

    def test_downgrade_refuses_observed_versions_with_or_without_origins(self):
        config = db.alembic_config(self.engine)
        for origins_present in (False, True):
            if origins_present:
                with self.engine.begin() as connection:
                    connection.execute(insert(ManagedAssetOrigin).values(**self.origin("phone")))
            with self.subTest(origins_present=origins_present), self.assertRaisesRegex(RuntimeError, "retain its versions and origins"), self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.downgrade(config, "0026")
            with self.engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), self.head_revision)
                self.assertEqual(connection.execute(select(ManagedAssetVersion.metadata_snapshot)).scalar_one(), SNAPSHOT)
                self.assertEqual(len(connection.execute(select(ManagedAssetOrigin)).all()), int(origins_present))
