import contextlib
import unittest
from unittest import mock

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_switch
from darkroom.domain.ingest import cleanup, part_free, run
from darkroom.storage import paths
from darkroom.storage.models import Event, Export, Person
from tests.import_world import NOW
from tests.privacy import no_leaks
from tests.test_cleanup import TOO_SOON, Imported, PerPartImported
from tests.test_part_free import TICK, FreeWorld

PAUSED = ("pausing", "on", "stopped")
FIXED = {state: managed_switch.WAITS[state] for state in PAUSED}


def set_state(world, slug, state, since=NOW):
    world.opened.execute(update(Person).where(Person.id == world.ids[slug]).values(managed_imports=state, managed_imports_since=since))
    world.opened.commit()


def flip_elsewhere(world, slug, state):
    with Session(world.engine) as other:
        other.execute(update(Person).where(Person.id == world.ids[slug]).values(managed_imports=state, managed_imports_since=NOW))
        other.commit()


def kept_events(world, kind="cleanup_kept"):
    world.opened.expire_all()
    return [(row.subject, row.detail) for row in world.opened.execute(select(Event).where(Event.type == kind).order_by(Event.id)).scalars()]


class WholeExportCleanupTests(Imported):
    def test_staging_and_zips_stay_while_the_person_is_pausing_on_or_stopped_and_go_once_off(self):
        for state in PAUSED:
            with self.subTest(state=state):
                set_state(self.world, "jo", state)
                with no_leaks(self, "IMG_1005", "IMG_2001"):
                    report = self.clean()
                self.assertEqual((report.staging, report.parts, report.holds), (0, 0, 0))
                self.assertGreaterEqual(report.kept, 1)
                self.assertTrue(self.staging.is_dir())
                self.assertTrue(all(part.is_file() for part in self.zips))
                self.assertTrue(self.hold.is_file())
                self.assertEqual(self.kept_tree(), self.kept)
        self.assertEqual(kept_events(self.world), [("export 1", FIXED["pausing"])])
        set_state(self.world, "jo", None)
        report = self.clean()
        self.assertEqual((report.staging, report.parts), (1, 2))
        self.assertFalse(self.staging.exists())

    def test_the_switch_that_commits_after_the_candidate_query_stops_the_batch(self):
        real = cleanup.imported_at

        def pausing_after_the_query(opened, export):
            found = real(opened, export)
            flip_elsewhere(self.world, "jo", "pausing")
            return found

        with mock.patch.object(cleanup, "imported_at", side_effect=pausing_after_the_query):
            report = self.clean()
        self.assertEqual((report.staging, report.parts, report.kept), (0, 0, 1))
        self.assertTrue(self.staging.is_dir())
        self.assertTrue(all(part.is_file() for part in self.zips))

    def test_the_switch_that_commits_after_the_proof_is_recorded_stops_the_removal(self):
        real = managed_switch.hold
        calls = []

        def pausing_before_the_second_hold(opened, photos, person, writer, export=None):
            calls.append(writer)
            if len(calls) == 2:
                flip_elsewhere(self.world, "jo", "pausing")
            return real(opened, photos, person, writer, export)

        with mock.patch.object(managed_switch, "hold", side_effect=pausing_before_the_second_hold):
            report = self.clean()
        self.assertEqual((calls[:2], report.staging, report.parts), (["cleanup", "cleanup"], 0, 0))
        self.assertTrue(self.staging.is_dir())
        self.assertTrue(all(part.is_file() for part in self.zips))
        self.world.opened.expire_all()
        set_state(self.world, "jo", None)
        self.assertEqual(self.clean().staging, 1)

    def test_another_persons_switch_does_not_keep_this_persons_export(self):
        for state in PAUSED:
            set_state(self.world, "sam", state)
        report = self.clean()
        self.assertEqual((report.staging, report.parts), (1, 2))

    def test_an_account_whose_import_lock_is_held_is_left_for_the_next_tick(self):
        account_id = self.world.opened.get(Export, self.export_id).account_id
        with run.account_lock(self.world.settings, account_id):
            report = self.clean()
        self.assertEqual((report.staging, report.parts, report.kept), (0, 0, 1))
        self.assertTrue(self.staging.is_dir())
        self.assertEqual(self.clean().staging, 1)

    def test_the_lock_is_taken_before_any_write_transaction_is_open(self):
        real = run.account_lock
        seen = []

        @contextlib.contextmanager
        def watching(settings, account_id):
            seen.append(self.world.opened.in_transaction())
            with real(settings, account_id):
                yield

        with mock.patch.object(run, "account_lock", watching):
            self.clean()
        self.assertEqual(seen, [False])


class ReviewHoldCleanupTests(Imported):
    def resolved_hold(self):
        self.resolve(NOW)
        self.assertTrue(self.hold.is_file())

    def test_a_resolved_hold_stays_while_the_person_is_pausing_on_or_stopped_and_goes_once_off(self):
        self.resolved_hold()
        for state in PAUSED:
            with self.subTest(state=state):
                set_state(self.world, "jo", state)
                report = self.clean()
                self.assertEqual(report.holds, 0)
                self.assertTrue(self.hold.is_file())
                self.assertEqual(self.review().hold_path, "_inbox/jo/review/export-1/IMG_1005.JPG")
        self.assertEqual(kept_events(self.world), [("export 1", FIXED["pausing"]), ("review holds of jo", FIXED["pausing"])])
        set_state(self.world, "jo", None)
        self.assertEqual(self.clean().holds, 1)
        self.assertFalse(self.hold.exists())
        self.assertIsNone(self.review().hold_path)

    def test_the_switch_that_commits_after_the_candidate_query_stops_the_hold_removal(self):
        self.resolved_hold()
        real = cleanup.resolved_holds

        def pausing_after_the_query(opened, cutoff):
            found = real(opened, cutoff)
            flip_elsewhere(self.world, "jo", "pausing")
            return found

        with mock.patch.object(cleanup, "resolved_holds", side_effect=pausing_after_the_query):
            report = cleanup.clean(self.world.opened, self.world.photos, NOW + 400 * 86400.0, self.world.settings)
        self.world.opened.commit()
        self.assertEqual(report.holds, 0)
        self.assertTrue(self.hold.is_file())
        self.assertIsNotNone(self.review().hold_path)

    def test_another_persons_switch_does_not_keep_the_hold(self):
        self.resolved_hold()
        for state in PAUSED:
            set_state(self.world, "sam", state)
        self.assertEqual(self.clean().holds, 1)
        self.assertFalse(self.hold.exists())


class SweepTests(Imported):
    def test_an_interrupted_removal_is_finished_for_a_person_in_every_state(self):
        for state in (None, *PAUSED):
            with self.subTest(state=state):
                set_state(self.world, "jo", state)
                folder = self.root / "jo" / "google-only" / "2017" / "07"
                photo, aside = folder / "IMG_2001.JPG", folder / f"{paths.DROPPING}0123456789abcdef-IMG_2001.JPG"
                sidecar, sidecar_aside = folder / "IMG_2001.JPG.xmp", folder / f"{paths.DROPPING}0123456789abcdef-IMG_2001.JPG.xmp"
                data, xmp_data = photo.read_bytes(), sidecar.read_bytes()
                photo.rename(aside)
                sidecar.rename(sidecar_aside)
                report = self.clean(TOO_SOON)
                self.assertEqual((report.restored, photo.read_bytes(), sidecar.read_bytes()), (1, data, xmp_data))
                self.assertFalse(aside.exists())


class PerPartCleanupTests(PerPartImported):
    def test_zips_stay_while_the_person_is_pausing_on_or_stopped_and_go_once_off(self):
        for state in PAUSED:
            with self.subTest(state=state):
                set_state(self.world, "jo", state)
                report = self.clean()
                self.assertEqual((report.parts, report.holds), (0, 0))
                self.assertGreaterEqual(report.kept, 1)
                self.assertTrue(self.zips_kept())
                self.assertEqual(self.kept_tree(), self.kept)
        set_state(self.world, "jo", None)
        self.assertEqual(self.clean().parts, 3)

    def test_the_switch_that_commits_after_the_candidate_query_stops_the_zip_removal(self):
        real = cleanup.imported_at

        def pausing_after_the_query(opened, export):
            found = real(opened, export)
            flip_elsewhere(self.world, "jo", "pausing")
            return found

        with mock.patch.object(cleanup, "imported_at", side_effect=pausing_after_the_query):
            report = self.clean()
        self.assertEqual((report.parts, report.kept), (0, 1))
        self.assertTrue(self.zips_kept())


class PartFreeingTests(unittest.TestCase):
    def setUp(self):
        self.world = FreeWorld(self)
        self.export = self.world.made()

    def kept(self, kind="export_part_kept"):
        return self.world.kind_of_events(kind)

    def test_an_unfinished_old_way_export_is_freed_while_pausing(self):
        set_state(self.world, "jo", "pausing")
        report = self.world.free()
        self.assertEqual((report.parts, report.kept), (2, 0))
        self.assertEqual(self.world.states(), [(1, "freed"), (2, "freed")])
        self.assertEqual(self.world.zips(), {})

    def test_a_finished_old_way_export_keeps_its_zips_and_held_copies_while_pausing(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        self.assertEqual(len(held), 1)
        set_state(self.world, "jo", "pausing")
        report = self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((report.held, report.kept), (0, 1))
        self.assertEqual(self.world.held_store(), held)
        self.assertEqual(self.kept("export_held_kept"), ["held copies: managed_switch"])
        set_state(self.world, "jo", None)
        self.assertEqual(self.world.free(at=NOW + 4 * TICK).held, 1)
        self.assertEqual(self.world.held_store(), {})

    def test_on_and_stopped_keep_every_zip_and_held_copy_of_an_old_way_export(self):
        for state in ("on", "stopped"):
            with self.subTest(state=state):
                set_state(self.world, "jo", state)
                zips = self.world.zips()
                with no_leaks(self, "IMG_5001", "IMG_1003", "made-up pier"):
                    report = self.world.free(at=NOW + TICK)
                self.assertEqual((report.parts, report.held, report.kept), (0, 0, 2))
                self.assertEqual((self.world.zips(), self.world.states()), (zips, [(1, None), (2, None)]))
                self.assertEqual(self.world.held_store(), {})
                self.assertEqual(self.kept(), ["part 1: managed_switch", "part 2: managed_switch"])
        set_state(self.world, "jo", None)
        self.assertEqual(self.world.free(at=NOW + 2 * TICK).parts, 2)

    def test_the_held_copies_stay_for_on_and_stopped_after_the_export_is_imported(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        for state in ("on", "stopped"):
            with self.subTest(state=state):
                set_state(self.world, "jo", state)
                report = self.world.free(at=NOW + 3 * TICK)
                self.assertEqual((report.held, self.world.held_store()), (0, held))

    def test_the_switch_that_commits_while_the_zip_is_being_proved_stops_the_removal(self):
        real = part_free.prove_part
        calls = []

        def pausing_between_the_proofs(*args, **kwargs):
            found = real(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                flip_elsewhere(self.world, "jo", "on")
            return found

        zips = self.world.zips()
        with mock.patch.object(part_free, "prove_part", side_effect=pausing_between_the_proofs):
            report = self.world.free()
        self.assertEqual(report.parts, 0)
        self.assertEqual(self.world.zips(), zips)
        self.assertIn("part 1: managed_switch", self.kept())
        self.assertEqual([state for number, state in self.world.states() if number == 1], [None])

    def test_the_switch_that_commits_while_the_held_copies_are_being_proved_stops_the_removal(self):
        self.world.free()
        self.world.finish(self.export, at=NOW + 2 * TICK)
        held = self.world.held_store()
        real = part_free.prove_held
        calls = []

        def switching_after_the_first_proof(*args, **kwargs):
            found = real(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                flip_elsewhere(self.world, "jo", "stopped")
            return found

        with mock.patch.object(part_free, "prove_held", side_effect=switching_after_the_first_proof):
            report = self.world.free(at=NOW + 3 * TICK)
        self.assertEqual((report.held, self.world.held_store()), (0, held))
        self.assertEqual(self.kept("export_held_kept"), ["held copies: managed_switch"])
        self.assertEqual(self.world.entry("Photos from 2019/IMG_1003.JPG").held_state, None)

    def test_a_claimed_zip_is_put_back_even_while_the_switch_keeps_it(self):
        zipped = self.world.zip_of(1)
        real = paths.rename_no_replace

        def claim_then_stop(source, target, folder, into=None):
            real(source, target, folder, into)
            raise RuntimeError("interrupted")

        with mock.patch.object(paths, "rename_no_replace", side_effect=claim_then_stop):
            self.world.free()
        self.assertFalse(zipped.exists())
        set_state(self.world, "jo", "on")
        self.world.free(at=NOW + 2 * TICK)
        self.assertTrue(zipped.is_file())
        self.assertEqual([path.name for path in self.world.folder().glob(f"{paths.FREEING_CLAIM}*")], [])

    def test_another_persons_switch_does_not_keep_this_persons_zips(self):
        for state in PAUSED:
            set_state(self.world, "sam", state)
        self.assertEqual(self.world.free().parts, 2)

