import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select, update
from sqlalchemy.orm import Session

import run
from darkroom import config
from darkroom.domain import immich_reconcile as reconcile
from darkroom.domain import legacy, managed_switch, takeout_copies
from darkroom.domain.ingest import part_ingest
from darkroom.domain.ingest import run as ingest_run
from darkroom.providers import immich
from darkroom.storage import db, secretbox
from darkroom.storage.models import Alert, DuplicateRemoval, Event, Export, ExportPart, GoogleAccount, ManagedImportIntent, MediaItem, Person, PixelCopy
from tests.fakes.immich import FakeAsset
from tests.import_world import NOW, tree
from tests.privacy import no_leaks
from tests.shared import add_person
from tests.test_immich_reconcile import ReconcileCase
from tests.test_legacy import MOMENT, LegacyWorld, listing
from tests.test_managed_ingest import KEY, SECRET, ManagedWorld
from tests.test_managed_switch import photo_in

PAUSED = ("pausing", "on", "stopped")


def flip_elsewhere(engine, person_id, state, since=NOW):
    with Session(engine) as other:
        other.execute(update(Person).where(Person.id == person_id).values(managed_imports=state, managed_imports_since=since))
        other.commit()


class ReconcileWorld(ReconcileCase):
    def set_state(self, state, since=NOW):
        self.jo.managed_imports, self.jo.managed_imports_since = state, since
        self.world.opened.commit()

    def alerts(self):
        return sorted(row.key for row in self.world.opened.execute(select(Alert)).scalars())

    def paused_events(self):
        return list(self.world.opened.execute(select(Event.detail).where(Event.type == "immich_reconcile_paused", Event.subject == "jo")).scalars())

    def originals(self):
        return sorted(name for name in tree(self.world.root) if name.startswith("jo/originals/") and not name.endswith(".xmp"))


class ReconcilePausedTests(ReconcileWorld):
    def test_a_run_after_the_switch_began_lists_nothing_writes_nothing_and_queues_nothing(self):
        self.photo()
        alerts = self.alerts()
        for number, state in enumerate(PAUSED):
            with self.subTest(state=state):
                self.set_state(state, NOW + number)
                before, requests = tree(self.world.root), len(self.world.fake.requests())
                report = self.run_it(queue=reconcile.PixelFolderQueue())
                self.assertEqual((report.paused, report.listed, report.stored, report.queued, report.failed), (True, 0, 0, 0, 0))
                self.assertEqual((tree(self.world.root), len(self.world.fake.requests())), (before, requests))
                self.assertEqual((self.rows(), self.copies(), self.alerts(), self.jo.immich_reconciled_until), ({}, [], alerts, None))
                self.assertEqual(self.world.opened.execute(select(Event).where(Event.type.in_(("immich_reconciled", "immich_reconcile_failed")))).all(), [])

    def test_a_run_with_the_switch_off_still_runs(self):
        self.photo()
        self.set_state(None)
        report = self.run_it()
        self.assertEqual((report.paused, report.stored), (False, 1))

    def test_the_pause_is_noted_once_per_change_of_state(self):
        self.set_state("pausing", NOW)
        for later in range(3):
            self.run_it(at=NOW + 1 + later)
        self.assertEqual(self.paused_events(), [managed_switch.RECONCILE_PAUSED])
        self.set_state("on", NOW + 60)
        self.run_it(at=NOW + 120)
        self.run_it(at=NOW + 180)
        self.assertEqual(len(self.paused_events()), 2)

    def test_a_removal_already_begun_is_finished_while_paused_and_nothing_else_is_removed(self):
        takeout = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.path.like("jo/google-only/%IMG_2001%"))).scalar_one()
        self.world.fake.add_asset(FakeAsset(id="dup", owner_id="u-jo", data=(self.world.root / takeout.path).read_bytes(), name="IMG_9001.JPG"))
        with mock.patch.object(takeout_copies, "finish"):
            self.run_it()
        (pending,) = self.world.opened.execute(select(DuplicateRemoval)).scalars()
        self.assertEqual(pending.state, "pending")
        kept = self.originals()
        self.set_state("pausing")
        requests = len(self.world.fake.requests())
        report = self.run_it(at=NOW + 3600)
        self.assertEqual((report.paused, report.duplicates_removed), (True, 1))
        self.assertEqual(len(self.world.fake.requests()), requests)
        self.assertFalse((self.world.root / takeout.path).exists())
        self.assertEqual(self.originals(), kept)
        self.assertEqual(self.world.opened.execute(select(DuplicateRemoval.state)).scalar_one(), "removed")

    def test_a_matched_pair_nobody_had_begun_removing_is_left_alone(self):
        takeout = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.path.like("jo/google-only/%IMG_2001%"))).scalar_one()
        self.world.fake.add_asset(FakeAsset(id="dup", owner_id="u-jo", data=(self.world.root / takeout.path).read_bytes(), name="IMG_9001.JPG"))
        with mock.patch.object(takeout_copies, "resolve"):
            self.run_it()
        self.assertEqual(self.world.opened.execute(select(DuplicateRemoval)).all(), [])
        self.set_state("pausing")
        before = tree(self.world.root)
        report = self.run_it(at=NOW + 3600)
        self.assertEqual((report.paused, report.duplicates_removed, report.duplicates_refused, report.duplicates_failed), (True, 0, 0, 0))
        self.assertEqual(tree(self.world.root), before)
        self.assertEqual(self.world.opened.execute(select(DuplicateRemoval)).all(), [])

    def test_a_run_that_holds_its_lock_when_the_switch_begins_completes(self):
        self.photo()
        real = immich.Client.search_stable

        def listing_then_pausing(client, *args, **kwargs):
            flip_elsewhere(self.world.engine, self.jo.id, "pausing")
            return real(client, *args, **kwargs)

        with mock.patch.object(immich.Client, "search_stable", listing_then_pausing):
            report = self.run_it()
        self.assertEqual((report.paused, report.stored), (False, 1))
        self.assertTrue(self.run_it(at=NOW + 3600).paused)

    def test_the_state_is_read_after_the_lock_is_taken(self):
        self.photo()
        real = reconcile.person_lock

        @contextlib.contextmanager
        def flipping(settings, person):
            flip_elsewhere(self.world.engine, person.id, "pausing")
            with real(settings, person):
                yield

        before, requests = tree(self.world.root), len(self.world.fake.requests())
        with mock.patch.object(reconcile, "person_lock", flipping):
            report = self.run_it()
        self.assertEqual((report.paused, report.stored, tree(self.world.root), len(self.world.fake.requests())), (True, 0, before, requests))

    def test_reconcile_all_skips_nobody_and_opens_no_failure_alert_while_paused(self):
        self.photo()
        self.set_state("pausing")
        alerts = self.alerts()
        done = reconcile.reconcile_all(self.world.opened, self.settings, NOW)
        self.assertEqual({slug: report.paused for slug, report in done.items()}, {"jo": True})
        self.assertEqual(self.alerts(), alerts)

    def test_nothing_private_reaches_a_log_event_or_alert(self):
        self.photo(name="CANARY_PHOTO_NAME.JPG")
        self.set_state("pausing")
        with no_leaks(self, "CANARY_PHOTO_NAME", SECRET, self.world.fake.url, database=self.world.engine):
            self.run_it()


class LegacyRefusedTests(unittest.TestCase):
    def setUp(self):
        self.home = LegacyWorld(self)
        self.world = self.home.world
        self.home.imported()
        self.home.put("fresh.jpg", self.world.scratch_jpeg("fresh.jpg", taken="2015:01:01 10:00:00", offset="+00:00", colour=(1, 2, 3)))
        self.home.put("moment.jpg", self.world.scratch_jpeg("moment.jpg", colour=(11, 119, 31), **MOMENT))
        self.home.configure()

    def set_state(self, state):
        self.home.jo.managed_imports, self.home.jo.managed_imports_since = state, NOW
        self.world.opened.commit()

    def legacy_events(self):
        return self.world.opened.execute(select(Event.id).where(Event.type == "legacy_imported")).all()

    def test_an_import_after_the_switch_began_is_refused_and_writes_nothing(self):
        for state in PAUSED:
            with self.subTest(state=state):
                self.set_state(state)
                before, source = tree(self.world.root), listing(self.home.source)
                with self.assertRaises(managed_switch.Refused) as refused:
                    self.home.run(queue=legacy.PixelFolderQueue())
                self.assertEqual(str(refused.exception), managed_switch.REFUSED)
                self.assertEqual((tree(self.world.root), listing(self.home.source), self.home.rows(), self.legacy_events()), (before, source, {}, []))
                self.assertFalse((self.world.root / "jo" / "originals" / "legacy").exists())
                self.assertEqual(self.world.opened.execute(select(PixelCopy)).all(), [])

    def test_the_switch_off_still_imports(self):
        self.set_state(None)
        self.assertGreater(self.home.run().new, 0)

    def test_the_state_is_read_after_the_lock_is_taken(self):
        real = legacy.person_lock

        @contextlib.contextmanager
        def flipping(settings, person):
            flip_elsewhere(self.world.engine, person.id, "pausing")
            with real(settings, person):
                yield

        before = tree(self.world.root)
        with mock.patch.object(legacy, "person_lock", flipping), self.assertRaises(managed_switch.Refused):
            self.home.run()
        self.assertEqual((tree(self.world.root), self.home.rows(), self.legacy_events()), (before, {}, []))

    def test_an_import_that_holds_its_lock_when_the_switch_begins_finishes(self):
        real = legacy.files_of

        def listing_then_pausing(root):
            flip_elsewhere(self.world.engine, self.home.jo.id, "pausing")
            return real(root)

        with mock.patch.object(legacy, "files_of", listing_then_pausing):
            report = self.home.run()
        self.assertGreater(report.new, 0)
        with self.assertRaises(managed_switch.Refused):
            self.home.run(at=NOW + 3600)

    def test_the_refusal_names_no_one(self):
        self.set_state("pausing")
        with no_leaks(self, "fresh.jpg", "moment.jpg", database=self.world.engine), self.assertRaises(managed_switch.Refused):
            self.home.run()


class FreshExportWaitsTests(unittest.TestCase):
    def intents(self, world):
        world.opened.expire_all()
        return world.opened.execute(select(ManagedImportIntent)).scalars().all()

    def states(self, world):
        world.opened.expire_all()
        return [row.ingest_state for row in world.opened.execute(select(ExportPart).order_by(ExportPart.number)).scalars()]

    def alerts(self, world):
        world.opened.expire_all()
        return [(row.key, row.kind, row.closed is None) for row in world.opened.execute(select(Alert).order_by(Alert.id)).scalars()]

    def set_state(self, world, state, since=NOW):
        flip_elsewhere(world.engine, world.ids["jo"], state, since)
        world.opened.expire_all()

    def test_a_fresh_export_waits_while_the_switch_is_being_made(self):
        world = ManagedWorld(self, state="pausing")
        export = world.create(photo_in(1), parts=2)
        before = tree(world.root)
        with no_leaks(self, KEY, SECRET, "IMG_7001.JPG", world.fake.url, database=world.engine):
            with self.assertRaises(ingest_run.NotReady):
                world.ingest(export, 1)
            self.assertEqual(part_ingest.run_queued(world.opened, world.settings, NOW), [])
        self.assertEqual((self.states(world), self.intents(world), world.google_only(), tree(world.root)), (["queued", "queued"], [], set(), before))
        self.assertEqual((world.status(export), self.alerts(world)), (("downloading", None), []))
        self.assertEqual(world.opened.execute(select(Event).where(Event.type.in_(("export_part_failed", "export_part_refused")))).all(), [])
        self.set_state(world, "on")
        world.ingest(export, 1)
        self.assertEqual([(row.export_id, row.kind) for row in self.intents(world)], [(export.id, "import")])

    def test_the_switch_coming_on_between_the_refresh_and_the_route_decision_is_seen(self):
        world = ManagedWorld(self, state=None)
        export = world.create(photo_in(1), parts=2)
        real = managed_switch.begin

        def begin_after_the_request(*args, **kwargs):
            self.set_state(world, "pausing")
            return real(*args, **kwargs)

        before = tree(world.root)
        with mock.patch.object(managed_switch, "begin", begin_after_the_request), self.assertRaises(ingest_run.NotReady):
            world.ingest(export, 1)
        self.assertEqual((self.states(world), self.intents(world), world.google_only(), tree(world.root)), (["queued", "queued"], [], set(), before))
        self.set_state(world, "on")
        world.ingest(export, 1)
        self.assertEqual(len(self.intents(world)), 1)

    def test_an_export_begun_the_old_way_finishes_the_old_way_while_pausing(self):
        world = ManagedWorld(self, state=None)
        export = world.create(photo_in(1), parts=2)
        world.ingest(export, 1)
        self.set_state(world, "pausing")
        world.ingest(export, 2)
        self.assertEqual((self.states(world), self.intents(world), len(world.google_only())), (["ingested", "ingested"], [], 1))

    def test_a_stopped_persons_fresh_export_waits_with_one_alert_over_many_ticks(self):
        world = ManagedWorld(self, state="stopped")
        export = world.create(photo_in(1), parts=2)
        before = tree(world.root)
        with no_leaks(self, KEY, SECRET, "IMG_7001.JPG", world.fake.url, database=world.engine):
            for _ in range(3):
                self.assertEqual(part_ingest.run_queued(world.opened, world.settings, NOW), [])
        self.assertEqual((self.states(world), self.intents(world), tree(world.root)), (["queued", "queued"], [], before))
        self.assertEqual(self.alerts(world), [(f"managed:stopped:{export.id}", "managed_stopped", True)])
        message = world.alert(f"managed:stopped:{export.id}").message
        self.assertIn("Immich-managed imports of jo are stopped", message)
        self.assertNotIn("IMG_7001", message)

    def test_an_on_person_without_a_canonical_immich_user_waits_and_nothing_is_placed_the_old_way(self):
        world = ManagedWorld(self)
        export = world.create(photo_in(1), parts=2)
        flip_elsewhere(world.engine, world.ids["jo"], "on")
        with Session(world.engine) as other:
            other.execute(update(Person).where(Person.id == world.ids["jo"]).values(immich_user_id=None))
            other.commit()
        before = tree(world.root)
        for _ in range(2):
            self.assertEqual(part_ingest.run_queued(world.opened, world.settings, NOW), [])
        self.assertEqual((self.states(world), self.intents(world), world.google_only(), tree(world.root)), (["queued", "queued"], [], set(), before))
        self.assertEqual(self.alerts(world), [(f"managed:no_user:{export.id}", "managed_no_user", True)])
        with Session(world.engine) as other:
            other.execute(update(Person).where(Person.id == world.ids["jo"]).values(immich_user_id=world.jo().immich_user_id or "11111111-1111-4111-8111-111111111111"))
            other.commit()
        world.opened.expire_all()
        world.ingest(export, 1)
        self.assertEqual(len(self.intents(world)), 1)
        self.assertEqual(self.alerts(world), [(f"managed:no_user:{export.id}", "managed_no_user", False)])

    def test_an_export_that_waited_for_a_stopped_switch_is_uploaded_once_it_is_on_and_the_alert_closes(self):
        world = ManagedWorld(self, state="stopped")
        export = world.create(world.scenario, parts=3)
        self.assertEqual(part_ingest.run_queued(world.opened, world.settings, NOW), [])
        self.set_state(world, "on", NOW + 60)
        part_ingest.run_queued(world.opened, world.settings, NOW + 120)
        self.assertEqual([(row.export_id, row.kind) for row in self.intents(world)], [(export.id, "import")])
        self.assertEqual(self.alerts(world), [(f"managed:stopped:{export.id}", "managed_stopped", False)])

    def test_an_export_with_an_intent_is_imported_while_stopped(self):
        world = ManagedWorld(self)
        export = world.create(world.scenario, parts=3)
        world.ingest(export, 1)
        self.set_state(world, "stopped")
        world.ingest(export, 2)
        world.ingest(export, 3)
        world.finish(export)
        self.assertEqual(world.status(export), ("imported", "done"))
        self.assertEqual(len(self.intents(world)), 1)
        self.assertTrue(world.assets())
        self.assertEqual((world.google_only(), [key for key, _, _ in self.alerts(world) if key.startswith("managed:stopped")]), (set(), []))


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        values = {"DARKROOM_PHOTOS_ROOT": str(self.base / "photos"), "DARKROOM_DATA_DIR": str(self.base / "data"), "DARKROOM_SECRET_KEY": "k",
                  "DARKROOM_PUBLIC_URL": "https://d.example", "IMMICH_URL": "http://127.0.0.1:9", "IMMICH_ADMIN_KEY": "canary-admin-key"}
        patcher = mock.patch.dict(os.environ, values, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        self.engine = db.make_engine(db.database_url(config.load()))
        self.addCleanup(self.engine.dispose)
        (self.base / "photos").mkdir()
        (self.base / "old").mkdir()
        with db.session(self.engine) as opened:
            self.person_id = add_person(opened, "canaryslugperson", "Canary Personname")
            person = opened.get(Person, self.person_id)
            person.immich_user_id = "11111111-1111-4111-8111-111111111111"
            person.immich_api_key_sealed = secretbox.seal("a-key", "k")
            account = GoogleAccount(email="canary-account@x.test", default_person_id=self.person_id, created=1.0)
            opened.add(account)
            opened.flush()
            opened.add(Export(account_id=account.id, kind="initial", status="imported", owner_person_id=self.person_id))

    def set_state(self, state):
        flip_elsewhere(self.engine, self.person_id, state)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_import_legacy_exits_one_with_the_fixed_reason_and_writes_nothing(self):
        for state in PAUSED:
            with self.subTest(state=state):
                self.set_state(state)
                with db.session(self.engine) as opened:
                    opened.execute(update(Person).where(Person.id == self.person_id).values(legacy_path=str(self.base / "old")))
                before = tree(self.base / "photos")
                code, out, err = self.call("import-legacy", "--person", "canaryslugperson")
                self.assertEqual((code, out, err), (1, "", f"import-legacy: {managed_switch.REFUSED}\n"))
                self.assertEqual(tree(self.base / "photos"), before)

    def test_a_refused_import_legacy_does_not_save_the_folder(self):
        self.set_state("pausing")
        code, _, err = self.call("import-legacy", "--person", "canaryslugperson", "--dir", str(self.base / "old"))
        self.assertEqual((code, err), (1, f"import-legacy: {managed_switch.REFUSED}\n"))
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Person, self.person_id).legacy_path)

    def test_reconcile_immich_exits_one_with_the_fixed_reason_and_writes_nothing(self):
        for state in PAUSED:
            with self.subTest(state=state):
                self.set_state(state)
                before = tree(self.base / "photos")
                code, out, err = self.call("reconcile-immich", "--person", "canaryslugperson")
                self.assertEqual((code, out, err.splitlines()[-1]), (1, "", f"reconcile-immich: canaryslugperson: {managed_switch.RECONCILE_PAUSED}"))
                self.assertEqual(len(err.splitlines()), 2)
                self.assertEqual(tree(self.base / "photos"), before)

    def test_with_the_switch_off_neither_command_is_held_back_by_it(self):
        with db.session(self.engine) as opened:
            opened.execute(update(Person).where(Person.id == self.person_id).values(legacy_path=str(self.base / "old")))
        code, _, err = self.call("import-legacy", "--person", "canaryslugperson")
        self.assertNotIn(managed_switch.REFUSED, err)
        self.assertEqual(code, 0)
        with mock.patch.object(reconcile, "reconcile_person", return_value=reconcile.Report()) as called:
            code, out, err = self.call("reconcile-immich", "--person", "canaryslugperson")
        self.assertEqual((code, err, called.call_count), (0, "", 1))
        self.assertNotIn("paused", out)


if __name__ == "__main__":
    unittest.main()
