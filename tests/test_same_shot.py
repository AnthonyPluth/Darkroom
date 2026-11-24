import json
import unittest
import uuid
from datetime import UTC, datetime
from unittest import mock

from sqlalchemy import select

from darkroom.domain import alerts, managed_ingest, managed_upload, review, same_shot
from darkroom.domain.managed_upload import Report
from darkroom.domain.ingest import cleanup, finish_export, part_free
from darkroom.providers import immich
from darkroom.storage.models import (Alert, DedupeDecision, Event, ManagedAssetOrigin, ManagedAssetVersion, ManagedImportReceipt, ManagedSameShotHold, MediaItem,
                                     TakeoutEntry)
from tests.fakes.immich import FakeAsset, extract
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_managed_ingest import KEY, OWNER, SECRET, ManagedWorld, single
from tests.test_part_ingest import Crash

INSTANT = 1500000000.0
OTHER = "22222222-2222-4222-8222-222222222222"
NAME = "CANARY_NEIGHBOUR_NAME.JPG"
SHA = "a" * 40


def stamp(seconds=0.0):
    return datetime.fromtimestamp(INSTANT + seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def asset(n, **changes):
    fields = {"owner_id": OWNER, "library_id": None, "sha1": f"{n:040x}", "file_name": f"IMG_{n}.JPG", "kind": "IMAGE", "visibility": "timeline",
              "live_video_id": None, "trashed": False, "offline": False}
    fields.update(changes)
    return immich.Asset(id=f"{n:08x}-0000-4000-8000-000000000000", **fields)


def shot(n, seconds=0.0, make="Apple", model="iPhone 11", width=32, height=24, duration=None, **changes):
    return immich.Nearby(asset(n, **changes), datetime.fromtimestamp(INSTANT + seconds, UTC), make, model, width, height, duration)


def incoming(**changes):
    fields = {"owner_id": OWNER, "sha1": SHA, "instant": INSTANT, "kind": "IMAGE", "name": "IMG_5001.JPG", "make": "Apple", "model": "iPhone 11", "width": 32,
              "height": 24, "duration_ms": None, "edited": False}
    fields.update(changes)
    return same_shot.Incoming(**fields)


def near(*shots, crowded=False):
    return immich.NearShots(tuple(shots), crowded)


def ids(verdict):
    return [candidate.asset_id for candidate in verdict.candidates]


class MatchTests(unittest.TestCase):
    def test_an_asset_of_the_owner_within_a_second_with_other_bytes_is_a_candidate_with_its_agreement_flags(self):
        verdict = same_shot.match(incoming(), near(shot(1, 0.5), shot(2, -1.0, make="Pixel", width=64, height=48), shot(3, 1.0, width=16, height=12)))
        self.assertEqual([(each.asset_id[:8], each.camera, each.size, each.more_pixels, each.library) for each in verdict.candidates],
                         [("00000001", True, True, False, "managed"), ("00000002", False, False, False, "managed"), ("00000003", True, False, True, "managed")])
        self.assertTrue(verdict.held and not verdict.crowded)

    def test_an_asset_more_than_a_second_away_is_not_a_candidate_and_the_edge_is_inclusive(self):
        self.assertEqual(ids(same_shot.match(incoming(), near(shot(1, 1.001), shot(2, -1.001), shot(3, 2.0)))), [])
        self.assertEqual(len(ids(same_shot.match(incoming(), near(shot(1, 1.0), shot(2, -1.0))))), 2)

    def test_another_owners_asset_is_never_a_candidate_and_never_authorises_anything(self):
        verdict = same_shot.match(incoming(), near(shot(1, 0, owner_id=OTHER), shot(2, 0, owner_id=OTHER, sha1=SHA)))
        self.assertEqual((ids(verdict), verdict.held), ([], False))

    def test_an_asset_with_the_same_bytes_in_the_window_means_the_photo_is_already_there_and_nothing_is_held(self):
        verdict = same_shot.match(incoming(), near(shot(1, 0.2), shot(2, 0.4, sha1=SHA)))
        self.assertEqual((ids(verdict), verdict.held), ([], False))

    def test_the_same_bytes_of_another_owner_do_not_clear_a_candidate(self):
        self.assertEqual(len(ids(same_shot.match(incoming(), near(shot(1, 0.2), shot(2, 0.4, sha1=SHA, owner_id=OTHER))))), 1)

    def test_a_still_never_matches_a_clip_and_a_clip_never_matches_a_still(self):
        self.assertEqual(ids(same_shot.match(incoming(), near(shot(1, 0, kind="VIDEO", duration=3000)))), [])
        self.assertEqual(ids(same_shot.match(incoming(kind="VIDEO", name="VID_1.MP4", duration_ms=3000), near(shot(1, 0)))), [])
        verdict = same_shot.match(incoming(kind="VIDEO", name="VID_1.MP4", duration_ms=3200, width=None, height=None), near(shot(1, 0, kind="VIDEO", duration=3900)))
        self.assertEqual([(each.kind, each.size, each.more_pixels) for each in verdict.candidates], [("VIDEO", True, False)])

    def test_a_live_photos_hidden_motion_video_and_the_video_a_still_names_are_skipped(self):
        hidden = shot(1, 0, kind="IMAGE", visibility="hidden")
        motion = shot(2, 0)
        still = shot(3, 0.1, live_video_id=motion.asset.id)
        verdict = same_shot.match(incoming(), near(hidden, motion, still))
        self.assertEqual(ids(verdict), [still.asset.id])

    def test_a_raw_file_never_matches_a_jpeg_and_two_raw_files_do(self):
        self.assertEqual(ids(same_shot.match(incoming(), near(shot(1, 0, file_name="IMG_1.NEF")))), [])
        self.assertEqual(ids(same_shot.match(incoming(name="IMG_5001.CR3"), near(shot(1, 0)))), [])
        self.assertEqual(len(ids(same_shot.match(incoming(name="IMG_5001.CR3"), near(shot(1, 0, file_name="IMG_1.NEF"))))), 1)

    def test_an_asset_that_is_only_darkrooms_own_takeout_upload_is_skipped(self):
        first, second = shot(1, 0), shot(2, 0.3)
        verdict = same_shot.match(incoming(), near(first, second), {first.asset.id})
        self.assertEqual(ids(verdict), [second.asset.id])

    def test_trashed_offline_and_external_assets_of_the_owner_hold_with_their_state(self):
        verdict = same_shot.match(incoming(), near(shot(1, 0, trashed=True), shot(2, 0, offline=True), shot(3, 0, library_id="lib-x"), shot(4, 0, trashed=True, offline=True)))
        self.assertEqual([each.library for each in verdict.candidates], ["trashed", "offline", "external", "trashed"])

    def test_a_crowded_window_holds_without_candidates_and_the_edited_flag_is_carried(self):
        verdict = same_shot.match(incoming(edited=True), near(crowded=True))
        self.assertEqual((verdict.held, verdict.crowded, verdict.candidates, verdict.edited), (True, True, (), True))
        self.assertTrue(json.loads(same_shot.snapshot(verdict))["crowded"])

    def test_the_stored_candidates_carry_no_file_name_place_or_description_and_are_ascii(self):
        stored = same_shot.snapshot(same_shot.match(incoming(), near(shot(1, 0.5, file_name="CANARY_é.JPG"))))
        self.assertTrue(stored.isascii())
        self.assertNotIn("CANARY", stored)
        self.assertEqual(sorted(json.loads(stored)["candidates"][0]), ["asset_id", "camera", "instant", "kind", "library", "more_pixels", "sha1", "size"])

    def test_a_camera_or_size_that_is_missing_never_counts_as_agreeing(self):
        verdict = same_shot.match(incoming(make=None, width=None, height=None), near(shot(1, 0, make=None, width=None, height=None)))
        self.assertEqual([(each.camera, each.size, each.more_pixels) for each in verdict.candidates], [(False, False, False)])


def with_neighbour(world, seconds=0.0, **changes):
    owner = changes.pop("owner_id", OWNER)
    fields = {"name": NAME, "file_created_at": stamp(seconds)}
    fields.update(changes)
    return world.fake.add_asset(FakeAsset(str(uuid.uuid4()), owner, b"neighbour bytes " + uuid.uuid4().bytes, **fields))


def holds(world):
    world.opened.expire_all()
    return world.opened.execute(select(ManagedSameShotHold).order_by(ManagedSameShotHold.id)).scalars().all()


class HoldTests(unittest.TestCase):
    def photo(self, world, name="IMG_5001.JPG"):
        export = world.create(single(name), parts=1)
        world.ingest_all(export, (1,))
        return export

    def posted(self, world):
        return [call for call in world.fake.requests() if call == ("POST", "/api/assets")]

    def test_a_different_byte_asset_at_the_instant_holds_the_photo_and_nothing_is_uploaded_or_deleted(self):
        world = ManagedWorld(self)
        neighbour = with_neighbour(world, 0.4)
        with no_leaks(self, KEY, SECRET, NAME, "IMG_5001.JPG", world.fake.url):
            export = self.photo(world)
            world.finish(export)
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, "same_shot")})
        self.assertEqual(self.posted(world), [])
        self.assertEqual([asset.id for asset in world.fake.state.assets], [neighbour.id])
        self.assertEqual({method for method, _ in world.fake.requests()} - {"GET"}, {"POST"})
        self.assertEqual([path for method, path in world.fake.requests() if method == "POST"], ["/api/search/metadata"])
        (hold,) = holds(world)
        listed = json.loads(hold.candidates)
        self.assertEqual((hold.status, hold.person_id, hold.owner_id, hold.found_at, hold.resolved_at, hold.asset_id), ("pending", world.ids["jo"], OWNER, NOW, None, None))
        self.assertEqual([(each["asset_id"], each["library"], each["camera"], each["size"]) for each in listed["candidates"]], [(neighbour.id, "managed", False, False)])
        self.assertEqual((listed["edited"], listed["crowded"]), (False, False))
        self.assertEqual(world.count(ManagedImportReceipt), 1)

    def test_the_export_finishes_with_one_alert_for_the_export_and_person_and_the_held_count(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        world.finish(export)
        found = world.alert(f"managed:same_shot:{export.id}:jo")
        self.assertEqual((found.kind, found.person_id, found.closed), ("same_shot", world.ids["jo"], None))
        self.assertEqual(found.message, "1 photo looks like one already in Immich and waits for you. Nothing was uploaded or deleted.")
        self.assertEqual(len(world.opened.execute(select(Alert).where(Alert.key.startswith("managed:same_shot:"))).scalars().all()), 1)
        self.assertIn("1 item of export", world.alert(f"managed:held:{export.id}").message)
        self.assertEqual(world.opened.execute(select(Event.type, Event.subject, Event.detail).where(Event.type == "same_shot_held")).all(), [("same_shot_held", "receipt 1", "1 candidates")])

    def test_the_alert_counts_every_pending_hold_and_closes_once_none_is_left(self):
        world = ManagedWorld(self)
        with_neighbour(world)

        def build(builder):
            builder.photo("IMG_5001.JPG", part=0, data=builder.image_bytes(".JPG"), taken=1500000000)
            builder.photo("IMG_5002.JPG", part=0, data=builder.image_bytes(".JPG"), taken=1500000000)
        export = world.create(build, parts=1)
        world.ingest_all(export, (1,))
        world.finish(export)
        key = f"managed:same_shot:{export.id}:jo"
        self.assertIn("2 photos look like ones already in Immich and wait for you", world.alert(key).message)
        person, found = world.jo(), world.opened.get(type(export), export.id)
        for hold in holds(world):
            hold.status, hold.resolved_at = "void", NOW + 5
        world.opened.commit()
        same_shot.alert(world.opened, world.settings, found, person, NOW + 6, None)
        self.assertEqual(world.alert(key).closed, NOW + 6)

    def test_an_asset_more_than_a_second_away_or_of_another_owner_does_not_hold_the_photo(self):
        world = ManagedWorld(self)
        with_neighbour(world, 1.5)
        with_neighbour(world, 0.0, owner_id=OTHER)
        export = self.photo(world)
        world.finish(export)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("committed", "created", None)})
        self.assertEqual((holds(world), world.status(export)), ([], ("imported", "done")))
        self.assertIsNone(world.alert(f"managed:same_shot:{export.id}:jo"))

    def test_an_exact_copy_in_the_window_is_the_existing_duplicate_path_and_not_a_hold(self):
        world = ManagedWorld(self)
        export = self.photo(world)
        data = world.bytes_of(export, "IMG_5001.JPG")
        world.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, data, name="IMG_9001.JPG", file_created_at=stamp(0.2), exif=extract(data, None)))
        with_neighbour(world, 0.3)
        with mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        self.assertEqual((holds(world), world.receipts()), ([], {"IMG_5001.JPG": ("bytes_verified", "duplicate", "not_enriched")}))

    def test_a_trashed_asset_of_the_owner_holds_the_photo(self):
        world = ManagedWorld(self)
        neighbour = with_neighbour(world, 0.0, trashed=True)
        world.finish(self.photo(world))
        (hold,) = holds(world)
        self.assertEqual([(each["asset_id"], each["library"]) for each in json.loads(hold.candidates)["candidates"]], [(neighbour.id, "trashed")])

    def test_a_pending_hold_is_never_released_by_age_by_the_candidate_going_or_a_later_tick(self):
        world = ManagedWorld(self)
        neighbour = with_neighbour(world)
        export = self.photo(world)
        world.finish(export)
        world.fake.state.assets.remove(neighbour)
        for at in (NOW + DAY, NOW + 30 * DAY):
            finish_export.finish_ready(world.opened, world.settings, at, world.tools)
            managed_ingest.send_kept(world.opened, world.settings, world.photos, at, None)
            cleanup.clean(world.opened, world.photos, at)
            part_free.free_ready(world.opened, world.settings, at)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, "same_shot")})
        self.assertEqual((self.posted(world), [hold.status for hold in holds(world)], len(world.zips(world.opened.get(type(export), export.id)))), ([], ["pending"], 1))

    def test_a_held_photos_source_stays_pinned_in_the_inbox_while_it_waits(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        world.finish(export)
        self.assertEqual(cleanup.clean(world.opened, world.photos, NOW + 60 * DAY).parts, 0)
        self.assertEqual(part_free.free_ready(world.opened, world.settings, NOW + 60 * DAY).parts, 0)
        self.assertEqual(len(world.zips(world.opened.get(type(export), export.id))), 1)

    def test_a_second_tick_over_a_held_photo_asks_immich_nothing_and_makes_no_second_hold(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        world.finish(export)
        seen = len(world.fake.requests())
        entry = world.opened.execute(select(TakeoutEntry).where(TakeoutEntry.kind == "media")).scalar_one()
        intent = managed_ingest.intent_of(world.opened, world.opened.get(type(export), export.id))
        receipt = world.opened.execute(select(ManagedImportReceipt)).scalar_one()
        reader = managed_ingest.reader_of(world.settings, world.jo(), intent, None)
        outcome = same_shot.gate(world.opened, reader, intent, receipt.id, entry, "IMG_5001.JPG", NOW + 5)
        self.assertEqual((outcome, len(world.fake.requests()), len(holds(world))), ("held", seen, 1))

    def test_an_unreachable_immich_makes_no_hold_sends_nothing_and_the_next_tick_checks_again(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        with mock.patch.object(immich.ManagedReader, "taken_near", side_effect=immich.ImmichFailed("down")):
            world.finish(export)
        self.assertEqual((world.status(export), world.receipts(), holds(world), self.posted(world)), (("downloading", "placed"), {"IMG_5001.JPG": ("prepared", None, None)}, [], []))
        self.assertIn("1 upload of export", world.alert(f"managed:pending:{export.id}").message)
        world.finish(export, at=NOW + 10)
        self.assertEqual((world.status(export), world.receipts(), len(holds(world))), (("imported", "done"), {"IMG_5001.JPG": ("prepared", None, "same_shot")}, 1))

    def test_an_answer_immich_gives_that_is_not_understood_is_never_read_as_no_match(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        real = immich.Client.call

        def call(client, method, path, *args, **kwargs):
            if path == "/api/search/metadata":
                return {"assets": {"items": "nonsense", "nextCursor": None}}
            return real(client, method, path, *args, **kwargs)
        with mock.patch.object(immich.Client, "call", call):
            world.finish(export)
        self.assertEqual((holds(world), self.posted(world), world.status(export)), ([], [], ("downloading", "placed")))

    def test_a_wrong_key_is_refused_before_any_search_and_the_receipt_stays_pending(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        world.fake.state.permissions = ("asset.read", "server.about", "user.read")
        world.finish(export)
        self.assertEqual((holds(world), world.status(export), [r for r in world.fake.requests() if r[1] == "/api/search/metadata"]), ([], ("downloading", "placed"), []))

    def test_a_crash_between_the_check_and_the_hold_commit_leaves_no_hold_and_nothing_sent_and_the_next_tick_holds(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        export_id = export.id
        with mock.patch.object(same_shot.events, "record", side_effect=Crash()), self.assertRaises(Crash):
            world.finish(export)
        world.reopen()
        self.assertEqual((holds(world), world.receipts(), self.posted(world)), ([], {"IMG_5001.JPG": ("prepared", None, None)}, []))
        world.finish(world.opened.get(type(export), export_id), at=NOW + 10)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, "same_shot")})
        self.assertEqual(len(holds(world)), 1)

    def test_two_workers_that_check_the_same_receipt_make_one_hold_and_the_second_sends_nothing(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        world.finish(export)
        intent = managed_ingest.intent_of(world.opened, world.opened.get(type(export), export.id))
        receipt = world.opened.execute(select(ManagedImportReceipt)).scalar_one()
        verdict = same_shot.Verdict((), True, False)
        self.assertEqual(same_shot.make_hold(world.opened, intent, receipt.id, verdict, NOW + 1), "held")
        self.assertEqual(len(holds(world)), 1)

    def test_a_claimed_receipt_and_a_receipt_with_a_decided_hold_row_are_not_checked(self):
        world = ManagedWorld(self)
        export = self.photo(world)
        with mock.patch.object(managed_upload, "upload_receipt", return_value=Report("pending", "not sent in this test")):
            world.finish(export)
        entry = world.opened.execute(select(TakeoutEntry).where(TakeoutEntry.kind == "media")).scalar_one()
        intent = managed_ingest.intent_of(world.opened, world.opened.get(type(export), export.id))
        receipt = world.opened.execute(select(ManagedImportReceipt)).scalar_one()
        reader = managed_ingest.reader_of(world.settings, world.jo(), intent, None)

        def gate():
            with mock.patch.object(immich.ManagedReader, "taken_near", side_effect=AssertionError("must not be asked")):
                return same_shot.gate(world.opened, reader, intent, receipt.id, entry, "IMG_5001.JPG", NOW)
        fresh = world.opened.get(ManagedImportReceipt, receipt.id, populate_existing=True)
        fresh.state, fresh.send_attempts = "outcome_unknown", 1
        world.opened.commit()
        self.assertEqual(gate(), "clear")
        fresh = world.opened.get(ManagedImportReceipt, receipt.id, populate_existing=True)
        fresh.state, fresh.send_attempts = "prepared", 0
        world.opened.add(ManagedSameShotHold(person_id=intent.person_id, owner_id=intent.owner_id, receipt_id=receipt.id, found_at=NOW, candidates="{}", status="upload",
                                             resolved_at=NOW))
        world.opened.commit()
        self.assertEqual(gate(), "clear")

    def test_an_owner_who_chose_to_upload_is_not_checked_again(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = self.photo(world)
        world.finish(export)
        hold = holds(world)[0]
        receipt = world.opened.get(ManagedImportReceipt, hold.receipt_id)
        hold.status, hold.resolved_at, receipt.metadata_hold = "upload", NOW + 5, None
        world.opened.commit()
        intent = managed_ingest.intent_of(world.opened, world.opened.get(type(export), export.id))
        entry = world.opened.execute(select(TakeoutEntry).where(TakeoutEntry.kind == "media")).scalar_one()
        reader = managed_ingest.reader_of(world.settings, world.jo(), intent, None)
        seen = len(world.fake.requests())
        self.assertEqual(same_shot.gate(world.opened, reader, intent, receipt.id, entry, "IMG_5001.JPG", NOW + 6), "clear")
        self.assertEqual(len(world.fake.requests()), seen)

    def test_a_photo_held_before_upload_for_its_time_is_never_checked(self):
        world = ManagedWorld(self)
        with mock.patch.object(immich.ManagedReader, "taken_near", return_value=immich.NearShots((), False)) as asked:
            export = world.create(world.scenario_with_live_photo, parts=3)
            world.ingest_all(export, range(1, 4))
            world.finish(export)
        self.assertEqual((world.receipts()["IMG_2003.JPG"], asked.call_count), (("prepared", None, "no_instant"), 2))

    def test_keep_both_on_a_review_pair_is_not_checked_and_goes_to_immich(self):
        world = ManagedWorld(self)
        world.run_managed()
        held = world.opened.execute(select(ManagedImportReceipt.id)).scalars().first()
        pending = world.opened.execute(select(review.ReviewItem.id)).scalar_one()
        review.resolve(world.opened, world.photos, pending, "keep_both", NOW + 5, review.FileEffects())
        world.opened.commit()
        row = world.opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.resource_key == f"review:{pending}")).scalar_one()
        taken = json.loads(row.expected_metadata)["taken_utc"]
        world.fake.add_asset(FakeAsset(str(uuid.uuid4()), OWNER, b"another shot", name="NEIGHBOUR_1.JPG", file_created_at=datetime.fromtimestamp(taken, UTC)
                                       .isoformat(timespec="milliseconds").replace("+00:00", "Z")))
        finish_export.finish_ready(world.opened, world.settings, NOW + 10, world.tools)
        self.assertEqual((world.receipts()[f"review:{pending}"], holds(world), held is not None), (("committed", "created", None), [], True))

    def test_a_later_photo_of_the_same_takeout_is_not_held_by_the_first_ones_upload_but_is_by_a_phone_original(self):
        world = ManagedWorld(self)

        def build(builder):
            builder.photo("IMG_5001.JPG", part=0, data=builder.image_bytes(".JPG"), taken=1500000000)
            builder.photo("IMG_5002.JPG", part=0, data=builder.image_bytes(".JPG"), taken=1500000000)
        export = world.create(build, parts=1)
        world.ingest_all(export, (1,))
        world.finish(export)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("committed", "created", None), "IMG_5002.JPG": ("committed", "created", None)})
        self.assertEqual(holds(world), [])
        asset_ids = {each.id for each in world.fake.state.assets}
        self.assertEqual((len(asset_ids), same_shot.takeout_only(world.opened, world.ids["jo"], OWNER, asset_ids)), (2, asset_ids))
        first = ManagedAssetVersion(person_id=world.ids["jo"], owner_id=OWNER, asset_id=sorted(asset_ids)[0], sha1="c" * 40, bytes=10, kind="IMAGE", state="bytes_verified",
                                    metadata_snapshot="{}", seen_at=NOW, bytes_verified_at=NOW)
        world.opened.add(first)
        world.opened.flush()
        world.opened.add(ManagedAssetOrigin(version_id=first.id, person_id=first.person_id, owner_id=first.owner_id, asset_id=first.asset_id, sha1=first.sha1,
                                            kind="phone", source_key="phone-1", evidence_snapshot="{}", recorded_at=NOW))
        world.opened.commit()
        self.assertEqual(same_shot.takeout_only(world.opened, world.ids["jo"], OWNER, asset_ids), asset_ids - {first.asset_id})
        self.assertEqual(same_shot.takeout_only(world.opened, world.ids["jo"], OTHER, asset_ids), set())
        self.assertEqual(same_shot.takeout_only(world.opened, world.ids["jo"], OWNER, set()), set())


class ReversalTests(unittest.TestCase):
    def held(self):
        world = ManagedWorld(self)
        with_neighbour(world)
        export = world.create(single("IMG_5001.JPG"), parts=1)
        world.ingest_all(export, (1,))
        world.finish(export)
        decision = world.opened.execute(select(DedupeDecision)).scalars().first()
        return world, export, decision

    def test_reversing_the_decision_of_a_held_photo_answers_409_with_a_fixed_reason_until_it_is_resolved(self):
        world, _, decision = self.held()
        with self.assertRaises(review.WrongState) as raised:
            review.reverse(world.opened, decision.id, NOW + 5)
        self.assertEqual(str(raised.exception), same_shot.WAITS_FOR_REVERSAL)
        self.assertIsNone(world.opened.get(DedupeDecision, decision.id).reversed_at)
        hold = holds(world)[0]
        hold.status, hold.resolved_at = "void", NOW + 6
        world.opened.commit()
        self.assertEqual(review.reverse(world.opened, decision.id, NOW + 7).reversed_at, NOW + 7)

    def test_reassigning_a_photo_whose_import_waits_in_a_hold_answers_409_with_a_fixed_reason(self):
        world, _, decision = self.held()
        entry = world.opened.execute(select(TakeoutEntry).where(TakeoutEntry.decision_id == decision.id)).scalars().first()
        item = MediaItem(person_id=world.ids["jo"], path="jo/google-only/IMG_5001.JPG", sha1="b" * 40, bytes=10, source="takeout", export_id=entry.export_id, indexed_at=NOW)
        world.opened.add(item)
        world.opened.flush()
        self.assertFalse(same_shot.pending_for_item(world.opened, item.id))
        entry.item_id = item.id
        world.opened.commit()
        self.assertTrue(same_shot.pending_for_item(world.opened, item.id))
        with self.assertRaises(review.WrongState) as raised:
            review.reassign(world.opened, world.photos, item.id, world.ids["sam"], NOW + 5, review.FileEffects())
        self.assertEqual(str(raised.exception), same_shot.WAITS_FOR_REASSIGNMENT)

    def test_the_wording_of_the_alert_is_titled_and_fixed(self):
        self.assertEqual(alerts.TITLES["same_shot"], "Photos wait for you")
        self.assertNotIn("IMG_", same_shot.WAITS_FOR_REVERSAL + same_shot.WAITS_FOR_REASSIGNMENT)


if __name__ == "__main__":
    unittest.main()
