import ast
import unittest
from pathlib import Path

from sqlalchemy import select

from darkroom.domain.ingest import run
from darkroom.storage.models import DedupeDecision, Export, ExportPart, ImportRun, ManagedImportIntent, Person, TakeoutEntry
from tests.import_world import NOW
from tests.test_managed_ingest import OWNER, ManagedWorld
from tests.test_part_ingest import PartWorld

DOMAIN = Path(__file__).resolve().parent.parent / "darkroom" / "domain"
NAME = "Takeout/Google Photos/Photos from 2019/IMG_7001.JPG"


def photo_in(part):
    def build(builder):
        builder.photo("IMG_7001.JPG", gps=(45.4, 12.3), part=part)
    return build


class WayWorld(PartWorld):
    def export(self, status="downloaded", queue=True):
        return self.create(photo_in(0), parts=2, status=status, queue=queue)

    def way(self, export):
        self.opened.flush()
        self.opened.expire_all()
        return run.way(self.opened, self.photos, self.opened.get(Export, export.id))

    def entry(self, export, **values):
        self.opened.add(TakeoutEntry(export_id=export.id, part_number=1, name=NAME, kind="media", bytes=1, crc32=1, listed_at=NOW, **values))

    def decision(self, export):
        found = DedupeDecision(export_id=export.id, account_id=self.account, source_path=NAME, tier=5, reason="unique", decided_by="auto", decided_at=NOW)
        self.opened.add(found)
        self.opened.flush()
        return found.id

    def intent(self, export):
        self.opened.add(ManagedImportIntent(person_id=self.ids[self.people[0]], export_id=export.id, owner_id=OWNER, resource_manifest="{}", created_at=NOW))


class WayTests(unittest.TestCase):
    def setUp(self):
        self.world = WayWorld(self)

    def test_an_export_nothing_has_touched_is_fresh(self):
        self.assertEqual(self.world.way(self.world.export()), "fresh")

    def test_a_per_part_export_whose_parts_are_only_listed_is_fresh(self):
        self.assertEqual(self.world.way(self.world.export(queue=False)), "fresh")

    def test_an_export_still_downloading_is_fresh(self):
        self.assertEqual(self.world.way(self.world.export(status="downloading")), "fresh")

    def test_a_whole_export_that_is_only_queued_is_fresh(self):
        export = self.world.export()
        export.stage = "queued"
        self.world.opened.add(ImportRun(export_id=export.id, started=NOW, status="queued", stage="queued"))
        self.assertEqual(self.world.way(export), "fresh")

    def test_an_entry_only_listed_is_fresh(self):
        export = self.world.export()
        self.world.entry(export)
        self.assertEqual(self.world.way(export), "fresh")

    def test_every_stage_from_unpacking_on_is_old(self):
        for stage in ("unpack", *run.STAGES[1:]):
            with self.subTest(stage=stage):
                export = self.world.export()
                export.stage = stage
                self.assertEqual(self.world.way(export), "old")

    def test_an_import_run_that_ran_is_old(self):
        for status in ("running", "done", "failed"):
            with self.subTest(status=status):
                export = self.world.export()
                self.world.opened.add(ImportRun(export_id=export.id, started=NOW, status=status))
                self.assertEqual(self.world.way(export), "old")

    def test_a_part_that_began_ingesting_is_old(self):
        for state in ("ingesting", "ingested", "refused", "failed"):
            with self.subTest(state=state):
                export = self.world.export()
                self.world.part(export, 2).ingest_state = state
                self.assertEqual(self.world.way(export), "old")

    def test_an_entry_with_a_decision_an_outcome_or_a_held_copy_is_old(self):
        for values in ({"decision_id": "decision"}, {"outcome": "placed"}, {"outcome": "waiting_export"}, {"held_path": "held/0001.copy"}):
            with self.subTest(values=sorted(values)):
                export = self.world.export()
                if values.get("decision_id") == "decision":
                    values = {"decision_id": self.world.decision(export)}
                self.world.entry(export, **values)
                self.assertEqual(self.world.way(export), "old")

    def test_a_staging_tree_on_disk_is_old(self):
        export = self.world.export()
        self.world.photos.staging(self.world.people[0], f"export-{export.id}").mkdir(parents=True)
        self.assertEqual(self.world.way(export), "old")

    def test_a_staging_name_that_is_a_link_is_not_a_staging_tree(self):
        export = self.world.export()
        staging = self.world.photos.staging(self.world.people[0], f"export-{export.id}")
        staging.parent.mkdir(parents=True, exist_ok=True)
        elsewhere = self.world.root / "elsewhere"
        elsewhere.mkdir()
        staging.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.world.way(export), "fresh")

    def test_an_intent_is_managed_whatever_else_is_there(self):
        export = self.world.export()
        export.stage = "placed"
        self.world.part(export, 1).ingest_state = "ingested"
        self.world.opened.add(ImportRun(export_id=export.id, started=NOW, status="done"))
        self.world.entry(export, outcome="placed")
        self.world.intent(export)
        self.assertEqual(self.world.way(export), "managed")

    def test_a_backfill_intent_does_not_make_an_export_managed(self):
        export = self.world.export()
        self.world.opened.add(ManagedImportIntent(person_id=self.world.ids[self.world.people[0]], export_id=export.id, kind="backfill", owner_id=OWNER,
                                                  resource_manifest="{}", created_at=NOW))
        self.assertEqual(self.world.way(export), "fresh")

    def test_the_route_test_keeps_its_own_name(self):
        export = self.world.export()
        export.status = "importing"
        self.assertTrue(run.staged_whole(self.world.opened, self.world.photos, export))
        self.assertEqual(self.world.way(export), "fresh")


class RouteTests(unittest.TestCase):
    def intents(self, world):
        world.opened.expire_all()
        return world.opened.execute(select(ManagedImportIntent)).scalars().all()

    def test_a_fresh_export_of_a_person_with_managed_imports_on_gets_an_intent(self):
        world = ManagedWorld(self)
        export = world.create(photo_in(1), parts=2)
        world.ingest(export, 1)
        self.assertEqual([(row.export_id, row.kind) for row in self.intents(world)], [(export.id, "import")])

    def test_a_whole_export_that_is_only_queued_is_still_routed_the_managed_way(self):
        world = ManagedWorld(self)
        export = world.create(photo_in(1), parts=2)
        world.opened.get(Export, export.id).stage = "queued"
        world.opened.commit()
        world.ingest(export, 1)
        self.assertEqual(len(self.intents(world)), 1)

    def test_an_export_with_a_refused_part_stays_old_and_is_placed_the_old_way(self):
        world = ManagedWorld(self)
        export = world.create(photo_in(1), parts=2)
        world.part(export, 1).ingest_state = "refused"
        world.opened.commit()
        world.ingest(export, 2)
        self.assertEqual(self.intents(world), [])
        self.assertEqual(len(world.google_only()), 1)

    def test_an_export_with_a_staging_tree_stays_old(self):
        world = ManagedWorld(self)
        export = world.create(photo_in(1), parts=2)
        world.photos.staging(world.people[0], f"export-{export.id}").mkdir(parents=True)
        world.ingest(export, 1)
        self.assertEqual(self.intents(world), [])

    def test_an_export_begun_the_old_way_is_not_switched_when_the_setting_comes_on(self):
        world = ManagedWorld(self, state=None)
        export = world.create(photo_in(1), parts=2)
        world.ingest(export, 1)
        world.opened.get(Person, world.ids["jo"]).managed_imports = "on"
        world.opened.commit()
        world.ingest(export, 2)
        self.assertEqual(self.intents(world), [])
        self.assertEqual(len(world.google_only()), 1)
        self.assertEqual({row.ingest_state for row in world.opened.execute(select(ExportPart)).scalars()}, {"ingested"})


def started_definitions(source):
    return [(node.lineno, node.name) for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("started", "started_whole")]


class OneDefinitionTests(unittest.TestCase):
    def test_no_module_in_the_domain_defines_started_again(self):
        found = [(str(path.relative_to(DOMAIN)), *where) for path in sorted(DOMAIN.rglob("*.py")) for where in started_definitions(path.read_text())]
        self.assertEqual(found, [])

    def test_the_check_finds_either_name_at_any_depth(self):
        source = "def started(opened, export):\n    return True\n\nclass Run:\n    async def started_whole(self):\n        return False\n"
        self.assertEqual(started_definitions(source), [(1, "started"), (5, "started_whole")])
        self.assertEqual(started_definitions("def staged_whole():\n    return True\n"), [])

if __name__ == "__main__":
    unittest.main()
