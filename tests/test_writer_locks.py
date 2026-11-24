import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy.orm import Session

from darkroom.domain import immich_reconcile, legacy, video_times, writer_locks
from darkroom.domain.ingest import run as ingest_run
from darkroom.storage import locks
from darkroom.storage.models import Export, GoogleAccount, Person
from tests.shared import add_person, own_database, settings_from


class Recorder:
    def __init__(self):
        self.events = []

    def lock(self, kind):
        @contextlib.contextmanager
        def entered(settings, found):
            name = found if isinstance(found, int) else found.slug
            self.events.append(("in", kind, name))
            try:
                yield
            finally:
                self.events.append(("out", kind, name))

        return entered


class LockWorld(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = settings_from(DARKROOM_DATA_DIR=str(Path(folder.name) / "data"))
        self.engine = own_database(self)
        self.opened = writer_session(self)
        self.ids = {slug: add_person(self.opened, slug) for slug in ("ann", "bo", "cy")}
        self.opened.commit()
        self.people = [self.opened.get(Person, self.ids[slug]) for slug in ("ann", "bo", "cy")]

    def hold(self, people=None, accounts=(1, 2)):
        with contextlib.ExitStack() as stack:
            writer_locks.hold(stack, self.opened, self.settings, self.people if people is None else people, accounts)
            return [path.name for path in sorted((self.settings.data_dir / "locks").glob("*.lock")) if locks.is_held(path)]

    def held_after_refusal(self, path):
        with locks.exclusive(path):
            with self.assertRaises(writer_locks.Busy):
                self.hold()
            return [found.name for found in sorted((self.settings.data_dir / "locks").glob("*.lock")) if locks.is_held(found)]


def writer_session(test):
    opened = Session(test.engine, expire_on_commit=False)
    test.addCleanup(opened.close)
    return opened


class OrderTests(LockWorld):
    def recorded(self, people, accounts):
        recorder = Recorder()
        with mock.patch.object(ingest_run, "account_lock", recorder.lock("account")), mock.patch.object(legacy, "person_lock", recorder.lock("legacy")), \
                mock.patch.object(immich_reconcile, "person_lock", recorder.lock("reconcile")):
            with contextlib.ExitStack() as stack:
                writer_locks.hold(stack, self.opened, self.settings, people, accounts)
        return recorder.events

    def test_account_locks_ascending_then_each_persons_legacy_and_reconcile_locks(self):
        events = self.recorded(self.people, [7, 3])
        entered = [event[1:] for event in events if event[0] == "in"]
        self.assertEqual(entered, [("account", 3), ("account", 7), ("legacy", "ann"), ("reconcile", "ann"), ("legacy", "bo"), ("reconcile", "bo"),
                                   ("legacy", "cy"), ("reconcile", "cy")])

    def test_people_are_taken_by_ascending_id_whatever_order_they_come_in(self):
        events = self.recorded(list(reversed(self.people)), [1])
        self.assertEqual([event[2] for event in events if event[0] == "in" and event[1] == "legacy"], ["ann", "bo", "cy"])

    def test_an_account_given_twice_is_locked_once(self):
        events = self.recorded(self.people[:1], [4, 4, 2])
        self.assertEqual([event[2] for event in events if event[0] == "in" and event[1] == "account"], [2, 4])

    def test_locks_are_released_in_reverse_order(self):
        events = self.recorded(self.people[:1], [1, 2])
        entered = [event[1:] for event in events if event[0] == "in"]
        left = [event[1:] for event in events if event[0] == "out"]
        self.assertEqual(left, list(reversed(entered)))

    def test_no_account_and_no_person_takes_nothing(self):
        self.assertEqual(self.recorded([], []), [])


class RefusalTests(LockWorld):
    def test_holds_every_lock_while_the_stack_is_open(self):
        with contextlib.ExitStack() as stack:
            writer_locks.hold(stack, self.opened, self.settings, self.people[:1], [1])
            names = sorted(path.name for path in (self.settings.data_dir / "locks").glob("*.lock") if locks.is_held(path))
        self.assertEqual(names, ["immich-reconcile-ann.lock", "import-account-1.lock", "legacy-import-ann.lock"])

    def test_each_kind_of_lock_held_elsewhere_refuses_and_releases_what_was_taken(self):
        locks_dir = self.settings.data_dir / "locks"
        elsewhere = {"an account": ingest_run.lock_file(self.settings, 2), "a legacy import": locks_dir / "legacy-import-bo.lock",
                     "a reconcile": locks_dir / "immich-reconcile-cy.lock"}
        for name, path in elsewhere.items():
            with self.subTest(name):
                self.assertEqual(self.held_after_refusal(path), [path.name])

    def test_an_account_locked_in_this_process_refuses_too(self):
        with ingest_run.account_lock(self.settings, 2):
            with self.assertRaises(writer_locks.Busy):
                self.hold()

    def test_the_refusal_names_what_was_busy_and_not_a_path(self):
        with locks.exclusive(self.settings.data_dir / "locks" / "legacy-import-bo.lock"):
            with self.assertRaises(writer_locks.Busy) as raised:
                self.hold()
        self.assertEqual(str(raised.exception), "a legacy import is already running for this person")

    def test_taking_them_again_after_a_refusal_works(self):
        with locks.exclusive(self.settings.data_dir / "locks" / "immich-reconcile-ann.lock"):
            with self.assertRaises(writer_locks.Busy):
                self.hold()
        self.assertEqual(len(self.hold()), 8)


class AccountsOfTests(LockWorld):
    def test_an_account_serving_the_person_and_an_account_of_an_export_the_person_owns(self):
        serving = GoogleAccount(email="a@x.test", default_person_id=self.ids["ann"], created=1.0)
        other = GoogleAccount(email="b@x.test", default_person_id=self.ids["bo"], created=1.0)
        self.opened.add_all([serving, other])
        self.opened.flush()
        self.opened.add(Export(account_id=other.id, kind="initial", google_id="g", created_at=1.0, ready_at=1.0, expires_at=2.0, part_count=1,
                               status="downloaded", owner_person_id=self.ids["ann"]))
        self.opened.commit()
        self.assertEqual(writer_locks.accounts_of(self.opened, self.people[0]), {serving.id, other.id})
        self.assertEqual(writer_locks.accounts_of(self.opened, self.people[1]), {other.id})
        self.assertEqual(writer_locks.accounts_of(self.opened, self.people[2]), set())

    def test_video_times_no_longer_has_its_own_copy(self):
        self.assertFalse(hasattr(video_times, "hold_locks"))
        self.assertFalse(hasattr(video_times, "accounts_of"))


if __name__ == "__main__":
    unittest.main()
