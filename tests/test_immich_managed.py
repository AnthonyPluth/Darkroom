import base64
import hashlib
import io
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
