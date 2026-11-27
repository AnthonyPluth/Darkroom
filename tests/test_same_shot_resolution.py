import contextlib
import io
import os
import unittest
from unittest import mock

from sqlalchemy import select

import run
from darkroom.domain import managed_ingest, managed_upload, pixel_source, same_shot, same_shot_resolution as resolution
from darkroom.domain.ingest import finish_export, hold_apply, same_shot_apply
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import (Alert, Event, ManagedAssetOrigin, ManagedAssetVersion, ManagedBackfillItem, ManagedImportReceipt,
                                     MediaItem)
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.shared import own_database_url
from tests.test_managed_live_photo import LiveWorld, live
from tests.test_managed_ingest import KEY, OWNER, SECRET, ManagedWorld, single
from tests.test_same_shot import NAME, holds, with_neighbour

LATER = NOW + 100


class Held:
    def held(self, **neighbour):
        world = ManagedWorld(self)
        found = with_neighbour(world, 0.4, **neighbour)
        export = world.create(single("IMG_5001.JPG"), parts=1)
        world.ingest_all(export, (1,))
        world.finish(export)
        (hold,) = holds(world)
        return world, export, found, hold

    def act(self, world, hold, action, asset=None, at=LATER, client=None):
        return resolution.act(world.opened, world.settings, client or world.tools.immich, world.jo(), hold.id, action, asset, None, at)

    def searches(self, world):
        return len([call for call in world.fake.requests() if call == ("POST", "/api/search/metadata")])

    def posts(self, world):
        return [call for call in world.fake.requests() if call == ("POST", "/api/assets")]

    def refused(self, world, hold, error, reason, action="represented", asset=None, **kwargs):
        with self.assertRaises(error) as raised:
            self.act(world, hold, action, asset, **kwargs)
        self.assertEqual(str(raised.exception), reason)
        world.opened.rollback()
        self.assertEqual(holds(world)[0].status, "pending")
        self.assertEqual(world.opened.execute(select(ManagedAssetOrigin)).first(), None)


class UploadTests(Held, unittest.TestCase):
    def test_upload_releases_the_photo_and_a_later_tick_sends_it_from_the_imported_export_without_checking_again(self):
        world, export, found, hold = self.held()
        seen = self.searches(world)
        with no_leaks(self, KEY, SECRET, NAME, "IMG_5001.JPG", world.fake.url):
            done = self.act(world, hold, "upload")
        self.assertEqual((done.status, done.resolved_at, done.resolved_by, done.asset_id), ("upload", LATER, None, None))
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, None)})
        self.assertEqual(self.posts(world), [])
        self.assertEqual(world.opened.execute(select(Event.type, Event.subject, Event.detail).where(Event.type == "same_shot_resolved")).all(),
                         [("same_shot_resolved", "receipt 1", f"hold {hold.id} upload")])
        self.assertEqual(world.alert(f"managed:same_shot:{export.id}:jo").closed, LATER)
        with no_leaks(self, KEY, SECRET, NAME, "IMG_5001.JPG", world.fake.url):
            finish_export.finish_ready(world.opened, world.settings, LATER + 10, world.tools)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("committed", "created", None)})
        self.assertEqual((len(self.posts(world)), len(world.fake.state.assets), self.searches(world)), (1, 2, seen))
        self.assertIn(found, world.fake.state.assets)
        finish_export.finish_ready(world.opened, world.settings, LATER + 20, world.tools)
        self.assertEqual(len(self.posts(world)), 1)

    def test_a_decided_hold_is_not_decided_twice_and_another_persons_hold_is_not_found(self):
        world, _, _, hold = self.held()
        self.act(world, hold, "upload")
        with self.assertRaises(resolution.Conflict) as raised:
            self.act(world, hold, "upload")
        self.assertEqual(str(raised.exception), resolution.NOT_PENDING)
        with self.assertRaises(resolution.Conflict):
            self.act(world, hold, "represented", with_asset(world))
        sam = world.opened.get(type(world.jo()), world.ids["sam"])
        with self.assertRaises(resolution.NoSuchHold):
            resolution.act(world.opened, world.settings, None, sam, hold.id, "undo", None, None, LATER)

    def test_upload_is_refused_once_the_receipt_was_claimed_or_the_hold_is_gone(self):
        world, _, _, hold = self.held()
        receipt = world.opened.get(ManagedImportReceipt, hold.receipt_id)
        receipt.send_attempts = 1
        world.opened.commit()
        self.refused(world, hold, resolution.Conflict, resolution.NOT_HELD, "upload")

    def test_undo_puts_an_unsent_upload_back_to_pending_and_the_alert_returns(self):
        world, export, _, hold = self.held()
        self.act(world, hold, "upload")
        done = self.act(world, hold, "undo", at=LATER + 1)
        self.assertEqual((done.status, done.resolved_at, done.resolved_by, done.asset_id), ("pending", None, None, None))
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, "same_shot")})
        self.assertIn("1 photo looks like one", world.alert(f"managed:same_shot:{export.id}:jo").message)
        self.assertIsNone(world.alert(f"managed:same_shot:{export.id}:jo").closed)
        finish_export.finish_ready(world.opened, world.settings, LATER + 10, world.tools)
        self.assertEqual((self.posts(world), world.receipts()), ([], {"IMG_5001.JPG": ("prepared", None, "same_shot")}))
        self.assertEqual([detail for (detail,) in world.opened.execute(select(Event.detail).where(Event.type == "same_shot_resolved").order_by(Event.id))],
                         [f"hold {hold.id} upload", f"hold {hold.id} undo"])

    def test_undo_is_refused_after_the_photo_was_sent_and_with_nothing_to_undo(self):
        world, _, _, hold = self.held()
        self.refused(world, hold, resolution.Conflict, resolution.NOTHING_TO_UNDO, "undo")
        self.act(world, hold, "upload")
        finish_export.finish_ready(world.opened, world.settings, LATER + 10, world.tools)
        with self.assertRaises(resolution.Conflict) as raised:
            self.act(world, hold, "undo", at=LATER + 20)
        self.assertEqual(str(raised.exception), resolution.SENT)
        world.opened.rollback()
        self.assertEqual((holds(world)[0].status, world.receipts()), ("upload", {"IMG_5001.JPG": ("committed", "created", None)}))

    def test_a_failed_send_after_upload_keeps_the_copy_tries_again_and_alerts_after_a_day(self):
        world, _, _, hold = self.held()
        self.act(world, hold, "upload")
        with mock.patch.object(immich.ManagedReader, "upload", side_effect=immich.ImmichFailed("down")):
            finish_export.finish_ready(world.opened, world.settings, LATER + 10, world.tools)
            self.assertIsNone(world.alert(f"managed:same-shot-upload:{hold.id}"))
            finish_export.finish_ready(world.opened, world.settings, LATER + DAY + 10, world.tools)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, None)})
        self.assertIn("could not complete the upload", world.alert(f"managed:same-shot-upload:{hold.id}").message)
        finish_export.finish_ready(world.opened, world.settings, LATER + DAY + 20, world.tools)
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("committed", "created", None)})
        self.assertIsNotNone(world.alert(f"managed:same-shot-upload:{hold.id}").closed)

    def test_an_unreadable_copy_or_an_unreachable_immich_keeps_the_hold_released_and_alerts_once_after_a_day(self):
        world, _, _, hold = self.held()
        self.act(world, hold, "upload")
        tick = {"unreadable": mock.patch.object(hold_apply, "source_of", side_effect=hold_apply.UnreadableSource),
                "unreachable": mock.patch.object(managed_ingest, "reader_of", side_effect=immich.ImmichFailed("down"))}
        for number, (name, patched) in enumerate(tick.items()):
            with self.subTest(name), patched:
                finish_export.finish_ready(world.opened, world.settings, LATER + 10 + number, world.tools)
                self.assertIsNone(world.alert(f"managed:same-shot-upload:{hold.id}"))
                finish_export.finish_ready(world.opened, world.settings, LATER + DAY + 10 + number, world.tools)
                self.assertIn("could not complete the upload", world.alert(f"managed:same-shot-upload:{hold.id}").message)
            world.opened.execute(Alert.__table__.delete())
            world.opened.commit()
        self.assertEqual((self.posts(world), world.receipts()), ([], {"IMG_5001.JPG": ("prepared", None, None)}))

    def test_a_hold_that_is_no_longer_released_when_its_turn_comes_is_not_sent(self):
        world, _, _, hold = self.held()
        self.act(world, hold, "upload")
        self.assertEqual(same_shot_apply.due(world.opened), [hold.receipt_id])
        self.act(world, hold, "undo", at=LATER + 1)
        same_shot_apply.send_one(world.opened, world.settings, world.photos, hold.receipt_id, LATER + 2, world.tools, mock.Mock(side_effect=AssertionError))
        same_shot_apply.send_compound(world.opened, world.settings, world.photos, 1, hold.id, LATER + 2, world.tools)
        self.assertEqual((same_shot_apply.due(world.opened), self.posts(world), world.receipts()), ([], [], {"IMG_5001.JPG": ("prepared", None, "same_shot")}))


def with_asset(world):
    return world.fake.state.assets[0].id


class RepresentedTests(Held, unittest.TestCase):
    def test_already_in_immich_verifies_the_candidate_records_its_version_and_a_takeout_origin_and_sends_nothing(self):
        world, export, found, hold = self.held()
        before = (self.searches(world), len(world.fake.requests()))
        with no_leaks(self, KEY, SECRET, NAME, "IMG_5001.JPG", world.fake.url):
            done = self.act(world, hold, "represented", found.id)
        self.assertEqual((done.status, done.asset_id, done.resolved_at), ("represented", found.id, LATER))
        version = world.opened.execute(select(ManagedAssetVersion)).scalar_one()
        self.assertEqual((version.asset_id, version.sha1, version.bytes, version.state, version.library_id, version.owner_id, version.person_id),
                         (found.id, same_shot_sha(found), len(found.data), "bytes_verified", None, OWNER, world.ids["jo"]))
        origin = world.opened.execute(select(ManagedAssetOrigin)).scalar_one()
        receipt = world.opened.get(ManagedImportReceipt, hold.receipt_id)
        self.assertEqual((origin.kind, origin.source_key, origin.version_id, origin.intent_id, origin.receipt_id, origin.receipt_state),
                         ("takeout", f"same-shot:{hold.id}", version.id, receipt.intent_id, None, None))
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, "same_shot")})
        self.assertEqual(self.posts(world), [])
        self.assertEqual(world.opened.execute(select(Event.detail).where(Event.type == "same_shot_resolved")).scalar_one(), f"hold {hold.id} represented")
        self.assertEqual(world.alert(f"managed:same_shot:{export.id}:jo").closed, LATER)
        self.assertGreater(len(world.fake.requests()), before[1])
        finish_export.finish_ready(world.opened, world.settings, LATER + 10, world.tools)
        self.assertEqual((self.posts(world), len(world.fake.state.assets), world.receipts()), ([], 1, {"IMG_5001.JPG": ("prepared", None, "same_shot")}))

    def test_the_origin_keeps_the_pixel_copy_from_going_back_to_the_same_account_but_proves_no_google_backup(self):
        world, export, found, hold = self.held()
        self.act(world, hold, "represented", found.id)
        version = world.opened.execute(select(ManagedAssetVersion)).scalar_one()
        self.assertTrue(pixel_source.returns_home(world.opened, version.id, export.account_id))
        self.assertFalse(pixel_source.returns_home(world.opened, version.id, export.account_id + 1))
        self.assertEqual({decision.decided_by for decision in world.opened.execute(select(type(self.decision(world)))).scalars()}, {"auto"})
        self.assertEqual(same_shot.takeout_only(world.opened, world.ids["jo"], OWNER, {found.id}), set())

    def decision(self, world):
        from darkroom.storage.models import DedupeDecision

        return world.opened.execute(select(DedupeDecision)).scalars().first()

    def test_an_existing_version_is_refreshed_and_its_phone_origin_stays_so_a_pixel_copy_still_goes_back(self):
        world, export, found, hold = self.held()
        old = ManagedAssetVersion(person_id=world.ids["jo"], owner_id=OWNER, asset_id=found.id, sha1=same_shot_sha(found), bytes=len(found.data), kind="IMAGE",
                                  state="observed", metadata_snapshot="{}", seen_at=NOW)
        world.opened.add(old)
        world.opened.flush()
        world.opened.add(ManagedAssetOrigin(version_id=old.id, person_id=old.person_id, owner_id=OWNER, asset_id=found.id, sha1=old.sha1, kind="phone",
                                            source_key="phone-1", evidence_snapshot="{}", recorded_at=NOW))
        world.opened.commit()
        self.act(world, hold, "represented", found.id)
        world.opened.expire_all()
        self.assertEqual(world.opened.execute(select(ManagedAssetVersion.state, ManagedAssetVersion.id)).all(), [("bytes_verified", old.id)])
        self.assertEqual(sorted(world.opened.execute(select(ManagedAssetOrigin.kind)).scalars()), ["phone", "takeout"])
        self.assertFalse(pixel_source.returns_home(world.opened, old.id, export.account_id))

    def test_each_refusal_leaves_the_hold_pending_and_records_nothing(self):
        world, _, found, hold = self.held()
        self.refused(world, hold, resolution.Refused, resolution.NEEDS_ASSET)
        self.refused(world, hold, resolution.Refused, resolution.NEEDS_ASSET, asset="")
        self.refused(world, hold, resolution.Refused, resolution.NOT_A_CANDIDATE, asset="00000000-0000-4000-8000-000000000000")
        self.refused(world, hold, resolution.Refused, resolution.NO_ASSET, "upload", asset=found.id)
        self.refused(world, hold, resolution.Refused, "the action must be one of upload, represented, undo", "leave")
        found.trashed = True
        self.refused(world, hold, resolution.Conflict, resolution.GONE, asset=found.id)
        found.trashed = False
        found.data = b"changed bytes"
        self.refused(world, hold, resolution.Conflict, resolution.CHANGED, asset=found.id)
        self.assertEqual(world.opened.execute(select(ManagedAssetVersion)).first(), None)
        self.assertEqual(self.posts(world), [])

    def test_a_candidate_that_was_trashed_or_external_when_the_hold_was_made_can_only_be_uploaded(self):
        for changes in ({"trashed": True}, {"library_id": "lib-x"}):
            with self.subTest(changes=changes):
                world, _, found, hold = self.held(**changes)
                self.refused(world, hold, resolution.Conflict, resolution.NOT_MANAGED, asset=found.id)
                self.assertEqual(self.act(world, hold, "upload").status, "upload")

    def test_a_crowded_window_has_no_candidates_so_only_upload_resolves_it(self):
        world, _, found, hold = self.held()
        hold.candidates = same_shot.snapshot(same_shot.Verdict((), True, False))
        world.opened.commit()
        self.refused(world, hold, resolution.Conflict, resolution.CROWDED, asset=found.id)
        self.assertEqual(self.act(world, hold, "upload").status, "upload")

    def test_an_unreachable_immich_changes_nothing(self):
        world, _, found, hold = self.held()
        with mock.patch.object(immich.ManagedReader, "check_access", side_effect=immich.ImmichFailed("down")):
            self.refused(world, hold, resolution.Unavailable, resolution.UNAVAILABLE, asset=found.id)

    def test_a_hold_that_was_released_while_immich_was_read_is_not_recorded_as_already_in_immich(self):
        world, _, found, hold = self.held()
        released = world.opened.get(ManagedImportReceipt, hold.receipt_id)

        def release(*args, **kwargs):
            released.send_attempts = 1
            world.opened.commit()
            return mock.DEFAULT

        with mock.patch.object(resolution, "read_back", wraps=resolution.read_back, side_effect=release):
            self.refused(world, hold, resolution.Conflict, resolution.NOT_HELD, asset=found.id)

    def test_undo_removes_the_origin_and_keeps_the_version_and_a_later_decision_can_be_made(self):
        world, _, found, hold = self.held()
        self.act(world, hold, "represented", found.id)
        done = self.act(world, hold, "undo", at=LATER + 1)
        self.assertEqual((done.status, done.asset_id, done.resolved_at), ("pending", None, None))
        self.assertEqual((world.opened.execute(select(ManagedAssetOrigin)).first(), len(world.opened.execute(select(ManagedAssetVersion)).all())), (None, 1))
        self.assertEqual(world.receipts(), {"IMG_5001.JPG": ("prepared", None, "same_shot")})
        self.assertEqual(self.act(world, hold, "upload", at=LATER + 2).status, "upload")

    def test_undo_is_refused_while_a_backfill_mapping_depends_on_the_version(self):
        world, _, found, hold = self.held()
        self.act(world, hold, "represented", found.id)
        version = world.opened.execute(select(ManagedAssetVersion)).scalar_one()
        item = MediaItem(person_id=world.ids["jo"], path="jo/originals/a.jpg", sha1="b" * 40, bytes=10, source="phone", indexed_at=NOW)
        world.opened.add(item)
        world.opened.flush()
        world.opened.add(ManagedBackfillItem(person_id=world.ids["jo"], item_id=item.id, outcome="mapped", version_id=version.id, checked_at=NOW, since=NOW))
        world.opened.commit()
        with self.assertRaises(resolution.Conflict) as raised:
            self.act(world, hold, "undo", at=LATER + 1)
        self.assertEqual(str(raised.exception), resolution.DEPENDED)
        world.opened.rollback()
        self.assertEqual((holds(world)[0].status, len(world.opened.execute(select(ManagedAssetOrigin)).all())), ("represented", 1))

    def test_a_version_recorded_with_other_bytes_refuses_and_a_damaged_hold_lists_without_candidates(self):
        world, _, found, hold = self.held()
        world.opened.add(ManagedAssetVersion(person_id=world.ids["jo"], owner_id=OWNER, asset_id=found.id, sha1=same_shot_sha(found), bytes=1, kind="IMAGE",
                                             state="bytes_verified", metadata_snapshot="{}", seen_at=NOW, bytes_verified_at=NOW))
        world.opened.commit()
        self.refused(world, hold, resolution.Conflict, resolution.CHANGED, asset=found.id)
        hold.candidates = "not json"
        world.opened.get(ManagedImportReceipt, hold.receipt_id).expected_metadata = " { } "
        world.opened.commit()
        (row,) = resolution.listing(world.opened, world.jo())
        self.assertEqual((row["candidates"], row["taken"], row["edited"]), ([], None, False))
        self.assertEqual(resolution.lines([row], "http://immich.test"), [f"hold {hold.id}: taken at an unknown time"])
        self.refused(world, hold, resolution.Refused, resolution.NOT_A_CANDIDATE, asset=found.id)


def same_shot_sha(found):
    import hashlib

    return hashlib.sha1(found.data, usedforsecurity=False).hexdigest()


class LiveUploadTests(unittest.TestCase):
    def test_undo_is_refused_once_either_half_of_a_released_live_photo_was_claimed_and_allowed_before(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))

        def held(opened, reader, intent, receipt_id, entry, name, now):
            return same_shot.make_hold(opened, intent, receipt_id, entry.decision_id, same_shot.Verdict((), True, False), now)
        with mock.patch.object(same_shot, "gate", held), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        (hold,) = holds(world)
        person = world.jo()
        resolution.act(world.opened, world.settings, None, person, hold.id, "upload", None, None, LATER)
        video = world.opened.execute(select(ManagedImportReceipt).where(ManagedImportReceipt.id != hold.receipt_id)).scalars().all()
        self.assertEqual(len(video), 1)
        video[0].send_attempts = 1
        world.opened.commit()
        with self.assertRaises(resolution.Conflict) as raised:
            resolution.act(world.opened, world.settings, None, person, hold.id, "undo", None, None, LATER + 1)
        self.assertEqual(str(raised.exception), resolution.SENT)
        world.opened.rollback()
        video[0].send_attempts = 0
        world.opened.commit()
        self.assertEqual(resolution.act(world.opened, world.settings, None, person, hold.id, "undo", None, None, LATER + 2).status, "pending")

    def test_a_released_live_photo_is_sent_as_one_compound_from_an_imported_export_and_closes_its_alert(self):
        world = LiveWorld(self)
        export = world.create(live, parts=1)
        world.ingest_all(export, (1,))

        def held(opened, reader, intent, receipt_id, entry, name, now):
            return same_shot.make_hold(opened, intent, receipt_id, entry.decision_id, same_shot.Verdict((), True, False), now)
        with mock.patch.object(same_shot, "gate", held), mock.patch.object(managed_ingest.time, "sleep"):
            world.finish(export)
        (hold,) = holds(world)
        self.assertEqual((world.uploads(), world.status(export)), (0, ("imported", "done")))
        resolution.act(world.opened, world.settings, world.tools.immich, world.jo(), hold.id, "upload", None, None, LATER)
        self.assertIsNone(world.compound().committed_at)
        with mock.patch.object(managed_ingest.time, "sleep"), mock.patch.object(immich.ManagedReader, "upload", side_effect=immich.ImmichFailed("down")):
            finish_export.finish_ready(world.opened, world.settings, LATER + DAY + 5, world.tools)
        self.assertIsNone(world.compound().committed_at)
        self.assertIn("could not complete the upload", world.alert(f"managed:same-shot-upload:{hold.id}").message)
        with mock.patch.object(managed_ingest.time, "sleep"):
            finish_export.finish_ready(world.opened, world.settings, LATER + DAY + 6 + managed_upload.LEASE_SECONDS, world.tools)
        self.assertEqual((world.uploads() >= 2, world.compound().committed_at is not None), (True, True))
        self.assertEqual(sorted(asset.name for asset in world.fake.state.assets), ["IMG_3001.HEIC", "IMG_3001.MOV"])
        self.assertIsNotNone(world.alert(f"managed:same-shot-upload:{hold.id}").closed)


class ListingTests(Held, unittest.TestCase):
    def test_list_shows_ids_flags_and_links_and_never_a_name_a_place_or_a_description(self):
        world, _, found, hold = self.held()
        with no_leaks(self, KEY, SECRET, NAME, "IMG_5001.JPG", world.fake.url):
            rows = resolution.listing(world.opened, world.jo())
            text = "\n".join(resolution.lines(rows, "http://immich.test"))
        self.assertEqual([row["id"] for row in rows], [hold.id])
        self.assertIn(f"hold {hold.id}: taken 2017-07-14T", text)
        self.assertIn(f"candidate {found.id} (managed; same camera no, same size no, Takeout has more pixels no): http://immich.test/photos/{found.id}", text)
        self.assertEqual(resolution.lines([], "http://immich.test"), ["no same-shot holds are waiting"])
        self.act(world, hold, "upload")
        self.assertEqual(resolution.listing(world.opened, world.jo()), [])
        self.assertEqual(resolution.listing(world.opened, world.opened.get(type(world.jo()), world.ids["sam"])), [])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.url = own_database_url(self)
        self.engine = db.make_engine(self.url)
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        self.environment = {"DARKROOM_PHOTOS_ROOT": "/tmp", "DARKROOM_DATA_DIR": "/tmp", "DATABASE_URL": self.engine.url.render_as_string(hide_password=False)}

    def same_shot(self, *argv):
        with mock.patch.dict(os.environ, self.environment, clear=True), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = run.main(["same-shot", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_a_missing_person_and_a_hold_that_is_not_the_persons_are_refused_with_fixed_texts(self):
        code, out, err = self.same_shot("list", "--person", "jo")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no person called jo", err)
        with db.session(self.engine) as opened:
            from darkroom.storage.models import Person

            opened.add(Person(name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0))
        self.assertEqual(self.same_shot("list", "--person", "jo")[:2], (0, "no same-shot holds are waiting\n"))
        code, out, err = self.same_shot("resolve", "--person", "jo", "--hold", "7", "--action", "upload")
        self.assertEqual((code, out, err), (1, "", "same-shot: person jo has no hold 7; nothing changed\n"))

    def test_a_refusal_prints_its_fixed_reason_and_changes_nothing(self):
        with db.session(self.engine) as opened:
            from darkroom.storage.models import Person

            opened.add(Person(name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0))
        with mock.patch.object(run.same_shot_resolution, "act", side_effect=resolution.Conflict(resolution.NOT_PENDING)):
            code, out, err = self.same_shot("resolve", "--person", "jo", "--hold", "7", "--action", "upload")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"same-shot upload: {resolution.NOT_PENDING}; nothing changed\n", err)

    def test_the_command_is_listed_and_the_table_check_names_the_migration(self):
        with mock.patch.object(run, "inspect") as inspected:
            inspected.return_value.has_table.return_value = False
            code, out, err = self.same_shot("list", "--person", "jo")
        self.assertEqual((code, out, err), (1, "", run.SAME_SHOT_NOT_MIGRATED + "\n"))


if __name__ == "__main__":
    unittest.main()
