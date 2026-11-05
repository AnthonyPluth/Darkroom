import shutil
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom.domain import accounts, exports, people
from darkroom.domain.ingest import run
from darkroom.storage import db
from darkroom.storage.models import DedupeDecision, Export, GoogleAccount, MediaItem
from tests.import_world import NOW, World
from tests.test_exports_reconcile import ReconcileCase, listed


def google_only(world, slug):
    return sorted(path.name for path in (world.root / slug / "google-only").rglob("*.JPG"))


def change_default(world, slug):
    accounts.update(world.opened, world.account, None, world.ids[slug], NOW)
    world.opened.commit()


def after(target, action):
    def wrapped(*args, **kwargs):
        result = target(*args, **kwargs)
        action()
        return result

    return wrapped


class OwnerIsStoredTests(ReconcileCase):
    def test_an_announced_export_is_owned_by_the_accounts_default_person_when_it_is_recorded(self):
        self.reconcile([listed()])
        with db.session(self.engine) as opened:
            default = opened.get(GoogleAccount, self.account).default_person_id
        self.assertEqual([row.owner_person_id for row in self.rows()], [default])

    def test_a_hand_dropped_export_is_owned_by_the_person_whose_inbox_it_came_from(self):
        world = World(self)
        self.assertEqual(world.hand_dropped().owner_person_id, world.ids["jo"])


class ImportKeepsItsOwnerTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.downloaded(owner="jo")

    def staging(self, slug):
        return self.world.root / "_inbox" / slug / f"export-{self.export.id}" / "staging"

    def assert_all_with_jo(self):
        self.assertEqual(google_only(self.world, "jo"), ["IMG_2001.JPG", "IMG_2002.JPG"])
        self.assertEqual(google_only(self.world, "sam"), [])
        takeout = self.world.opened.execute(select(MediaItem.person_id).where(MediaItem.source == "takeout")).scalars().all()
        self.assertEqual(set(takeout), {self.world.ids["jo"]})

    def test_a_default_person_change_after_the_download_is_finished_does_not_move_the_import(self):
        change_default(self.world, "sam")
        done = self.world.run_import(self.export)
        self.assertEqual((done.status, done.failed), ("done", 0))
        self.assert_all_with_jo()
        self.assertTrue(self.staging("jo").is_dir())
        self.assertFalse(self.staging("sam").exists())

    def test_a_default_person_change_between_unpack_and_place_does_not_move_the_import(self):
        with mock.patch.object(run, "stage_unpack", after(run.stage_unpack, lambda: change_default(self.world, "sam"))):
            done = self.world.run_import(self.export)
        self.assertEqual(self.world.opened.get(GoogleAccount, self.world.account).default_person_id, self.world.ids["sam"])
        self.assertEqual((done.status, done.failed), ("done", 0))
        self.assert_all_with_jo()

    def test_a_default_person_change_between_dedupe_and_place_does_not_move_the_import(self):
        with mock.patch.object(run, "stage_dedupe", after(run.stage_dedupe, lambda: change_default(self.world, "sam"))):
            done = self.world.run_import(self.export)
        self.assertEqual((done.status, done.failed), ("done", 0))
        self.assert_all_with_jo()
        owners = self.world.opened.execute(select(DedupeDecision.person_id).where(DedupeDecision.tier == 5)).scalars().all()
        self.assertEqual(set(owners), {self.world.ids["jo"]})

    def test_prepare_uses_the_owners_staging_when_an_earlier_person_has_one_too(self):
        shutil.move(self.world.root / "_inbox/jo/google-123", self.world.root / "_inbox/sam/google-123")
        self.export.owner_person_id = self.world.ids["sam"]
        self.export.inbox_dir = str(self.world.root / "_inbox/sam/google-123")
        self.world.opened.commit()
        stray = self.staging("jo") / "Takeout" / "Google Photos" / "Photos from 2019"
        stray.mkdir(parents=True)
        (stray / "STRAY.JPG").write_bytes(b"not the export")
        done = self.world.run_import(self.export)
        self.assertEqual((done.status, done.failed), ("done", 0))
        self.assertEqual(google_only(self.world, "sam"), ["IMG_2001.JPG", "IMG_2002.JPG"])
        self.assertEqual(google_only(self.world, "jo"), [])
        self.assertEqual([path.name for path in stray.iterdir()], ["STRAY.JPG"])

    def test_a_missing_owner_staging_is_not_replaced_by_another_persons(self):
        run.stage_unpack(self.world.opened, self.world.photos, self.export, run.latest_run(self.world.opened, self.export.id) or run.current_run(self.world.opened, self.export, NOW), NOW)
        shutil.move(self.staging("jo"), self.staging("sam"))
        self.assertIsNone(run.staging_of(self.world.opened, self.world.photos, self.world.opened.get(Export, self.export.id)))


class OldExportsTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.export = self.world.downloaded()

    def test_an_export_without_an_owner_follows_the_default_person_like_before(self):
        self.assertIsNone(self.export.owner_person_id)
        self.assertEqual(exports.owner_of(self.world.opened, self.export).id, self.world.ids["jo"])
        self.world.opened.get(GoogleAccount, self.world.account).default_person_id = self.world.ids["sam"]
        self.assertEqual(exports.owner_of(self.world.opened, self.export).id, self.world.ids["sam"])

    def test_changing_the_default_person_first_pins_the_exports_that_have_no_owner(self):
        change_default(self.world, "sam")
        self.assertEqual(self.world.opened.get(Export, self.export.id).owner_person_id, self.world.ids["jo"])
        done = self.world.run_import(self.export)
        self.assertEqual((done.status, done.failed), ("done", 0))
        self.assertEqual(google_only(self.world, "jo"), ["IMG_2001.JPG", "IMG_2002.JPG"])
        self.assertEqual(google_only(self.world, "sam"), [])

    def test_saving_the_same_default_person_pins_nothing(self):
        change_default(self.world, "jo")
        self.assertIsNone(self.world.opened.get(Export, self.export.id).owner_person_id)

    def test_the_first_unpack_claims_the_owner_and_a_later_change_cannot_move_it(self):
        with mock.patch.object(run, "stage_unpack", after(run.stage_unpack, lambda: change_default(self.world, "sam"))):
            self.world.run_import(self.export)
        self.assertEqual(self.world.opened.get(Export, self.export.id).owner_person_id, self.world.ids["jo"])
        self.assertEqual(google_only(self.world, "jo"), ["IMG_2001.JPG", "IMG_2002.JPG"])
        self.assertEqual(google_only(self.world, "sam"), [])

    def test_an_export_without_an_owner_still_finds_staging_under_any_person(self):
        self.world.run_import(self.export)
        self.world.opened.get(GoogleAccount, self.world.account).default_person_id = self.world.ids["sam"]
        self.world.opened.get(Export, self.export.id).owner_person_id = None
        self.assertIsNotNone(run.staging_of(self.world.opened, self.world.photos, self.world.opened.get(Export, self.export.id)))


class OwnerBlocksPersonDeletionTests(unittest.TestCase):
    def test_a_person_who_owns_an_export_cannot_be_deleted(self):
        world = World(self)
        world.downloaded(owner="sam")
        self.assertIn("Takeout exports they own", people.blockers(world.opened, world.ids["sam"]))
        self.assertNotIn("Takeout exports they own", people.blockers(world.opened, world.ids["jo"]))


if __name__ == "__main__":
    unittest.main()
