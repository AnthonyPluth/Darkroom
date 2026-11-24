import contextlib
import dataclasses
import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from sqlalchemy import insert, select

import run
from darkroom import config
from darkroom.domain import retention_gate
from darkroom.providers import immich
from darkroom.storage import db, secretbox
from darkroom.storage.schema import metadata
from darkroom.storage.models import (
    Event,
    Export,
    GoogleAccount,
    ManagedAssetVersion,
    ManagedImportIntent,
    ManagedImportReceipt,
    Person,
    RecoveryPoint,
    RecoveryPointAsset,
)
from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson
from tests.privacy import no_leaks
from tests.shared import own_database_url

NOW = 1_000_000.0
EARLIER = NOW - 3600.0
LATER = NOW + 60.0
SECRET = "recovery-secret-key-canary"
JO_KEY = "recovery-jo-key-canary"
SAM_KEY = "recovery-sam-key-canary"
ADMIN = "recovery-admin-key-canary"
JO = "11111111-1111-4111-8111-111111111111"
SAM = "22222222-2222-4222-8222-222222222222"
EXTERNAL_LIBRARY = "99999999-9999-4999-8999-999999999999"
SNAPSHOT = " { } \n"


def asset_id(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def bytes_of(number):
    return f"generated recovery original {number}".encode()


def sha1_of(number):
    return hashlib.sha1(bytes_of(number), usedforsecurity=False).hexdigest()


class RecoveryWorld(unittest.TestCase):
    def setUp(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        self.fake = FakeImmich([FakePerson("jo", JO, "lib-jo", JO_KEY), FakePerson("sam", SAM, "lib-sam", SAM_KEY)], ADMIN).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version = "v3.4.1"
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.settings = dataclasses.replace(self.loaded(), secret_key=SECRET, immich_url=self.fake.url, immich_admin_key=ADMIN)
        with self.engine.begin() as connection:
            connection.execute(insert(Person), [
                {"id": 1, "name": "Jo", "slug": "jo", "originals_path": "o", "google_only_path": "g", "created": 1.0, "immich_user_id": JO,
                 "immich_api_key_sealed": secretbox.seal(JO_KEY, SECRET)},
                {"id": 2, "name": "Sam", "slug": "sam", "originals_path": "s", "google_only_path": "t", "created": 2.0, "immich_user_id": SAM,
                 "immich_api_key_sealed": secretbox.seal(SAM_KEY, SECRET)},
            ])
            connection.execute(insert(GoogleAccount).values(id=1, email="jo@example.test", default_person_id=1, created=1.0))
            connection.execute(insert(Export).values(id=1, account_id=1, kind="manual"))
            for intent_id, person_id, owner in ((1, 1, JO), (2, 2, SAM)):
                connection.execute(insert(ManagedImportIntent).values(id=intent_id, person_id=person_id, owner_id=owner, export_id=1, resource_manifest=SNAPSHOT,
                                                                     created_at=1.0))
        self.receipt_count = 0
        self.version_count = 0

    def loaded(self):
        with mock.patch.dict(os.environ, {"DARKROOM_DATA_DIR": self.folder, "DARKROOM_PHOTOS_ROOT": self.folder}, clear=True):
            return config.load()

    def stored(self, number, owner=JO, **changes):
        self.fake.add_asset(FakeAsset(asset_id(number), owner, bytes_of(number), **changes))
        return asset_id(number)

    def receipt(self, number, intent=1, state="committed", committed_at=EARLIER, sha1=None, asset=None):
        self.receipt_count += 1
        sha1 = sha1 or sha1_of(number)
        values = {"id": self.receipt_count, "intent_id": intent, "resource_key": f"generated-resource-{self.receipt_count}", "compound_key": "generated-group",
                  "source_sha1": sha1, "expected_bytes": 10, "expected_metadata": SNAPSHOT, "state": state, "asset_id": asset or asset_id(number),
                  "upload_outcome": "created", "updated_at": 1.0}
        if state in ("bytes_verified", "metadata_verified", "committed"):
            values |= {"verified_sha1": sha1, "verified_bytes": 10, "bytes_verified_at": EARLIER}
        if state in ("metadata_verified", "committed"):
            values |= {"metadata_evidence": SNAPSHOT, "metadata_verified_at": EARLIER}
        if state == "committed":
            values |= {"committed_at": committed_at}
        if state in ("prepared", "outcome_unknown"):
            values |= {"asset_id": None, "upload_outcome": None}
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportReceipt).values(**values))

    def version(self, number, person=1, owner=JO, state="bytes_verified", verified_at=EARLIER, sha1=None, asset=None):
        self.version_count += 1
        values = {"id": self.version_count, "person_id": person, "owner_id": owner, "asset_id": asset or asset_id(number), "sha1": sha1 or sha1_of(number),
                  "kind": "IMAGE", "state": state, "metadata_snapshot": SNAPSHOT, "seen_at": 1.0}
        if state == "bytes_verified":
            values |= {"bytes": 10, "bytes_verified_at": verified_at}
        if state == "external":
            values |= {"library_id": EXTERNAL_LIBRARY}
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedAssetVersion).values(**values))

    def begin(self, at=NOW):
        return retention_gate.begin(self.engine, self.settings, at)

    def points(self):
        with db.session(self.engine) as opened:
            return [(row.id, row.state, row.manifest_count, row.failure, row.finished_at) for row in opened.execute(select(RecoveryPoint).order_by(RecoveryPoint.id)).scalars()]

    def manifest(self, point=1):
        with db.session(self.engine) as opened:
            query = select(RecoveryPointAsset).where(RecoveryPointAsset.recovery_point_id == point).order_by(RecoveryPointAsset.asset_id)
            return [(row.owner_id, row.asset_id, row.sha1, row.source) for row in opened.execute(query).scalars()]

    def events(self):
        with db.session(self.engine) as opened:
            return [(row.type, row.subject, row.detail) for row in opened.execute(select(Event).order_by(Event.id)).scalars()]

    def assert_no_point(self):
        self.assertEqual(self.points(), [])
        self.assertEqual(self.manifest(), [])


class ManifestTests(RecoveryWorld):
    def test_a_committed_receipt_and_a_verified_version_are_listed_with_their_sources_and_a_digest(self):
        self.stored(1)
        self.stored(2)
        self.receipt(1)
        self.version(2)
        begun = self.begin()
        self.assertEqual((begun.state, begun.listed, begun.confirmed, begun.refused), ("open", 2, 2, 0))
        self.assertEqual(self.points(), [(begun.point_id, "open", 2, None, None)])
        self.assertEqual(self.manifest(), [(JO, asset_id(1), sha1_of(1), "receipt"), (JO, asset_id(2), sha1_of(2), "version")])
        lines = "".join(f"{owner} {asset} {sha1}\n" for owner, asset, sha1, _ in self.manifest())
        with db.session(self.engine) as opened:
            point = opened.get(RecoveryPoint, begun.point_id)
            self.assertEqual((point.opened_at, point.manifest_sha256), (NOW, hashlib.sha256(lines.encode()).hexdigest()))
            self.assertEqual((point.taken_at, point.kept_until, point.scopes, point.tool, point.snapshot_ref), (None, None, None, None, None))
        self.assertEqual(self.events(), [("recovery_point_opened", f"recovery point {begun.point_id}", "listed 2, confirmed 2")])

    def test_the_digest_does_not_depend_on_the_order_the_assets_were_found_in(self):
        rows = [(JO, asset_id(2), sha1_of(2)), (JO, asset_id(1), sha1_of(1)), (SAM, asset_id(3), sha1_of(3))]
        self.assertEqual(retention_gate.manifest_sha256(rows), retention_gate.manifest_sha256(reversed(rows)))
        self.assertNotEqual(retention_gate.manifest_sha256(rows), retention_gate.manifest_sha256(rows[:2]))
        self.assertEqual(retention_gate.manifest_sha256([]), hashlib.sha256(b"").hexdigest())

    def test_every_person_with_managed_assets_is_covered_under_their_own_key(self):
        self.stored(1)
        self.stored(2, SAM)
        self.receipt(1)
        self.receipt(2, intent=2)
        self.assertEqual(self.begin().confirmed, 2)
        self.assertEqual({owner for owner, *_ in self.manifest()}, {JO, SAM})

    def test_an_asset_that_is_both_a_receipt_and_a_version_is_listed_once_as_the_receipt(self):
        self.stored(1)
        self.receipt(1)
        self.version(1)
        begun = self.begin()
        self.assertEqual((begun.listed, begun.confirmed), (1, 1))
        self.assertEqual(self.manifest(), [(JO, asset_id(1), sha1_of(1), "receipt")])

    def test_no_managed_assets_open_a_point_with_an_empty_manifest(self):
        begun = self.begin()
        self.assertEqual((begun.state, begun.listed, begun.confirmed), ("open", 0, 0))
        self.assertEqual(self.points(), [(1, "open", 0, None, None)])
        self.assertEqual(self.manifest(), [])
        self.assertEqual(self.fake.requests(), [])

    def test_two_begins_make_two_points_and_leave_the_first_alone(self):
        self.stored(1)
        self.receipt(1)
        first, second = self.begin(), self.begin(NOW + 5)
        self.assertEqual((first.point_id, second.point_id), (1, 2))
        self.assertEqual(self.manifest(1), self.manifest(2))
        self.assertEqual([row[:2] for row in self.points()], [(1, "open"), (2, "open")])


class NotCoveredTests(RecoveryWorld):
    def test_receipts_that_are_not_committed_are_not_listed(self):
        for number, state in enumerate(("prepared", "outcome_unknown", "returned", "bytes_verified", "metadata_verified"), start=1):
            self.stored(number)
            self.receipt(number, state=state)
        begun = self.begin()
        self.assertEqual((begun.listed, begun.confirmed), (0, 0))
        self.assertEqual(self.manifest(), [])

    def test_a_receipt_committed_after_begin_started_is_not_listed(self):
        self.stored(1)
        self.stored(2)
        self.receipt(1, committed_at=NOW)
        self.receipt(2, committed_at=LATER)
        self.begin()
        self.assertEqual([asset for _, asset, *_ in self.manifest()], [asset_id(1)])

    def test_versions_that_are_not_verified_or_were_verified_after_begin_started_are_not_listed(self):
        for number, state in ((1, "observed"), (2, "missing"), (3, "trashed"), (4, "offline"), (5, "external"), (6, "superseded")):
            self.stored(number)
            self.version(number, state=state)
        self.stored(7)
        self.version(7, verified_at=LATER)
        self.assertEqual(self.begin().listed, 0)
        self.assertEqual(self.manifest(), [])

    def test_an_asset_trashed_offline_or_in_an_external_library_in_immich_is_left_out(self):
        self.stored(1, trashed=True)
        self.stored(2, offline=True)
        self.stored(3, library_id=EXTERNAL_LIBRARY)
        self.stored(4)
        for number in (1, 2, 3, 4):
            self.receipt(number)
        begun = self.begin()
        self.assertEqual((begun.listed, begun.confirmed, begun.refused), (4, 1, 3))
        self.assertEqual([asset for _, asset, *_ in self.manifest()], [asset_id(4)])

    def test_an_asset_whose_bytes_changed_in_immich_is_left_out(self):
        self.stored(1)
        self.receipt(1, sha1="b" * 40)
        self.assertEqual((self.begin().confirmed, self.manifest()), (0, []))

    def test_an_asset_owned_by_another_person_in_immich_is_left_out(self):
        self.stored(1, owner=SAM)
        self.receipt(1)
        self.assertEqual((self.begin().confirmed, self.manifest()), (0, []))

    def test_an_asset_immich_no_longer_has_is_left_out_and_the_others_are_still_read(self):
        self.receipt(1)
        self.stored(2)
        self.receipt(2)
        self.fake.state.missing_status = 404
        begun = self.begin()
        self.assertEqual((begun.state, begun.confirmed, begun.refused), ("open", 1, 1))
        self.assertEqual([asset for _, asset, *_ in self.manifest()], [asset_id(2)])

    def test_an_asset_id_that_is_not_an_immich_id_is_left_out_without_a_request_for_it(self):
        self.receipt(1, asset="../../users")
        self.stored(2)
        self.receipt(2)
        self.assertEqual(self.begin().confirmed, 1)
        self.assertEqual([path for _, path in self.fake.requests() if path.startswith("/api/assets/")], [f"/api/assets/{asset_id(2)}"])

    def test_an_answer_that_names_no_checksum_leaves_the_asset_out(self):
        self.stored(1)
        self.receipt(1)
        self.fake.state.drop_fields = ("checksum",)
        self.assertEqual((self.begin().confirmed, self.manifest()), (0, []))

    def test_a_receipt_and_a_version_that_disagree_keep_only_the_one_immich_confirms(self):
        self.stored(1)
        self.receipt(1, sha1="c" * 40)
        self.version(1)
        self.begin()
        self.assertEqual(self.manifest(), [(JO, asset_id(1), sha1_of(1), "version")])


class ImmichTroubleTests(RecoveryWorld):
    def setUp(self):
        super().setUp()
        self.stored(1)
        self.receipt(1)
        self.stored(2, SAM)
        self.receipt(2, intent=2)

    def assert_failed_point(self, begun):
        self.assertEqual((begun.state, begun.confirmed), ("failed", 0))
        self.assertEqual(self.points(), [(begun.point_id, "failed", 0, "immich_unreachable", NOW)])
        self.assertEqual(self.manifest(begun.point_id), [])
        self.assertEqual([event[0] for event in self.events()], ["recovery_point_failed"])

    def test_immich_down_records_a_failed_point_with_no_manifest(self):
        self.fake.mode("down")
        self.assert_failed_point(self.begin())

    def test_immich_refusing_every_key_records_a_failed_point(self):
        self.fake.mode("401")
        self.assert_failed_point(self.begin())

    def test_a_key_with_an_extra_grant_records_a_failed_point_and_reads_no_asset(self):
        self.fake.state.permissions = (*self.fake.state.permissions, "asset.delete")
        self.assert_failed_point(self.begin())
        self.assertFalse([path for _, path in self.fake.requests() if path.startswith("/api/assets/")])

    def test_an_unsupported_immich_version_records_a_failed_point(self):
        self.fake.state.version = "v9.9.9"
        self.assert_failed_point(self.begin())

    def test_no_immich_address_records_a_failed_point(self):
        self.settings = dataclasses.replace(self.settings, immich_url="")
        self.assert_failed_point(self.begin())

    def test_an_unreadable_stored_key_for_every_person_records_a_failed_point(self):
        self.settings = dataclasses.replace(self.settings, secret_key="another-secret-key-entirely")
        self.assert_failed_point(self.begin())
        self.assertEqual(self.fake.requests(), [])

    def test_one_person_whose_key_fails_leaves_the_others_covered_and_the_point_open(self):
        with db.session(self.engine, writing=True) as opened:
            opened.get(Person, 2).immich_api_key_sealed = None
        begun = self.begin()
        self.assertEqual((begun.state, begun.listed, begun.confirmed, begun.refused), ("open", 2, 1, 1))
        self.assertEqual(self.manifest(), [(JO, asset_id(1), sha1_of(1), "receipt")])

    def test_a_read_that_breaks_off_leaves_the_rest_of_that_person_out_and_the_point_open_for_the_others(self):
        self.stored(3)
        self.receipt(3)
        self.fake.state.intercept = lambda method, path, slug: 500 if path == f"/api/assets/{asset_id(3)}" else None
        begun = self.begin()
        self.assertEqual(begun.state, "open")
        self.assertEqual([asset for _, asset, *_ in self.manifest()], [asset_id(1), asset_id(2)])

    def test_a_point_that_cannot_be_written_leaves_nothing_behind(self):
        with mock.patch.object(retention_gate.events, "record", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.begin()
        self.assert_no_point()


class ReadsOnlyTests(RecoveryWorld):
    def test_begin_sends_only_get_requests_and_changes_nothing_but_its_own_rows(self):
        self.stored(1)
        self.receipt(1)
        self.version(1)
        with self.engine.connect() as connection:
            tables = [name for name in metadata.tables if name not in ("recovery_points", "recovery_point_assets", "events")]
            before = {name: connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one() for name in tables}
        self.begin()
        self.assertEqual({method for method, _ in self.fake.requests()}, {"GET"})
        with self.engine.connect() as connection:
            self.assertEqual({name: connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one() for name in tables}, before)

    def test_the_receipts_and_versions_it_lists_are_left_as_they_were(self):
        self.stored(1)
        self.receipt(1)
        with db.session(self.engine) as opened:
            before = [(row.id, row.state, row.committed_at, row.updated_at) for row in opened.execute(select(ManagedImportReceipt)).scalars()]
        self.begin()
        with db.session(self.engine) as opened:
            self.assertEqual([(row.id, row.state, row.committed_at, row.updated_at) for row in opened.execute(select(ManagedImportReceipt)).scalars()], before)


class CommandTests(RecoveryWorld):
    def setUp(self):
        super().setUp()
        self.environment = {"DARKROOM_PHOTOS_ROOT": self.folder, "DARKROOM_DATA_DIR": self.folder, "DARKROOM_SECRET_KEY": SECRET, "IMMICH_URL": self.fake.url,
                            "IMMICH_ADMIN_KEY": ADMIN, "DATABASE_URL": self.engine.url.render_as_string(hide_password=False)}

    def command(self, *argv):
        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(run, "utc_now", return_value=datetime.fromtimestamp(NOW, UTC)), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = run.main(["recovery-point", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_begin_prints_the_point_and_its_counts(self):
        self.stored(1)
        self.stored(2, trashed=True)
        self.receipt(1)
        self.receipt(2)
        code, out, err = self.command("begin")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(err), {"event": "recovery point opened", "id": 1, "listed": 2, "confirmed": 1})
        self.assertEqual(out, "recovery point 1 opened: 2 photos listed, 1 confirmed in Immich and in its manifest, 1 left out\n")
        self.assertEqual(self.points(), [(1, "open", 1, None, None)])

    def test_begin_exits_non_zero_and_says_so_when_immich_cannot_be_read(self):
        self.stored(1)
        self.receipt(1)
        self.fake.mode("down")
        code, out, err = self.command("begin")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines()[-1], run.RECOVERY_UNREACHABLE.format(id=1))
        self.assertEqual(json.loads(err.splitlines()[0]), {"event": "recovery point failed", "id": 1, "listed": 1, "confirmed": 0})
        self.assertEqual(self.points(), [(1, "failed", 0, "immich_unreachable", NOW)])

    def test_begin_asks_to_migrate_first_when_the_table_is_missing(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.environment["DATABASE_URL"] = f"sqlite:///{other.name}/old.db"
        engine = db.make_engine(self.environment["DATABASE_URL"])
        self.addCleanup(engine.dispose)
        engine.dispose()
        code, out, err = self.command("begin")
        self.assertEqual((code, out, err.strip()), (1, "", run.RECOVERY_NOT_MIGRATED))

    def test_an_unknown_action_is_refused_and_writes_nothing(self):
        with self.assertRaises(SystemExit):
            self.command("finish")
        self.assertEqual(self.points(), [])

    def test_nothing_private_is_printed_logged_or_stored_outside_the_manifest(self):
        self.stored(1, name="IMG_PRIVATE_CANARY.JPG")
        self.receipt(1)
        self.fake.state.permissions = (*self.fake.state.permissions, "asset.delete")
        with no_leaks(self, JO_KEY, SAM_KEY, SECRET, ADMIN, "IMG_PRIVATE_CANARY.JPG", self.fake.url, database=self.engine):
            self.command("begin")
            self.fake.state.permissions = ("user.read", "server.about", "asset.read", "asset.download")
            self.command("begin")
            self.fake.mode("down")
            self.command("begin")


class CustodyAllTests(RecoveryWorld):
    def reader(self, key=JO_KEY, owner=JO):
        return immich.ManagedReader(immich.Client(self.fake.url, ADMIN, timeout=3), key, owner)

    def test_it_checks_access_once_and_reads_each_asset_with_a_get(self):
        for number in (1, 2, 3):
            self.stored(number)
        reads = self.reader().custody_all([asset_id(n) for n in (1, 2, 3)])
        self.assertEqual((sorted(reads.found), reads.refused, reads.complete), ([asset_id(n) for n in (1, 2, 3)], (), True))
        requests = self.fake.requests()
        self.assertEqual(requests.count(("GET", "/api/users/me")), 1)
        self.assertEqual(requests.count(("GET", "/api/api-keys/me")), 1)
        self.assertEqual({method for method, _ in requests}, {"GET"})
        self.assertEqual([path for _, path in requests if path.startswith("/api/assets/")], [f"/api/assets/{asset_id(n)}" for n in (1, 2, 3)])

    def test_assets_that_are_not_the_persons_available_originals_are_refused_but_the_rest_are_read(self):
        self.stored(1, trashed=True)
        self.stored(2)
        self.stored(3, SAM)
        reads = self.reader().custody_all([asset_id(1), asset_id(2), asset_id(3), asset_id(4), "not-an-id"])
        self.assertEqual(sorted(reads.found), [asset_id(2)])
        self.assertEqual(sorted(reads.refused), sorted([asset_id(1), asset_id(3), asset_id(4), "not-an-id"]))
        self.assertTrue(reads.complete)

    def test_a_deadline_that_has_passed_reads_nothing_and_refuses_everything(self):
        self.stored(1)
        reads = self.reader().custody_all([asset_id(1)], deadline=0.0)
        self.assertEqual((reads.found, reads.refused, reads.complete), ({}, (asset_id(1),), False))

    def test_a_deadline_that_passes_midway_refuses_the_rest(self):
        for number in (1, 2, 3):
            self.stored(number)
        with mock.patch("darkroom.providers.immich.time.monotonic", side_effect=[1.0, 1.0, 9.0]):
            reads = self.reader().custody_all([asset_id(n) for n in (1, 2, 3)], deadline=5.0)
        self.assertEqual((sorted(reads.found), reads.refused, reads.complete), ([asset_id(1), asset_id(2)], (asset_id(3),), False))

    def test_a_failure_other_than_a_missing_asset_stops_the_reads_and_says_incomplete(self):
        for number in (1, 2, 3):
            self.stored(number)
        self.fake.state.intercept = lambda method, path, slug: 500 if path == f"/api/assets/{asset_id(2)}" else None
        reads = self.reader().custody_all([asset_id(n) for n in (1, 2, 3)])
        self.assertEqual((sorted(reads.found), reads.refused, reads.complete), ([asset_id(1)], (asset_id(2), asset_id(3)), False))

    def test_a_key_that_fails_the_access_check_raises_before_any_asset_is_read(self):
        self.stored(1)
        self.fake.state.permissions = (*self.fake.state.permissions, "asset.delete")
        with self.assertRaises(immich.ImmichFailed):
            self.reader().custody_all([asset_id(1)])
        self.assertFalse([path for _, path in self.fake.requests() if path.startswith("/api/assets/")])

    def test_the_answer_holds_no_asset_id_in_its_text(self):
        self.stored(1)
        self.assertNotIn(asset_id(1), repr(self.reader().custody_all([asset_id(1), asset_id(2)])))

