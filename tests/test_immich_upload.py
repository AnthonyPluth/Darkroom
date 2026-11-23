import hashlib
import io
import re
import threading
import unittest

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_upload
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount, ManagedImportIntent, ManagedImportReceipt, Person
from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson
from tests.privacy import no_leaks
from tests.shared import own_database_url

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
EXISTING = "33333333-3333-4333-8333-333333333333"
FOREIGN = "44444444-4444-4444-8444-444444444444"
EXTERNAL = "55555555-5555-4555-8555-555555555555"
OFFLINE = "66666666-6666-4666-8666-666666666666"
PERSON_KEY = "upload-person-key-canary"
OTHER_KEY = "upload-other-key-canary"
ADMIN_KEY = "upload-admin-key-canary"
FILENAME = "generated-upload-name-canary.jpg"
TAKEN = "2024-01-02T03:04:05+00:00"
DATA = b"generated upload original bytes"
NOW = 1_800_000_000.0
GRANTS = ("asset.download", "asset.read", "asset.upload", "server.about", "user.read")
UPLOAD, BULK, SEARCH = ("POST", "/api/assets"), ("POST", "/api/assets/bulk-upload-check"), ("POST", "/api/search/metadata")
READS = {("GET", "/api/users/me"), ("GET", "/api/server/about"), ("GET", "/api/api-keys/me"), UPLOAD, BULK, SEARCH}


def sha1(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


class ManagedUploadTests(unittest.TestCase):
    def setUp(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            connection.execute(insert(Person).values(id=1, name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0))
            connection.execute(insert(GoogleAccount).values(id=1, email="jo@example.test", default_person_id=1, created=1.0))
            connection.execute(insert(Export).values(id=1, account_id=1, kind="manual"))
            connection.execute(insert(ManagedImportIntent).values(id=1, person_id=1, export_id=1, owner_id=OWNER, resource_manifest="{}", created_at=1.0))
        self.fake = FakeImmich([FakePerson("jo", OWNER, "lib-jo", PERSON_KEY), FakePerson("sam", OTHER, "lib-sam", OTHER_KEY)], ADMIN_KEY).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version, self.fake.state.permissions = "v3.4.1", GRANTS
        self.client = immich.Client(self.fake.url, ADMIN_KEY, timeout=5)
        self.reader = immich.ManagedReader(self.client, PERSON_KEY, OWNER, access="import")
        self.opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.opened.close)

    def receipt(self, key="part/still", data=DATA):
        with self.engine.begin() as connection:
            return connection.execute(insert(ManagedImportReceipt).values(
                intent_id=1, resource_key=key, compound_key=key, source_sha1=sha1(data), expected_bytes=len(data),
                expected_metadata="{}", updated_at=1.0)).inserted_primary_key[0]

    def upload(self, receipt_id, data=DATA, reader=None, opened=None, now=NOW):
        def body():
            return immich.MultipartUpload(immich.UploadSource(io.BytesIO(data), len(data), sha1(data)), FILENAME, TAKEN, TAKEN)
        return managed_upload.upload_receipt(opened or self.opened, reader or self.reader, receipt_id, body, now)

    def row(self, receipt_id):
        receipt = ManagedImportReceipt
        with self.engine.connect() as connection:
            return tuple(connection.execute(select(receipt.state, receipt.asset_id, receipt.upload_outcome, receipt.verified_sha1, receipt.verified_bytes,
                                                   receipt.bytes_verified_at).where(receipt.id == receipt_id)).one())

    def attempts(self, receipt_id):
        with self.engine.connect() as connection:
            return tuple(connection.execute(select(ManagedImportReceipt.send_attempts, ManagedImportReceipt.send_lease_until)
                                            .where(ManagedImportReceipt.id == receipt_id)).one())

    def sent(self, request=UPLOAD):
        return self.fake.requests().count(request)

    def record(self, receipt_id, on_path, **values):
        def recorded(method, path, slug):
            if path == on_path:
                self.fake.state.intercept = None
                with self.engine.begin() as connection:
                    connection.execute(update(ManagedImportReceipt).where(ManagedImportReceipt.id == receipt_id).values(**values))
        self.fake.state.intercept = recorded

    def assert_only_reads_and_uploads(self):
        for method, path in self.fake.requests():
            self.assertTrue((method, path) in READS or (method == "GET" and re.fullmatch(r"/api/assets/[0-9a-f-]{36}(?:/original)?", path)), (method, path))

    def test_a_new_original_is_created_read_back_unedited_and_verified(self):
        receipt = self.receipt()
        report = self.upload(receipt)
        (asset,) = self.fake.state.assets
        self.assertEqual(report.outcome, "verified")
        self.assertEqual(self.row(receipt), ("bytes_verified", asset.id, "created", sha1(DATA), len(DATA), NOW))
        self.assertEqual((asset.owner_id, asset.library_id, asset.data), (OWNER, None, DATA))
        self.assertIn(("GET", f"/api/assets/{asset.id}/original"), self.fake.requests())
        self.assertEqual(self.sent(), 1)
        self.assert_only_reads_and_uploads()
        self.fake.state.requests.clear()
        self.assertEqual(self.upload(receipt).outcome, "verified")
        self.assertEqual(self.fake.requests(), [])

    def test_the_same_bytes_already_managed_for_this_owner_are_a_verified_duplicate(self):
        self.fake.add_asset(FakeAsset(EXISTING, OWNER, DATA))
        receipt = self.receipt()
        self.assertEqual(self.upload(receipt).outcome, "verified")
        self.assertEqual(self.row(receipt), ("bytes_verified", EXISTING, "duplicate", sha1(DATA), len(DATA), NOW))
        self.assertEqual(len(self.fake.state.assets), 1)

    def test_concurrent_uploads_of_the_same_bytes_verify_one_asset(self):
        receipts, reports = [self.receipt("part/a"), self.receipt("part/b")], {}

        def run(receipt_id):
            with Session(self.engine, expire_on_commit=False) as opened:
                reports[receipt_id] = self.upload(receipt_id, opened=opened)
        threads = [threading.Thread(target=run, args=(receipt_id,)) for receipt_id in receipts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        (asset,) = self.fake.state.assets
        rows = [self.row(receipt_id) for receipt_id in receipts]
        self.assertEqual([reports[receipt_id].outcome for receipt_id in receipts], ["verified", "verified"])
        self.assertEqual({(row[0], row[1]) for row in rows}, {("bytes_verified", asset.id)})
        self.assertEqual(sorted(row[2] for row in rows), ["created", "duplicate"])
        self.assertEqual(self.sent(), 2)

    def test_two_workers_that_both_read_a_prepared_receipt_send_it_once(self):
        receipt, reports, waited = self.receipt(), [], []
        together = threading.Barrier(2, timeout=10)

        def both_read_it_first(method, path, slug):
            if path == "/api/api-keys/me" and len(waited) < 2:
                waited.append(path)
                together.wait()
        self.fake.state.intercept = both_read_it_first

        def run():
            with Session(self.engine, expire_on_commit=False) as opened:
                reports.append(self.upload(receipt, opened=opened).outcome)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        (asset,) = self.fake.state.assets
        self.assertEqual((len(waited), self.sent()), (2, 1))
        self.assertIn("verified", reports)
        self.assertLessEqual(set(reports), {"verified", "pending"})
        self.assertEqual((self.row(receipt)[:3], self.attempts(receipt)), (("bytes_verified", asset.id, "created"), (1, None)))
        self.assertEqual(self.upload(receipt).outcome, "verified")
        self.assertEqual(self.sent(), 1)

    def test_an_attempt_in_flight_holds_the_receipt_until_its_lease_ends_and_then_recovery_runs_first(self):
        receipt = self.receipt()
        with self.engine.begin() as connection:
            connection.execute(update(ManagedImportReceipt).where(ManagedImportReceipt.id == receipt)
                               .values(state="outcome_unknown", send_attempts=1, send_lease_until=NOW + 60))
        self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual(self.fake.requests(), [])
        self.assertEqual(self.upload(receipt, now=NOW + 60).outcome, "verified")
        posts = [path for method, path in self.fake.requests() if method == "POST"]
        self.assertEqual(posts, [BULK[1], UPLOAD[1]])
        self.assertEqual(self.attempts(receipt), (2, None))

    def test_a_lost_response_is_found_by_checksum_before_any_retry(self):
        receipt = self.receipt()
        self.fake.state.lose_uploads = 1
        self.fake.state.intercept = lambda method, path, slug: 500 if path == BULK[1] else None
        self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual(self.row(receipt)[:2], ("outcome_unknown", None))
        self.fake.state.intercept = None
        self.assertEqual(self.upload(receipt).outcome, "verified")
        (asset,) = self.fake.state.assets
        self.assertEqual(self.row(receipt)[:3], ("bytes_verified", asset.id, "duplicate"))
        self.assertEqual([path for method, path in self.fake.requests() if method == "POST"], [UPLOAD[1], BULK[1], BULK[1], SEARCH[1]])

    def test_a_5xx_is_retried_a_bounded_number_of_times_each_after_a_checksum_check(self):
        receipt = self.receipt()
        self.fake.state.intercept = lambda method, path, slug: 503 if path == UPLOAD[1] else None
        self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual(self.row(receipt)[:2], ("outcome_unknown", None))
        self.assertEqual((self.sent(), self.sent(BULK), self.fake.state.assets), (managed_upload.MAX_SENDS, managed_upload.MAX_SENDS - 1, []))
        self.assertEqual(self.attempts(receipt), (managed_upload.MAX_SENDS, None))
        self.fake.state.intercept = None
        self.assertEqual(self.upload(receipt).outcome, "verified")
        (asset,) = self.fake.state.assets
        self.assertEqual(self.row(receipt)[:3], ("bytes_verified", asset.id, "created"))

    def test_a_failed_read_back_stays_returned_and_is_read_again_without_sending_again(self):
        receipt = self.receipt()
        self.fake.state.intercept = lambda method, path, slug: 500 if path.endswith("/original") else None
        self.assertEqual(self.upload(receipt).outcome, "pending")
        (asset,) = self.fake.state.assets
        self.assertEqual(self.row(receipt), ("returned", asset.id, "created", None, None, None))
        self.fake.state.intercept = None
        self.assertEqual(self.upload(receipt).outcome, "verified")
        self.assertEqual((self.row(receipt)[:2], self.sent()), (("bytes_verified", asset.id), 1))

    def test_a_trashed_duplicate_is_unusable_and_never_restored(self):
        self.fake.add_asset(FakeAsset(EXISTING, OWNER, DATA, trashed=True))
        receipt = self.receipt()
        self.assertEqual(self.upload(receipt).outcome, "unusable")
        self.assertEqual(self.row(receipt), ("returned", EXISTING, "duplicate", None, None, None))
        self.assertTrue(self.fake.state.assets[0].trashed)
        self.assertNotIn(("GET", f"/api/assets/{EXISTING}/original"), self.fake.requests())
        self.assert_only_reads_and_uploads()

    def test_another_owners_external_or_offline_asset_is_unusable(self):
        self.fake.add_asset(FakeAsset(FOREIGN, OTHER, DATA))
        self.fake.add_asset(FakeAsset(EXTERNAL, OWNER, DATA, library_id="lib-jo"))
        self.fake.add_asset(FakeAsset(OFFLINE, OWNER, DATA, library_id="lib-jo", offline=True))
        for name, asset_id in (("unreadable", FOREIGN), ("partner", FOREIGN), ("external", EXTERNAL), ("offline", OFFLINE)):
            if name == "partner":
                self.fake.state.shared.add(FOREIGN)
            self.fake.state.upload_answer = {"id": asset_id, "status": "duplicate"}
            receipt = self.receipt(name)
            with self.subTest(name):
                self.assertEqual(self.upload(receipt).outcome, "unusable")
                self.assertEqual(self.row(receipt)[:4], ("returned", asset_id, "duplicate", None))
        self.assertFalse([path for method, path in self.fake.requests() if path.endswith("/original")])

    def test_recovery_ignores_external_and_partner_copies_and_uploads_a_managed_original(self):
        self.fake.add_asset(FakeAsset(EXTERNAL, OWNER, DATA, library_id="lib-jo"))
        self.fake.add_asset(FakeAsset(FOREIGN, OTHER, DATA))
        self.fake.state.shared.add(FOREIGN)
        self.fake.state.search_shared = True
        failures = iter((502,))
        self.fake.state.intercept = lambda method, path, slug: next(failures, None) if path == UPLOAD[1] else None
        receipt = self.receipt()
        self.assertEqual(self.upload(receipt).outcome, "verified")
        state, asset_id, outcome = self.row(receipt)[:3]
        self.assertEqual((state, outcome), ("bytes_verified", "created"))
        self.assertNotIn(asset_id, (EXTERNAL, FOREIGN))
        self.assertEqual((self.sent(), self.sent(BULK), self.sent(SEARCH)), (2, 1, 1))

    def test_a_revoked_key_leaves_the_receipt_prepared_and_sends_nothing(self):
        receipt = self.receipt()
        self.fake.state.people = [person for person in self.fake.state.people if person.slug != "jo"]
        self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual(self.row(receipt)[0], "prepared")
        self.assertEqual(self.fake.requests(), [("GET", "/api/users/me")])

    def test_a_key_revoked_during_the_upload_is_not_resent_and_is_checked_by_checksum_next_time(self):
        receipt = self.receipt()
        self.fake.state.intercept = lambda method, path, slug: 401 if path == UPLOAD[1] else None
        self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual((self.row(receipt)[0], self.sent(), self.attempts(receipt)), ("outcome_unknown", 1, (1, None)))
        self.fake.state.intercept = None
        self.assertEqual(self.upload(receipt).outcome, "verified")
        self.assertEqual((self.sent(), self.sent(BULK)), (2, 1))

    def test_a_read_only_admin_broader_or_other_owners_key_never_uploads(self):
        receipt = self.receipt()
        for reader in (immich.ManagedReader(self.client, PERSON_KEY, OWNER), immich.ManagedReader(self.client, OTHER_KEY, OTHER, access="import")):
            self.assertEqual(self.upload(receipt, reader=reader).outcome, "pending")
        self.assertEqual(self.fake.requests(), [])
        for change in ("admin", "grants"):
            self.fake.state.admins, self.fake.state.permissions = (("jo",), GRANTS) if change == "admin" else ((), (*GRANTS, "asset.delete"))
            with self.subTest(change):
                self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual((self.row(receipt)[0], self.sent(), self.fake.state.assets), ("prepared", 0, []))

    def test_a_body_that_is_not_the_receipts_source_is_never_sent(self):
        receipt = self.receipt()
        self.assertEqual(self.upload(receipt, data=b"other generated bytes").outcome, "pending")
        self.assertEqual((self.row(receipt)[0], self.fake.requests()), ("prepared", []))
        with self.assertRaises(LookupError):
            self.upload(receipt + 1)

    def test_an_answer_another_worker_recorded_is_read_back_without_sending_again(self):
        self.fake.add_asset(FakeAsset(EXISTING, OWNER, DATA))
        receipt = self.receipt()
        self.record(receipt, "/api/api-keys/me", state="returned", asset_id=EXISTING, upload_outcome="duplicate")
        self.assertEqual(self.upload(receipt).outcome, "verified")
        self.assertEqual((self.row(receipt)[:2], self.sent()), (("bytes_verified", EXISTING), 0))

    def test_a_receipt_that_already_names_another_asset_is_left_for_the_owner(self):
        receipt = self.receipt()
        self.record(receipt, UPLOAD[1], state="returned", asset_id=FOREIGN, upload_outcome="created")
        self.assertEqual(self.upload(receipt).outcome, "pending")
        self.assertEqual(self.row(receipt)[:2], ("returned", FOREIGN))
        self.assertEqual(len(self.fake.state.assets), 1)

    def test_no_key_address_or_filename_reaches_logs_reports_or_the_journal(self):
        replies = []
        with no_leaks(self, PERSON_KEY, ADMIN_KEY, OTHER_KEY, FILENAME, self.fake.url, database=self.engine, replies=replies):
            for name, intercept in (("5xx", lambda method, path, slug: 500 if path == UPLOAD[1] else None), ("revoked", lambda method, path, slug: 401), ("created", None)):
                self.fake.state.intercept = intercept
                replies.append(repr(self.upload(self.receipt(name))).encode())
            self.fake.state.upload_answer = {"id": FOREIGN, "status": "duplicate"}
            replies.append(repr(self.upload(self.receipt("unusable"))).encode())


if __name__ == "__main__":
    unittest.main()
