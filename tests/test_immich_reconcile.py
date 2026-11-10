import dataclasses
import hashlib
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.domain import immich_reconcile as reconcile
from darkroom.domain import index, media, pixel_folder
from darkroom.providers import exiftool, immich
from darkroom.storage import secretbox
from darkroom.storage.models import Alert, Event, Export, ImmichAsset, ImportRun, IndexSkip, MediaItem, Person, PixelCopy
from tests.fakes.immich import FakeAsset
from tests.fixtures import media as fixtures
from tests.import_world import NOW, World, tree

OLD = "2020-01-01T00:00:00.000Z"
SECRET = "reconcile-secret"


def sha1(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.settings = dataclasses.replace(self.world.settings, secret_key=SECRET)
        self.jo = self.world.opened.get(Person, self.world.ids["jo"])
        self.jo.immich_api_key_sealed = secretbox.seal("key-jo", SECRET)
        self.world.opened.commit()
        export = self.world.downloaded()
        self.world.run_import(export)
        self.counter = 0

    def photo(self, **fields):
        self.counter += 1
        data = self.world.scratch_jpeg(f"p{self.counter}.jpg", taken=f"2021:03:0{self.counter} 10:00:00", offset="+00:00", colour=(self.counter, 9, 9))
        fields.setdefault("id", f"a{self.counter}")
        fields.setdefault("name", f"IMG_{self.counter:04d}.JPG")
        return self.world.fake.add_asset(FakeAsset(owner_id="u-jo", data=data, updated_at=fields.pop("updated_at", "2026-01-01T00:00:00.000Z"), **fields))

    def run_it(self, at=NOW, queue=None):
        return reconcile.reconcile_person(self.world.opened, self.settings, self.jo, at, queue=queue)

    def stored(self):
        return sorted(p.name for p in (self.world.root / "jo" / "originals" / "immich").rglob("*") if p.is_file()) if (self.world.root / "jo" / "originals" / "immich").exists() else []

    def rows(self):
        return {row.asset_id: row for row in self.world.opened.execute(select(ImmichAsset)).scalars()}

    def copies(self):
        return list(self.world.opened.execute(select(PixelCopy).order_by(PixelCopy.id)).scalars())


class StoreTests(ReconcileCase):
    def test_a_phone_upload_is_written_once_into_originals_and_queued_for_the_pixel_even_when_takeout_matches(self):
        asset = self.photo()
        confirmed = self.world.fake.add_asset(FakeAsset(id="old", owner_id="u-jo", data=(self.world.root / "jo/originals/IMG_1001.JPG").read_bytes(), name="IMG_1001.JPG"))
        report = self.run_it(queue=reconcile.PixelFolderQueue())
        self.assertEqual((report.listed, report.stored, report.copied, report.already_there, report.queued), (2, 2, 1, 1, 2))
        (name,) = self.stored()
        self.assertTrue(name.startswith("IMG_0001-"))
        self.assertEqual((self.world.root / "jo/originals/immich" / name).read_bytes(), asset.data)
        item = self.world.opened.execute(select(MediaItem).where(MediaItem.sha1 == sha1(asset.data))).scalar_one()
        self.assertEqual(item.path, f"jo/originals/immich/{name}")
        queued = {copy.item_id: copy for copy in self.copies()}
        self.assertEqual(len(queued), 2)
        self.assertTrue(all(copy.always_send and copy.state == "queued" for copy in queued.values()))
        self.assertEqual({row.status for row in self.rows().values()}, {"stored"})
        self.assertIn(confirmed.id, self.rows())

    def test_a_second_run_changes_nothing_and_rewrites_no_original(self):
        self.photo()
        self.run_it(queue=reconcile.PixelFolderQueue())
        before, rows, copies = tree(self.world.root / "jo" / "originals"), len(self.rows()), len(self.copies())
        again = self.run_it(at=NOW + 3600, queue=reconcile.PixelFolderQueue())
        self.assertEqual((again.stored, again.copied, again.queued), (0, 0, 0))
        self.assertEqual((tree(self.world.root / "jo" / "originals"), len(self.rows()), len(self.copies())), (before, rows, copies))

    def test_only_phone_uploads_of_this_person_count(self):
        self.photo(metadata=())
        self.photo(library_id="lib-jo")
        self.photo(trashed=True)
        self.world.fake.add_asset(FakeAsset(id="sams", owner_id="u-sam", data=b"x"))
        self.photo(id="owned-by-partner", updated_at="2026-01-01T00:00:00.000Z")
        before = tree(self.world.root / "jo" / "originals")
        report = self.run_it()
        self.assertEqual((report.stored, report.ignored), (1, 2))
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(set(tree(self.world.root / "jo" / "originals")) - set(before), {f"immich/{self.stored()[0]}"})
        self.assertNotIn("sams", self.rows())

    def test_pages_are_all_read_and_only_the_window_is_listed_next_time(self):
        self.world.fake.state.page_size = 1
        for _ in range(3):
            self.photo()
        self.assertEqual(self.run_it().stored, 3)
        self.photo(updated_at=OLD)
        late = self.photo(updated_at=reconcile.stamp(NOW + 1800))
        report = self.run_it(at=NOW + 3600)
        self.assertEqual((report.listed, report.stored), (1, 1))
        self.assertEqual(self.rows()[late.id].status, "stored")
        self.assertEqual(self.jo.immich_reconciled_until, NOW + 3600 - reconcile.MARGIN_SECONDS)

    def test_an_asset_leaving_the_window_between_pages_does_not_hide_another_one(self):
        self.world.fake.state.page_size = 1
        first, second, third = self.photo(), self.photo(), self.photo()

        def move_first(count):
            if count == 2:
                first.updated_at = reconcile.stamp(NOW + 30)

        self.world.fake.state.on_search = move_first
        self.run_it()
        self.assertEqual({asset_id for asset_id, row in self.rows().items() if row.status == "stored"}, {second.id, third.id})
        self.world.fake.state.on_search = None
        self.run_it(at=NOW + 3600)
        self.assertEqual(self.rows()[first.id].status, "stored")

    def test_listing_that_never_settles_fails_the_run(self):
        self.photo()
        calls = iter(range(100))

        def drifting(self, key, since, until):
            return iter([immich.asset_of(FakeAsset(id=f"x{next(calls)}", owner_id="u-jo", data=b"x").json())])

        with mock.patch.object(immich.Client, "search_updated", drifting), self.assertRaisesRegex(immich.ImmichFailed, "kept changing"):
            self.run_it()
        self.assertIsNone(self.jo.immich_reconciled_until)

    def test_nothing_is_ever_deleted_or_changed_in_immich(self):
        self.photo()
        before = len(self.world.fake.requests())
        self.run_it()
        methods = {(method, path) for method, path in self.world.fake.requests()[before:] if method != "GET"}
        self.assertEqual(methods, {("POST", "/api/search/metadata")})


class LivePhotoTests(ReconcileCase):
    def live(self):
        video = self.world.fake.add_asset(FakeAsset(id="vid", owner_id="u-jo", data=b"\x00\x00\x00\x14ftypqt  video-bytes", name="IMG_0001.MOV", kind="VIDEO",
                                                  visibility="hidden", metadata=()))
        still = self.photo(live_video_id="vid")
        return still, video

    def test_both_halves_are_stored_linked_and_queued(self):
        still, video = self.live()
        with mock.patch("darkroom.domain.immich_reconcile.index.index_file", wraps=reconcile.index.index_file):
            report = self.run_it(queue=reconcile.PixelFolderQueue())
        self.assertEqual((report.stored, report.queued, report.waiting), (2, 2, 0))
        rows = self.rows()
        self.assertEqual((rows[still.id].linked_asset_id, rows[video.id].linked_asset_id), (video.id, still.id))
        self.assertEqual((rows[still.id].kind, rows[video.id].kind), ("photo", "live_video"))
        self.assertEqual(len(self.copies()), 2)

    def test_a_hidden_video_without_its_still_waits_and_is_never_stored_alone(self):
        self.world.fake.add_asset(FakeAsset(id="vid", owner_id="u-jo", data=b"v", kind="VIDEO", visibility="hidden", metadata=()))
        report = self.run_it()
        self.assertEqual((report.stored, report.waiting), (0, 1))
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.rows()["vid"].status, "waiting")

    def test_a_still_whose_video_has_not_arrived_waits_and_is_stored_when_it_does(self):
        still = self.photo(live_video_id="vid")
        report = self.run_it()
        self.assertEqual((report.stored, report.waiting, self.rows()[still.id].status), (0, 1, "waiting"))
        self.assertEqual(self.stored(), [])
        self.world.fake.add_asset(FakeAsset(id="vid", owner_id="u-jo", data=b"\x00\x00\x00\x14ftypqt  later", name="IMG_0001.MOV", kind="VIDEO", visibility="hidden", metadata=()))
        later = self.run_it(at=NOW + 3600)
        self.assertEqual((later.stored, self.rows()[still.id].status, self.rows()["vid"].status), (2, "stored", "stored"))


class FailureTests(ReconcileCase):
    def test_a_download_that_does_not_match_its_checksum_writes_nothing_and_is_retried(self):
        asset = self.photo()
        good = asset.data
        asset.data = b"truncated"
        asset.checksum = __import__("base64").b64encode(bytes.fromhex(sha1(good))).decode()
        report = self.run_it()
        self.assertEqual((report.stored, report.failed, self.stored(), self.rows()[asset.id].status), (0, 1, [], "failed"))
        self.assertEqual(self.world.opened.execute(select(Event).where(Event.type == "immich_asset_failed")).scalars().one().detail, "ChecksumMismatch")
        asset.data = good
        retried = self.run_it(at=NOW + 3600)
        self.assertEqual((retried.stored, retried.failed, self.rows()[asset.id].status), (1, 0, "stored"))

    def test_an_unreachable_immich_alerts_once_until_a_good_run_and_keeps_the_window(self):
        self.photo()
        self.world.fake.mode("500")
        self.assertEqual(reconcile.reconcile_all(self.world.opened, self.settings, NOW), {})
        reconcile.reconcile_all(self.world.opened, self.settings, NOW + 1)
        key = f"immich:reconcile:{self.jo.id}"
        self.assertEqual(len(self.world.opened.execute(select(Alert).where(Alert.key == key)).scalars().all()), 1)
        self.assertEqual((self.jo.immich_reconciled_until, self.stored()), (None, []))
        self.world.fake.mode("ok")
        self.assertEqual(reconcile.reconcile_all(self.world.opened, self.settings, NOW + 2)["jo"].stored, 1)
        self.world.fake.mode("500")
        reconcile.reconcile_all(self.world.opened, self.settings, NOW + 3)
        self.assertEqual(len(self.world.opened.execute(select(Alert).where(Alert.key == key)).scalars().all()), 2)

    def test_a_key_of_another_immich_user_fails_the_run_alerts_and_keeps_the_window(self):
        self.photo()
        self.jo.immich_api_key_sealed = secretbox.seal("key-sam", SECRET)
        self.assertEqual(reconcile.reconcile_all(self.world.opened, self.settings, NOW), {})
        self.assertEqual((self.jo.immich_reconciled_until, self.stored()), (None, []))
        failure = self.world.opened.execute(select(Event).where(Event.type == "immich_reconcile_failed")).scalars().one()
        self.assertIn("different user", failure.detail)
        self.assertEqual(len(self.world.opened.execute(select(Alert).where(Alert.key == f"immich:reconcile:{self.jo.id}")).scalars().all()), 1)

    def test_a_refused_key_stops_the_run_and_keeps_the_window(self):
        self.photo()
        self.jo.immich_api_key_sealed = secretbox.seal("wrong", SECRET)
        with self.assertRaises(immich.ImmichFailed) as caught:
            self.run_it()
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual((self.jo.immich_reconciled_until, self.stored()), (None, []))


class PoisonAssetTests(ReconcileCase):
    def test_a_photo_claiming_billions_of_pixels_is_stored_without_being_decoded_and_the_run_finishes(self):
        bomb = self.world.fake.add_asset(FakeAsset(id="bomb", owner_id="u-jo", data=fixtures.pixel_bomb_png(), name="IMG_9000.PNG"))
        after = self.photo()
        report = self.run_it()
        self.assertEqual((report.stored, report.failed), (2, 0))
        self.assertEqual({self.rows()[bomb.id].status, self.rows()[after.id].status}, {"stored"})
        self.assertEqual(self.jo.immich_reconciled_until, NOW - reconcile.MARGIN_SECONDS)

    def test_a_photo_that_breaks_the_index_fails_alone_and_the_rest_are_stored(self):
        poison = self.photo()
        after = self.photo()
        real = media.fingerprint

        def fingerprint(path, tags):
            if path.read_bytes() == poison.data:
                raise MemoryError()
            return real(path, tags)

        with mock.patch.object(media, "fingerprint", fingerprint):
            report = self.run_it()
        self.assertEqual((report.stored, report.failed), (1, 1))
        self.assertEqual((self.rows()[poison.id].status, self.rows()[after.id].status), ("failed", "stored"))
        self.assertEqual(self.jo.immich_reconciled_until, NOW - reconcile.MARGIN_SECONDS)
        self.assertEqual(self.world.opened.execute(select(Event).where(Event.type == "immich_asset_failed")).scalars().one().detail, "CopyMismatch")
        self.assertEqual(self.world.opened.execute(select(IndexSkip)).scalars().all(), [])
        retried = self.run_it(at=NOW + 3600)
        self.assertEqual((retried.stored, retried.failed, self.rows()[poison.id].status), (1, 0, "stored"))

    def test_a_poisoned_photo_stays_set_aside_counted_and_unread_until_its_retry_is_due(self):
        poison = self.photo()
        after = self.photo()
        real = media.fingerprint
        reads = []

        def fingerprint(path, tags):
            if path.read_bytes() == poison.data:
                reads.append(path.name)
                raise OverflowError("a crafted value")
            return real(path, tags)

        with mock.patch.object(media, "fingerprint", fingerprint):
            report = self.run_it()
            self.assertEqual((report.stored, report.failed), (1, 1))
            self.assertEqual(index.skipped_by_person(self.world.opened), {self.jo.id: 1})
            again = self.run_it(at=NOW + 3600)
        self.assertEqual(len(reads), 1)
        self.assertEqual((again.stored, again.failed, self.rows()[poison.id].status, self.rows()[after.id].status), (0, 1, "failed", "stored"))
        self.assertEqual({event.detail for event in self.world.opened.execute(select(Event).where(Event.type == "immich_asset_failed")).scalars()}, {"SetAside"})
        (row,) = self.world.opened.execute(select(IndexSkip)).scalars().all()
        self.assertEqual(row.reason, index.UNUSABLE)
        stale = self.world.opened.execute(select(MediaItem).where(MediaItem.path == row.path)).scalars().all()
        self.assertEqual([item for item in stale if item.sha1 != index.UNKNOWN_SHA1], [])
        self.assertIsNone(self.world.opened.execute(select(MediaItem).where(MediaItem.sha1 == sha1(poison.data))).scalar_one_or_none())
        later = self.run_it(at=NOW + index.RETRY_SET_ASIDE_SECONDS + 7200)
        self.assertEqual((later.stored, later.failed, self.rows()[poison.id].status), (1, 0, "stored"))
        self.assertEqual(index.skipped_by_person(self.world.opened), {})


class OrderTests(ReconcileCase):
    def test_it_waits_for_the_takeout_the_legacy_import_and_a_readable_key(self):
        lee = World(self, people=("jo",), pixel_people=("jo",))
        lee_person = lee.opened.get(Person, lee.ids["jo"])
        lee_person.immich_api_key_sealed = secretbox.seal("key-jo", SECRET)
        settings = dataclasses.replace(lee.settings, secret_key=SECRET)
        with self.assertRaisesRegex(reconcile.NotReady, "Takeout"):
            reconcile.reconcile_person(lee.opened, settings, lee_person, NOW)
        self.jo.legacy_path = "/old"
        with self.assertRaisesRegex(reconcile.NotReady, "legacy"):
            self.run_it()
        self.world.opened.add(Event(time=1.0, type="legacy_imported", subject="jo"))
        self.assertEqual(self.run_it().failed, 0)
        self.jo.immich_api_key_sealed = None
        with self.assertRaisesRegex(reconcile.NotReady, "no Immich user"):
            self.run_it()

    def test_an_unreadable_key_is_a_visible_failure_not_a_silent_skip(self):
        self.photo()
        for sealed, settings, wanted in ((secretbox.seal("key-jo", "other-secret"), self.settings, "cannot be read"), (secretbox.seal("key-jo", SECRET), self.world.settings, "SECRET_KEY")):
            with self.subTest(wanted=wanted):
                self.jo.immich_api_key_sealed = sealed
                with self.assertRaisesRegex(immich.ImmichFailed, wanted):
                    self.run_it() if settings is self.settings else reconcile.reconcile_person(self.world.opened, settings, self.jo, NOW)
        self.jo.immich_api_key_sealed = secretbox.seal("key-jo", "other-secret")
        self.assertEqual(reconcile.reconcile_all(self.world.opened, self.settings, NOW), {})
        failure = self.world.opened.execute(select(Event).where(Event.type == "immich_reconcile_failed")).scalars().one()
        self.assertIn("cannot be read", failure.detail)
        self.assertEqual((len(self.world.opened.execute(select(Alert).where(Alert.key == f"immich:reconcile:{self.jo.id}")).scalars().all()), self.jo.immich_reconciled_until), (1, None))

    def test_a_pixel_added_after_the_first_offer_gets_the_earlier_uploads_and_the_first_is_untouched(self):
        self.photo()
        self.run_it(queue=reconcile.PixelFolderQueue())
        before = [(c.id, c.state, c.queued_at, c.always_send) for c in self.copies()]
        other = pixel_folder.Pixel(name="Kitchen Pixel", google_account_id=self.world.account, created=2.0)
        self.world.opened.add(other)
        self.world.opened.flush()
        self.world.opened.add(pixel_folder.PixelPerson(pixel_id=other.id, person_id=self.jo.id))
        later = self.run_it(at=NOW + 3600, queue=reconcile.PixelFolderQueue())
        self.assertEqual(later.queued, 1)
        by_pixel = {copy.pixel_id: copy for copy in self.copies()}
        self.assertEqual((by_pixel[other.id].state, by_pixel[other.id].always_send), ("queued", True))
        self.assertEqual([(c.id, c.state, c.queued_at, c.always_send) for c in self.copies() if c.pixel_id == self.world.pixel], before)
        self.assertEqual(self.run_it(at=NOW + 7200, queue=reconcile.PixelFolderQueue()).queued, 0)

    def test_names_keep_only_safe_characters(self):
        asset = immich.Asset("x", "u", None, "ab" * 20, "../../we irdé/na me.JPG", "IMAGE", "timeline", None, False, False)
        self.assertEqual(reconcile.copy_name(asset), "na_me-abababab.jpg")
        odd = dataclasses.replace(asset, file_name="")
        self.assertEqual(reconcile.copy_name(odd), "asset-abababab")


class TakeoutCopyTests(ReconcileCase):
    def google_only(self):
        return sorted(p.relative_to(self.world.root).as_posix() for p in (self.world.root / "jo" / "google-only").rglob("*") if p.is_file())

    def test_takeout_then_immich_stores_a_photo_once_and_the_phone_original_still_goes_to_the_pixel(self):
        takeout = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.path.like("jo/google-only/%IMG_2001%"))).scalar_one()
        path, data = takeout.path, (self.world.root / takeout.path).read_bytes()
        others = [name for name in self.google_only() if not name.startswith(path)]
        asset = self.world.fake.add_asset(FakeAsset(id="dup", owner_id="u-jo", data=data, name="IMG_9001.JPG"))
        report = self.run_it(queue=reconcile.PixelFolderQueue())
        self.assertEqual((report.stored, report.duplicates_removed), (1, 1))
        self.assertEqual(self.google_only(), others)
        name, sidecar = self.stored()
        self.assertEqual(((self.world.root / "jo/originals/immich" / name).read_bytes(), sidecar), (data, f"{name}.xmp"))
        carried = next(iter(exiftool.read([self.world.root / "jo/originals/immich" / sidecar]).values()))
        self.assertEqual((media.tag(carried, "Description"), media.tag(carried, "GPSLatitude") is not None), ("A made-up canal", True))
        self.assertEqual([(c.item_id, c.state, c.always_send) for c in self.copies()], [(self.rows()[asset.id].original_item_id, "queued", True)])
        before = (tree(self.world.root), len(self.copies()))
        again = self.run_it(at=NOW + 3600, queue=reconcile.PixelFolderQueue())
        export = self.world.opened.execute(select(Export)).scalars().one()
        export.status, export.stage = "importing", "unpacked"
        self.world.opened.add(ImportRun(export_id=export.id, started=NOW + 3600, status="queued", stage="unpacked"))
        self.world.opened.commit()
        self.assertEqual(self.world.run_import(export, at=NOW + 3600).status, "done")
        self.assertEqual((again.stored, again.duplicates_removed), (0, 0))
        self.assertEqual((tree(self.world.root), len(self.copies())), before)


class SettleTests(ReconcileCase):
    def test_originals_stored_before_a_later_asset_fails_still_reach_the_pixel(self):
        first, second = self.photo(), self.photo()
        real = immich.Client.metadata_keys

        def failing(client, key, asset_id):
            if asset_id == second.id:
                raise immich.ImmichFailed("Immich answered 500", status=500)
            return real(client, key, asset_id)

        with mock.patch.object(immich.Client, "metadata_keys", failing), self.assertRaises(immich.ImmichFailed):
            self.run_it(queue=reconcile.PixelFolderQueue())
        self.assertEqual(self.rows()[first.id].status, "stored")
        self.assertEqual([copy.item_id for copy in self.copies()], [self.rows()[first.id].original_item_id])


class ProviderTests(unittest.TestCase):
    def test_checksums_and_assets_are_checked(self):
        digest = bytes(range(20))
        encoded = __import__("base64").b64encode(digest).decode()
        self.assertEqual(immich.sha1_of_checksum(encoded), digest.hex())
        for bad in ("***", __import__("base64").b64encode(b"short").decode()):
            with self.assertRaises(immich.ImmichFailed):
                immich.sha1_of_checksum(bad)
        for bad in (None, {}, {"id": "a", "ownerId": "u", "type": "IMAGE"}):
            with self.assertRaises(immich.ImmichFailed):
                immich.asset_of(bad)
        for bad in ("", "a/b", "a?b"):
            with self.assertRaises(immich.ImmichFailed):
                immich.asset_path(bad)

    def test_search_and_metadata_replies_are_checked(self):
        client = immich.Client("http://127.0.0.1:1", "k")
        for reply in ({}, {"assets": {"items": "no"}}, []):
            with mock.patch.object(immich.Client, "call", return_value=reply), self.assertRaises(immich.ImmichFailed):
                list(client.search_updated("k", None, "2026-01-01T00:00:00.000Z"))
        with mock.patch.object(immich.Client, "call", return_value={}), self.assertRaises(immich.ImmichFailed):
            client.metadata_keys("k", "a")


if __name__ == "__main__":
    unittest.main()
