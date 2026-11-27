import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run
from darkroom import config
from darkroom.domain import managed_switch, managed_switch_command
from darkroom.storage import db, secretbox
from darkroom.storage.models import Event, Export, GoogleAccount, ManagedImportIntent, Person
from tests.fakes.immich import FakeImmich, FakePerson
from tests.privacy import no_leaks
from tests.shared import add_person

OWNER = "11111111-1111-4111-8111-111111111111"
KEY = "managed-person-key-canary"
SECRET = "k"
GRANTS = ("asset.download", "asset.read", "asset.upload", "server.about", "user.read")


class CommandCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.fake = FakeImmich([FakePerson("jo-managed", OWNER, "lib-managed", KEY)]).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version, self.fake.state.permissions = "v3.4.1", GRANTS
        values = {"DARKROOM_PHOTOS_ROOT": str(self.root / "photos"), "DARKROOM_DATA_DIR": str(self.root / "data"), "DARKROOM_SECRET_KEY": SECRET,
                  "DARKROOM_PUBLIC_URL": "https://d.example", "IMMICH_URL": self.fake.url, "IMMICH_ADMIN_KEY": "admin-key", "DARKROOM_PIXEL_IMMICH_SOURCE": "1"}
        patcher = mock.patch.dict(os.environ, values, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        self.engine = db.make_engine(db.database_url(config.load()))
        self.addCleanup(self.engine.dispose)
        with db.session(self.engine) as opened:
            add_person(opened, "jo")

    def keyed(self, user=OWNER, key=KEY):
        with db.session(self.engine, writing=True) as opened:
            person = opened.query(Person).one()
            person.immich_user_id = user
            person.immich_api_key_sealed = None if key is None else secretbox.seal(key, SECRET)

    def state(self, state, intent=False):
        with db.session(self.engine, writing=True) as opened:
            person = opened.query(Person).one()
            person.managed_imports, person.managed_imports_since = state, 1.0
            if intent:
                opened.flush()
                opened.add(ManagedImportIntent(person_id=person.id, export_id=self.export("imported", opened), owner_id=OWNER, resource_manifest="{}", created_at=1.0, kind="import"))

    def export(self, status, opened=None):
        if opened is None:
            with db.session(self.engine, writing=True) as inside:
                return self.export(status, inside)
        person = opened.query(Person).one()
        account = GoogleAccount(email="jo@example.com", default_person_id=person.id, created=1.0)
        opened.add(account)
        opened.flush()
        found = Export(account_id=account.id, kind="manual", google_id="g", status=status, stage="placed", owner_person_id=person.id)
        opened.add(found)
        opened.flush()
        return found.id

    def now(self):
        with db.session(self.engine) as opened:
            return opened.query(Person).one().managed_imports

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(["managed-imports", *args])
        return code, out.getvalue(), err.getvalue()

    def events(self):
        with db.session(self.engine) as opened:
            return [(row.type, row.detail) for row in opened.query(Event).filter(Event.type.in_(("managed_imports_set", "managed_imports_on"))).order_by(Event.id)]


class OnTests(CommandCase):
    def test_on_is_refused_while_pixel_copies_from_immich_are_off_and_nothing_changes(self):
        self.keyed()
        with mock.patch.dict(os.environ, {"DARKROOM_PIXEL_IMMICH_SOURCE": "0"}):
            code, out, err = self.call("on", "--person", "jo")
        self.assertEqual((code, out, managed_switch.PIXEL_SOURCE_OFF in err, self.events()), (1, "", True, []))

    def test_on_validates_then_switches_at_once_when_nothing_is_running(self):
        self.keyed()
        code, out, err = self.call("on", "--person", "jo")
        self.assertEqual((code, err, self.now()), (0, "", "on"))
        self.assertIn("Immich-managed imports on", out)
        self.assertEqual(self.events(), [("managed_imports_set", "pausing"), ("managed_imports_on", "on")])

    def test_on_stays_pausing_and_says_why_while_an_old_way_export_runs(self):
        self.keyed()
        self.export("downloading")
        code, out, _ = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now()), (0, "pausing"))
        self.assertIn("old way", out)
        self.assertIn("Exports waited for:", out)

    def test_on_is_refused_without_a_user_and_key_and_changes_nothing(self):
        code, out, err = self.call("on", "--person", "jo")
        self.assertEqual((code, out, self.now(), self.events()), (1, "", None, []))
        self.assertIn("nothing was changed", err)

    def test_on_is_refused_for_a_key_that_belongs_to_someone_else(self):
        self.keyed(user="22222222-2222-4222-8222-222222222222")
        code, _, err = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now(), self.events()), (1, None, []))
        self.assertIn("does not belong to the expected person", err)

    def test_on_is_refused_for_a_key_with_the_wrong_grants(self):
        self.keyed()
        self.fake.state.permissions = (*GRANTS, "asset.delete")
        code, _, err = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now()), (1, None))
        self.assertIn("approved import permissions", err)

    def test_on_is_refused_when_immich_is_down(self):
        self.keyed()
        self.fake.stop()
        code, _, err = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now()), (1, None))
        self.assertIn("nothing was changed", err)

    def test_a_key_replaced_while_it_is_checked_is_checked_again(self):
        self.keyed()
        real = managed_switch_command.live_check
        replaced = []

        def check(settings, person, client=None):
            found = real(settings, person, client)
            if not replaced:
                replaced.append(1)
                self.keyed(key="another-key-canary")
            return found

        with mock.patch.object(managed_switch_command, "live_check", side_effect=check):
            code, _, err = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now(), self.events()), (1, None, []))
        self.assertIn("Immich refused the person key", err)

    def test_a_key_that_keeps_changing_is_refused_without_a_change(self):
        self.keyed()
        count = []

        def check(settings, person, client=None):
            count.append(1)
            self.keyed(key=f"another-key-canary-{len(count)}")

        with mock.patch.object(managed_switch_command, "live_check", side_effect=check):
            code, _, err = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now(), self.events()), (1, None, []))
        self.assertIn(managed_switch_command.CHANGED, err)

    def test_imports_turned_off_by_another_command_are_checked_before_they_are_turned_on(self):
        self.keyed()
        self.state("on", intent=True)
        self.fake.state.permissions = (*GRANTS, "asset.delete")
        with db.session(self.engine) as opened:
            person = opened.query(Person).one()
            self.assertEqual(person.managed_imports, "on")
            with db.session(self.engine, writing=True) as other:
                other.query(Person).one().managed_imports = None
            result = managed_switch_command.turn_on(opened, config.load(), person, 5.0)
        self.assertFalse(result.ok)
        self.assertIn("approved import permissions", result.lines[0])
        self.assertEqual((self.now(), self.events()), (None, []))

    def test_on_again_changes_nothing_and_asks_immich_nothing(self):
        self.keyed()
        self.state("on")
        before = len(self.fake.requests())
        code, out, _ = self.call("on", "--person", "jo")
        self.assertEqual((code, len(self.fake.requests()), self.events()), (0, before, []))
        self.assertIn("imports on", out)

    def test_on_from_stopped_validates_and_resumes_without_waiting(self):
        self.keyed()
        self.state("stopped", intent=True)
        code, _, _ = self.call("on", "--person", "jo")
        self.assertEqual((code, self.now()), (0, "on"))
        self.assertEqual(self.events(), [("managed_imports_set", "on")])


class OffTests(CommandCase):
    def test_off_before_anything_went_to_immich_goes_back_to_off(self):
        self.state("pausing")
        code, out, _ = self.call("off", "--person", "jo")
        self.assertEqual((code, self.now()), (0, None))
        self.assertIn("imports off", out)
        self.assertNotIn("Stopped, not off", out)

    def test_off_after_an_import_intent_exists_is_stopped(self):
        self.state("on", intent=True)
        code, out, _ = self.call("off", "--person", "jo")
        self.assertEqual((code, self.now()), (0, "stopped"))
        self.assertIn(managed_switch_command.STOPPED_NOTE, out)

    def test_off_again_from_stopped_stays_stopped(self):
        self.state("stopped", intent=True)
        code, _, _ = self.call("off", "--person", "jo")
        self.assertEqual((code, self.now(), self.events()), (0, "stopped", []))

    def test_a_backfill_intent_does_not_make_off_stopped(self):
        self.state("on")
        with db.session(self.engine, writing=True) as opened:
            opened.add(ManagedImportIntent(person_id=opened.query(Person).one().id, export_id=None, owner_id=OWNER, resource_manifest="{}", created_at=1.0, kind="backfill"))
        self.call("off", "--person", "jo")
        self.assertIsNone(self.now())


class StatusTests(CommandCase):
    def test_each_state_is_described_without_changing_anything(self):
        for state, wanted in (("pausing", "pausing"), ("on", "on"), ("stopped", "stopped")):
            with self.subTest(state=state):
                self.state(state)
                code, out, err = self.call("status", "--person", "jo")
                self.assertEqual((code, err, self.now()), (0, "", state))
                self.assertIn(f"imports {wanted}, for ", out)
                self.assertIn(managed_switch_command.PAUSED[state], out)
        self.assertEqual(self.events(), [])

    def test_off_has_no_paused_writers_and_no_side_effect(self):
        code, out, _ = self.call("status", "--person", "jo")
        self.assertEqual(code, 0)
        self.assertIn(managed_switch_command.PAUSED["off"], out)
        self.assertNotIn(managed_switch_command.SIDE_EFFECT, out)
        self.assertIn("Immich user and key: missing", out)

    def test_pausing_shows_the_side_effect_and_the_waiting_reason_and_on_does_not(self):
        self.state("pausing")
        with db.session(self.engine, writing=True) as opened:
            opened.query(Person).one().managed_switch_waiting = "reconcile"
        _, out, _ = self.call("status", "--person", "jo")
        self.assertIn(managed_switch_command.SIDE_EFFECT, out)
        self.assertIn(managed_switch.WAITING_REASONS["reconcile"], out)
        self.state("on")
        self.assertNotIn(managed_switch_command.SIDE_EFFECT, self.call("status", "--person", "jo")[1])

    def test_pausing_before_the_first_try_and_with_an_unknown_reason(self):
        self.state("pausing")
        self.assertIn("nothing recorded yet", self.call("status", "--person", "jo")[1])
        with db.session(self.engine, writing=True) as opened:
            opened.query(Person).one().managed_switch_waiting = "from_the_future"
        self.assertIn(managed_switch_command.UNKNOWN_WAITING, self.call("status", "--person", "jo")[1])

    def test_failed_old_way_exports_are_listed(self):
        self.state("pausing")
        wanted = managed_switch_command.GIVEN_UP.format(ids=self.export("failed"))
        self.assertIn(wanted, self.call("status", "--person", "jo")[1])

    def test_an_unknown_stored_state_is_reported(self):
        with mock.patch.object(managed_switch, "state_of", return_value=None):
            code, out, _ = self.call("status", "--person", "jo")
        self.assertEqual(code, 0)
        self.assertIn(managed_switch.UNKNOWN_REQUEST, out)

    def test_durations_read_as_days_hours_or_minutes(self):
        self.assertEqual([managed_switch_command.duration(s) for s in (30, 300, 7500, 200000)], ["0 min", "5 min", "2 h 5 min", "2 d 7 h"])


class UsageTests(CommandCase):
    def test_missing_person_flag_or_action_and_unknown_person(self):
        for args in ((), ("on",), ("--person", "jo"), ("maybe", "--person", "jo")):
            with self.subTest(args=args):
                self.assertEqual(self.call(*args)[0], 1)
        code, _, err = self.call("on", "--person", "nobody")
        self.assertEqual((code, err.strip()), (1, "no person called nobody"))

    def test_nothing_private_is_printed_or_stored(self):
        self.keyed()
        with no_leaks(self, KEY, SECRET + "-unused-canary", self.fake.url, database=self.engine):
            for action in ("on", "status", "off"):
                self.call(action, "--person", "jo")
