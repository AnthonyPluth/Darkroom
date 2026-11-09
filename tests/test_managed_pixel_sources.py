import unittest

from alembic import command
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from darkroom.storage import db
from darkroom.storage.models import GoogleAccount, ManagedAssetVersion, ManagedPixelSource, MediaItem, Pixel, PixelCopy
from tests.managed_pixel_fixtures import BIG_BYTES, SHA1, SNAPSHOT, seed_pixel_source_parents
from tests.shared import own_database_url


class ManagedPixelSourceTests(unittest.TestCase):
    def setUp(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            self.head_revision = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            seed_pixel_source_parents(connection)

    def source(self, **changes):
        return {"copy_id": 1, "pixel_id": 1, "item_id": 1, "person_id": 1, "version_id": 1, "owner_id": "owner-a", "asset_id": "asset-a",
                "sha1": SHA1, "bytes": BIG_BYTES, "generation": 3, "google_account_id": 1, "destination_email": "jo@example.test",
                "copy_snapshot": SNAPSHOT, "recorded_at": -0.125} | changes

    def test_exact_owner_version_large_size_and_copy_snapshot_round_trip(self):
        with self.engine.begin() as connection:
            before = connection.execute(select(PixelCopy).where(PixelCopy.id == 1)).one()
            connection.execute(insert(ManagedPixelSource).values(**self.source()))
        with db.session(self.engine) as opened:
            row = opened.scalars(select(ManagedPixelSource)).one()
            self.assertEqual((row.copy_id, row.version_id, row.owner_id, row.asset_id, row.sha1, row.bytes), (1, 1, "owner-a", "asset-a", SHA1, BIG_BYTES))
            self.assertEqual((row.generation, row.google_account_id, row.recorded_at, row.copy_snapshot), (3, 1, -0.125, SNAPSHOT))
            self.assertEqual(row.destination_email, "jo@example.test")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(PixelCopy).where(PixelCopy.id == 1)).one(), before)

    def test_another_person_uses_their_own_copy_version_and_account(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source(copy_id=2, pixel_id=2, item_id=2, person_id=2, version_id=2,
                                                                             owner_id="owner-b", asset_id="asset-b", sha1="b" * 40,
                                                                             generation=0, google_account_id=2, destination_email="sam@example.test")))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedPixelSource.person_id, ManagedPixelSource.owner_id)).one(), (2, "owner-b"))

    def test_bad_copy_item_owner_version_hash_size_and_account_bindings_are_refused(self):
        cases = ({"copy_id": 99}, {"pixel_id": 2}, {"item_id": 2}, {"person_id": 2}, {"version_id": 2}, {"owner_id": "owner-b"},
                 {"asset_id": "asset-b"}, {"sha1": "c" * 40}, {"bytes": BIG_BYTES - 1}, {"bytes": -1}, {"generation": -1},
                 {"google_account_id": 99}, {"destination_email": ""}, {"version_id": 99}, {"version_id": 4, "asset_id": "asset-d", "sha1": "d" * 40})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(insert(ManagedPixelSource).values(**(self.source() | changes)))

    def test_a_legacy_copy_with_a_different_persons_item_cannot_be_bound(self):
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source(copy_id=3, item_id=2)))

    def test_one_source_per_copy_generation_and_destination(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source()))
        for changes in ({}, {"version_id": 3, "sha1": "c" * 40}, {"destination_email": "other@example.test"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(insert(ManagedPixelSource).values(**(self.source() | changes)))

    def test_requeue_content_and_destination_changes_keep_the_prior_generation(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source()))
            connection.execute(update(PixelCopy).where(PixelCopy.id == 1).values(requeued=4, state="queued", sha1="c" * 40,
                                                                               queued_at=8.125, placed_at=None, pruned_at=None))
            connection.execute(update(MediaItem).where(MediaItem.id == 1).values(sha1="c" * 40))
            connection.execute(update(Pixel).where(Pixel.id == 1).values(google_account_id=2))
            connection.execute(insert(ManagedPixelSource).values(**self.source(generation=4, google_account_id=2, version_id=3, sha1="c" * 40,
                                                                             destination_email="sam@example.test", copy_snapshot=' {"state":"queued"} ', recorded_at=8.125)))
        with self.engine.connect() as connection:
            rows = connection.execute(select(ManagedPixelSource.generation, ManagedPixelSource.google_account_id, ManagedPixelSource.version_id,
                                             ManagedPixelSource.sha1, ManagedPixelSource.copy_snapshot).order_by(ManagedPixelSource.id)).all()
            self.assertEqual(rows, [(3, 1, 1, SHA1, SNAPSHOT), (4, 2, 3, "c" * 40, ' {"state":"queued"} ')])
            self.assertEqual(connection.execute(select(PixelCopy.requeued, PixelCopy.always_send, PixelCopy.seen_again_at).where(PixelCopy.id == 1)).one(),
                             (4, True, 1.125))

    def test_destination_email_changes_preserve_the_prior_account_identity_snapshot(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source()))
            connection.execute(update(GoogleAccount).where(GoogleAccount.id == 1).values(email="renamed@example.test"))
            connection.execute(update(PixelCopy).where(PixelCopy.id == 1).values(requeued=4))
            connection.execute(insert(ManagedPixelSource).values(**self.source(generation=4, destination_email="renamed@example.test")))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedPixelSource.generation, ManagedPixelSource.destination_email)
                                                .order_by(ManagedPixelSource.id)).all(), [(3, "jo@example.test"), (4, "renamed@example.test")])

    def test_referenced_history_survives_parent_deletion_and_identity_rewrites(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source()))
            connection.execute(update(Pixel).where(Pixel.id == 1).values(google_account_id=2))
        statements = (delete(PixelCopy).where(PixelCopy.id == 1), delete(ManagedAssetVersion).where(ManagedAssetVersion.id == 1),
                      delete(MediaItem).where(MediaItem.id == 1), delete(GoogleAccount).where(GoogleAccount.id == 1),
                      update(PixelCopy).where(PixelCopy.id == 1).values(item_id=2), update(MediaItem).where(MediaItem.id == 1).values(person_id=2),
                      update(ManagedAssetVersion).where(ManagedAssetVersion.id == 1).values(bytes=BIG_BYTES + 1))
        for statement in statements:
            with self.subTest(statement=str(statement)), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(statement)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(select(ManagedPixelSource.copy_snapshot, ManagedPixelSource.google_account_id)).one(), (SNAPSHOT, 1))

    def test_populated_source_history_cannot_be_downgraded(self):
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedPixelSource).values(**self.source()))
        config = db.alembic_config(self.engine)
        with self.assertRaisesRegex(RuntimeError, "retain its versioned references"), self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0027")
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), self.head_revision)
            self.assertEqual(connection.execute(select(ManagedPixelSource.copy_snapshot)).scalar_one(), SNAPSHOT)
