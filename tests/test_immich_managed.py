import base64
import hashlib
import io
import json
import unittest
import urllib.error
from unittest import mock

from darkroom import tls
from darkroom.providers import immich
from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson
from tests.privacy import no_leaks

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ASSET = "33333333-3333-4333-8333-333333333333"
PERSON_KEY = "managed-person-key-canary"
ADMIN_KEY = "managed-admin-key-canary"
DATA = b"generated managed original bytes"
SHA1 = hashlib.sha1(DATA, usedforsecurity=False).hexdigest()
READ_GRANTS = frozenset({"user.read", "server.about", "asset.read", "asset.download"})
IMPORT_GRANTS = READ_GRANTS | {"asset.upload"}
EXTRA_GRANTS = ("asset.delete", "asset.update", "asset.replace", "asset.copy", "asset.share", "asset.view", "asset.statistics",
                "library.read", "library.update", "user.update", "userProfileImage.update", "adminUser.read", "adminUser.update",
                "adminAuth.unlinkAll", "apiKey.create", "partner.read", "sharedLink.create", "trash.delete", "trash.restore",
                "passkey.read", "clusterGroup.admin", "unknown")
TAKEN = "2024-01-02T03:04:05+00:00"
EXTERNAL = "44444444-4444-4444-8444-444444444444"
OFFLINE = "55555555-5555-4555-8555-555555555555"


def upload_body():
    return immich.MultipartUpload(immich.UploadSource(io.BytesIO(DATA), len(DATA), SHA1), "generated.jpg", TAKEN, TAKEN)


def answering(status, raw):
    response = mock.MagicMock(status=status)
    response.read.return_value = raw
    response.__enter__.return_value = response
    return response


class ManagedAccessTests(unittest.TestCase):
    def setUp(self):
        self.person = FakePerson("jo", OWNER, "lib-jo", PERSON_KEY)
        self.fake = FakeImmich([self.person], ADMIN_KEY).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version = "v3.3.6"
        self.source = self.fake.add_asset(FakeAsset(ASSET, OWNER, DATA))
        self.client = immich.Client(self.fake.url, ADMIN_KEY, timeout=3)
        self.reader = immich.ManagedReader(self.client, PERSON_KEY, OWNER)

    def test_owned_managed_asset_and_unedited_bytes_use_only_the_person_key(self):
        with mock.patch.object(tls, "urlopen", wraps=tls.urlopen) as opened:
            asset = self.reader.get_asset(ASSET)
            self.assertEqual((asset.id, asset.owner_id, asset.sha1, asset.library_id), (ASSET, OWNER, SHA1, None))
            self.assertEqual(b"".join(self.reader.original_chunks(ASSET, SHA1)), DATA)
        self.assertTrue(all(call.kwargs["headers"]["x-api-key"] == PERSON_KEY for call in opened.call_args_list))
        self.assertTrue(opened.call_args_list[-1].args[0].endswith(f"/api/assets/{ASSET}/original?edited=false"))
        self.assertEqual([path for _, path in self.fake.requests()].count("/api/users/me"), 2)

    def test_a_reader_without_a_configured_admin_key_still_uses_the_explicit_person_key(self):
        reader = immich.ManagedReader(immich.Client(self.fake.url, ""), PERSON_KEY, OWNER)
        self.assertEqual(reader.get_asset(ASSET).sha1, SHA1)

    def test_missing_admin_control_character_keys_and_invalid_owners_never_dispatch(self):
        for key in ("", " ", "bad\nkey", "bad\rkey", "bad\x00key", "nonascii-\u2603", ADMIN_KEY):
            with self.subTest(key=key), self.assertRaises(immich.ImmichFailed):
                immich.ManagedReader(self.client, key, OWNER)
        for owner in ("", "u-jo", OWNER.upper(), "../users", None):
            if owner == OWNER:
                continue
            with self.subTest(owner=owner), self.assertRaises(immich.ImmichFailed):
                immich.ManagedReader(self.client, PERSON_KEY, owner)
        self.assertEqual(self.fake.requests(), [])

    def test_credential_urls_and_malformed_addresses_are_rejected_without_dispatch(self):
        for url in ("https://secret@example.invalid", "https://example.invalid?token=secret", "https://example.invalid#secret", "https://[", ""):
            with self.subTest(url=url), self.assertRaises(immich.ImmichFailed):
                immich.ManagedReader(immich.Client(url, ADMIN_KEY), PERSON_KEY, OWNER)
        self.assertEqual(self.fake.requests(), [])

    def test_unknown_and_destructive_operations_are_refused_before_network(self):
        paths = ("/api/assets", f"/api/assets/{ASSET}", f"/api/assets/{ASSET}/original", "/api/trash/empty",
                 "/api/libraries", "/api/users/me", "/api/api-keys", "/api/search/metadata")
        with mock.patch.object(tls, "urlopen") as opened:
            for method in ("DELETE", "PUT", "PATCH", "OPTIONS", "HEAD", "get", "POST"):
                for path in paths:
                    if (method, path) == ("POST", "/api/search/metadata"):
                        continue
                    with self.subTest(method=method, path=path), self.assertRaises(immich.ImmichFailed):
                        self.client.call(method, path, key=PERSON_KEY)
            for path in ("/api/unknown", f"/api/assets/{ASSET}/original?edited=true", "/api/assets/../users",
                         "/api/assets/%2e%2e", "/api/assets/a%2fb", f"/api/assets/{ASSET}?ownerId={OTHER}",
                         f"/api/assets/{ASSET}#fragment", "//example.invalid/api/users/me", "/api/users/me/",
                         "/api/users/me?x=1", "/api/assets/a/thumbnail", "/api/assets/a/metadata/unknown"):
                with self.subTest(path=path), self.assertRaises(immich.ImmichFailed):
                    self.client.call("GET", path, key=PERSON_KEY)
        opened.assert_not_called()

    def test_metadata_search_is_an_allowed_read_despite_using_post(self):
        self.assertEqual([a.id for a in self.client.search_updated(PERSON_KEY, None, "9999")], [ASSET])

    def test_invalid_asset_ids_and_checksums_do_not_even_check_access(self):
        for asset_id in ("", "not-a-uuid", "../users", ASSET + "/original", ASSET + "?edited=true", "%2f", None):
            with self.subTest(asset_id=asset_id), self.assertRaises(immich.ImmichFailed):
                self.reader.get_asset(asset_id)
        for checksum in ("", "f" * 39, "F" * 40, "z" * 40, SHA1 + "\n"):
            with self.subTest(checksum=checksum), self.assertRaises(immich.ImmichFailed):
                list(self.reader.original_chunks(ASSET, checksum))
        self.assertEqual(self.fake.requests(), [])

    def test_wrong_owner_stops_before_reading_server_or_asset(self):
        reader = immich.ManagedReader(self.client, PERSON_KEY, OTHER)
        with self.assertRaisesRegex(immich.ImmichFailed, "expected person"):
            reader.get_asset(ASSET)
        self.assertEqual(self.fake.requests(), [("GET", "/api/users/me")])

    def test_version_is_pinned_and_rechecked_after_a_successful_read(self):
        self.reader.get_asset(ASSET)
        self.fake.state.version = "v3.3.7"
        self.fake.state.requests.clear()
        with self.assertRaisesRegex(immich.ImmichFailed, "version is not supported"):
            self.reader.get_asset(ASSET)
        self.assertEqual(self.fake.requests(), [("GET", "/api/users/me"), ("GET", "/api/server/about")])

    def test_malformed_identity_version_and_permission_answers_fail_closed(self):
        real = self.client.call
        cases = (("/api/users/me", None), ("/api/users/me", {}), ("/api/users/me", {"id": 1}),
                 ("/api/server/about", None), ("/api/server/about", {"version": "3.3.6"}),
                 ("/api/server/about", {"version": "v3.3.6-dev"}), ("/api/api-keys/me", None),
                 ("/api/api-keys/me", {"permissions": "all"}), ("/api/api-keys/me", {"permissions": [{}]}))
        for target, answer in cases:
            def replied(method, path, target=target, answer=answer, **kwargs):
                return answer if path == target else real(method, path, **kwargs)
            with self.subTest(target=target, answer=answer), mock.patch.object(immich.Client, "call", side_effect=replied):
                with self.assertRaises(immich.ImmichFailed):
                    self.reader.get_asset(ASSET)
        self.assertNotIn(("GET", f"/api/assets/{ASSET}"), self.fake.requests())

    def test_missing_write_admin_and_unknown_permissions_are_refused_before_asset_read(self):
        approved = tuple(immich.MANAGED_READ_PERMISSIONS)
        cases = [(), approved[:-1]] + [(*approved, p) for p in ("all", "asset.upload", "asset.update", "asset.delete", "library.read", "unknown")]
        for permissions in cases:
            self.fake.state.permissions = permissions
            with self.subTest(permissions=permissions), self.assertRaisesRegex(immich.ImmichFailed, "approved read permissions"):
                self.reader.get_asset(ASSET)
        self.assertNotIn(("GET", f"/api/assets/{ASSET}"), self.fake.requests())

    def test_revocation_owner_reassignment_and_new_write_permissions_end_access(self):
        self.reader.get_asset(ASSET)
        for change in ("revoked", "reassigned", "permissions"):
            self.fake.state.requests.clear()
            self.fake.state.people = [self.person]
            self.fake.state.permissions = tuple(immich.MANAGED_READ_PERMISSIONS)
            if change == "revoked":
                self.fake.state.people = []
            elif change == "reassigned":
                self.fake.state.people = [FakePerson("sam", OTHER, "lib-sam", PERSON_KEY)]
            else:
                self.fake.state.permissions += ("asset.delete",)
            with self.subTest(change=change), self.assertRaises(immich.ImmichFailed):
                list(self.reader.original_chunks(ASSET, SHA1))
            self.assertNotIn(("GET", f"/api/assets/{ASSET}"), self.fake.requests())
            self.assertNotIn(("GET", f"/api/assets/{ASSET}/original"), self.fake.requests())

    def test_partner_external_trashed_offline_and_malformed_assets_cannot_supply_originals(self):
        raw = self.source.json()
        changes = ({"ownerId": OTHER}, {"id": OTHER}, {"libraryId": "external"}, {"libraryId": 0},
                   {"isTrashed": True}, {"isOffline": True}, {"isTrashed": 0}, {"isOffline": None},
                   {"type": "OTHER"}, {"livePhotoVideoId": "invalid"}, {"checksum": "invalid"},
                   {"checksum": base64.b64encode(b"short").decode()})
        cases = [None, []] + [raw | change for change in changes]
        cases += [{key: value for key, value in raw.items() if key != missing}
                  for missing in ("libraryId", "isTrashed", "isOffline", "ownerId", "id", "checksum", "type")]
        real = self.client.call
        for answer in cases:
            def replied(method, path, answer=answer, **kwargs):
                return answer if path == f"/api/assets/{ASSET}" else real(method, path, **kwargs)
            with self.subTest(answer=answer), mock.patch.object(immich.Client, "call", side_effect=replied):
                with self.assertRaises(immich.ImmichFailed):
                    list(self.reader.original_chunks(ASSET, SHA1))
        self.assertNotIn(("GET", f"/api/assets/{ASSET}/original"), self.fake.requests())

    def test_changed_catalogue_checksum_prevents_download_and_bad_download_fails_verification(self):
        with self.assertRaisesRegex(immich.ChecksumMismatch, "changed since"):
            list(self.reader.original_chunks(ASSET, "a" * 40))
        self.assertNotIn(("GET", f"/api/assets/{ASSET}/original"), self.fake.requests())
        self.source.data = b"different generated bytes"
        self.source.checksum = base64.b64encode(bytes.fromhex(SHA1)).decode()
        with self.assertRaisesRegex(immich.ChecksumMismatch, "does not match"):
            list(self.reader.original_chunks(ASSET, SHA1))

    def importer(self):
        self.fake.state.permissions = tuple(sorted(IMPORT_GRANTS))
        return immich.ManagedReader(self.client, PERSON_KEY, OWNER, access="import")

    def test_only_the_reviewed_versions_are_accepted_on_both_paths(self):
        for access in ("read", "import"):
            for version in ("v3.3.6", "v3.4.1"):
                self.fake.state.version = version
                reader = self.importer() if access == "import" else self.reader
                self.fake.state.permissions = tuple(sorted(IMPORT_GRANTS if access == "import" else READ_GRANTS))
                with self.subTest(access=access, version=version):
                    self.assertEqual(reader.get_asset(ASSET).sha1, SHA1)

    def test_unreviewed_and_malformed_versions_are_refused_before_the_key_or_asset_is_read(self):
        versions = ("v3.3.5", "v3.3.7", "v3.4.0", "v3.4.2", "v3.4.10", "v3.5.0", "v4.0.0", "3.4.1", "V3.4.1", "v3.4.1-rc.0",
                    "v3.4.1 ", " v3.4.1", "v3.4", "v3.4.x", "v3.4.*", "v3.4.1.0", "", None, 341, ["v3.4.1"])
        for access in ("read", "import"):
            reader = self.importer() if access == "import" else self.reader
            for version in versions:
                self.fake.state.version = version
                self.fake.state.requests.clear()
                with self.subTest(access=access, version=version), self.assertRaisesRegex(immich.ImmichFailed, "version is not supported"):
                    reader.get_asset(ASSET)
                self.assertEqual(self.fake.requests(), [("GET", "/api/users/me"), ("GET", "/api/server/about")])

    def test_read_path_still_requires_exactly_the_four_read_grants(self):
        self.fake.state.version = "v3.4.1"
        accepted = [tuple(READ_GRANTS)]
        refused = [(), tuple(IMPORT_GRANTS), ("all",), (*READ_GRANTS, "all")]
        refused += [tuple(READ_GRANTS - {missing}) for missing in READ_GRANTS]
        refused += [(*READ_GRANTS, extra) for extra in EXTRA_GRANTS]
        self.check_grants(self.reader, accepted, refused, "approved read permissions")

    def test_import_path_requires_exactly_the_four_read_grants_and_asset_upload(self):
        self.fake.state.version = "v3.4.1"
        reader = self.importer()
        accepted = [tuple(IMPORT_GRANTS), (*READ_GRANTS, "asset.upload"), ("asset.upload", *sorted(READ_GRANTS, reverse=True))]
        refused = [(), tuple(READ_GRANTS), ("asset.upload",), ("all",), (*IMPORT_GRANTS, "all")]
        refused += [tuple(IMPORT_GRANTS - {missing}) for missing in IMPORT_GRANTS]
        refused += [(*IMPORT_GRANTS, extra) for extra in EXTRA_GRANTS]
        refused += [tuple(IMPORT_GRANTS - {"asset.upload"} | {"asset.update"}), tuple(IMPORT_GRANTS - {"asset.download"} | {"asset.delete"})]
        self.check_grants(reader, accepted, refused, "approved import permissions")

    def test_the_pixel_path_takes_exactly_the_read_or_exactly_the_import_grants_and_only_reads(self):
        self.fake.state.version = "v3.4.1"
        reader = immich.ManagedReader(self.client, PERSON_KEY, OWNER, access="read_or_import")
        accepted = [tuple(READ_GRANTS), tuple(IMPORT_GRANTS)]
        refused = [(), ("asset.upload",), ("all",), (*IMPORT_GRANTS, "all")]
        refused += [tuple(READ_GRANTS - {missing}) for missing in READ_GRANTS]
        refused += [(*IMPORT_GRANTS, extra) for extra in EXTRA_GRANTS] + [(*READ_GRANTS, extra) for extra in EXTRA_GRANTS if extra != "asset.upload"]
        self.fake.state.requests.clear()
        self.check_grants(reader, accepted, refused, "approved read or import permissions")
        self.assertEqual({method for method, _ in self.fake.requests()}, {"GET"})
        for permissions in accepted:
            self.fake.state.permissions = permissions
            with self.subTest(permissions=permissions), self.assertRaisesRegex(immich.ImmichFailed, "only a managed import"):
                reader.upload(upload_body(), lambda: None)
            with self.subTest(permissions=permissions), self.assertRaisesRegex(immich.ImmichFailed, "only a managed import"):
                reader.bulk_check(SHA1)
        self.fake.state.permissions, self.fake.state.admins = tuple(IMPORT_GRANTS), ("jo",)
        with self.assertRaisesRegex(immich.ImmichFailed, "administrator"):
            reader.get_asset(ASSET)
        self.assertEqual({method for method, _ in self.fake.requests()}, {"GET"})

    def check_grants(self, reader, accepted, refused, message):
        for permissions in accepted:
            self.fake.state.permissions = permissions
            with self.subTest(permissions=permissions):
                self.assertEqual(b"".join(reader.original_chunks(ASSET, SHA1)), DATA)
        for permissions in refused:
            self.fake.state.permissions = permissions
            self.fake.state.requests.clear()
            with self.subTest(permissions=permissions), self.assertRaisesRegex(immich.ImmichFailed, message):
                list(reader.original_chunks(ASSET, SHA1))
            self.assertNotIn(("GET", f"/api/assets/{ASSET}"), self.fake.requests())

    def test_an_admin_user_is_refused_on_both_paths_even_with_exact_grants(self):
        self.fake.state.version = "v3.4.1"
        for access in ("read", "import"):
            reader = self.importer() if access == "import" else self.reader
            if access == "read":
                self.fake.state.permissions = tuple(READ_GRANTS)
            self.fake.state.admins = ("jo",)
            self.fake.state.requests.clear()
            with self.subTest(access=access), self.assertRaisesRegex(immich.ImmichFailed, "administrator"):
                reader.get_asset(ASSET)
            self.assertEqual(self.fake.requests(), [("GET", "/api/users/me")])
            self.fake.state.admins = ()
            self.assertEqual(reader.get_asset(ASSET).id, ASSET)

    def test_a_missing_or_non_boolean_admin_flag_is_refused(self):
        real = self.client.call
        for access in ("read", "import"):
            reader = self.importer() if access == "import" else self.reader
            if access == "read":
                self.fake.state.permissions = tuple(READ_GRANTS)
            for flag in ("missing", None, 0, "false", True, 1):
                answer = {"id": OWNER} if flag == "missing" else {"id": OWNER, "isAdmin": flag}
                def replied(method, path, answer=answer, **kwargs):
                    return answer if path == "/api/users/me" else real(method, path, **kwargs)
                with self.subTest(access=access, flag=flag), mock.patch.object(immich.Client, "call", side_effect=replied):
                    with self.assertRaisesRegex(immich.ImmichFailed, "administrator"):
                        reader.get_asset(ASSET)

    def test_an_unknown_access_kind_is_refused_without_dispatch(self):
        for access in ("upload", "write", "all", "", None, "READ", "import "):
            with self.subTest(access=access), self.assertRaises(immich.ImmichFailed):
                immich.ManagedReader(self.client, PERSON_KEY, OWNER, access=access)
        self.assertEqual(self.fake.requests(), [])

    def test_the_import_path_still_cannot_upload_trash_update_or_delete(self):
        self.importer().check_access()
        with mock.patch.object(tls, "urlopen") as opened:
            for method, path in (("POST", "/api/assets"), ("POST", "/api/assets/bulk-upload-check"), ("PUT", f"/api/assets/{ASSET}"),
                                 ("PUT", "/api/assets"), ("DELETE", "/api/assets"), ("POST", "/api/trash/empty"),
                                 ("POST", "/api/trash/restore"), ("PUT", f"/api/assets/{ASSET}/metadata")):
                with self.subTest(method=method, path=path), self.assertRaisesRegex(immich.ImmichFailed, "not allowed"):
                    self.client.call(method, path, key=PERSON_KEY, body={})
        opened.assert_not_called()

    def test_only_the_import_path_adds_the_upload_and_its_check_and_never_trash_update_or_delete(self):
        for method, path in sorted(immich.IMPORT_ROUTES):
            with self.assertRaisesRegex(immich.ImmichFailed, "not allowed"):
                immich.require_allowed_request(method, path)
            with self.assertRaisesRegex(immich.ImmichFailed, "not allowed"):
                immich.require_allowed_request(method, path, importing=1)
            immich.require_allowed_request(method, path, importing=True)
        refused = [(method, path) for method in ("DELETE", "PUT", "PATCH") for path in ("/api/assets", immich.BULK_CHECK_PATH, f"/api/assets/{ASSET}")]
        refused += [("POST", path) for path in ("/api/trash/empty", "/api/trash/restore", "/api/trash/restore/assets", f"/api/assets/{ASSET}", "/api/assets/",
                                                "/api/assets?x=1", f"{immich.BULK_CHECK_PATH}/", "/api/assets/copy", "/api/assets/jobs", f"/api/assets/{ASSET}/original")]
        refused += [("GET", "/api/assets"), ("POST", "/api/libraries"), ("PUT", f"/api/assets/{ASSET}/metadata")]
        with mock.patch.object(tls, "urlopen") as opened:
            for method, path in refused:
                with self.subTest(method=method, path=path), self.assertRaisesRegex(immich.ImmichFailed, "not allowed"):
                    self.client.call(method, path, key=PERSON_KEY, body={}, importing=True)
        opened.assert_not_called()

    def test_a_read_reader_cannot_upload_or_check_an_upload_and_sends_nothing(self):
        sent = []
        with self.assertRaisesRegex(immich.ImmichFailed, "only a managed import"):
            self.reader.upload(upload_body(), lambda: sent.append(1))
        with self.assertRaisesRegex(immich.ImmichFailed, "only a managed import"):
            self.reader.bulk_check(SHA1)
        for key in (ADMIN_KEY, "", None):
            with self.subTest(key=key), self.assertRaisesRegex(immich.ImmichFailed, "person's own key"):
                self.client.upload(key, upload_body())
            with self.subTest(key=key), self.assertRaisesRegex(immich.ImmichFailed, "person's own key"):
                self.client.bulk_check(key, SHA1)
        self.assertEqual((sent, self.fake.requests()), ([], []))

    def test_an_upload_streams_the_body_with_the_person_key_and_reads_its_answer(self):
        body = upload_body()
        answer = answering(201, json.dumps({"id": ASSET, "status": "created"}).encode())
        with mock.patch.object(tls, "urlopen", return_value=answer) as opened:
            self.assertEqual(self.client.upload(PERSON_KEY, body), immich.UploadResult(ASSET, "created"))
        sent = opened.call_args.kwargs
        self.assertIs(sent["data"], body)
        self.assertEqual((opened.call_args.args[0], sent["method"], sent["headers"]["x-api-key"], sent["headers"]["Content-Length"]),
                         (f"{self.fake.url}/api/assets", "POST", PERSON_KEY, body.headers["Content-Length"]))
        self.assertTrue(sent["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertGreaterEqual(sent["timeout"], immich.UPLOAD_TIMEOUT)

    def test_upload_answers_below_500_are_refusals_and_the_rest_are_uncertain_without_echoing_them(self):
        canary = "private-upload-answer-canary"
        replies = []
        with no_leaks(self, PERSON_KEY, ADMIN_KEY, canary, replies=replies):
            for status in (400, 401, 403, 413, 500, 502, 503):
                error = urllib.error.HTTPError(self.fake.url, status, canary, {}, io.BytesIO(canary.encode()))
                with self.subTest(status=status), mock.patch.object(tls, "urlopen", side_effect=error), self.assertRaises(immich.ImmichFailed) as caught:
                    self.client.upload(PERSON_KEY, upload_body())
                self.assertEqual((isinstance(caught.exception, immich.UploadUncertain), caught.exception.status), (status >= 500, status))
                replies.append(str(caught.exception).encode())
            for problem in (ConnectionResetError(canary), TimeoutError(canary), tls.Refused("redirects are not followed")):
                with self.subTest(problem=type(problem)), mock.patch.object(tls, "urlopen", side_effect=problem), self.assertRaises(immich.UploadUncertain) as caught:
                    self.client.upload(PERSON_KEY, upload_body())
                replies.append(str(caught.exception).encode())
            for status, raw in ((201, canary.encode()), (201, b"{}"), (200, json.dumps({"id": ASSET, "status": "created"}).encode()),
                                (202, json.dumps({"id": ASSET, "status": "created"}).encode()), (201, b" " * (immich.CHECK_LIMIT + 1))):
                with self.subTest(status=status), mock.patch.object(tls, "urlopen", return_value=answering(status, raw)), self.assertRaises(immich.UploadUncertain) as caught:
                    self.client.upload(PERSON_KEY, upload_body())
                replies.append(str(caught.exception).encode())

    def test_upload_checks_and_checksum_searches_fail_closed_on_answers_they_do_not_understand(self):
        reader, real, check = self.importer(), self.client.call, immich.BULK_CHECK_ID
        duplicate = {"id": check, "action": "reject", "reason": "duplicate", "assetId": ASSET}
        bulk = (None, {}, {"results": []}, {"results": [{"id": "other", "action": "accept"}]}, {"results": [duplicate, duplicate]},
                {"results": [duplicate | {"reason": "unsupported-format"}]}, {"results": [duplicate | {"assetId": "not-a-uuid"}]})
        search = (None, {}, {"assets": {"items": {}}}, {"assets": {"items": [], "nextCursor": "100"}}, {"assets": {"items": [{"id": ASSET}]}})
        for path, answers, ask in ((immich.BULK_CHECK_PATH, bulk, reader.bulk_check), ("/api/search/metadata", search, reader.managed_with_checksum)):
            for answer in answers:
                def replied(method, called, answer=answer, path=path, **kwargs):
                    return answer if called == path else real(method, called, **kwargs)
                with self.subTest(path=path, answer=answer), mock.patch.object(immich.Client, "call", side_effect=replied), self.assertRaises(immich.ImmichFailed):
                    ask(SHA1)
        def unflagged(method, called, **kwargs):
            return {"results": [duplicate]} if called == immich.BULK_CHECK_PATH else real(method, called, **kwargs)
        with mock.patch.object(immich.Client, "call", side_effect=unflagged):
            self.assertEqual(reader.bulk_check(SHA1), immich.BulkMatch(ASSET, True))
        self.assertEqual(reader.bulk_check(SHA1), immich.BulkMatch(ASSET, False))
        self.assertIsNone(reader.bulk_check("a" * 40))
        self.fake.state.requests.clear()
        for sha1 in ("", "F" * 40, SHA1 + "\n", None):
            for ask in (self.client.bulk_check, self.client.search_checksum):
                with self.subTest(sha1=sha1, ask=ask.__name__), self.assertRaisesRegex(immich.ImmichFailed, "not a source checksum"):
                    ask(PERSON_KEY, sha1)
        self.assertEqual(self.fake.requests(), [])

    def test_a_checksum_search_keeps_only_this_owners_available_managed_asset(self):
        for asset_id, owner, changes in ((OTHER, OTHER, {}), (EXTERNAL, OWNER, {"library_id": "lib-jo"}),
                                         (OFFLINE, OWNER, {"library_id": "lib-jo", "offline": True})):
            self.fake.add_asset(FakeAsset(asset_id, owner, DATA, **changes))
        self.fake.state.shared.add(OTHER)
        self.fake.state.search_shared = True
        self.assertEqual(len(self.client.search_checksum(PERSON_KEY, SHA1)), 4)
        self.assertEqual(self.importer().managed_with_checksum(SHA1), [ASSET])
        self.source.trashed = True
        self.assertEqual(self.importer().managed_with_checksum(SHA1), [])

    def test_read_back_needs_the_sources_checksum_and_size_in_an_available_unedited_original(self):
        reader = self.importer()
        reader.verify_original(ASSET, SHA1, len(DATA))
        self.assertIn(("GET", f"/api/assets/{ASSET}/original"), self.fake.requests())
        with self.assertRaisesRegex(immich.NotCustody, "other bytes"):
            reader.verify_original(ASSET, "a" * 40, len(DATA))
        with self.assertRaisesRegex(immich.ChecksumMismatch, "size"):
            reader.verify_original(ASSET, SHA1, len(DATA) + 1)
        self.fake.state.requests.clear()
        for sha1, size in (("F" * 40, 1), (SHA1, 0), (SHA1, True), (SHA1, "1"), (None, 1)):
            with self.subTest(sha1=sha1, size=size), self.assertRaisesRegex(immich.ImmichFailed, "not a recorded source"):
                reader.verify_original(ASSET, sha1, size)
        self.assertEqual(self.fake.requests(), [])
        for change, value, before in (("trashed", True, False), ("offline", True, False), ("library_id", "lib-jo", None), ("owner_id", OTHER, OWNER)):
            setattr(self.source, change, value)
            if change == "owner_id":
                self.fake.state.shared.add(ASSET)
            with self.subTest(change=change), self.assertRaises(immich.NotCustody):
                reader.verify_original(ASSET, SHA1, len(DATA))
            setattr(self.source, change, before)
        self.assertNotIn(("GET", f"/api/assets/{ASSET}/original"), self.fake.requests())

    def test_a_grant_added_after_a_successful_import_check_ends_access(self):
        reader = self.importer()
        reader.get_asset(ASSET)
        for extra in ("asset.delete", "asset.update", "all"):
            self.fake.state.permissions = (*IMPORT_GRANTS, extra)
            with self.subTest(extra=extra), self.assertRaises(immich.ImmichFailed):
                list(reader.original_chunks(ASSET, SHA1))
        self.fake.state.permissions = tuple(IMPORT_GRANTS)
        self.fake.state.version = "v3.4.2"
        with self.assertRaisesRegex(immich.ImmichFailed, "version is not supported"):
            reader.get_asset(ASSET)

    def test_secrets_do_not_escape_repr_network_errors_or_rejection_messages(self):
        canary = "private-response-body-canary"
        replies = []
        with no_leaks(self, PERSON_KEY, ADMIN_KEY, canary, replies=replies):
            replies.extend([repr(self.reader).encode(), repr(self.client).encode()])
            for status in (401, 403, 500):
                error = urllib.error.HTTPError(self.fake.url, status, canary, {}, io.BytesIO(canary.encode()))
                with mock.patch.object(tls, "urlopen", side_effect=error):
                    with self.assertRaises(immich.ImmichFailed) as caught:
                        self.reader.get_asset(ASSET)
                    replies.append(str(caught.exception).encode())
            with self.assertRaises(immich.ImmichFailed) as caught:
                self.client.call("DELETE", f"/api/assets/{canary}", key=PERSON_KEY)
            replies.append(str(caught.exception).encode())


if __name__ == "__main__":
    unittest.main()
