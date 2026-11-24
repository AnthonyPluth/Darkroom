import hashlib
import io
import json
import unittest
import urllib.error
from datetime import UTC, datetime
from unittest import mock

from darkroom import tls
from darkroom.providers import immich
from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson
from tests.privacy import no_leaks

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
PERSON_KEY = "near-person-key-canary"
ADMIN_KEY = "near-admin-key-canary"
READ_GRANTS = ("asset.download", "asset.read", "server.about", "user.read")
IMPORT_GRANTS = (*READ_GRANTS, "asset.upload")
SHOT = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
INSTANT = SHOT.timestamp()
SEARCH = ("POST", "/api/search/metadata")


def asset_id(n):
    return f"{n:08x}-0000-4000-8000-000000000000"


def stamp(seconds):
    return datetime.fromtimestamp(INSTANT + seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TakenNearTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich([FakePerson("jo", OWNER, "lib-jo", PERSON_KEY)], ADMIN_KEY).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version = "v3.4.1"
        self.client = immich.Client(self.fake.url, ADMIN_KEY, timeout=3)

    def reader(self, access="read"):
        self.fake.state.permissions = IMPORT_GRANTS if access == "import" else READ_GRANTS
        return immich.ManagedReader(self.client, PERSON_KEY, OWNER, access=access)

    def add(self, n, seconds=0.0, **changes):
        return self.fake.add_asset(FakeAsset(asset_id(n), changes.pop("owner_id", OWNER), f"bytes {n}".encode(), file_created_at=stamp(seconds), **changes))

    def bodies(self, called):
        return [json.loads(call.kwargs["data"]) for call in called.call_args_list if call.args[0].endswith("/api/search/metadata")]

    def test_the_query_asks_for_the_padded_window_on_file_created_at_with_exif_in_the_shape_the_pinned_sources_page(self):
        self.add(1)
        with mock.patch.object(tls, "urlopen", wraps=tls.urlopen) as opened:
            shots = self.reader().taken_near(INSTANT, 1.0)
        self.assertEqual(self.bodies(opened), [{"filter": {"takenAt": {"gte": "2024-05-01T11:59:58.000Z", "lte": "2024-05-01T12:00:02.000Z"}},
                                                "withExif": True, "size": 100, "orderBy": {"field": "fileCreatedAt", "direction": "asc"}}])
        self.assertTrue(all(call.kwargs["headers"]["x-api-key"] == PERSON_KEY for call in opened.call_args_list))
        self.assertEqual(self.fake.requests()[-1], SEARCH)
        self.assertEqual(self.fake.requests()[:3], [("GET", "/api/users/me"), ("GET", "/api/server/about"), ("GET", "/api/api-keys/me")])
        self.assertEqual(([n.asset.id for n in shots.found], shots.crowded), ([asset_id(1)], False))
        with mock.patch.object(tls, "urlopen", wraps=tls.urlopen) as opened:
            self.reader().taken_near(INSTANT + 0.25, 0)
        self.assertEqual(self.bodies(opened)[0]["filter"], {"takenAt": {"gte": "2024-05-01T11:59:59.250Z", "lte": "2024-05-01T12:00:01.250Z"}})

    def test_every_approved_access_may_search_and_each_refusal_stops_before_the_search(self):
        self.add(1)
        for access in ("read", "import", "read_or_import"):
            with self.subTest(access=access):
                self.assertEqual(len(self.reader(access).taken_near(INSTANT, 1.0).found), 1)
        refusals = (("read", IMPORT_GRANTS, {}), ("import", READ_GRANTS, {}), ("read", (*READ_GRANTS, "asset.delete"), {}),
                    ("read_or_import", ("all",), {}), ("read", READ_GRANTS, {"version": "v3.4.2"}), ("read", READ_GRANTS, {"admins": ("jo",)}))
        for access, grants, state in refusals:
            reader = immich.ManagedReader(self.client, PERSON_KEY, OWNER, access=access)
            self.fake.state.permissions, self.fake.state.version, self.fake.state.admins = grants, state.get("version", "v3.4.1"), state.get("admins", ())
            self.fake.state.requests.clear()
            with self.subTest(access=access, grants=grants, state=state), self.assertRaises(immich.ImmichFailed):
                reader.taken_near(INSTANT, 1.0)
            self.assertNotIn(SEARCH, self.fake.requests())
        self.fake.state.admins, self.fake.state.version = (), "v3.4.1"
        self.fake.state.requests.clear()
        with self.assertRaisesRegex(immich.ImmichFailed, "expected person"):
            immich.ManagedReader(self.client, PERSON_KEY, OTHER).taken_near(INSTANT, 1.0)
        self.assertNotIn(SEARCH, self.fake.requests())

    def test_only_this_owners_assets_come_back_and_trashed_offline_external_and_hidden_ones_stay(self):
        self.add(1)
        self.add(2, 0.4, trashed=True)
        self.add(3, -0.6, library_id="lib-jo", offline=True)
        self.add(4, 0.9, visibility="hidden", kind="VIDEO")
        self.add(5, 0.0, owner_id=OTHER)
        self.add(6, 3.0)
        self.fake.state.shared.add(asset_id(5))
        self.fake.state.search_shared = True
        shots = self.reader().taken_near(INSTANT, 1.0)
        found = {n.asset.id: n for n in shots.found}
        self.assertEqual(sorted(found), [asset_id(n) for n in (1, 2, 3, 4)])
        self.assertTrue(found[asset_id(2)].asset.trashed)
        self.assertEqual((found[asset_id(3)].asset.library_id, found[asset_id(3)].asset.offline), ("lib-jo", True))
        self.assertEqual((found[asset_id(4)].asset.kind, found[asset_id(4)].asset.visibility, found[asset_id(4)].duration_ms), ("VIDEO", "hidden", None))

    def test_a_candidates_instant_is_its_capture_time_and_otherwise_its_file_time(self):
        exif = {"dateTimeOriginal": stamp(0.5), "make": "Generated", "model": "Model 1", "exifImageWidth": 4032, "exifImageHeight": 3024}
        self.add(1, -0.5, exif=exif)
        self.add(2, 0.25)
        self.add(3, 0.0, exif={"dateTimeOriginal": None, "make": "", "exifImageWidth": -1}, kind="VIDEO")
        found = {n.asset.id: n for n in self.reader().taken_near(INSTANT, 1.0).found}
        self.assertEqual(found[asset_id(1)].taken, datetime(2024, 5, 1, 12, 0, 0, 500000, tzinfo=UTC))
        self.assertEqual((found[asset_id(1)].make, found[asset_id(1)].model, found[asset_id(1)].width, found[asset_id(1)].height), ("Generated", "Model 1", 4032, 3024))
        self.assertEqual(found[asset_id(2)].taken, datetime(2024, 5, 1, 12, 0, 0, 250000, tzinfo=UTC))
        self.assertEqual((found[asset_id(2)].make, found[asset_id(2)].width), (None, None))
        self.assertEqual((found[asset_id(3)].taken, found[asset_id(3)].make, found[asset_id(3)].width, found[asset_id(3)].duration_ms), (SHOT, None, None, 300))

    def test_an_upload_not_extracted_yet_is_found_by_its_upload_time(self):
        self.fake.state.extraction_delay = 3600
        data = b"generated near bytes"
        source = immich.UploadSource(io.BytesIO(data), len(data), hashlib.sha1(data, usedforsecurity=False).hexdigest())
        body = immich.MultipartUpload(source, "generated.jpg", "2024-05-01T14:00:00.300+02:00", "2024-05-01T14:00:00.300+02:00")
        uploaded = self.reader("import").upload(body, lambda: None)
        found = self.reader().taken_near(INSTANT, 1.0).found
        self.assertEqual([(n.asset.id, n.taken) for n in found], [(uploaded.asset_id, datetime(2024, 5, 1, 12, 0, 0, 300000, tzinfo=UTC))])

    def test_pages_are_followed_up_to_ten_and_a_fuller_window_is_crowded_never_empty(self):
        self.fake.state.page_size = 2
        for n in range(20):
            self.add(n + 1, n / 20)
        self.fake.state.requests.clear()
        shots = self.reader().taken_near(INSTANT, 1.0)
        self.assertEqual((len(shots.found), shots.crowded, self.fake.requests().count(SEARCH)), (20, False, 10))
        self.add(21, 0.99)
        self.fake.state.requests.clear()
        shots = self.reader().taken_near(INSTANT, 1.0)
        self.assertEqual((shots.found, shots.crowded, self.fake.requests().count(SEARCH)), ((), True, 10))

    def test_bad_instants_and_windows_never_reach_immich(self):
        for instant, seconds in ((float("nan"), 1.0), (float("inf"), 1.0), (True, 1.0), ("2024", 1.0), (None, 1.0), (1e20, 1.0),
                                 (INSTANT, -0.5), (INSTANT, 61), (INSTANT, float("nan")), (INSTANT, False), (INSTANT, "1")):
            with self.subTest(instant=instant, seconds=seconds), self.assertRaisesRegex(immich.ImmichFailed, "not a capture time"):
                self.reader().taken_near(instant, seconds)
        self.assertEqual(self.fake.requests(), [])

    def test_answers_it_does_not_understand_fail_closed_and_never_read_as_no_match(self):
        good = FakeAsset(asset_id(1), OWNER, b"one", file_created_at=stamp(0)).json()
        pages = ([None], [{}], [{"assets": {"items": {}}}], [{"assets": []}], [{"assets": {"items": [good] * 101}}],
                 [{"assets": {"items": [], "nextCursor": ""}}], [{"assets": {"items": [], "nextCursor": 5}}],
                 [{"assets": {"items": [], "nextCursor": "c1"}}, {"assets": {"items": [], "nextCursor": "c1"}}],
                 [{"assets": {"items": ["x"]}}], [{"assets": {"items": [good | {"id": None}]}}], [{"assets": {"items": [good | {"checksum": "not base64"}]}}],
                 [{"assets": {"items": [good | {"fileCreatedAt": "yesterday"}]}}], [{"assets": {"items": [{k: v for k, v in good.items() if k != "fileCreatedAt"}]}}],
                 [{"assets": {"items": [good | {"fileCreatedAt": "2024-05-01T12:00:00"}]}}])
        reader, real = self.reader(), self.client.call
        for answers in pages:
            queue = list(answers)

            def replied(method, path, queue=queue, **kwargs):
                return queue.pop(0) if path == "/api/search/metadata" else real(method, path, **kwargs)
            with self.subTest(answers=answers), mock.patch.object(immich.Client, "call", side_effect=replied), self.assertRaises(immich.ImmichFailed):
                reader.taken_near(INSTANT, 1.0)
        for mode in ("500", "down", "401"):
            self.fake.mode(mode)
            with self.subTest(mode=mode), self.assertRaises(immich.ImmichFailed):
                reader.taken_near(INSTANT, 1.0)

    def test_an_asset_repeated_across_pages_comes_back_once(self):
        first = FakeAsset(asset_id(1), OWNER, b"one", file_created_at=stamp(0)).json()
        second = FakeAsset(asset_id(2), OWNER, b"two", file_created_at=stamp(0)).json()
        answers = [{"assets": {"items": [first], "nextCursor": "c1"}}, {"assets": {"items": [first, second], "nextCursor": None}}]
        real = self.client.call

        def replied(method, path, **kwargs):
            return answers.pop(0) if path == "/api/search/metadata" else real(method, path, **kwargs)
        with mock.patch.object(immich.Client, "call", side_effect=replied) as called:
            shots = self.reader().taken_near(INSTANT, 1.0)
        self.assertEqual([n.asset.id for n in shots.found], [asset_id(1), asset_id(2)])
        self.assertEqual([c.kwargs["body"].get("cursor") for c in called.call_args_list if c.args[1] == "/api/search/metadata"], [None, "c1"])

    def test_names_places_descriptions_keys_and_answer_bodies_never_escape(self):
        name, place, description, body = "IMG_near-name-canary.JPG", "near-place-canary", "near-description-canary", "near-answer-body-canary"
        exif = {"dateTimeOriginal": stamp(0), "city": place, "description": description, "latitude": 1.5, "longitude": 2.5}
        self.add(1, 0.0, name=name, exif=exif)
        replies = []
        with no_leaks(self, PERSON_KEY, ADMIN_KEY, name, place, description, body, replies=replies):
            shots = self.reader().taken_near(INSTANT, 1.0)
            replies.extend([repr(shots).encode(), repr(shots.found[0]).encode(), str(shots).encode()])
            real = tls.urlopen
            for status in (400, 401, 500):
                error = urllib.error.HTTPError(self.fake.url, status, body, {}, io.BytesIO(json.dumps({"message": body, "name": name}).encode()))

                def failing(url, *args, error=error, **kwargs):
                    if url.endswith("/api/search/metadata"):
                        raise error
                    return real(url, *args, **kwargs)
                with mock.patch.object(tls, "urlopen", side_effect=failing), self.assertRaises(immich.ImmichFailed) as caught:
                    self.reader().taken_near(INSTANT, 1.0)
                replies.append(str(caught.exception).encode())
            reader, real_call = self.reader(), self.client.call

            def odd(method, path, **kwargs):
                return {"assets": {"items": [{"id": name, "ownerId": place}]}} if path == "/api/search/metadata" else real_call(method, path, **kwargs)
            with mock.patch.object(immich.Client, "call", side_effect=odd), self.assertRaisesRegex(immich.ImmichFailed, "without its id") as caught:
                reader.taken_near(INSTANT, 1.0)
            replies.append(str(caught.exception).encode())
        self.assertEqual(shots.found[0].asset.file_name, name)


if __name__ == "__main__":
    unittest.main()
