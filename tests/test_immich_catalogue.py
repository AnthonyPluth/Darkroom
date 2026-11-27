import base64
import dataclasses
import unittest
import types
import uuid
from unittest import mock

from sqlalchemy import select

from darkroom.domain import dedupe, immich_catalogue, legacy, pixel_folder, pixel_source
from darkroom.domain import immich_reconcile as reconcile
from darkroom.domain.ingest import place
from darkroom.providers import exiftool, immich
from darkroom.storage.models import (Alert, DedupeDecision, DuplicateRemoval, Event, Export, ManagedAssetOrigin, ManagedAssetVersion, ManagedPixelSource, MediaItem, Pixel,
                                     PixelPerson)
from tests.fakes.immich import FakeAsset
from tests.import_world import NOW, tree
from tests.privacy import no_leaks
from tests.test_immich_reconcile import SECRET, ReconcileCase, sha1
from tests.test_legacy import LegacyWorld


class CatalogueCase(ReconcileCase):
    def setUp(self):
        super().setUp()
        self.settings = dataclasses.replace(self.settings, pixel_immich_source=True)
        self.set_state("on")

    def set_state(self, state):
        self.jo.managed_imports, self.jo.managed_imports_since = state, NOW
        self.world.opened.commit()

    def asset(self, data=None, **fields):
        found = self.photo(id=str(uuid.uuid4()), **fields)
        if data is not None:
            found.data = data
        return found

    def cataloged(self):
        return list(self.world.opened.execute(select(MediaItem).where(MediaItem.path.like("jo/immich-catalogue/%")).order_by(MediaItem.id)).scalars())

    def versions(self):
        return list(self.world.opened.execute(select(ManagedAssetVersion).order_by(ManagedAssetVersion.id)).scalars())


class CataloguingTests(CatalogueCase):
    def test_a_phone_upload_is_catalogued_and_queued_as_a_managed_pixel_copy_and_nothing_is_written_to_disk(self):
        asset = self.asset()
        before = tree(self.world.root)
        report = self.run_it()
        self.assertEqual((report.paused, report.listed, report.cataloged, report.stored, report.queued, report.failed), (False, 1, 1, 0, 1, 0))
        self.assertEqual(tree(self.world.root), before)
        (item,) = self.cataloged()
        self.assertEqual((item.source, item.path, item.sha1, item.bytes), ("phone", f"jo/immich-catalogue/{asset.id}.jpg", sha1(asset.data), len(asset.data)))
        row = self.rows()[asset.id]
        self.assertEqual((row.status, row.original_item_id, row.queued_at), ("cataloged", item.id, NOW))
        (version,) = self.versions()
        self.assertEqual((version.asset_id, version.owner_id, version.sha1, version.bytes, version.state, version.library_id),
                         (asset.id, "u-jo", item.sha1, item.bytes, pixel_source.VERIFIED, None))
        (origin,) = self.world.opened.execute(select(ManagedAssetOrigin)).scalars()
        self.assertEqual((origin.kind, origin.version_id, origin.receipt_id, origin.intent_id), ("phone", version.id, None, None))
        (copy,) = [found for found in self.copies() if found.item_id == item.id]
        self.assertEqual((copy.state, copy.always_send, copy.sha1, copy.bytes), ("queued", True, item.sha1, item.bytes))
        self.assertTrue(pixel_source.converted(self.world.opened, copy.id))
        (source,) = self.world.opened.execute(select(ManagedPixelSource)).scalars()
        self.assertEqual((source.asset_id, source.version_id, source.item_id), (asset.id, version.id, item.id))
        self.assertEqual(pixel_source.copy_name(self.world.photos, "jo", item.path, asset.id), f"immich/{asset.id}.jpg")

    def test_the_catalogue_item_carries_the_fingerprints_a_re_encoded_takeout_copy_is_matched_by_and_no_scratch_file_is_left(self):
        self.asset()
        self.run_it()
        (item,) = self.cataloged()
        self.assertTrue(all(value is not None for value in (item.captured_utc, item.captured_local, item.offset_minutes, item.width, item.height, item.phash, item.dhash)))
        self.assertEqual((item.time_source, item.mtime), ("exif_offset", None))
        self.assertIn(item.id, [shot.id for shot in dedupe.PersonIndex.load(self.world.opened, self.world.account).phone])
        self.assertEqual(list(self.settings.data_dir.glob("catalogue-*")), [])

    def test_a_file_exiftool_cannot_use_is_catalogued_by_its_bytes_alone(self):
        asset = self.asset(data=b"not a photo at all")
        report = self.run_it()
        (item,) = self.cataloged()
        self.assertEqual((report.cataloged, item.sha1, item.bytes, item.captured_utc, item.phash), (1, sha1(asset.data), len(asset.data), None, None))

    def test_an_original_whose_metadata_cannot_be_read_now_is_not_catalogued_and_is_retried(self):
        asset = self.asset()

        def unreadable(paths):
            raise exiftool.Unreadable("exiftool is not answering")

        report = reconcile.reconcile_person(self.world.opened, self.settings, self.jo, NOW, read=unreadable)
        self.assertEqual((report.cataloged, report.failed, self.cataloged(), self.rows()[asset.id].status), (0, 1, [], "failed"))
        self.assertEqual(list(self.settings.data_dir.glob("catalogue-*")), [])
        self.assertEqual(self.run_it(at=NOW + 3600).cataloged, 1)

    def test_the_stored_window_and_the_event_are_kept_and_the_event_says_what_was_catalogued(self):
        self.asset()
        self.run_it()
        self.assertEqual(self.jo.immich_reconciled_until, NOW - reconcile.MARGIN_SECONDS)
        detail = self.world.opened.execute(select(Event.detail).where(Event.type == "immich_reconciled")).scalar_one()
        self.assertEqual(detail, "1 listed, 1 cataloged, 0 already stored, 0 ignored, 0 waiting, 0 failed, 1 queued for the Pixels")

    def test_a_second_run_changes_nothing(self):
        self.asset()
        self.run_it()
        before = (tree(self.world.root), len(self.cataloged()), len(self.versions()), [(c.id, c.state, c.queued_at) for c in self.copies()], len(self.rows()))
        self.jo.immich_reconciled_until = None
        again = self.run_it(at=NOW + 3600)
        self.assertEqual((again.cataloged, again.already_seen, again.queued), (0, 1, 0))
        self.assertEqual((tree(self.world.root), len(self.cataloged()), len(self.versions()), [(c.id, c.state, c.queued_at) for c in self.copies()], len(self.rows())), before)

    def test_a_stopped_person_is_catalogued_the_same_way(self):
        self.set_state("stopped")
        self.asset()
        self.assertEqual(self.run_it().cataloged, 1)

    def test_only_this_persons_phone_uploads_count(self):
        self.asset(metadata=())
        self.asset(library_id="lib-jo")
        self.asset(trashed=True)
        self.world.fake.add_asset(FakeAsset(id=str(uuid.uuid4()), owner_id="u-sam", data=b"x"))
        report = self.run_it()
        self.assertEqual((report.cataloged, report.ignored, len(self.cataloged())), (0, 3, 0))

    def test_a_live_photo_catalogues_both_halves_linked_and_queues_both(self):
        video_id = str(uuid.uuid4())
        video = self.world.fake.add_asset(FakeAsset(id=video_id, owner_id="u-jo", data=b"\x00\x00\x00\x14ftypqt  video-bytes", name="IMG_0001.MOV", kind="VIDEO",
                                                  visibility="hidden", metadata=()))
        still = self.asset(live_video_id=video_id)
        report = self.run_it()
        self.assertEqual((report.cataloged, report.queued, report.waiting), (2, 2, 0))
        rows = self.rows()
        self.assertEqual((rows[still.id].status, rows[video.id].status, rows[still.id].linked_asset_id, rows[video.id].linked_asset_id), ("cataloged", "cataloged", video_id, still.id))
        self.assertEqual(sorted(item.path for item in self.cataloged()), sorted([f"jo/immich-catalogue/{still.id}.jpg", f"jo/immich-catalogue/{video_id}.mov"]))
        by_asset = {version.asset_id: version for version in self.versions()}
        self.assertEqual((by_asset[still.id].linked_video_asset_id, by_asset[still.id].kind, by_asset[video_id].kind), (video_id, "IMAGE", "VIDEO"))
        self.assertEqual(len([copy for copy in self.copies() if pixel_source.converted(self.world.opened, copy.id)]), 2)

    def test_a_still_whose_video_has_not_arrived_waits_and_is_catalogued_when_it_does(self):
        video_id = str(uuid.uuid4())
        still = self.asset(live_video_id=video_id)
        first = self.run_it()
        self.assertEqual((first.cataloged, first.waiting, self.rows()[still.id].status), (0, 1, "waiting"))
        self.world.fake.add_asset(FakeAsset(id=video_id, owner_id="u-jo", data=b"\x00\x00\x00\x14ftypqt  video-bytes", name="IMG_0001.MOV", kind="VIDEO",
                                          visibility="hidden", metadata=()))
        later = self.run_it(at=NOW + 3600)
        self.assertEqual((later.cataloged, later.waiting, self.rows()[still.id].status), (2, 0, "cataloged"))

    def test_a_download_that_does_not_match_its_checksum_catalogues_nothing_and_is_retried(self):
        asset = self.asset()
        good = asset.data
        asset.data = b"truncated"
        asset.checksum = base64.b64encode(bytes.fromhex(sha1(good))).decode()
        report = self.run_it()
        self.assertEqual((report.cataloged, report.failed, self.rows()[asset.id].status, self.cataloged(), self.versions()), (0, 1, "failed", [], []))
        self.assertEqual(self.world.opened.execute(select(Event.detail).where(Event.type == "immich_asset_failed")).scalar_one(), "ChecksumMismatch")
        asset.data = good
        retried = self.run_it(at=NOW + 3600)
        self.assertEqual((retried.cataloged, retried.failed, self.rows()[asset.id].status), (1, 0, "cataloged"))

    def test_a_photo_already_in_originals_is_marked_stored_and_not_catalogued_again(self):
        asset = self.asset()
        data = asset.data
        target = self.world.root / "jo" / "originals" / "immich"
        target.mkdir(parents=True)
        (target / "earlier.jpg").write_bytes(data)
        self.world.index_all()
        report = self.run_it()
        self.assertEqual((report.cataloged, report.stored, report.already_there, self.cataloged(), self.versions()), (0, 1, 1, [], []))
        self.assertEqual(self.rows()[asset.id].status, "stored")

    def test_the_catalogue_is_not_a_phone_original_for_the_pixel_folders_own_queue(self):
        self.asset()
        self.run_it()
        (item,) = self.cataloged()
        with self.assertRaises(pixel_folder.NotInOriginals):
            pixel_folder.in_originals(self.world.opened, item)

    def test_a_catalogued_photo_never_makes_a_matched_takeout_copy_lose_its_file(self):
        takeout = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.path.like("jo/google-only/%IMG_2001%"))).scalar_one()
        self.world.fake.add_asset(FakeAsset(id=str(uuid.uuid4()), owner_id="u-jo", data=(self.world.root / takeout.path).read_bytes(), name="IMG_9001.JPG"))
        before = tree(self.world.root)
        self.assertEqual(self.run_it().cataloged, 1)
        self.assertEqual((tree(self.world.root), self.world.opened.execute(select(DuplicateRemoval)).all()), (before, []))

    def test_nothing_private_reaches_a_log_event_alert_or_the_database(self):
        self.asset(name="CANARY_PHOTO_NAME.JPG")
        with no_leaks(self, "CANARY_PHOTO_NAME", SECRET, self.world.fake.url, database=self.world.engine):
            self.run_it()


class RefusalTests(CatalogueCase):
    def failures(self):
        return list(self.world.opened.execute(select(Event.detail).where(Event.type == "immich_asset_failed")).scalars())

    def test_an_id_that_is_not_safe_in_a_path_is_never_catalogued(self):
        asset = immich.Asset("a/../b", "u-jo", None, "a" * 40, "x.jpg", "IMAGE", "timeline", None, False, False)
        with self.assertRaises(reconcile.CopyMismatch):
            immich_catalogue.CatalogueRun.path_of(types.SimpleNamespace(person=types.SimpleNamespace(slug="jo")), asset)

    def test_a_catalogue_item_holding_other_bytes_fails_the_asset_and_is_left_as_it_is(self):
        asset = self.asset()
        self.world.opened.add(MediaItem(person_id=self.jo.id, source="phone", path=f"jo/immich-catalogue/{asset.id}.jpg", sha1="b" * 40, bytes=3, indexed_at=NOW))
        self.world.opened.commit()
        report = self.run_it()
        self.assertEqual((report.cataloged, report.failed, self.rows()[asset.id].status, self.failures(), self.versions()), (0, 1, "failed", ["CopyMismatch"], []))
        (item,) = self.cataloged()
        self.assertEqual((item.sha1, item.bytes), ("b" * 40, 3))

    def test_an_asset_that_is_neither_a_photo_nor_a_video_is_not_catalogued(self):
        asset = self.asset(kind="AUDIO")
        report = self.run_it()
        self.assertEqual((report.cataloged, report.failed, self.failures(), self.cataloged()), (0, 1, ["CopyMismatch"], []))
        self.assertEqual(self.rows()[asset.id].status, "failed")


class PixelQueueTests(CatalogueCase):
    def test_with_pixel_copies_from_immich_off_the_photo_is_catalogued_and_waits_for_a_queue_until_they_are_on(self):
        self.settings = dataclasses.replace(self.settings, pixel_immich_source=False)
        asset = self.asset()
        report = self.run_it()
        self.assertEqual((report.cataloged, report.queued, self.copies(), self.rows()[asset.id].queued_at), (1, 0, [], None))
        self.settings = dataclasses.replace(self.settings, pixel_immich_source=True)
        later = self.run_it(at=NOW + 3600)
        self.assertEqual((later.cataloged, later.queued, len(self.copies()), self.rows()[asset.id].queued_at), (0, 1, 1, NOW + 3600))

    def test_a_pixel_added_later_gets_the_earlier_uploads_and_the_first_is_untouched(self):
        self.asset()
        self.run_it()
        before = [(c.id, c.state, c.queued_at, c.always_send) for c in self.copies()]
        other = Pixel(name="Kitchen Pixel", google_account_id=self.world.account, created=2.0)
        self.world.opened.add(other)
        self.world.opened.flush()
        self.world.opened.add(PixelPerson(pixel_id=other.id, person_id=self.jo.id))
        later = self.run_it(at=NOW + 3600)
        self.assertEqual(later.queued, 1)
        by_pixel = {copy.pixel_id: copy for copy in self.copies()}
        self.assertEqual((by_pixel[other.id].state, by_pixel[other.id].always_send), ("queued", True))
        self.assertEqual([(c.id, c.state, c.queued_at, c.always_send) for c in self.copies() if c.pixel_id == self.world.pixel], before)
        self.assertEqual(self.run_it(at=NOW + 7200).queued, 0)

    def test_an_item_the_managed_queue_refuses_is_catalogued_and_left_unqueued(self):
        asset = self.asset()
        with mock.patch.object(pixel_folder, "queue_managed", side_effect=pixel_source.NotManaged("refused")):
            report = self.run_it()
        self.assertEqual((report.cataloged, report.queued, self.copies(), self.rows()[asset.id].queued_at), (1, 0, [], None))
        self.assertEqual(self.run_it(at=NOW + 3600).queued, 1)


class BackToOffTests(CatalogueCase):
    def off_again(self):
        self.set_state(None)

    def test_a_person_turned_off_gets_every_catalogued_original_written_by_the_next_classic_run_though_the_window_moved_on(self):
        asset = self.asset()
        self.run_it()
        self.off_again()
        report = self.run_it(at=NOW + 7200)
        self.assertEqual((report.listed, report.stored, report.copied, self.rows()[asset.id].status), (0, 1, 1, "stored"))
        (name,) = self.stored()
        self.assertEqual((self.world.root / "jo/originals/immich" / name).read_bytes(), asset.data)
        self.assertEqual(self.run_it(at=NOW + 10800).stored, 0)

    def test_a_catalogued_live_photo_gets_both_halves_written_when_the_person_is_turned_off(self):
        video_id = str(uuid.uuid4())
        self.world.fake.add_asset(FakeAsset(id=video_id, owner_id="u-jo", data=b"\x00\x00\x00\x14ftypqt  video-bytes", name="IMG_0001.MOV", kind="VIDEO",
                                          visibility="hidden", metadata=()))
        still = self.asset(live_video_id=video_id)
        self.run_it()
        self.off_again()
        report = self.run_it(at=NOW + 7200)
        self.assertEqual((report.stored, report.copied, len(self.stored())), (2, 2, 2))
        self.assertEqual({row.status for row in self.rows().values()}, {"stored"})
        self.assertIn(still.id, self.rows())

    def test_a_catalogue_item_is_not_a_phone_original_for_an_old_way_takeout_of_an_off_person(self):
        self.asset()
        self.run_it()
        (item,) = self.cataloged()
        account = self.world.account
        self.assertIn(item.id, dedupe.PersonIndex.load(self.world.opened, account).by_id)
        self.off_again()
        self.assertNotIn(item.id, dedupe.PersonIndex.load(self.world.opened, account).by_id)

    def test_an_original_for_a_matched_catalogue_item_is_never_looked_for_in_the_folder(self):
        self.asset()
        self.run_it()
        (item,) = self.cataloged()
        export = self.world.opened.execute(select(Export)).scalars().first()
        decision = DedupeDecision(export_id=export.id, account_id=self.world.account, source_path="catalogue-match.jpg", tier=1, reason="same bytes", decided_by="auto",
                                  decided_at=NOW, matched_item_id=item.id)
        self.world.opened.add(decision)
        self.world.opened.commit()
        self.set_state("pausing")
        self.assertIsNone(place.matched_original(self.world.opened, self.world.photos, decision))
        self.set_state(None)
        self.assertIsNone(place.matched_original(self.world.opened, self.world.photos, decision))


class LegacyCatalogueTests(unittest.TestCase):
    def test_a_legacy_file_is_copied_into_originals_though_a_catalogue_item_has_its_bytes(self):
        home = LegacyWorld(self)
        world = home.world
        home.imported()
        fresh = world.scratch_jpeg("fresh.jpg", taken="2015:01:01 10:00:00", offset="+00:00", colour=(1, 2, 3))
        home.put("fresh.jpg", fresh)
        home.configure()
        world.opened.add(MediaItem(person_id=home.jo.id, source="phone", path="jo/immich-catalogue/catalogued.jpg", sha1=sha1(fresh), bytes=len(fresh), indexed_at=NOW))
        world.opened.commit()
        home.run()
        copies = list((world.root / "jo" / "originals" / legacy.LEGACY_FOLDER).iterdir())
        self.assertEqual([path.read_bytes() for path in copies], [fresh])


class ModeTests(CatalogueCase):
    def test_the_catalogue_run_is_the_one_the_modes_name(self):
        self.assertEqual(immich_catalogue.CatalogueRun.done, ("stored", "cataloged"))
        self.assertEqual(reconcile.Run.done, ("stored",))

    def test_a_person_being_switched_is_neither_listed_nor_catalogued(self):
        self.asset()
        self.set_state("pausing")
        requests, alerts = len(self.world.fake.requests()), self.world.opened.execute(select(Alert.key)).all()
        report = self.run_it()
        self.assertEqual((report.paused, report.cataloged, len(self.world.fake.requests()), self.cataloged()), (True, 0, requests, []))
        self.assertEqual(self.world.opened.execute(select(Alert.key)).all(), alerts)
