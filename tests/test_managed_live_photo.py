import dataclasses
import unittest
import uuid
from unittest import mock

from sqlalchemy import select, update

from darkroom.domain import managed_compound, managed_ingest, managed_metadata, managed_upload, same_shot
from darkroom.domain.ingest import finish_export, part_reader
from darkroom.storage.models import ManagedCompound, ManagedCompoundMember, ManagedImportReceipt, TakeoutEntry
from tests.fakes.immich import FakeAsset, extract
from tests.import_world import NOW
from tests.privacy import no_leaks
from tests.test_managed_ingest import ALLOWED, KEY, OWNER, PHOTOS, SECRET, ManagedWorld
from tests.test_part_ingest import Crash, short


PLACE = (40.7, -74.0)


def live(builder):
    builder.live_photo("IMG_3001", part=0, video_gps=PLACE)


def borrowed(builder):
    builder.live_photo("IMG_3001", part=0, video_sidecar=False)


def with_single(builder):
    builder.live_photo("IMG_3001", part=0, video_gps=PLACE)
    builder.photo("IMG_3002.JPG", part=0)


class LiveWorld(ManagedWorld):
    def run_live(self, build=live, parts=1):
        export = self.create(build, parts=parts)
        self.ingest_all(export, range(1, parts + 1))
        with mock.patch.object(managed_ingest.time, "sleep"):
            self.finish(export)
        return export

    def compound(self):
        self.opened.expire_all()
        return self.opened.execute(select(ManagedCompound)).scalar_one_or_none()

    def members(self):
        self.opened.expire_all()
        names = {f"takeout-entry:{row.id}": short(row.name).removeprefix(PHOTOS) for row in self.opened.execute(select(TakeoutEntry)).scalars()}
        return {names[row.resource_key]: row.role for row in self.opened.execute(select(ManagedCompoundMember)).scalars()}

    def uploads(self):
        return self.fake.requests().count(("POST", "/api/assets"))

    def asset_named(self, name):
        return next(asset for asset in self.fake.state.assets if asset.name == name)

    def immich_copy(self, export, name, **fields):
        row = self.opened.execute(select(TakeoutEntry).where(TakeoutEntry.name.like(f"%{name}"))).scalars().one()
        data = self.bytes_of(export, name) if row.part_number == 1 else None
        kind = "VIDEO" if name.endswith(".MOV") else "IMAGE"
        asset = FakeAsset(str(uuid.uuid4()), OWNER, data, name=name, kind=kind, exif=extract(data, None), **fields)
        self.fake.add_asset(asset)
        return asset


class FinishTests(unittest.TestCase):
    def test_a_takeout_live_photo_is_uploaded_video_first_linked_hidden_and_committed_as_one_compound(self):
        world = LiveWorld(self)
        with no_leaks(self, KEY, SECRET, "IMG_3001.HEIC", "IMG_3001.MOV", world.fake.url):
            export = world.run_live(with_single)
        self.assertEqual(world.status(export), ("imported", "done"))
        committed = ("committed", "created", None)
        self.assertEqual(world.receipts(), {"IMG_3001.HEIC": committed, "IMG_3001.MOV": committed, "IMG_3002.JPG": committed})
        self.assertEqual(world.members(), {"IMG_3001.HEIC": "live_still", "IMG_3001.MOV": "live_video"})
        compound = world.compound()
        self.assertEqual((compound.hold, compound.committed_at), (None, NOW))
        still, video = world.asset_named("IMG_3001.HEIC"), world.asset_named("IMG_3001.MOV")
        self.assertEqual((still.live_video_id, video.visibility, video.kind), (video.id, "hidden", "VIDEO"))
        uploaded = [asset.name for asset in world.fake.state.assets]
        self.assertLess(uploaded.index("IMG_3001.MOV"), uploaded.index("IMG_3001.HEIC"))
        self.assertEqual(world.uploads(), 3)
        self.assertEqual([f"{m} {p}" for m, p in world.fake.requests() if not ALLOWED.fullmatch(f"{m} {p}")], [])
        self.assertEqual(world.google_only(), set())
        self.assertIsNone(world.alert("managed:held:1"))
        self.assertIsNone(world.alert("managed:pending:1"))

    def test_a_video_with_no_sidecar_of_its_own_is_sent_with_the_stills_place_and_a_borrowed_sidecar_never_corrects_it(self):
        world = LiveWorld(self)
        export = world.run_live(borrowed)
        self.assertEqual(world.status(export), ("imported", "done"))
        video = world.asset_named("IMG_3001.MOV")
        still = world.asset_named("IMG_3001.HEIC")
        self.assertEqual((video.exif["latitude"], video.exif["longitude"]), (still.exif["latitude"], still.exif["longitude"]))
        row = world.opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.state == "committed")).scalars().first()
        self.assertEqual(world.compound().committed_at, NOW)
        intended = [managed_metadata.intended_of(each.expected_metadata) for each in world.opened.execute(select(ManagedImportReceipt)).scalars()]
        self.assertEqual(sorted(found.role for found in intended), ["live_still", "live_video"])
        self.assertIsNotNone(row)

    def test_both_halves_wait_together_when_either_is_not_wanted_and_nothing_is_sent_for_them(self):
        for half in ("IMG_3001.HEIC", "IMG_3001.MOV"):
            with self.subTest(half):
                world = LiveWorld(self)
                real = managed_ingest.wanted

                def only_one(opened, export, root, real=real, half=half):
                    return real(opened, export, root) and not root.name.endswith(half)
                with mock.patch.object(managed_ingest, "wanted", only_one):
                    world.run_live(with_single)
                self.assertEqual(sorted(world.receipts()), ["IMG_3002.JPG"])
                self.assertEqual((world.uploads(), world.compound()), (1, None))
                self.assertIn("1 item of export 1", world.alert("managed:held:1").message)

    def test_a_pair_the_grouping_refuses_is_held_and_nothing_of_it_is_sent(self):
        world = LiveWorld(self)

        def build(builder):
            builder.live_photo("IMG_3001", part=0, video_gps=PLACE)
            builder.video("IMG_3001.MP4", part=0)
        world.run_live(build)
        self.assertEqual((world.receipts(), world.uploads(), world.compound()), ({}, 0, None))
        self.assertIn("3 items of export 1 were not uploaded", world.alert("managed:held:1").message)

    def test_a_still_that_carries_its_own_motion_video_holds_the_pair_and_nothing_is_frozen(self):
        world = LiveWorld(self)
        with mock.patch.object(managed_compound, "embedded_motion", return_value=True):
            world.run_live()
        self.assertEqual((world.receipts(), world.uploads(), world.compound()), ({}, 0, None))
        self.assertIn("2 items of export 1", world.alert("managed:held:1").message)

    def test_a_half_that_cannot_go_as_it_is_freezes_the_compound_held_and_nothing_is_sent_for_either(self):
        world = LiveWorld(self)
        real = managed_metadata.intend

        def video_unknown(tags, google, kind, role="single", corrects=True):
            found = real(tags, google, kind, role, corrects)
            return dataclasses.replace(found, hold="unknown_offset") if role == "live_video" else found
        with mock.patch.object(managed_metadata, "intend", video_unknown):
            world.run_live()
        self.assertEqual((world.compound().hold, world.uploads()), ("unknown_offset", 0))
        self.assertEqual(sorted(state for state, _, _ in world.receipts().values()), ["prepared", "prepared"])
        self.assertIn("2 items of export 1", world.alert("managed:held:1").message)

    def test_a_pair_that_reaches_the_same_chunk_is_never_split_across_two(self):
        rows = [TakeoutEntry(id=number, bytes=5) for number in range(1, 5)]
        groups = [(rows[0],), (rows[1], rows[2]), (rows[3],)]

        def chunked(files, size):
            return [[[row.id for row in group] for group in chunk] for chunk in part_reader.grouped_batches_of(groups, files, size)]
        self.assertEqual(chunked(3, 100), [[[1], [2, 3]], [[4]]])
        self.assertEqual(chunked(100, 8), [[[1]], [[2, 3]], [[4]]])
        self.assertEqual(chunked(100, 100), [[[1], [2, 3], [4]]])
        self.assertEqual(list(part_reader.grouped_batches_of([], 3, 8)), [])


class ResumeTests(unittest.TestCase):
    def test_an_interruption_between_the_halves_resumes_with_only_the_still_and_one_video_in_immich(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        sent = []

        def still_fails(method, path, slug):
            if (method, path) == ("POST", "/api/assets"):
                sent.append(path)
                return 503 if len(sent) > 1 else None
            return None
        world.fake.state.intercept = still_fails
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual(world.status(export), ("downloading", "placed"))
        self.assertEqual(sorted(world.receipts().values()), [("metadata_verified", "created", None), ("outcome_unknown", None, None)])
        self.assertEqual([asset.name for asset in world.fake.state.assets], ["IMG_3001.MOV"])
        self.assertIn("2 uploads of export 1", world.alert("managed:pending:1").message)
        world.fake.state.intercept = None
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export, at=NOW + 2 * managed_upload.LEASE_SECONDS)
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertEqual(sorted(asset.name for asset in world.fake.state.assets), ["IMG_3001.HEIC", "IMG_3001.MOV"])
        self.assertEqual(world.compound().committed_at, NOW + 2 * managed_upload.LEASE_SECONDS)
        self.assertEqual(world.asset_named("IMG_3001.MOV").visibility, "hidden")

    def test_a_lost_answer_for_the_video_and_then_for_the_still_is_found_by_checksum_and_never_sent_twice(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        world.fake.state.lose_uploads = 1
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual((world.status(export), world.uploads()), (("imported", "done"), 2))
        self.assertEqual(sorted(asset.name for asset in world.fake.state.assets), ["IMG_3001.HEIC", "IMG_3001.MOV"])
        self.assertEqual(world.asset_named("IMG_3001.HEIC").live_video_id, world.asset_named("IMG_3001.MOV").id)

        twin = LiveWorld(self)
        export = twin.create(live, parts=1)
        twin.ingest_all(export, (1,))
        counted = []

        def lose_still(method, path, slug):
            if (method, path) == ("POST", "/api/assets"):
                counted.append(path)
                if len(counted) == 2:
                    twin.fake.state.lose_uploads = 1
            return None
        twin.fake.state.intercept = lose_still
        with mock.patch.object(managed_ingest.time, "sleep"):
            twin.finish(export)
        self.assertEqual((twin.status(export), twin.uploads()), (("imported", "done"), 2))
        self.assertEqual(sorted(asset.name for asset in twin.fake.state.assets), ["IMG_3001.HEIC", "IMG_3001.MOV"])
        self.assertEqual(twin.compound().committed_at, NOW)

    def test_a_crash_after_the_freeze_resumes_to_the_uninterrupted_result(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        export_id = export.id
        real, crashed = managed_upload.returned, []

        def crash_once(*args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise Crash()
            return real(*args, **kwargs)
        with mock.patch.object(managed_upload, "returned", crash_once), mock.patch.object(managed_ingest.time, "sleep"), self.assertRaises(Crash):
            world.finish(export)
        world.reopen()
        self.assertEqual(world.count(ManagedCompound), 1)
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(world.opened.get(finish_export.Export, export_id), at=NOW + 2 * managed_upload.LEASE_SECONDS)
        self.assertEqual(world.status(world.opened.get(finish_export.Export, export_id)), ("imported", "done"))
        self.assertEqual(sorted(asset.name for asset in world.fake.state.assets), ["IMG_3001.HEIC", "IMG_3001.MOV"])
        self.assertEqual(world.count(ManagedCompound), 1)

    def test_a_pair_that_is_no_longer_grouped_is_never_sent_as_two_singles(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        world.fake.state.intercept = lambda method, path, slug: 503 if (method, path) == ("POST", "/api/assets") else None
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        before = world.uploads()
        world.fake.state.intercept = None
        ungrouped = managed_compound.Grouping((), {})
        with mock.patch.object(managed_compound, "groups", return_value=ungrouped), mock.patch.object(finish_export, "live_roots", return_value=set()):
            world.finish(export, at=NOW + 2 * managed_upload.LEASE_SECONDS)
        self.assertEqual(world.uploads(), before)
        self.assertEqual(world.count(ManagedCompound), 1)
        self.assertIn("2 items of export 1", world.alert("managed:held:1").message)


class PreflightTests(unittest.TestCase):
    def prepared(self, build=live):
        world = LiveWorld(self)
        export = world.create(build, parts=1)
        world.ingest_all(export, (1,))
        return world, export

    def settle(self, world, export):
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)

    def held_untouched(self, world, export, reason):
        self.settle(world, export)
        self.assertEqual((world.compound().hold, world.uploads(), world.status(export)), (reason, 0, ("imported", "done")))
        self.assertEqual(sorted(state for state, _, _ in world.receipts().values()), ["prepared", "prepared"])
        self.assertIn("2 items of export 1", world.alert("managed:held:1").message)
        self.assertEqual(world.count(ManagedCompound), 1)

    def test_a_visible_standalone_duplicate_video_is_a_conflict_and_nothing_is_sent(self):
        world, export = self.prepared()
        world.immich_copy(export, "IMG_3001.MOV")
        self.held_untouched(world, export, "compound_conflict")
        self.assertEqual(world.fake.state.assets[0].visibility, "timeline")

    def test_a_duplicate_still_linked_to_another_video_or_to_none_is_a_conflict(self):
        for linked in (True, False):
            with self.subTest(linked=linked):
                world, export = self.prepared()
                other = world.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, b"another video", name="OTHER.MOV", kind="VIDEO", visibility="hidden",
                                                       exif={"dateTimeOriginal": None}))
                world.immich_copy(export, "IMG_3001.HEIC", live_video_id=other.id if linked else None)
                self.held_untouched(world, export, "compound_conflict")

    def test_a_trashed_duplicate_of_either_half_is_a_conflict(self):
        for name in ("IMG_3001.MOV", "IMG_3001.HEIC"):
            with self.subTest(name):
                world, export = self.prepared()
                world.immich_copy(export, name, trashed=True)
                self.held_untouched(world, export, "compound_conflict")

    def test_a_checksum_only_another_user_owns_is_not_a_conflict(self):
        world, export = self.prepared()
        data = world.bytes_of(export, "IMG_3001.MOV")
        world.fake.add_asset(FakeAsset(str(uuid.uuid4()), "22222222-2222-4222-8222-222222222222", data, name="IMG_3001.MOV", kind="VIDEO", exif=extract(data, None)))
        self.settle(world, export)
        self.assertEqual((world.compound().committed_at, world.status(export)), (NOW, ("imported", "done")))

    def test_a_whole_live_photo_already_in_immich_is_not_a_conflict_and_is_never_uploaded_again(self):
        world, export = self.prepared()
        video = world.immich_copy(export, "IMG_3001.MOV", visibility="hidden")
        still = world.immich_copy(export, "IMG_3001.HEIC", live_video_id=video.id)
        self.settle(world, export)
        self.assertEqual((world.compound().hold, world.uploads(), len(world.fake.state.assets)), (None, 1, 2))
        self.assertEqual(world.receipts()["IMG_3001.MOV"], ("bytes_verified", "duplicate", "not_enriched"))
        self.assertEqual((still.live_video_id, video.visibility), (video.id, "hidden"))
        self.assertEqual(world.status(export), ("imported", "done"))

    def test_an_immich_that_cannot_answer_the_check_leaves_the_pair_pending_with_nothing_sent(self):
        world, export = self.prepared()
        world.fake.state.intercept = lambda method, path, slug: 503 if (method, path) == ("POST", "/api/assets/bulk-upload-check") else None
        self.settle(world, export)
        self.assertEqual((world.uploads(), world.status(export), world.compound().hold), (0, ("downloading", "placed"), None))
        self.assertIn("2 uploads of export 1", world.alert("managed:pending:1").message)


class SequenceTests(unittest.TestCase):
    def test_a_video_trashed_before_its_still_is_sent_holds_the_compound_unusable_and_the_still_is_never_sent(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        real = managed_metadata.complete_receipt
        calls = []

        def trash_after_video(*args, **kwargs):
            report = real(*args, **kwargs)
            if not calls:
                calls.append(True)
                world.asset_named("IMG_3001.MOV").trashed = True
            return report
        with mock.patch.object(managed_metadata, "complete_receipt", trash_after_video), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual((world.compound().hold, world.uploads(), world.status(export)), ("unusable", 1, ("imported", "done")))
        self.assertEqual([asset.name for asset in world.fake.state.assets], ["IMG_3001.MOV"])
        self.assertIn("2 items of export 1", world.alert("managed:held:1").message)
        finish_export.finish_ready(world.opened, world.settings, NOW + 10, world.tools)
        self.assertEqual(world.uploads(), 1)

    def test_a_video_changed_in_immich_before_its_still_is_sent_is_not_linked(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        real = managed_metadata.complete_receipt
        calls = []

        def swap_after_video(*args, **kwargs):
            report = real(*args, **kwargs)
            if not calls:
                calls.append(True)
                world.asset_named("IMG_3001.MOV").kind = "IMAGE"
            return report
        with mock.patch.object(managed_metadata, "complete_receipt", swap_after_video), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual((world.compound().hold, world.uploads()), ("unusable", 1))

    def test_a_still_returned_unlinked_is_pending_for_a_day_and_then_not_settled_and_the_export_completes(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        world.fake.state.keep_unlinked = True
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
            self.assertEqual(world.status(export), ("downloading", "placed"))
            self.assertEqual(world.receipts()["IMG_3001.HEIC"], ("bytes_verified", "created", "not_linked"))
            world.finish(export, at=NOW + managed_ingest.SETTLE_SECONDS - 1)
            self.assertEqual((world.status(export), world.receipts()["IMG_3001.HEIC"][2]), (("downloading", "placed"), "not_linked"))
            world.finish(export, at=NOW + managed_ingest.SETTLE_SECONDS)
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertEqual(world.receipts()["IMG_3001.HEIC"], ("bytes_verified", "created", managed_ingest.NOT_SETTLED))
        self.assertEqual(world.receipts()["IMG_3001.MOV"][0], "metadata_verified")
        self.assertIsNone(world.compound().committed_at)
        self.assertIn("2 items of export 1", world.alert("managed:held:1").message)

    def test_a_third_receipt_or_a_changed_member_stops_the_commit(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        world.fake.state.keep_unlinked = False
        real = managed_compound.freeze

        def altered(opened, intent_id, key, members, now, wanted):
            frozen = real(opened, intent_id, key, members, now, wanted)
            opened.execute(update(ManagedCompoundMember).where(ManagedCompoundMember.role == "live_video").values(source_sha1="c" * 40))
            opened.commit()
            return frozen
        with mock.patch.object(managed_compound, "freeze", altered), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual((world.compound().committed_at, world.status(export)), (None, ("downloading", "placed")))
        self.assertEqual(sorted(state for state, _, _ in world.receipts().values()), ["metadata_verified", "metadata_verified"])


class SameShotTests(unittest.TestCase):
    def test_a_different_byte_same_shot_of_the_still_holds_the_whole_compound_before_anything_is_sent(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))

        def held(opened, reader, intent, receipt_id, entry, name, now):
            return same_shot.make_hold(opened, intent, receipt_id, entry.decision_id, same_shot.Verdict((), True, False), now)
        with mock.patch.object(same_shot, "gate", held), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
            first = (world.uploads(), world.status(export))
        self.assertEqual(first, (0, ("imported", "done")))
        self.assertEqual(sorted(hold for _, _, hold in world.receipts().values() if hold), [same_shot.HOLD])
        self.assertEqual([asset.name for asset in world.fake.state.assets], [])
        with mock.patch.object(managed_ingest.time, "sleep"):
            finish_export.finish_ready(world.opened, world.settings, NOW + 10, world.tools)
        self.assertEqual(world.uploads(), 0)
        self.assertIsNone(world.compound().committed_at)

    def test_the_gate_is_asked_once_for_the_still_and_only_before_anything_of_the_pair_is_sent(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        asked = []
        real = same_shot.gate

        def watching(opened, reader, intent, receipt_id, entry, name, now):
            asked.append(name)
            return real(opened, reader, intent, receipt_id, entry, name, now)
        world.fake.state.intercept = lambda method, path, slug: 503 if (method, path) == ("POST", "/api/assets") and world.uploads() > 1 else None
        with mock.patch.object(same_shot, "gate", watching), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
            world.fake.state.intercept = None
            world.finish(export, at=NOW + 2 * managed_upload.LEASE_SECONDS)
        self.assertEqual(asked, ["IMG_3001.HEIC"])
        self.assertEqual(world.status(export), ("imported", "done"))


class ManifestTests(unittest.TestCase):
    def test_an_unknown_manifest_sends_nothing_for_a_pair_either(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))
        world.opened.execute(update(managed_ingest.ManagedImportIntent).values(resource_manifest="{}"))
        world.opened.commit()
        world.finish(export)
        self.assertEqual((world.uploads(), world.count(ManagedCompound), world.status(export)), (0, 0, ("downloading", "placed")))

