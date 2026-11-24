import unittest

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom.domain import managed_switch
from darkroom.domain.ingest import run
from darkroom.storage import db
from darkroom.storage.models import Person
from tests.shared import another_writer_holds_the_lock, impatient_engine, write_elsewhere
from tests.test_managed_switch import WayWorld

STATES = (None, "pausing", "on", "stopped")
RUN, WAIT, REFUSE = "run", "wait", "refuse"
EXPECTED = {
    ("legacy", None): (RUN, REFUSE, REFUSE, REFUSE),
    ("route", "fresh"): (RUN, WAIT, RUN, WAIT),
    ("route", "old"): (RUN, RUN, RUN, RUN),
    ("route", "managed"): (RUN, RUN, RUN, RUN),
    ("ingest", "fresh"): (RUN, WAIT, RUN, WAIT),
    ("ingest", "old"): (RUN, RUN, REFUSE, REFUSE),
    ("ingest", "managed"): (RUN, RUN, RUN, RUN),
    ("whole_import", "fresh"): (RUN, REFUSE, REFUSE, REFUSE),
    ("whole_import", "old"): (RUN, RUN, REFUSE, REFUSE),
    ("whole_import", "managed"): (RUN, REFUSE, REFUSE, REFUSE),
    ("review", "fresh"): (RUN, RUN, REFUSE, REFUSE),
    ("review", "old"): (RUN, RUN, REFUSE, REFUSE),
    ("review", "managed"): (RUN, RUN, RUN, RUN),
    ("cleanup", None): (RUN, WAIT, WAIT, WAIT),
    ("holds", None): (RUN, WAIT, WAIT, WAIT),
    ("sweep", None): (RUN, RUN, RUN, RUN),
    ("part_free", None): (RUN, WAIT, WAIT, WAIT),
}


class HoldWorld(WayWorld):
    def person(self):
        self.opened.expire_all()
        return self.opened.get(Person, self.ids[self.people[0]])

    def set_state(self, state):
        self.person().managed_imports = state
        self.opened.commit()

    def made(self, way):
        export = self.export()
        if way == "old":
            export.stage = "placed"
        if way == "managed":
            self.intent(export)
        self.opened.commit()
        return export

    def hold(self, writer, export=None):
        managed_switch.hold(self.opened, self.photos, self.person(), writer, export)

    def result(self, writer, export=None):
        try:
            self.hold(writer, export)
        except managed_switch.Paused:
            return WAIT
        except managed_switch.Refused:
            return REFUSE
        finally:
            self.opened.rollback()
        return RUN


class TableTests(unittest.TestCase):
    def setUp(self):
        self.world = HoldWorld(self)

    def test_every_writer_in_every_state(self):
        for (writer, way), expected in EXPECTED.items():
            for state, want in zip(STATES, expected, strict=True):
                with self.subTest(writer=writer, way=way, state=state):
                    self.world.set_state(state)
                    export = None if way is None else self.world.made(way)
                    self.assertEqual(self.world.result(writer, export), want)

    def test_the_table_has_a_row_for_every_writer_and_way(self):
        self.assertEqual(set(EXPECTED), set(managed_switch.TABLE))

    def test_a_writer_that_depends_on_the_way_needs_its_export(self):
        for writer in sorted(managed_switch.BY_THE_WAY):
            with self.subTest(writer=writer), self.assertRaises(ValueError):
                self.world.hold(writer)

    def test_a_setting_nobody_knows_waits_for_every_writer_and_nothing_runs(self):
        self.world.set_state("sideways")
        for writer, way in EXPECTED:
            with self.subTest(writer=writer):
                export = None if way is None else self.world.made(way)
                self.assertEqual(self.world.result(writer, export), WAIT)

    def test_a_pause_is_a_not_ready_and_a_refusal_is_not(self):
        self.assertTrue(issubclass(managed_switch.Paused, run.NotReady))
        self.assertFalse(issubclass(managed_switch.Refused, run.NotReady))

    def test_each_reason_is_a_fixed_text_without_a_name_or_a_path(self):
        reasons = {}
        for state, writer in (("pausing", "cleanup"), ("on", "cleanup"), ("stopped", "cleanup"), ("sideways", "sweep")):
            self.world.set_state(state)
            with self.assertRaises(managed_switch.Paused) as raised:
                self.world.hold(writer)
            reasons[state] = str(raised.exception)
            self.world.opened.rollback()
        self.assertEqual(reasons, {"pausing": managed_switch.WAITS["pausing"], "on": managed_switch.WAITS["on"], "stopped": managed_switch.WAITS["stopped"],
                                   "sideways": managed_switch.UNKNOWN_STATE})
        self.world.set_state("on")
        with self.assertRaises(managed_switch.Refused) as refused:
            self.world.hold("legacy")
        self.assertEqual(str(refused.exception), managed_switch.REFUSED)


class PartFreeTests(unittest.TestCase):
    def setUp(self):
        self.world = HoldWorld(self)
        self.world.set_state("pausing")

    def test_an_old_way_export_that_is_not_finished_is_freed_while_pausing(self):
        for status in ("downloaded", "importing", "unpacking"):
            with self.subTest(status=status):
                export = self.world.made("old")
                export.status = status
                self.assertEqual(self.world.result("part_free", export), RUN)

    def test_an_old_way_export_that_is_finished_is_kept_while_pausing(self):
        for status in ("imported", "failed", "expired"):
            with self.subTest(status=status):
                export = self.world.made("old")
                export.status = status
                self.world.opened.commit()
                self.assertEqual(self.world.result("part_free", export), WAIT)

    def test_a_fresh_or_managed_export_is_kept_while_pausing(self):
        for way in ("fresh", "managed"):
            with self.subTest(way=way):
                self.assertEqual(self.world.result("part_free", self.world.made(way)), WAIT)

    def test_an_old_way_export_is_kept_once_on_or_stopped_whatever_its_status(self):
        for state in ("on", "stopped"):
            self.world.set_state(state)
            export = self.world.made("old")
            export.status = "downloaded"
            self.world.opened.commit()
            with self.subTest(state=state):
                self.assertEqual(self.world.result("part_free", export), WAIT)


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.world = HoldWorld(self)
        self.engine = self.world.engine

    def statements(self):
        seen = []
        event.listen(self.engine, "before_cursor_execute", lambda connection, cursor, statement, *rest: seen.append(statement.strip()))
        return seen

    def test_the_claim_write_is_sent_before_the_person_is_read_again(self):
        seen = self.statements()
        export = self.world.made("old")
        person = self.world.person()
        del seen[:]
        managed_switch.hold(self.world.opened, self.world.photos, person, "ingest", export)
        kinds = [entry for entry in seen if "people" in entry]
        self.assertTrue(kinds[0].startswith("UPDATE"), kinds[0])
        self.assertIn("managed_imports=people.managed_imports", kinds[0].replace(" ", ""))
        self.assertTrue(any(entry.startswith("SELECT") for entry in kinds[1:]))

    def test_a_state_committed_after_the_caller_read_is_seen_before_anything_is_decided(self):
        person = self.world.person()
        self.assertIsNone(person.managed_imports)
        self.assertEqual(write_elsewhere(self, self.engine, "UPDATE people SET managed_imports = 'on' WHERE id = :id", id=person.id), "committed")
        with self.assertRaises(managed_switch.Refused):
            managed_switch.hold(self.world.opened, self.world.photos, person, "legacy")
        self.assertEqual(person.managed_imports, "on")

    def test_a_change_to_the_state_waits_for_the_caller_to_finish_its_batch(self):
        self.world.hold("cleanup")
        person_id = self.world.person().id
        self.assertEqual(write_elsewhere(self, self.engine, "UPDATE people SET managed_imports = 'pausing' WHERE id = :id", id=person_id), "locked")
        self.world.opened.commit()
        self.assertEqual(write_elsewhere(self, self.engine, "UPDATE people SET managed_imports = 'pausing' WHERE id = :id", id=person_id), "committed")

    def test_a_writer_that_cannot_get_the_write_lock_fails_busy_and_decides_nothing(self):
        impatient = impatient_engine(self, self.engine, 0.1)
        with another_writer_holds_the_lock(self.engine):
            with Session(impatient, expire_on_commit=False) as opened:
                person = opened.get(Person, self.world.person().id)
                with self.assertRaises(OperationalError) as raised:
                    managed_switch.hold(opened, self.world.photos, person, "cleanup")
                self.assertTrue(db.is_busy(raised.exception))

    def test_a_snapshot_older_than_a_committed_transition_never_reaches_the_file(self):
        victim = self.world.root / "victim.zip"
        victim.write_bytes(b"x")
        person = self.world.person()
        self.world.opened.connection().exec_driver_sql("BEGIN" if self.engine.dialect.name == "sqlite" else "SELECT 1")
        self.assertIsNone(self.world.opened.execute(text("SELECT managed_imports FROM people WHERE id = :id"), {"id": person.id}).scalar())
        self.assertEqual(write_elsewhere(self, self.engine, "UPDATE people SET managed_imports = 'pausing' WHERE id = :id", id=person.id), "committed")
        outcome = []
        try:
            managed_switch.hold(self.world.opened, self.world.photos, person, "cleanup")
            victim.unlink()
        except OperationalError as error:
            outcome.append(("busy", db.is_busy(error)))
        except managed_switch.Paused:
            outcome.append(("paused", True))
        expected = {"sqlite": [("busy", True)], "postgresql": [("paused", True)]}[self.engine.dialect.name]
        self.assertEqual(outcome, expected)
        self.assertTrue(victim.exists())
        self.world.opened.rollback()
        with self.assertRaises(managed_switch.Paused):
            managed_switch.hold(self.world.opened, self.world.photos, self.world.person(), "cleanup")
        self.assertTrue(victim.exists())


if __name__ == "__main__":
    unittest.main()
