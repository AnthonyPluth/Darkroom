import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from darkroom.domain import accounts, browser_jobs, people
from darkroom.storage import db
from darkroom.storage.models import BrowserJob, GoogleAccount, Person, Pixel, PixelPerson
from darkroom.storage.paths import Photos, ProfileNotRemoved
from tests.shared import add_person, another_writer_holds_the_lock, impatient_engine, on_postgres, own_database, try_elsewhere, write_elsewhere


def add_account(opened, email, default_person):
    account = GoogleAccount(email=email, default_person_id=default_person, created=1.0)
    opened.add(account)
    opened.flush()
    return account.id


def add_pixel(opened, account_id, people):
    pixel = Pixel(name="Pixel", google_account_id=account_id, created=1.0)
    opened.add(pixel)
    opened.flush()
    for person in people:
        opened.add(PixelPerson(pixel_id=pixel.id, person_id=person))
    opened.flush()
    return pixel.id


class ServedPeopleTests(unittest.TestCase):
    def test_an_account_with_no_pixels_serves_its_default_person(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo = add_person(opened, "jo")
            add_person(opened, "sam")
            account = add_account(opened, "jo@x.test", jo)
            self.assertEqual(accounts.served_people(opened, account), {jo})

    def test_a_shared_pixel_adds_its_people(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo, sam, kim = (add_person(opened, slug) for slug in ("jo", "sam", "kim"))
            account = add_account(opened, "family@x.test", jo)
            add_pixel(opened, account, [sam, kim])
            self.assertEqual(accounts.served_people(opened, account), {jo, sam, kim})

    def test_one_person_per_pixel_is_the_plain_case(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo = add_person(opened, "jo")
            account = add_account(opened, "jo@x.test", jo)
            add_pixel(opened, account, [jo])
            self.assertEqual(accounts.served_people(opened, account), {jo})

    def test_accounts_do_not_leak_into_each_other(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo, sam = add_person(opened, "jo"), add_person(opened, "sam")
            a, b = add_account(opened, "a@x.test", jo), add_account(opened, "b@x.test", sam)
            add_pixel(opened, a, [jo])
            self.assertEqual(accounts.served_people(opened, a), {jo})
            self.assertEqual(accounts.served_people(opened, b), {sam})

    def test_an_unknown_account_serves_nobody(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            self.assertEqual(accounts.served_people(opened, 999), set())

    def test_accounts_serving_a_person(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo, sam = add_person(opened, "jo"), add_person(opened, "sam")
            own = add_account(opened, "jo@x.test", jo)
            shared = add_account(opened, "family@x.test", sam)
            add_pixel(opened, shared, [jo])
            self.assertEqual(accounts.accounts_serving(opened, jo), {own, shared})
            self.assertEqual(accounts.accounts_serving(opened, sam), {shared})


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.photos = Photos(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def account_with_job(self, status, finished=None, message=None):
        with db.session(self.engine) as opened:
            name = f"jo{status.replace('_', '')}{finished}"
            account = add_account(opened, f"{name}@x.test", add_person(opened, name))
            job = browser_jobs.enqueue(opened, account, "noop", None, 1.0)
            job.status = status
            job.finished = finished
            job.message = message
        return account

    def refused(self, account, now=2.0, says="browser job that has not finished"):
        with db.session(self.engine) as opened:
            with self.assertRaisesRegex(accounts.InUse, says):
                accounts.delete(opened, self.photos, account, now)
        with db.session(self.engine) as opened:
            self.assertIsNotNone(opened.get(GoogleAccount, account))
            self.assertEqual(opened.query(BrowserJob).filter_by(account_id=account).count(), 1)

    def deleted(self, account, now=2.0):
        with db.session(self.engine) as opened:
            accounts.delete(opened, self.photos, account, now)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(GoogleAccount, account))
            self.assertEqual(opened.query(BrowserJob).filter_by(account_id=account).count(), 0)

    def test_a_running_or_waiting_job_refuses_the_delete_and_changes_nothing(self):
        for status in browser_jobs.OPEN_STATUSES:
            with self.subTest(status=status):
                self.refused(self.account_with_job(status))

    def test_a_job_the_lease_expired_still_refuses_until_the_lease_window_has_passed(self):
        account = self.account_with_job("failed", finished=1000.0, message=browser_jobs.EXPIRED_MESSAGE)
        self.refused(account, now=1000.0 + browser_jobs.LEASE_SECONDS - 1, says="failed when its worker stopped reporting less than 3 minutes ago.*try again in a few minutes")
        self.deleted(account, now=1000.0 + browser_jobs.LEASE_SECONDS + 1)

    def test_the_delete_holds_the_write_lock_before_it_checks_for_browser_jobs(self):
        account = self.account_with_job("queued")
        checked = browser_jobs.holds_profile
        seen = []

        def claim_meanwhile(opened, account_id, now):
            seen.append(write_elsewhere(self, self.engine, "UPDATE browser_jobs SET status = 'running', started = 2.0, heartbeat = 2.0 WHERE account_id = :account", account=account_id))
            return checked(opened, account_id, now)

        with mock.patch.object(browser_jobs, "holds_profile", claim_meanwhile):
            self.deleted(account)
        self.assertEqual(seen, ["locked"])

    def test_a_write_committed_after_the_deletes_first_statement_waits_for_the_delete(self):
        account = self.account_with_job("queued")
        seen = []

        def write_meanwhile(conn, cursor, statement, parameters, context, executemany):
            if not seen:
                seen.append(write_elsewhere(self, self.engine, "UPDATE browser_jobs SET heartbeat = 3.0 WHERE account_id = :account", account=account))

        event.listen(self.engine, "after_cursor_execute", write_meanwhile)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", write_meanwhile)
        with db.session(self.engine) as opened:
            accounts.delete(opened, self.photos, account, 2.0)
        self.assertEqual(seen, ["locked"])
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(GoogleAccount, account))

    def test_a_delete_that_cannot_get_the_write_lock_is_busy_and_changes_nothing(self):
        account = self.account_with_job("done")
        impatient = impatient_engine(self, self.engine, 0.05)
        with another_writer_holds_the_lock(self.engine):
            with db.session(impatient) as opened:
                with self.assertRaisesRegex(accounts.Busy, "try deleting the account again in a moment"):
                    accounts.delete(opened, self.photos, account, 2.0)
        with db.session(self.engine) as opened:
            self.assertIsNotNone(opened.get(GoogleAccount, account))
        with db.session(self.engine) as opened, mock.patch.object(opened, "execute", side_effect=OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(OperationalError):
                accounts.delete(opened, self.photos, account, 2.0)

    def test_finished_and_queued_jobs_do_not_block_the_delete(self):
        for status in ("done", "failed", "queued"):
            with self.subTest(status=status):
                self.deleted(self.account_with_job(status, finished=1000.0, message="TimeoutError"))

    def downloads_of(self, account):
        return self.photos.root / "_inbox" / ".downloads" / str(account)

    def test_the_delete_removes_the_accounts_download_folder_and_no_other(self):
        account = self.account_with_job("done", finished=1000.0)
        other = self.downloads_of(account + 1000)
        for folder in (self.downloads_of(account), other):
            folder.mkdir(parents=True)
            (folder / "leftover.crdownload").write_bytes(b"x")
        self.deleted(account)
        self.assertFalse(self.downloads_of(account).exists())
        self.assertTrue((other / "leftover.crdownload").is_file())

    def test_an_account_without_a_download_folder_is_deleted(self):
        self.deleted(self.account_with_job("done", finished=1000.0))

    def test_a_download_folder_that_is_a_link_is_not_followed_and_fails_the_delete(self):
        account = self.account_with_job("done", finished=1000.0)
        held = self.photos.root / "_inbox" / "jo" / "review" / "export-1"
        held.mkdir(parents=True)
        (held / "IMG_1.JPG").write_bytes(b"held")
        staging = self.photos.root / "_inbox" / ".downloads"
        (held.parent / str(account)).mkdir()
        (held.parent / str(account) / "IMG_2.JPG").write_bytes(b"held")
        for link, target in ((self.downloads_of(account), held), (staging, held.parent)):
            with self.subTest(link=link.name):
                if link != staging:
                    staging.mkdir(parents=True)
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(ProfileNotRemoved, "downloads folder is a link"):
                    with db.session(self.engine) as opened:
                        accounts.delete(opened, self.photos, account, 2.0)
                with db.session(self.engine) as opened:
                    self.assertIsNotNone(opened.get(GoogleAccount, account))
                self.assertEqual((held / "IMG_1.JPG").read_bytes(), b"held")
                self.assertEqual((held.parent / str(account) / "IMG_2.JPG").read_bytes(), b"held")
                link.unlink()
                if link != staging:
                    staging.rmdir()


class CheckThenWriteTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        with db.session(self.engine) as opened:
            self.person = add_person(opened, "jo")

    def test_two_creations_with_case_variants_of_one_email_cannot_both_pass_the_check(self):
        with db.session(self.engine) as first:
            accounts.create(first, "User@example.com", self.person, 1.0)
            second = try_elsewhere(self, self.engine, lambda opened: accounts.create(opened, "user@example.com", self.person, 2.0))
        self.assertEqual(second, "locked")
        with self.assertRaises(accounts.EmailTaken), db.session(self.engine) as again:
            accounts.create(again, "user@example.com", self.person, 3.0)
        with db.session(self.engine) as opened:
            self.assertEqual([each.email for each in opened.query(GoogleAccount)], ["User@example.com"])


if on_postgres():

    class PostgresCheckThenWriteTests(unittest.TestCase):
        def setUp(self):
            self.engine = own_database(self)
            with db.session(self.engine) as opened:
                self.person = add_person(opened, "jo")

        def test_a_second_creation_between_the_first_ones_check_and_write_is_held_off_until_it_commits(self):
            real, outcome = accounts.require_free_email, []

            def second_between_check_and_write(opened, email, except_id=None):
                real(opened, email, except_id)
                if not outcome:
                    outcome.append(try_elsewhere(self, self.engine, lambda other: accounts.create(other, "user@example.com", self.person, 2.0)))

            with mock.patch.object(accounts, "require_free_email", second_between_check_and_write), db.session(self.engine) as first:
                accounts.create(first, "User@example.com", self.person, 1.0)
            self.assertEqual(outcome, ["locked"])
            with db.session(self.engine) as opened:
                self.assertEqual([each.email for each in opened.query(GoogleAccount)], ["User@example.com"])

        def test_a_person_delete_holds_off_an_account_made_for_them_after_its_check(self):
            real, outcome = people.blockers, []

            def account_meanwhile(opened, person_id):
                found = real(opened, person_id)
                outcome.append(try_elsewhere(self, self.engine, lambda other: accounts.create(other, "jo@x.test", person_id, 2.0)))
                return found

            with mock.patch.object(people, "blockers", account_meanwhile), db.session(self.engine) as opened:
                people.delete(opened, self.person, 3.0)
            self.assertEqual(outcome, ["locked"])
            with db.session(self.engine) as opened:
                self.assertEqual((opened.query(GoogleAccount).count(), opened.get(Person, self.person)), (0, None))


if __name__ == "__main__":
    unittest.main()


class UpdateLockTests(unittest.TestCase):
    def setUp(self):
        self.seed(own_database(self))

    def seed(self, engine):
        self.engine = engine
        with db.session(self.engine) as opened:
            self.person = add_person(opened, "jo")
            self.account = add_account(opened, "jo@x.test", self.person)

    def test_an_update_takes_the_write_lock_before_it_reads(self):
        seen = []

        def write_meanwhile(conn, cursor, statement, parameters, context, executemany):
            if not seen:
                seen.append(write_elsewhere(self, self.engine, "UPDATE google_accounts SET session_state = 'ok' WHERE id = :account", account=self.account))

        event.listen(self.engine, "after_cursor_execute", write_meanwhile)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", write_meanwhile)
        with db.session(self.engine) as opened:
            accounts.update(opened, self.account, "renamed@x.test", None, 2.0)
        self.assertEqual(seen, ["locked"])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account).email, "renamed@x.test")

    def test_an_update_that_cannot_get_the_write_lock_is_busy_and_changes_nothing(self):
        impatient = impatient_engine(self, self.engine, 0.05)
        with another_writer_holds_the_lock(self.engine):
            with db.session(impatient) as opened:
                with self.assertRaisesRegex(browser_jobs.Busy, "busy with another change"):
                    accounts.update(opened, self.account, "renamed@x.test", None, 2.0)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(GoogleAccount, self.account).email, "jo@x.test")

    def test_an_update_of_a_missing_account_is_not_found(self):
        with db.session(self.engine) as opened:
            with self.assertRaises(accounts.NoSuchAccount):
                accounts.update(opened, 9999, "renamed@x.test", None, 2.0)
