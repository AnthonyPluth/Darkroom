import unittest
import uuid
from unittest import mock

from sqlalchemy import select

from darkroom.domain import alerts, immich_reconcile, legacy, managed_switch, writer_locks
from darkroom.domain.ingest import run
from darkroom.storage import paths
from darkroom.storage.models import Alert, DuplicateRemoval, Event, Export, ExportPart, GoogleAccount, ImportRun, ManagedImportIntent, Person
from tests.import_world import NOW
from tests.privacy import no_leaks
from tests.shared import write_elsewhere
from tests.test_managed_switch import WayWorld
from tests.test_writer_locks import Recorder

DAY = alerts.DAY
CANARY = "canary-holiday-photo-name-0123456789.jpg"
SECOND_ACCOUNT = "second@x.test"


class SwitchWorld(WayWorld):
    def __init__(self, test, **kwargs):
        super().__init__(test, **kwargs)
        for slug in self.people:
            self.give_key(slug)

    def give_key(self, slug, user=True, key=True):
        person = self.person(slug)
        person.immich_user_id = str(uuid.uuid4()) if user else None
        person.immich_api_key_sealed = "sealed" if key else None
        self.opened.commit()

    def person(self, slug="jo"):
        self.opened.expire_all()
        return self.opened.get(Person, self.ids[slug])

    def state(self, slug="jo"):
        found = self.person(slug)
        return found.managed_imports, found.managed_imports_since, found.managed_switch_waiting

    def set_state(self, state, slug="jo", since=NOW):
        found = self.person(slug)
        found.managed_imports, found.managed_imports_since = state, since
        self.opened.commit()

    def request(self, want_on, at=NOW, slug="jo"):
        return managed_switch.request(self.opened, self.person(slug), want_on, at)

    def advance(self, at=NOW, slug="jo"):
        return managed_switch.advance(self.opened, self.settings, self.person(slug), at)

    def events(self, *types):
        self.opened.expire_all()
        found = select(Event).where(Event.type.in_(types)).order_by(Event.id)
        return [(row.type, row.subject, row.detail) for row in self.opened.execute(found).scalars()]

    def alerts(self):
        self.opened.expire_all()
        return [(row.key, row.kind, row.closed is None) for row in self.opened.execute(select(Alert).order_by(Alert.id)).scalars()]

    def import_intent(self, kind="import"):
        export = self.export() if kind == "import" else None
        self.opened.add(ManagedImportIntent(person_id=self.ids["jo"], export_id=None if export is None else export.id, owner_id="owner", resource_manifest="{}",
                                            created_at=NOW, kind=kind))
        self.opened.commit()

    def another_account(self, slug="jo"):
        found = GoogleAccount(email=SECOND_ACCOUNT, default_person_id=self.ids[slug], created=1.0)
        self.opened.add(found)
        self.opened.commit()
        return found.id

    def old_export(self, how="stage", google_id="google-old"):
        export = self.create(lambda builder: builder.photo("IMG_7001.JPG", part=0), parts=2, status="downloaded", google_id=google_id)
        if how == "stage":
            export.stage = "placed"
        elif how == "run":
            self.opened.add(ImportRun(export_id=export.id, started=NOW, status="running", stage="importing"))
        elif how == "part":
            self.opened.execute(select(ExportPart).where(ExportPart.export_id == export.id)).scalars().first().ingest_state = "ingesting"
        self.opened.commit()
        return export

    def leftover(self, slug, prefix, name="a.jpg"):
        folder = self.root / slug / "google-only" / "2020" / "01"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{prefix}{'0123456789abcdef-' if prefix == paths.DROPPING else ''}{name}"
        path.write_bytes(b"x")
        return path

    def pending_removal(self):
        self.opened.add(DuplicateRemoval(person_id=self.ids["jo"], kept_path="k", kept_sha1="a" * 40, removed_path="r", removed_sha1="b" * 40, removed_source="takeout",
                                         rule="identical", tier=1, state="pending", created_at=NOW))
        self.opened.commit()


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.world = SwitchWorld(self)

    def test_every_state_and_request_gives_the_designs_result(self):
        cases = [(None, True, "pausing"), ("pausing", True, "pausing"), ("on", True, "on"), ("stopped", True, "on"),
                 (None, False, None), ("pausing", False, None), ("on", False, None), ("stopped", False, "stopped")]
        for state, want_on, expected in cases:
            with self.subTest(state=state, want_on=want_on):
                self.world.set_state(state, since=None)
                self.assertIsNone(self.world.request(want_on))
                self.assertEqual(self.world.state()[0], expected)

    def test_off_after_something_went_to_immich_is_stopped_never_off(self):
        self.world.import_intent()
        for state in ("pausing", "on"):
            with self.subTest(state=state):
                self.world.set_state(state)
                self.assertIsNone(self.world.request(False))
                self.assertEqual(self.world.state()[0], "stopped")

    def test_a_backfill_intent_does_not_stop_a_person_going_back_to_off(self):
        self.world.import_intent("backfill")
        self.world.set_state("on")
        self.assertIsNone(self.world.request(False))
        self.assertIsNone(self.world.state()[0])

    def test_a_change_records_the_state_and_the_time_and_a_no_op_records_nothing(self):
        self.world.request(True, at=NOW + 5)
        self.assertEqual(self.world.state(), ("pausing", NOW + 5, None))
        self.assertEqual(self.world.events("managed_imports_set"), [("managed_imports_set", "jo", "pausing")])
        self.world.request(True, at=NOW + 9)
        self.assertEqual(self.world.state(), ("pausing", NOW + 5, None))
        self.world.request(False, at=NOW + 11)
        self.world.request(False, at=NOW + 12)
        self.assertEqual(self.world.state(), (None, NOW + 11, None))
        self.assertEqual([detail for _, _, detail in self.world.events("managed_imports_set")], ["pausing", "off"])

    def test_on_needs_a_canonical_immich_user_and_a_sealed_key_and_changes_nothing_without(self):
        for user, key in ((False, True), (True, False), (False, False)):
            with self.subTest(user=user, key=key):
                self.world.give_key("jo", user=user, key=key)
                self.assertEqual(self.world.request(True), managed_switch.NO_KEY)
                self.assertEqual(self.world.state(), (None, None, None))
                self.assertEqual(self.world.events("managed_imports_set"), [])
        self.world.person().immich_user_id = "u-jo"
        self.world.opened.commit()
        self.assertEqual(self.world.request(True), managed_switch.NO_KEY)

    def test_off_and_stopped_to_on_never_need_a_key_to_stay_or_leave(self):
        self.world.set_state("pausing")
        self.world.give_key("jo", user=False, key=False)
        self.assertIsNone(self.world.request(False))
        self.assertIsNone(self.world.state()[0])

    def test_stopped_goes_straight_to_on_without_taking_any_lock(self):
        self.world.set_state("stopped")
        with mock.patch.object(writer_locks, "hold", side_effect=AssertionError("no lock")):
            self.assertIsNone(self.world.request(True))
        self.assertEqual(self.world.state()[0], "on")

    def test_going_off_closes_the_pausing_alert_and_forgets_the_reason(self):
        self.world.request(True, at=NOW)
        with legacy.person_lock(self.world.settings, self.world.person()):
            self.world.advance(at=NOW + DAY + 1)
        self.assertEqual(self.world.alerts(), [(f"managed:pausing:{self.world.ids['jo']}", "managed_pausing", True)])
        self.world.request(False, at=NOW + DAY + 2)
        self.assertEqual(self.world.alerts(), [(f"managed:pausing:{self.world.ids['jo']}", "managed_pausing", False)])
        self.assertEqual(self.world.state()[2], None)


class AdvanceTests(unittest.TestCase):
    def setUp(self):
        self.world = SwitchWorld(self)
        self.world.set_state("pausing")

    def flips(self):
        self.assertIsNone(self.world.advance())
        self.assertEqual(self.world.state()[0], "on")

    def waits(self, reason, exports=()):
        found = self.world.advance()
        self.assertEqual(found, managed_switch.Waiting(reason, exports))
        self.assertEqual(self.world.state()[0], "pausing")
        self.assertEqual(self.world.state()[2], reason)

    def test_a_person_who_is_not_pausing_is_left_alone(self):
        for state in (None, "on", "stopped"):
            with self.subTest(state=state):
                self.world.set_state(state)
                with mock.patch.object(writer_locks, "hold", side_effect=AssertionError("no lock")):
                    self.assertIsNone(self.world.advance())
                self.assertEqual(self.world.state()[0], state)

    def test_nothing_in_flight_flips_to_on_records_it_and_forgets_the_reason(self):
        self.world.person().managed_switch_waiting = "recovery"
        self.world.opened.commit()
        self.assertIsNone(self.world.advance(at=NOW + 7))
        self.assertEqual(self.world.state(), ("on", NOW + 7, None))
        self.assertEqual(self.world.events("managed_imports_on"), [("managed_imports_on", "jo", "on")])

    def test_each_lock_held_in_turn_keeps_it_pausing_with_that_reason_and_it_flips_once_released(self):
        second = self.world.another_account()
        person = self.world.person()
        holders = {"account": lambda: run.account_lock(self.world.settings, self.world.account), "second account": lambda: run.account_lock(self.world.settings, second),
                   "legacy": lambda: legacy.person_lock(self.world.settings, person), "reconcile": lambda: immich_reconcile.person_lock(self.world.settings, person)}
        for name, holder in holders.items():
            with self.subTest(lock=name):
                with holder():
                    found = self.world.advance()
                    self.assertEqual(found.reason, "account" if "account" in name else name)
                    self.assertEqual(self.world.state()[0], "pausing")
                    self.assertEqual(self.world.state()[2], found.reason)
                    self.assertEqual(self.world.events("managed_imports_on"), [])
        self.flips()

    def test_a_lock_held_does_not_stop_a_different_person_switching(self):
        other = self.world.person("sam")
        other.managed_imports = "pausing"
        self.world.opened.commit()
        with legacy.person_lock(self.world.settings, self.world.person()):
            self.assertIsNone(self.world.advance(slug="sam"))
        self.assertEqual(self.world.state("sam")[0], "on")

    def test_an_account_linked_while_the_locks_were_taken_waits_a_tick_and_then_takes_its_lock(self):
        real = writer_locks.hold
        linked = []

        def hold_then_link(*args, **kwargs):
            real(*args, **kwargs)
            if not linked:
                linked.append(self.world.another_account())

        with mock.patch.object(writer_locks, "hold", hold_then_link):
            self.waits("accounts_changed")
            self.assertEqual(self.world.events("managed_imports_on"), [])
            second = []
            with mock.patch.object(run, "account_lock", wraps=run.account_lock) as locked:
                self.flips()
                second = sorted(call.args[1] for call in locked.call_args_list)
        self.assertEqual(second, sorted([self.world.account, linked[0]]))

    def test_the_locks_are_entered_account_ascending_then_legacy_then_reconcile(self):
        second = self.world.another_account()
        recorder = Recorder()
        with (mock.patch.object(run, "account_lock", recorder.lock("account")), mock.patch.object(legacy, "person_lock", recorder.lock("legacy")),
              mock.patch.object(immich_reconcile, "person_lock", recorder.lock("reconcile"))):
            self.flips()
        entered = [event[1:] for event in recorder.events if event[0] == "in"]
        self.assertEqual(entered, [("account", min(self.world.account, second)), ("account", max(self.world.account, second)), ("legacy", "jo"), ("reconcile", "jo")])
        self.assertEqual([event[1:] for event in recorder.events if event[0] == "out"][::-1], entered)

    def test_an_export_that_started_the_old_way_keeps_it_pausing_in_each_shape(self):
        for how in ("stage", "run", "part"):
            with self.subTest(how=how):
                export = self.world.old_export(how, google_id=f"google-{how}")
                self.waits("old_way_export", (export.id,))
                self.world.opened.get(Export, export.id).status = "imported"
                self.world.opened.commit()
        self.flips()

    def test_a_failed_and_an_expired_old_way_export_do_not_hold_it_back(self):
        for status in ("failed", "expired"):
            self.world.old_export("stage", google_id=f"google-{status}").status = status
        self.world.opened.commit()
        self.flips()

    def test_an_export_nothing_has_touched_does_not_hold_it_back(self):
        self.world.export()
        self.flips()

    def test_an_old_way_export_of_a_person_the_account_does_not_serve_is_not_theirs(self):
        export = self.world.old_export("stage")
        other = self.world.person("sam")
        export.owner_person_id = other.id
        self.world.opened.commit()
        self.flips()

    def test_each_interrupted_removal_keeps_it_pausing_and_another_persons_does_not(self):
        self.world.leftover("sam", paths.DROPPING)
        self.world.leftover("sam", paths.PLACING)
        self.flips()
        for how in ("row", "dropping", "placing"):
            with self.subTest(how=how):
                self.world.set_state("pausing")
                if how == "row":
                    self.world.pending_removal()
                else:
                    leftover = self.world.leftover("jo", paths.DROPPING if how == "dropping" else paths.PLACING)
                self.waits("recovery")
                if how == "row":
                    found = self.world.opened.execute(select(DuplicateRemoval)).scalars().one()
                    found.state = "removed"
                    self.world.opened.commit()
                else:
                    leftover.unlink()
                self.flips()

    def test_a_person_who_lost_the_key_or_the_user_while_pausing_keeps_waiting(self):
        for user, key in ((False, True), (True, False)):
            with self.subTest(user=user, key=key):
                self.world.give_key("jo", user=user, key=key)
                self.waits("no_key")
        self.world.give_key("jo")
        self.flips()

    def test_a_request_off_that_commits_before_the_write_lock_wins(self):
        real = writer_locks.hold

        def hold_then_turn_off(*args, **kwargs):
            real(*args, **kwargs)
            self.assertEqual(write_elsewhere(self, self.world.engine, "UPDATE people SET managed_imports = NULL WHERE id = :id", id=self.world.ids["jo"]), "committed")

        with mock.patch.object(writer_locks, "hold", hold_then_turn_off):
            self.assertIsNone(self.world.advance())
        self.assertIsNone(self.world.state()[0])
        self.assertEqual(self.world.events("managed_imports_on"), [])

    def test_an_error_inside_the_flip_releases_the_locks_and_changes_nothing(self):
        with mock.patch.object(managed_switch, "interrupted", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.world.advance()
        self.world.opened.rollback()
        self.assertEqual(self.world.state(), ("pausing", NOW, None))
        self.flips()


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.world = SwitchWorld(self)
        self.world.request(True, at=NOW)
        self.key = f"managed:pausing:{self.world.ids['jo']}"

    def wait(self, at):
        with legacy.person_lock(self.world.settings, self.world.person()):
            self.world.advance(at=at)

    def test_no_alert_opens_before_the_person_has_been_pausing_for_a_day(self):
        self.wait(NOW + DAY - 1)
        self.assertEqual(self.world.alerts(), [])
        self.assertEqual(self.world.state()[2], "legacy")

    def test_one_alert_opens_after_a_day_with_the_reason_and_not_again(self):
        for at in (NOW + DAY, NOW + DAY + 60, NOW + DAY + 120):
            self.wait(at)
        self.assertEqual(self.world.alerts(), [(self.key, "managed_pausing", True)])
        found = self.world.opened.execute(select(Alert)).scalars().one()
        self.assertEqual((found.subject, found.person_id), ("jo", self.world.ids["jo"]))
        self.assertIn(managed_switch.WAITING_REASONS["legacy"], found.message)

    def test_the_flip_closes_the_alert(self):
        self.wait(NOW + DAY)
        self.assertIsNone(self.world.advance(at=NOW + DAY + 5))
        self.assertEqual(self.world.alerts(), [(self.key, "managed_pausing", False)])
        self.assertEqual(self.world.state()[2], None)

    def test_a_person_who_went_off_meanwhile_gets_no_alert_and_no_reason(self):
        real = writer_locks.hold

        def refuse_after_off(*args, **kwargs):
            write_elsewhere(self, self.world.engine, "UPDATE people SET managed_imports = NULL WHERE id = :id", id=self.world.ids["jo"])
            raise writer_locks.Busy("legacy", "busy")

        with mock.patch.object(writer_locks, "hold", refuse_after_off):
            self.world.advance(at=NOW + 2 * DAY)
        self.assertEqual(self.world.alerts(), [])
        self.assertEqual(self.world.state()[2], None)
        self.assertIs(real, writer_locks.hold)


class PrivacyTests(unittest.TestCase):
    def test_no_file_name_or_path_reaches_an_event_an_alert_or_the_log(self):
        world = SwitchWorld(self)
        world.request(True, at=NOW)
        world.leftover("jo", paths.DROPPING, name=CANARY)
        with no_leaks(self, CANARY, str(world.root), database=world.engine):
            world.advance(at=NOW + 2 * DAY)
        self.assertEqual(world.state()[2], "recovery")
        self.assertEqual(len(world.alerts()), 1)


class TickTests(unittest.TestCase):
    def setUp(self):
        self.world = SwitchWorld(self)

    def tick(self):
        from darkroom.server import jobs

        jobs.build(self.world.settings, self.world.engine).run_one("imports", NOW)

    def test_the_import_tick_advances_every_pausing_person_before_importing(self):
        for slug in ("jo", "sam"):
            self.world.set_state("pausing", slug)
        self.tick()
        self.assertEqual([self.world.state(slug)[0] for slug in ("jo", "sam")], ["on", "on"])

    def test_a_person_the_tick_cannot_advance_does_not_stop_the_others_or_the_imports(self):
        for slug in ("jo", "sam"):
            self.world.set_state("pausing", slug)
        real = managed_switch.advance

        def failing_for_jo(opened, settings, person, now):
            if person.slug == "jo":
                raise RuntimeError("boom")
            return real(opened, settings, person, now)

        with mock.patch.object(managed_switch, "advance", failing_for_jo), mock.patch.object(run, "run_waiting") as imports:
            self.tick()
        self.assertEqual([self.world.state(slug)[0] for slug in ("jo", "sam")], ["pausing", "on"])
        imports.assert_called_once()

    def test_a_person_who_is_off_costs_the_tick_nothing(self):
        with mock.patch.object(managed_switch, "advance") as advanced:
            self.tick()
        advanced.assert_not_called()


class ReasonTests(unittest.TestCase):
    def test_every_reason_the_flip_can_give_has_a_fixed_sentence_without_a_name_or_a_path(self):
        for reason in ("reconcile", "legacy", "account", "accounts_changed", "old_way_export", "recovery", "no_key"):
            sentence = managed_switch.Waiting(reason).sentence
            self.assertTrue(sentence)
            self.assertNotIn("/", sentence)
        self.assertEqual(set(managed_switch.WAITING_REASONS), {"reconcile", "legacy", "account", "accounts_changed", "old_way_export", "recovery", "no_key"})


if __name__ == "__main__":
    unittest.main()
